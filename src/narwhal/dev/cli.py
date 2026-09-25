"""Installed commands for a local shared-GPU development fleet."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .. import command_results as results
from . import lifecycle, template


def main(argv: list[str] | None = None) -> int:
    """Dispatch finite development and offline configuration operations."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    selected, _ = results.output_arguments(arguments)
    return results.invoke(
        "narwhal",
        arguments,
        _main,
        operation="dev",
        capture_child_stdout=selected[:1] != ["config"],
    )


def _main(argv: list[str]) -> int:
    """Parse and execute the development lifecycle."""
    parser = argparse.ArgumentParser(prog="narwhal")
    results.add_format(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    from ..config import cli as config_cli

    config_cli.configure_parser(commands.add_parser("config", help="Inspect fleet files offline"))
    dev = commands.add_parser("dev", help="Run independent engines on one local NVIDIA GPU")
    actions = dev.add_subparsers(dest="action", required=True)
    for name in ("init", "up", "verify", "status", "down"):
        action = actions.add_parser(name)
        results.add_format(action)
        action.add_argument("--instance", type=Path, default=Path("runs/dev"))
        if name == "init":
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
            action.add_argument("--interface", default="eth0")
    args = parser.parse_args(argv)
    if args.command == "config":
        return config_cli.run(args)
    results.set_operation("dev " + args.action)
    root = args.instance.expanduser().resolve()
    results.set_data({"instance": str(root)})
    for name in ("instance.json", "lifecycle.json", "fleet.json"):
        results.add_artifact(name.removesuffix(".json"), root / name)
    try:
        if args.action == "init":
            spec = lifecycle.read(args.template) if args.template else template.reference()
            for argument, field in (
                ("engine_count", "engine_count"),
                ("gpu_memory_utilization", "gpu_memory_utilization"),
                ("device_allowance", "device_allowance"),
            ):
                if (value := getattr(args, argument)) is not None:
                    spec["allocation"][field] = value
            if args.port_base is not None:
                spec["ports"].update(
                    router=args.port_base,
                    engine_first=args.port_base + 1,
                    attestation_first=args.port_base + 101,
                    nixl_first=args.port_base + 201,
                )
            hub = Path.home() / ".cache/huggingface/hub"
            model = spec["model"]
            model_path = (
                args.model
                or hub
                / ("models--" + model["repository"].replace("/", "--"))
                / "snapshots"
                / model["revision"]
                / model["filename"]
            )
            model_dir = (
                args.model_dir
                or hub
                / ("models--" + model["tokenizer_repository"].replace("/", "--"))
                / "snapshots"
                / model["tokenizer_revision"]
            )
            template.materialize(
                root,
                model_dir=model_dir,
                model_path=model_path,
                fabric_interface=args.interface,
                gpu_uuid=args.gpu,
                template=spec,
            )
            result = {"status": "initialized", "instance": str(root)}
        else:
            operation = {
                "up": lifecycle.up,
                "verify": lifecycle.verify,
                "status": lifecycle.status,
                "down": lifecycle.down,
            }[args.action]
            result = operation(root)
        results.set_data(result)
        if result["status"] == "degraded":
            results.set_status("degraded")
            results.record_error("instance_degraded", "Development instance requires recovery")
        print(json.dumps(result, indent=2))
        return 1 if result["status"] == "degraded" else 0
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        if results.json_mode():
            raise
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
