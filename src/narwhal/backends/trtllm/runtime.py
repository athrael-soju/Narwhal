from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# Copied into each launch's hook as launch_engine.py and run inside the engine's environment.
LAUNCHER = Path(__file__)
API_KEY_ENV = "NARWHAL_TRTLLM_API_KEY"
SETTINGS_TAG = "NARWHAL_IMAGE_RUNTIME="
SERVER = "trtllm-serve"
# The runtime script runs alone in the image, so it keeps its own copy of this path.
BLOCK_REUSE = "kv_cache_config.enable_block_reuse"


def server_command(args: list[str]) -> list[str]:
    server = shutil.which(SERVER)
    if server is None:
        raise ValueError(f"{SERVER} is not on the engine's PATH")
    return [server, *args]


def serve(args: list[str]) -> None:
    command = server_command(args)
    os.execv(command[0], command)


def option(args: list[str], name: str) -> str | None:
    positions = [i for i, arg in enumerate(args) if arg == name]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        return None
    return args[positions[0] + 1]


def settings(expected: dict[str, str], args: list[str]) -> dict:
    import importlib.metadata as metadata

    observed = {name: metadata.version(name) for name in expected}
    if observed != expected:
        raise ValueError(f"image package versions differ: {json.dumps(observed)}")
    server_command(args)
    path = option(args, "--config")
    if path is None:
        raise ValueError("the launch passes no --config file")
    config = json.loads(Path(path).read_text())
    reuse = (config.get("kv_cache_config") or {}).get("enable_block_reuse", True)
    # A --set override applies after the configuration file.
    for index, arg in enumerate(args[:-1]):
        if arg == "--set" and args[index + 1].startswith(BLOCK_REUSE + "="):
            reuse = args[index + 1].partition("=")[2] == "true"
    return {
        "tensorrt_llm_version": observed.get("tensorrt_llm") or metadata.version("tensorrt_llm"),
        "prefix_caching": reuse,
        "kv_events": None,
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
    except (ValueError, OSError, ImportError) as error:
        parser.exit(1, f"narwhal-engine: {args.command}: {error}\n")
    print(SETTINGS_TAG + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
