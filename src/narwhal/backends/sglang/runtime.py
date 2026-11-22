from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

# Copied into each launch's hook as launch_engine.py and run inside the engine's environment.
LAUNCHER = Path(__file__)
BOOTSTRAP_PORT_ENV = "NARWHAL_SGLANG_BOOTSTRAP_PORT"
API_KEY_ENV = "NARWHAL_SGLANG_API_KEY"
SETTINGS_TAG = "NARWHAL_IMAGE_RUNTIME="


def server_command(args: list[str], environ: dict[str, str]) -> list[str]:
    command = [sys.executable, "-m", "sglang.launch_server", *args]
    command += ["--disaggregation-bootstrap-port", environ[BOOTSTRAP_PORT_ENV]]
    # The key stays out of the recorded launch arguments.
    if key := environ.get(API_KEY_ENV):
        command += ["--api-key", key]
    return command


def serve(args: list[str]) -> None:
    environ = dict(os.environ)
    command = server_command(args, environ)
    os.execve(command[0], command, registration_environment(environ))


# SGLang starts its scheduler in a spawned process, so the fix is installed by a sitecustomize
# module that every interpreter of the engine imports at startup.
SITE_HOOK = "import launch_engine\nlaunch_engine.keep_mooncake_registrations()\n"
MOONCAKE_MODULE = "sglang.srt.disaggregation.mooncake.conn"
METADATA_MODULE = "sglang.srt.disaggregation.utils"


def registration_environment(environ: dict[str, str]) -> dict[str, str]:
    site = Path(tempfile.mkdtemp(prefix="narwhal-sglang-site-"))
    (site / "sitecustomize.py").write_text(SITE_HOOK)
    path = environ.get("PYTHONPATH")
    return {**environ, "PYTHONPATH": f"{site}:{path}" if path else str(site)}


def keep_mooncake_registrations() -> None:
    """Keep each Mooncake memory registration for the life of the engine process.

    A role switch tears down the KV manager, which deregisters the KV pool and the metadata
    buffers, and the next role's manager registers them again under new remote keys. Peers keep
    the keys they read earlier, so their writes fail with remote access errors. Each region is
    registered once instead, and each metadata buffer shape is allocated once, so every key a
    peer has read stays valid.
    """
    registered: set[tuple[int, int]] = set()
    buffers: dict[str, Any] = {}
    lock = threading.Lock()

    def patch_manager(module: Any) -> None:
        manager = getattr(module, "MooncakeKVManager", None)
        if manager is None or not all(
            hasattr(manager, name)
            for name in (
                "_registerable_regions",
                "register_buffer_to_engine",
                "deregister_buffer_to_engine",
            )
        ):
            print(
                "narwhal: Mooncake registrations are not kept: this SGLang has no matching "
                "MooncakeKVManager",
                file=sys.stderr,
                flush=True,
            )
            return

        def register_buffer_to_engine(self: Any) -> None:
            with lock:
                regions = [r for r in self._registerable_regions() if r not in registered]
                if regions:
                    pointers, lengths = zip(*regions, strict=True)
                    self.engine.batch_register(list(pointers), list(lengths))
                    registered.update(regions)

        def deregister_buffer_to_engine(self: Any) -> None:
            if hasattr(self, "connection_pool"):
                with self.connection_lock:
                    self.connection_pool.clear()

        manager.register_buffer_to_engine = register_buffer_to_engine
        manager.deregister_buffer_to_engine = deregister_buffer_to_engine

    def patch_metadata(module: Any) -> None:
        allocate = getattr(module, "MetadataBuffers", None)
        if allocate is None:
            return

        def metadata_buffers(*args: Any, **kwargs: Any) -> Any:
            key = repr((args, sorted(kwargs.items())))
            with lock:
                if key not in buffers:
                    buffers[key] = allocate(*args, **kwargs)
                return buffers[key]

        module.MetadataBuffers = metadata_buffers

    sys.meta_path.insert(
        0, PatchFinder({MOONCAKE_MODULE: patch_manager, METADATA_MODULE: patch_metadata})
    )


class PatchFinder(importlib.abc.MetaPathFinder):
    """Run a patch on each named module right after it executes."""

    def __init__(self, patches: dict[str, Any]) -> None:
        self.patches = patches
        self.searching = False

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        if name not in self.patches or self.searching:
            return None
        self.searching = True
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self.searching = False
        if spec is None or spec.loader is None:
            return spec
        execute = spec.loader.exec_module
        patch = self.patches[name]

        def exec_module(module: Any) -> None:
            execute(module)
            patch(module)

        spec.loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def ships_code(directory: Path) -> bool:
    # A checkpoint's auto_map names classes that it ships as code.
    def has_auto_map(value: object) -> bool:
        if isinstance(value, dict):
            return bool(value.get("auto_map")) or any(has_auto_map(item) for item in value.values())
        if isinstance(value, list):
            return any(has_auto_map(item) for item in value)
        return False

    for name in ("config.json", "tokenizer_config.json"):
        path = directory / name
        if path.is_file() and has_auto_map(json.loads(path.read_text())):
            return True
    return False


def settings(expected: dict[str, str], args: list[str]) -> dict:
    import importlib.metadata as metadata

    observed = {name: metadata.version(name) for name in expected}
    if observed != expected:
        raise ValueError(f"image package versions differ: {json.dumps(observed)}")
    import sglang  # type: ignore[import-not-found]
    from sglang.srt.server_args import ServerArgs  # type: ignore[import-not-found]

    parser = argparse.ArgumentParser()
    ServerArgs.add_cli_args(parser)
    parsed = parser.parse_args([*args, "--disaggregation-bootstrap-port", "1"])
    # The launcher checks the model's metadata; a separate tokenizer resolves here.
    tokenizer = parsed.tokenizer_path
    if (
        tokenizer
        and tokenizer != parsed.model_path
        and not parsed.trust_remote_code
        and ships_code(Path(tokenizer))
    ):
        raise ValueError("Tokenizer metadata requires --trust-remote-code in the launch record")
    events = json.loads(parsed.kv_events_config) if parsed.kv_events_config else None
    return {
        "sglang_version": sglang.__version__,
        "prefix_caching": not parsed.disable_radix_cache,
        "kv_events": None
        if events is None
        else {key: events.get(key) for key in ("endpoint", "replay_endpoint")},
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        serve(argv[1:])
        return 0
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("_check")
    check.add_argument("expected")
    check.add_argument("arguments")
    args = parser.parse_args(argv)
    try:
        result = settings(json.loads(args.expected), json.loads(args.arguments))
    except (ValueError, ImportError, SystemExit) as error:
        parser.exit(1, f"narwhal-engine: {args.command}: {error}\n")
    print(SETTINGS_TAG + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
