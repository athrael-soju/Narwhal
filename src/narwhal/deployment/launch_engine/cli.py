from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from ... import command_results as results
from ...cli_support import add_version_argument
from .. import native_engine
from .backend import plan_launcher
from .check import check
from .plan import load, prepare
from .start import READY_SECONDS, start, start_shared


def main(argv: list[str] | None = None) -> int:
    return results.invoke("narwhal-engine", argv, _main, operation="engine")


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare, inspect and launch pinned engines with container or native backends."
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format")
    add_version_argument(parser)
    descriptions = {
        "prepare": "Pin launch inputs from NARWHAL_ENGINE_LAUNCH_CONFIG and the deployment "
        "environment into a fresh directory; supports container and native backends.",
        "check": "Check the prepared model, tokenizer, runtime packages and connector; "
        "write checked.json for the plan's container or native backend.",
        "measure-cache": "Run a temporary sizing container from a checked, unused plan; "
        "write cache-layout.json and remove the completed sizing container (container only).",
        "model-dimensions": "Inspect model dimensions using a checked plan; "
        "write model-dimensions.json (container only).",
        "handshake-policy": "Inspect the installed connector compatibility policy using a checked "
        "plan; write handshake-policy.json (container and native).",
        "start": "Start one serving container from a checked plan and record container.id; "
        "verify HTTP readiness separately (container only).",
        "capture-cache": "Capture cache-layout.json from the running container recorded by "
        "a checked plan (container only).",
        "cache-registration": "Resolve cache block grouping from a checked plan and a startup "
        "log or runtime layout; write cache-registration.json (container and native).",
        "start-shared": "Start two to eight checked plans on one declared GPU, verify each "
        "engine's readiness and memory allowance, and record shared-start.json "
        "(container and native).",
        "stop-native": "Stop owned process groups after validating the recorded native "
        "process identities in the run directory (native only).",
    }
    sub = parser.add_subparsers(dest="command", required=True)
    public = {
        name: sub.add_parser(name, help=description, description=description)
        for name, description in descriptions.items()
    }
    preparation = public["prepare"]
    preparation.add_argument("--out", type=Path, required=True, help="fresh launch directory")
    preparation.add_argument(
        "--backend",
        choices=("container", "native"),
        default="container",
        help="launch backend (default: %(default)s)",
    )
    for command in (
        "check",
        "measure-cache",
        "model-dimensions",
        "handshake-policy",
        "start",
        "capture-cache",
    ):
        public[command].add_argument(
            "--run", type=Path, required=True, help="prepared launch directory"
        )
    registration = public["cache-registration"]
    registration.add_argument("--run", type=Path, required=True, help="checked launch directory")
    source = registration.add_mutually_exclusive_group(required=True)
    source.add_argument("--startup-log", type=Path, help="serving log with a resolved KV layout")
    source.add_argument("--runtime-layout", type=Path, help="captured runtime cache layout JSON")
    shared = public["start-shared"]
    shared.add_argument(
        "--run",
        type=Path,
        action="append",
        required=True,
        help="checked launch directory; repeat for two to eight engines on the same GPU; "
        "sum of per-engine memory fractions must be <= device_allowance",
    )
    shared.add_argument(
        "--ready-seconds",
        type=int,
        default=READY_SECONDS,
        help="readiness budget in seconds per engine, at least 1 (default: %(default)s)",
    )
    shared.add_argument(
        "--backend",
        choices=("container", "native"),
        default="container",
        help="backend shared by every selected plan (default: %(default)s)",
    )
    public["stop-native"].add_argument(
        "--run", type=Path, required=True, help="native launch directory with process records"
    )
    args = parser.parse_args(argv)
    if args.command == "start-shared" and args.ready_seconds < 1:
        parser.error(f"--ready-seconds must be positive, got {args.ready_seconds}")
    results.set_operation(args.command)
    roots = [args.out] if args.command == "prepare" else args.run
    if not isinstance(roots, list):
        roots = [roots]
    results.set_data({"runs": [str(root.resolve()) for root in roots]})
    for root in roots:
        for name in (
            "launch.json",
            "checked.json",
            "runtime-check.log",
            "container.id",
            "native-process.json",
            "shared-start.json",
            "cache-layout.json",
            "model-dimensions.json",
            "cache-registration.json",
            "handshake-policy.json",
        ):
            results.add_artifact(name.removesuffix(".json"), root / name)
    os.umask(0o077)
    target = getattr(args, "run", getattr(args, "out", ""))
    context = f"narwhal-engine: {args.command}"
    try:
        if args.command == "prepare":
            try:
                prepare(args.out, dict(os.environ), backend=args.backend)
            except (KeyError, TypeError) as error:
                if results.json_mode():
                    raise
                input_source = os.environ.get("NARWHAL_ENGINE_LAUNCH_CONFIG", "role environment")
                parser.exit(2, f"{context}: invalid input in {input_source}: {error}\n")
        elif args.command == "start-shared":
            runs = [run.resolve() for run in args.run]
            if args.backend == "native":
                native_engine.start_shared(runs, args.ready_seconds)
            else:
                start_shared(runs, args.ready_seconds)
        elif args.command == "stop-native":
            native_engine.stop(args.run.resolve())
        else:
            run = args.run.resolve()
            try:
                plan = load(run)
            except (OSError, ValueError) as error:
                if results.json_mode():
                    raise
                parser.exit(2, f"{context}: load run {run}: {error}\n")
            if plan.get("backend") == "native" and args.command not in {
                "check",
                "cache-registration",
                "handshake-policy",
            }:
                raise ValueError(
                    f"{args.command} is container-only; use native shared start or stop"
                )
            engine = plan_launcher(plan)
            if args.command == "cache-registration":
                engine.registration_layout(
                    run,
                    plan,
                    args.runtime_layout or args.startup_log,
                    args.runtime_layout is not None,
                )
                return 0
            {
                "check": check,
                "measure-cache": engine.measure_cache,
                "capture-cache": engine.capture_cache,
                "model-dimensions": engine.model_dimensions,
                "handshake-policy": engine.handshake_policy,
                "start": start,
            }[args.command](run, plan)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as error:
        if results.json_mode():
            raise
        if isinstance(error, FileExistsError):
            parser.exit(1, f"{context}: artifact already exists: {error.filename or error}\n")
        status = 2 if args.command == "prepare" else 1
        if isinstance(error, subprocess.TimeoutExpired):
            parser.exit(status, f"{context}: {target}: timed out after {error.timeout} seconds\n")
        parser.exit(status, f"{context}: {target}: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
