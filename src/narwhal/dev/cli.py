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
    dev = commands.add_parser(
        "dev",
        help="Run independent engines on one local NVIDIA GPU",
        description="Use the native Linux/WSL2 runtime: init writes checked instance inputs, "
        "up reports launched after engine profiling and router startup, and verify reports "
        "ready after directed KV checks and a routed request.",
    )
    actions = dev.add_subparsers(dest="action", required=True)
    descriptions = {
        "init": "Check pinned model files, native runtime and GPU allocation, then write an "
        "initialized instance, or report reused when explicit settings match an existing "
        "instance. Reuse preserves operator edits.",
        "up": "Start an initialized instance, capture attestations, profile role splits and "
        "start its router; reports launched. Requires the pinned native runtime and GPU.",
        "verify": "Check a launched instance's directed KV paths, profiles and routed "
        "arithmetic response; retain metrics and evidence before reporting ready.",
        "status": "Inspect an initialized instance's recorded processes and HTTP health; "
        "report launched or verified ready, retaining verification failures as degraded.",
        "down": "Stop an instance's recorded native process groups after validating their "
        "identities; retain logs and measurements and report stopped.",
    }
    for name, description in descriptions.items():
        action = actions.add_parser(name, help=description, description=description)
        action.add_argument(
            "--instance",
            type=Path,
            default=Path("runs/dev"),
            help="private instance directory (default: %(default)s)",
        )
        if name == "init":
            action.description = (
                description + " To change settings, select a fresh directory with --instance. "
                "Omitted options use the saved instance or the selected template."
            )
            action.add_argument(
                "--template",
                type=Path,
                help="versioned model and runtime settings (default: installed reference)",
            )
            action.add_argument(
                "--model",
                type=Path,
                help="local GGUF matching the template checksum (default: pinned HF cache file)",
            )
            action.add_argument(
                "--model-dir",
                type=Path,
                help="pinned tokenizer and model config directory (default: pinned HF cache)",
            )
            action.add_argument("--gpu", help="physical GPU UUID (default: single discovered GPU)")
            action.add_argument(
                "--engine-count",
                type=int,
                help="independent engines, 2 to 8 (default: template value, reference 4)",
            )
            action.add_argument(
                "--port-base",
                type=int,
                help="router TCP port; engine HTTP, attestation and NIXL ranges start at "
                "+1, +101 and +201; all ports must fit 1..65535 and be distinct "
                "(default: template ports, reference router 18000)",
            )
            action.add_argument(
                "--gpu-memory-utilization",
                type=float,
                help="finite per-engine fraction of total GPU memory, 0 < fraction <= 1; "
                "engine-count * fraction <= device-allowance "
                "(default: template value, reference 0.1)",
            )
            action.add_argument(
                "--device-allowance",
                type=float,
                help="finite aggregate fraction of total GPU memory, <= 1 and >= the sum of "
                "engine fractions; bounds startup memory increase "
                "(default: template value, reference 0.5)",
            )
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
    except lifecycle.LifecycleDocumentError as exc:
        return failure("narwhal", f"{context}: load lifecycle", exc, 2)
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
