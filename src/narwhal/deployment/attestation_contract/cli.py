from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import httpx

from ..launch_engine.backend import EngineLauncher, plan_launcher
from .document import generate
from .evidence import read_json
from .fleet import finalize_fleet
from .sidecar import serve


def _launcher(run: Path) -> EngineLauncher:
    return plan_launcher(read_json(run / "launch.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive engine attestation and the router contract from checked "
        "deployment evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture-nixl")
    capture.add_argument("--run", required=True, type=Path)
    native = commands.add_parser("native-capture")
    native.add_argument("--run", required=True, type=Path)
    dimensions = commands.add_parser("capture-model-dimensions")
    dimensions.add_argument("--run", required=True, type=Path)
    engine = commands.add_parser("generate")
    engine.add_argument("--run", required=True, type=Path)
    engine.add_argument("--startup-log", required=True, type=Path)
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--run", required=True, type=Path)
    fleet = commands.add_parser("finalize-fleet")
    fleet.add_argument("--fleet", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture-nixl":
            result = _launcher(args.run).capture_connector(args.run)
            print(f"Captured pinned NIXL protocol in {result}")
        elif args.command == "native-capture":
            result = _launcher(args.run).capture_native(args.run)
            print(f"Captured native engine attestation in {result}")
        elif args.command == "capture-model-dimensions":
            result = _launcher(args.run).capture_model_dimensions(args.run)
            print(f"Captured live model dimensions in {result}")
        elif args.command == "generate":
            result = generate(args.run, args.startup_log)
            print(f"Generated private engine attestation in {result}")
        elif args.command == "serve":
            return serve(args.run)
        else:
            contract = finalize_fleet(args.fleet)
            print(f"Router fleet contract verified across live sidecars: {contract.fingerprint()}")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
        httpx.HTTPError,
    ) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
