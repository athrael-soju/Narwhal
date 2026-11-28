from __future__ import annotations

import argparse
import atexit
import importlib.abc
import importlib.util
import json
import logging
import os
import runpy
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


def server_args(args: list[str], environ: dict[str, str]) -> list[str]:
    """SGLang's server arguments, without the engine key."""
    return [*args, "--disaggregation-bootstrap-port", environ[BOOTSTRAP_PORT_ENV]]


def serve(args: list[str]) -> None:
    """Run SGLang's server in this process.

    SGLang reads its key from `--api-key`. A new process would carry it in the argument
    list the kernel shows every local user in `ps` and `/proc/<pid>/cmdline`, so the server runs
    here instead: the key enters SGLang's `sys.argv` in memory only, and the process keeps the
    argument list it started with, which holds no key. The key leaves the environment the
    server's own processes inherit, and `redact_output` keeps it out of what the server logs.
    """
    environ = dict(os.environ)
    key = environ.pop(API_KEY_ENV, "")
    arguments = server_args(args, environ)
    os.environ.clear()
    # SGLang spawns its scheduler, which imports the site hook; this process installs it here.
    os.environ.update(registration_environment(environ))
    keep_mooncake_registrations()
    if key:
        redact_output(key)
    sys.argv = ["sglang.launch_server", *arguments, *(["--api-key", key] if key else [])]
    runpy.run_module("sglang.launch_server", run_name="__main__", alter_sys=True)


REDACTED = "[REDACTED]"


def redact_output(key: str) -> None:
    """Replace the engine key with `REDACTED` in everything this process logs or prints.

    SGLang takes its key only as a server argument, from the command line or a `--config` file,
    and logs every argument at startup, so the key would reach the container's logs.
    `logging.basicConfig(force=True)` in SGLang replaces the root logger's handlers, and a filter
    on the root logger misses records from child loggers, so each record is redacted as it is
    created instead. Handlers SGLang and uvicorn configure later write to `sys.stdout` and
    `sys.stderr`, which are wrapped as well.
    """
    secrets = sorted({key, repr(key)[1:-1], json.dumps(key)[1:-1]}, key=len, reverse=True)
    make_record = logging.getLogRecordFactory()

    def record(*args: Any, **kwargs: Any) -> logging.LogRecord:
        made = make_record(*args, **kwargs)
        try:
            message = made.getMessage()
        except Exception:  # The handler reports a malformed record itself.
            return made
        if any(secret in message for secret in secrets):
            made.msg, made.args = redact(message, secrets), ()
        return made

    logging.setLogRecordFactory(record)
    for name in ("stdout", "stderr"):
        stream = RedactingStream(getattr(sys, name), secrets)
        setattr(sys, name, stream)
        atexit.register(stream.finish)


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


class RedactingStream:
    """A text stream that replaces each secret before writing to the stream it wraps.

    A write that ends in what could be the start of a secret holds that tail back until the
    next write, so a secret split across writes is still replaced.
    """

    def __init__(self, stream: Any, secrets: list[str]) -> None:
        self._stream = stream
        self._secrets = secrets
        self._pending = ""
        self._lock = threading.RLock()

    def write(self, text: str) -> int:
        with self._lock:
            pending = redact(self._pending + text, self._secrets)
            held = max(
                (
                    size
                    for secret in self._secrets
                    for size in range(min(len(secret) - 1, len(pending)), 0, -1)
                    if pending.endswith(secret[:size])
                ),
                default=0,
            )
            self._pending = pending[len(pending) - held :]
            if held < len(pending):
                self._stream.write(pending[: len(pending) - held])
        return len(text)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()

    def finish(self) -> None:
        """Write the held tail at exit. It holds no whole secret."""
        with self._lock:
            if self._pending:
                self._stream.write(self._pending)
                self._pending = ""
            self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


# SGLang starts its scheduler in a spawned process, so the fix is installed by a sitecustomize
# module that every interpreter of the engine imports at startup.
SITE_HOOK = (
    "import launch_engine\n"
    "launch_engine.keep_mooncake_registrations()\n"
    "launch_engine.run_next_sitecustomize(__file__)\n"
)
MOONCAKE_MODULE = "sglang.srt.disaggregation.mooncake.conn"
METADATA_MODULE = "sglang.srt.disaggregation.utils"
ROLE_SWITCH_MODULE = "sglang.srt.disaggregation.role_switch"
DECODE_MODULE = "sglang.srt.disaggregation.decode"
# The module, class (None for module functions) and functions that the fix replaces or calls.
REGISTRATION_TARGETS = (
    (
        MOONCAKE_MODULE,
        "MooncakeKVManager",
        ("_registerable_regions", "register_buffer_to_engine", "deregister_buffer_to_engine"),
    ),
    (METADATA_MODULE, "MetadataBuffers", ("__init__",)),
    (ROLE_SWITCH_MODULE, None, ("_reject_reason",)),
    (DECODE_MODULE, "DecodeTransferQueue", ("has_pending_deferred_releases",)),
)
PENDING_RELEASES = "instance is not idle; deferred KV releases are pending"


