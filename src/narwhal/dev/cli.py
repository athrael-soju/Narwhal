from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import httpx

from .. import command_results as results
from ..cli_errors import failure
from ..cli_support import add_version_argument
from . import ask, demo, lifecycle, setup, template, watch


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    selected, _ = results.output_arguments(arguments)
    return results.invoke(
        "narwhal",
        arguments,
        _main,
        operation="dev",
        capture_child_stdout=selected[:1] not in (["config"], ["diagnostics"]),
    )


def _gpu_list(value: str) -> list[str]:
    uuids = [item.strip() for item in value.split(",")]
    if not all(uuids):
        raise argparse.ArgumentTypeError("name each GPU by its UUID, separated by commas")
    return uuids


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="narwhal",
        description="Deploy a fleet to SSH hosts, run a local development fleet, inspect fleet "
        "files and collect diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Documentation: https://athrael-soju.github.io/Narwhal/CLI-Reference/\n\n"
            "Installed commands:\n"
            "  narwhal          Deploy to SSH hosts, run a dev fleet, inspect fleet files and "
            "collect diagnostics\n"
            "  narwhal-engine   Prepare and launch checked engine processes\n"
            "  narwhal-check    Run deployment preflight gates\n"
            "  narwhal-attest   Serve engine identity and attestation for one engine\n"
            "  narwhal-serve    Run a Narwhal router\n"
            "  narwhal-profile  Measure engine service curves and write router profiles"
        ),
    )
    add_version_argument(parser)
    results.add_format(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    from ..config import cli as config_cli
    from ..deployment import cli as deploy_cli
    from ..diagnostics import bundle

    config_cli.configure_parser(commands.add_parser("config", help="Inspect fleet files offline"))
    bundle.add_commands(commands)
    deploy_cli.add_commands(commands)
    dev = commands.add_parser(
        "dev",
        help="Run a development fleet on one Linux or WSL2 workstation",
        description="Run a development fleet on one Linux or WSL2 workstation. setup prepares "
        "engines that start directly, on a GPU or the CPU, and init prepares attested engines "
        "on one NVIDIA GPU or one GPU per engine. up starts the fleet, verify checks it, ask "
        "and watch exercise it, demo shows roles change under load, status reports it and down "
        "stops it.",
        epilog="Documentation: https://athrael-soju.github.io/Narwhal/cli/Dev/",
    )
    actions = dev.add_subparsers(dest="action", required=True)
    descriptions = {
        "setup": "Download the pinned engine build and model file into the instance directory, "
        "verify their checksums and initialize an instance whose engines start directly; "
        "reports initialized, or reused for an existing instance.",
        "init": "Check pinned model files, native runtime and GPU allocation, then write an "
        "initialized instance, or report reused when explicit settings match an existing "
        "instance. Reuse preserves operator edits.",
        "up": "Start an instance's engines and its router; reports launched. Sets up the "
        "instance first when it does not exist or when a setup option is given. An instance from "
        "init also captures attestations and profiles role splits, and requires the pinned "
        "native runtime and GPU.",
        "verify": "Check a launched instance's profiles and a routed arithmetic response, and "
        "for an instance from init its directed KV paths; retain metrics and evidence before "
        "reporting ready.",
        "status": "Inspect an initialized instance's recorded processes and HTTP health; "
        "report launched or verified ready, retaining verification failures as degraded.",
        "ask": "Send one prompt through a launched instance's router and report the reply, its "
        "time to first token, its tokens per second and the engines that served it; or send "
        "--repeat built-in prompts at --concurrency and report the spread.",
        "watch": "Redraw each engine's role and requests in flight, the time to first token and "
        "time per output token at the median and the 95th percentile, the throughput and the "
        "role switches of a launched instance, from the router's state and metrics.",
        "demo": "Start the fleet when it is not up, then send long replies and then long prompts "
        "at --concurrency for --phase-seconds each while the live view shows the engines change "
        "roles; reports the requests each phase sent and failed, the role switches and the "
        "final split.",
        "down": "Stop an instance's recorded native process groups after validating their "
        "identities; retain logs and measurements and report stopped, or with --remove delete "
        "the instance directory and report removed.",
    }
    for name, description in descriptions.items():
        action = actions.add_parser(name, help=description, description=description)
        results.add_format(action)
        action.add_argument(
            "--instance",
            type=Path,
            default=Path("runs/dev"),
            help="private instance directory (default: %(default)s)",
        )
        if name == "up":
            action.add_argument(
                "--alongside",
                action="store_true",
                help="start this fleet while another instance's fleet is up; each fleet takes "
                "its own memory",
            )
        if name == "demo":
            action.add_argument(
                "--concurrency",
                type=int,
                default=demo.CONCURRENCY,
                help="requests in flight at once (default: %(default)s)",
            )
            action.add_argument(
                "--phase-seconds",
                type=float,
                default=demo.PHASE_SECONDS,
                help="seconds of long replies, then of long prompts (default: %(default)s)",
            )
        if name in ("watch", "demo"):
            action.add_argument(
                "--interval",
                type=float,
                default=1.0,
                help="seconds between redraws (default: %(default)s)",
            )
        if name == "watch":
            action.add_argument(
                "--once", action="store_true", help="print one frame as the result and exit"
            )
        if name == "ask":
            action.add_argument("prompt", nargs="?", help="one prompt to send")
            action.add_argument(
                "--repeat",
                type=int,
                help="send this many requests, of the prompt when one is given and of the "
                "built-in prompts otherwise",
            )
            action.add_argument(
                "--concurrency",
                type=int,
                default=1,
                help="requests in flight at once with --repeat (default: %(default)s)",
            )
            action.add_argument(
                "--max-tokens",
                type=int,
                default=128,
                help="output token limit of each request (default: %(default)s)",
            )
        if name == "down":
            action.add_argument(
                "--remove",
                action="store_true",
                help="after stopping, delete the instance directory with its downloads and logs",
            )
        if name in ("setup", "up", "demo"):
            action.add_argument(
                "--model-name",
                help="served name of the model, among the installed templates that start "
                "their engines directly (default: the default template's model)",
            )
            action.add_argument(
                "--accelerator",
                help="engine build to download; auto selects by the host's devices, none keeps "
                "the model on the CPU, and another value names a build the template pins "
                "(default: auto, or the instance's build)",
            )
            action.add_argument(
                "--engine-count", type=int, help="engines, 2 to 8 (default: template value, 4)"
            )
            action.add_argument(
                "--port-base",
                type=int,
                help="router TCP port; engine ports start at +1 "
                "(default: router 18000, engines from 18101)",
            )
        if name == "init":
            action.description = (
                description + " To change settings, select a fresh directory with --instance. "
                "Omitted options use the saved instance or the selected template."
            )
            action.add_argument(
                "--template",
                type=Path,
                help="versioned model and runtime settings (default: installed small-GPU template)",
            )
            action.add_argument(
                "--model",
                type=Path,
                help="local GGUF file or model directory matching the template checksums "
                "(default: pinned HF cache snapshot)",
            )
            action.add_argument(
                "--model-dir",
                type=Path,
                help="pinned tokenizer and model config directory (default: pinned HF cache)",
            )
            gpus = action.add_mutually_exclusive_group()
            gpus.add_argument(
                "--gpu",
                help="physical GPU UUID that every engine shares (default: single discovered GPU)",
            )
            gpus.add_argument(
                "--gpus",
                type=_gpu_list,
                metavar="UUID,UUID,...",
                help="physical GPU UUIDs, one GPU per engine: engine n runs on the nth GPU; "
                "the engines form no shared_device group, so they can leave and rejoin the "
                "router at runtime",
            )
            action.add_argument(
                "--engine-count",
                type=int,
                help="independent engines, 2 to 8 (default: template value, installed 2)",
            )
            action.add_argument(
                "--port-base",
                type=int,
                help="router TCP port; engine HTTP, attestation and KV side-channel ranges "
                "start at +1, +101 and +201; all ports must fit 1..65535 and be distinct "
                "(default: template ports, installed router 18000)",
            )
            action.add_argument(
                "--gpu-memory-utilization",
                type=float,
                help="finite per-engine fraction of total GPU memory, 0 < fraction <= 1; "
                "on one shared GPU, engine-count * fraction <= device-allowance, and with "
                "--gpus, fraction <= device-allowance (default: template value)",
            )
            action.add_argument(
                "--device-allowance",
                type=float,
                help="finite aggregate fraction of total GPU memory, <= 1 and >= the sum of "
                "the fractions of the engines on one GPU; on a shared GPU, bounds the free "
                "memory init requires and the startup memory increase (default: template value)",
            )
            action.add_argument(
                "--engine-memory-mib",
                type=int,
                help="GPU memory of each engine in MiB, converted to a fraction of the GPU found "
                "(default: template value, installed 2867)",
            )
            action.add_argument(
                "--device-memory-mib",
                type=int,
                help="GPU memory the engines on one GPU may take in MiB: on a shared GPU, at "
                "least --engine-count times --engine-memory-mib, and with --gpus, at least "
                "--engine-memory-mib (default: template value, installed 7168, 9728 or 13312)",
            )
            action.add_argument(
                "--interface", help="Local KV transfer network interface (default: eth0)"
            )
            action.add_argument(
                "--engine-binary",
                type=Path,
                help="engine server executable, for a template that starts its engines directly",
            )
            action.add_argument(
                "--no-accelerator",
                action="store_true",
                default=None,
                help="keep the model on the CPU, for a template that starts its engines directly",
            )
    args = parser.parse_args(argv)
    watching = args.command == "dev" and args.action in ("watch", "demo")
    if watching and not (math.isfinite(args.interval) and args.interval > 0):
        parser.error("--interval must be a positive number")
    demoing = args.command == "dev" and args.action == "demo"
    if demoing and not (
        args.concurrency >= 1 and math.isfinite(args.phase_seconds) and args.phase_seconds > 0
    ):
        parser.error("--concurrency and --phase-seconds must be positive")
    if args.command == "dev" and args.action == "ask":
        if args.prompt is None and args.repeat is None:
            parser.error("dev ask takes a prompt, --repeat for the built-in prompts, or both")
        if min(1 if args.repeat is None else args.repeat, args.concurrency, args.max_tokens) < 1:
            parser.error("--repeat, --concurrency and --max-tokens must be positive")
    if args.command == "config":
        return config_cli.run(args)
    if args.command == "diagnostics":
        return bundle.run(args)
    if args.command == "deploy":
        return deploy_cli.run(args)
    results.set_operation("dev " + args.action)
    root = args.instance.expanduser().resolve()
    results.set_data({"instance": str(root)})
    for name in ("instance.json", "lifecycle.json", "fleet.json"):
        results.add_artifact(name.removesuffix(".json"), root / name)
    context = f"dev {args.action} {root}"
    # up sets up an instance that does not exist yet, or one whose settings it names.
    setting_up = args.action in ("up", "demo") and (
        not (root / "instance.json").exists()
        or any(
            value is not None
            for value in (args.model_name, args.accelerator, args.engine_count, args.port_base)
        )
    )
    if args.action not in ("init", "setup") and not setting_up:
        try:
            try:
                lifecycle.instance(root)
            except FileNotFoundError:
                if (root / "instance.json").exists():
                    raise
                raise FileNotFoundError(
                    "no instance here; run narwhal dev up or narwhal dev init"
                ) from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if results.json_mode():
                raise
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
                gpu_uuids=args.gpus,
                template=lifecycle.read(args.template) if args.template else None,
                engine_count=args.engine_count,
                port_base=args.port_base,
                gpu_memory_utilization=args.gpu_memory_utilization,
                device_allowance=args.device_allowance,
                engine_binary=args.engine_binary,
                accelerated=False if args.no_accelerator else None,
                engine_memory_mib=args.engine_memory_mib,
                device_memory_mib=args.device_memory_mib,
            )
            result = {"status": "reused" if reused else "initialized", "instance": str(root)}
        elif args.action == "setup" or setting_up:
            result = setup.run(
                root,
                model=args.model_name.lower() if args.model_name else None,
                accelerator=args.accelerator,
                engine_count=args.engine_count,
                port_base=args.port_base,
            )
            if args.action == "up":
                prepared: dict = result
                result = lifecycle.up(root, alongside=args.alongside)
                result["setup"] = prepared["status"]
                result["downloaded"] = prepared.get("downloaded", [])
            if args.action == "demo":
                result = _demo(root, args)
        elif args.action == "watch":
            # A result document describes one frame, so a JSON result takes a single reading.
            once = args.once or results.json_mode()
            result = watch.run(root, interval=args.interval, once=once)
            results.set_data(result)
            if once:
                print(json.dumps(result, indent=2))
            return 0
        elif args.action == "ask":
            result = ask.run(
                root,
                prompt=args.prompt,
                repeat=args.repeat,
                concurrency=args.concurrency,
                max_tokens=args.max_tokens,
            )
        elif args.action == "demo":
            result = _demo(root, args)
        elif args.action == "up":
            result = lifecycle.up(root, alongside=args.alongside)
        elif args.action == "down" and args.remove:
            result = lifecycle.remove(root)
        else:
            operation = {
                "verify": lifecycle.verify,
                "status": lifecycle.status,
                "down": lifecycle.down,
            }[args.action]
            result = operation(root)
        results.set_data(result)
        if result["status"] == "degraded":
            results.set_status("degraded")
            results.record_error("instance_degraded", "Development instance requires recovery")
        failed = args.action in ("ask", "demo") and result.get("failed")
        if failed:
            results.set_status("failed_gate")
            results.record_error("gate_failed", f"{failed} of {result['requests']} requests failed")
        print(json.dumps(result, indent=2))
        return 1 if failed or result["status"] == "degraded" else 0
    except metadata.PackageNotFoundError as exc:
        results.record_error("runtime_package_missing", str(exc), stage="runtime-package")
        return failure("narwhal", f"{context}: load runtime package", exc, 2)
    except lifecycle.LifecycleDocumentError as exc:
        if results.json_mode():
            raise
        return failure("narwhal", f"{context}: load lifecycle", exc, 2)
    except (KeyError, TypeError) as exc:
        if results.json_mode():
            raise
        input_operation = (
            f"read template {args.template or 'reference'}"
            if args.action == "init"
            else "read instance input"
        )
        return failure("narwhal", f"{context}: {input_operation}", exc, 2)
    except (
        OSError,
        ValueError,
        lifecycle.LifecycleOperationError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as exc:
        if results.json_mode():
            raise
        return failure("narwhal", context, exc, 2 if args.action in ("init", "setup") else 1)


def _demo(root: Path, args: argparse.Namespace) -> dict:
    """Start the fleet unless it is up, then run the demo load under the live view."""
    phase = (lifecycle._read_state(root) if (root / "lifecycle.json").exists() else {}).get("phase")
    if phase not in ask.PHASES:
        print("starting the fleet ...", file=sys.stderr, flush=True)
        lifecycle.up(root)
    return demo.run(
        root, seconds=args.phase_seconds, concurrency=args.concurrency, interval=args.interval
    )


if __name__ == "__main__":
    raise SystemExit(main())
