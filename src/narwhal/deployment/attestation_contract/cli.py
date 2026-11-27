from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import httpx

from .capture import capture
from .fleet import finalize_fleet
from .sidecar import serve, verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture engine attestation evidence, serve and verify the attestation, and "
        "derive the router contract."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    captured = commands.add_parser(
        "capture",
        help="run every attestation capture of a started engine and write its document",
    )
    captured.add_argument("--run", required=True, type=Path, help="engine launch directory")
    captured.add_argument(
        "--representative-layout",
        help="cache layout of the engine whose fabric budget this engine inherits",
    )
    served = commands.add_parser("serve", help="serve the engine's attestation document")
    served.add_argument("--run", required=True, type=Path, help="engine launch directory")
    verified = commands.add_parser(
        "verify", help="check the running sidecar against the live engine"
    )
    verified.add_argument("--run", required=True, type=Path, help="engine launch directory")
    fleet = commands.add_parser("finalize-fleet", help="write or check the fleet's engine contract")
    fleet.add_argument("--fleet", required=True, type=Path, help="router fleet file")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = capture(args.run, args.representative_layout)
            print(f"Generated private engine attestation in {result}")
        elif args.command == "serve":
            return serve(args.run)
        elif args.command == "verify":
            result = verify(args.run)
            print(f"Sidecar attestation matches the live engine and contract; captures in {result}")
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