def registration_environment(environ: dict[str, str]) -> dict[str, str]:
    site = Path(tempfile.mkdtemp(prefix="narwhal-sglang-site-"))
    (site / "sitecustomize.py").write_text(SITE_HOOK)
    path = environ.get("PYTHONPATH")
    return {**environ, "PYTHONPATH": f"{site}:{path}" if path else str(site)}


def run_next_sitecustomize(own: str) -> None:
    """Run the sitecustomize module that the hook's own one shadows, if any."""
    own_directory = Path(own).resolve().parent
    for entry in sys.path:
        if not entry or Path(entry).resolve() == own_directory:
            continue
        candidate = Path(entry) / "sitecustomize.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("_narwhal_next_sitecustomize", candidate)
            if spec is not None and spec.loader is not None:
                spec.loader.exec_module(importlib.util.module_from_spec(spec))
            return


def missing_registration_targets(sglang_root: Path) -> list[str]:
    """Name each part of SGLang's source that the registration fix needs and cannot find."""
    import ast

    missing = []
    for module, owner, functions in REGISTRATION_TARGETS:
        source = sglang_root.joinpath(*module.split(".")[1:]).with_suffix(".py")
        try:
            tree = ast.parse(source.read_text())
        except (OSError, SyntaxError):
            missing.append(module)
            continue
        body: list[ast.stmt] | None = tree.body
        if owner is not None:
            classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner]
            body = classes[0].body if classes else None
        if body is None:
            missing.append(f"{module}.{owner}")
            continue
        defined = {n.name for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        prefix = f"{module}.{owner}" if owner else module
        missing += [f"{prefix}.{name}" for name in functions if name not in defined]
    return missing


def keep_mooncake_registrations() -> None:
    """Keep each Mooncake memory registration for the life of the engine process.

    A role switch tears down the KV manager, which deregisters the KV pool and the metadata
    buffers, and the next role's manager registers them again under new remote keys. Peers keep
    the keys they read earlier, so their writes fail with remote access errors. Each region is
    registered once instead, and each metadata buffer shape is allocated once, so every key a
    peer has read stays valid. The regions are never freed while the process runs; SGLang's
    memory saver, which frees them, is not a launch option.

    Deregistration also made a write that a prefill peer was still sending into an aborted
    request's pages fail. Those pages stay held until the peer drains, so a switch is refused
    while any such hold is pending instead.
    """
    registered: set[tuple[int, int]] = set()
    buffers: dict[str, Any] = {}
    lock = threading.Lock()

    def report(message: str) -> None:
        print(f"narwhal: {message}", file=sys.stderr, flush=True)

    def patch_manager(module: Any) -> None:
        manager = getattr(module, "MooncakeKVManager", None)
        if manager is None or not all(
            hasattr(manager, name) for name in REGISTRATION_TARGETS[0][2]
        ):
            report(
                "Mooncake registrations are not kept: this SGLang has no matching MooncakeKVManager"
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
        report("Mooncake registrations are kept across role switches")

    def patch_metadata(module: Any) -> None:
        allocate = getattr(module, "MetadataBuffers", None)
        if allocate is None:
            report("metadata buffers are reallocated: this SGLang has no MetadataBuffers")
            return

        def metadata_buffers(*args: Any, **kwargs: Any) -> Any:
            key = repr((args, sorted(kwargs.items())))
            with lock:
                if key not in buffers:
                    buffers[key] = allocate(*args, **kwargs)
                return buffers[key]

        module.MetadataBuffers = metadata_buffers

    def patch_role_switch(module: Any) -> None:
        reject = getattr(module, "_reject_reason", None)
        if reject is None:
            report("role switches do not wait for deferred KV releases: no _reject_reason")
            return

        def reject_reason(scheduler: Any, new_role: str) -> Any:
            rejection = reject(scheduler, new_role)
            queue = getattr(scheduler, "disagg_decode_transfer_queue", None)
            if rejection is None and queue is not None and queue.has_pending_deferred_releases():
                return PENDING_RELEASES, True
            return rejection

        module._reject_reason = reject_reason

    sys.meta_path.insert(
        0,
        PatchFinder(
            {
                MOONCAKE_MODULE: patch_manager,
                METADATA_MODULE: patch_metadata,
                ROLE_SWITCH_MODULE: patch_role_switch,
            }
        ),
    )


class PatchFinder(importlib.abc.MetaPathFinder):
    """Run a patch on each named module right after it executes."""

    def __init__(self, patches: dict[str, Any]) -> None:
        self.patches = patches
        self.searching: set[str] = set()
        self.lock = threading.Lock()

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        with self.lock:
            if name not in self.patches or name in self.searching:
                return None
            self.searching.add(name)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            with self.lock:
                self.searching.discard(name)
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
    if missing := missing_registration_targets(Path(sglang.__file__).parent):
        raise ValueError(
            "this SGLang lacks what the Mooncake registration fix replaces: " + ", ".join(missing)
        )
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
