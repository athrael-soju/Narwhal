"""Installed commands for a local shared-GPU development fleet."""

from __future__ import annotations

import argparse
import json
import subprocess
from importlib import metadata
from pathlib import Path

import httpx

from ..cli_errors import failure
from ..cli_support import add_version_argument
from . import lifecycle, template


def main(argv: list[str] | None = None) -> int:
    """Dispatch the local development lifecycle."""
    parser = argparse.ArgumentParser(
        prog="narwhal",
        description="Manage a local shared-GPU development fleet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Installed commands:\n"
            "  narwhal          Initialize, launch, verify and stop a local development fleet\n"
            "  narwhal-engine   Prepare and launch checked engine processes\n"
            "  narwhal-check    Run deployment preflight gates\n"
            "  narwhal-attest   Serve engine identity and attestation for one vLLM engine\n"
            "  narwhal-serve    Run a Narwhal router\n"
            "  narwhal-profile  Measure engine service curves and write router profiles"
        ),
    )
    add_version_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    dev = commands.add_parser("dev", help="Run independent engines on one local NVIDIA GPU")
    actions = dev.add_subparsers(dest="action", required=True)
    for name in ("init", "up", "verify", "status", "down"):
        action = actions.add_parser(name)
        action.add_argument("--instance", type=Path, default=Path("runs/dev"))
        if name == "init":
            action.description = (
                "Create an instance, or report reused when explicit settings match an existing "
                "instance. Reuse preserves operator edits. To change settings, select a fresh "
                "directory with --instance."
            )
            action.add_argument(
                "--template", type=Path, help="Versioned model and runtime settings"
            )
            action.add_argument("--model", type=Path, help="Local GGUF file")
            action.add_argument(
                "--model-dir", type=Path, help="Pinned tokenizer and model config directory"
            )
            action.add_argument("--gpu", help="Physical GPU UUID")
            action.add_argument("--engine-count", type=int)
            action.add_argument(
                "--port-base",
                type=int,
                help="Router port; HTTP, attestation and NIXL use +1, +101 and +201",
            )
            action.add_argument("--gpu-memory-utilization", type=float)
            action.add_argument("--device-allowance", type=float)
            action.add_argument("--interface", help="Local NIXL/UCX interface (default: eth0)")
    args = parser.parse_args(argv)
    root = args.instance.expanduser().resolve()
    context = f"dev {args.action} {root}"
    if args.action != "init":
        try:
            lifecycle.instance(root)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return failure("narwhal", f"{context}: load instance", exc, 2)
    try:
        if args.action == "init":
            reused = root.exists()
            template.materialize(
                root,
                model_dir=args.model_dir,
                model_path=args.model,
                fabric_interface=args.interface,
                gpu_uuid=args.gpu,
                template=lifecycle.read(args.template) if args.template else None,
                engine_count=args.engine_count,
                port_base=args.port_base,
                gpu_memory_utilization=args.gpu_memory_utilization,
                device_allowance=args.device_allowance,
            )
            result = {"status": "reused" if reused else "initialized", "instance": str(root)}
        else:
            operation = {
                "up": lifecycle.up,
                "verify": lifecycle.verify,
                "status": lifecycle.status,
                "down": lifecycle.down,
            }[args.action]
            result = operation(root)
        print(json.dumps(result, indent=2))
        return 1 if result["status"] == "degraded" else 0
    except metadata.PackageNotFoundError as exc:
        return failure("narwhal", f"{context}: load runtime package", exc, 2)
    except (KeyError, TypeError) as exc:
        if args.action != "init":
            raise
        return failure(
            "narwhal", f"{context}: read template {args.template or 'reference'}", exc, 2
        )
    except (OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        return failure("narwhal", context, exc, 2 if args.action == "init" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
