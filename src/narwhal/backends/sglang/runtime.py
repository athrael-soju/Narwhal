from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
    command = server_command(args, dict(os.environ))
    os.execv(command[0], command)


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
