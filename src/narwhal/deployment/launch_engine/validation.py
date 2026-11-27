"""The `narwhal-engine` actions that validate an engine host and its live engine.

`inspect-host` runs before `prepare`, and `probe` after the engine starts. Each writes its
evidence into an `--out` directory and refuses to overwrite earlier evidence there. A failed
check exits 1, and with `--format json` reports `failed_gate`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import httpx

from ... import command_results as results
from .backend import plan_launcher
from .evidence import CheckFailed, evidence_directory
from .inspection import EVIDENCE, LOG, inspect, load_inputs
from .plan import load
from .probe import RECORD, probe, responses, summary

DESCRIPTIONS = {
    "inspect-host": "Check this engine host against NARWHAL_ENGINE_LAUNCH_CONFIG before "
    "prepare: the GPUs, image, model, run directory, fabric interface, planned ports and "
    "device permissions; write host-inspection.json (container and native).",
    "probe": "Check the live engine of a checked plan over HTTP: health, version, served "
    "model and, by engine, process start and one completion; write the responses and "
    "probe.json (container and native).",
}
OUT_HELP = "evidence directory, created when absent; must not hold earlier evidence"


def add_arguments(public: dict[str, argparse.ArgumentParser]) -> None:
    inspection = public["inspect-host"]
    inspection.add_argument("--out", type=Path, required=True, help=OUT_HELP)
    inspection.add_argument(
        "--backend",
        choices=("container", "native"),
        default="container",
        help="launch backend the engine will use (default: %(default)s)",
    )
    probing = public["probe"]
    probing.add_argument("--run", type=Path, required=True, help="checked launch directory")
    probing.add_argument("--out", type=Path, required=True, help=OUT_HELP)


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run `inspect-host` or `probe`, reporting a failed check as a failed gate."""
    os.umask(0o077)
    out = args.out.resolve()
    context = f"narwhal-engine: {args.command}"
    results.set_data({"out": str(out)})
    if args.command == "inspect-host":
        for name in (EVIDENCE, LOG):
            results.add_artifact(name.removesuffix(".json").removesuffix(".log"), out / name)
    try:
        if args.command == "inspect-host":
            _inspect(args, out, parser, context)
        else:
            _probe(args, out, parser, context)
    except CheckFailed as error:
        if results.json_mode():
            results.set_status("failed_gate")
            results.record_error("gate_failed", str(error), stage=args.command)
            print(error, file=sys.stderr)
            return 1
        parser.exit(1, f"{context}: {error}\n")
    except (ValueError, OSError, KeyError, httpx.HTTPError, subprocess.SubprocessError) as error:
        if results.json_mode():
            raise
        if isinstance(error, FileExistsError):
            parser.exit(1, f"{context}: evidence already exists: {error.filename}\n")
        parser.exit(1, f"{context}: {error}\n")
    return 0


def _inspect(
    args: argparse.Namespace, out: Path, parser: argparse.ArgumentParser, context: str
) -> None:
    out = evidence_directory(out, (EVIDENCE, LOG))
    try:
        inputs = load_inputs(dict(os.environ), args.backend, out)
    except (KeyError, TypeError, ValueError, OSError) as error:
        if results.json_mode():
            raise
        detail = f"{error.args[0]} is unset" if isinstance(error, KeyError) else error
        parser.exit(2, f"{context}: invalid input: {detail}\n")
    report = inspect(out, inputs)
    results.set_data({"role": report["role"], "passed": True})
    print(f"{report['role']}: host inspection passed; evidence in {out / EVIDENCE}")


def _probe(
    args: argparse.Namespace, out: Path, parser: argparse.ArgumentParser, context: str
) -> None:
    launch = args.run.resolve()
    results.set_data({"runs": [str(launch)]})
    try:
        plan = load(launch)
    except (OSError, ValueError) as error:
        if results.json_mode():
            raise
        parser.exit(2, f"{context}: load run {launch}: {error}\n")
    for name in (*responses(plan_launcher(plan).live_probe), RECORD):
        results.add_artifact(name.rsplit(".", 1)[0], out / name)
    model = os.environ.get("NARWHAL_ENGINE_MODEL_NAME", "")
    if not model:
        raise ValueError("set NARWHAL_ENGINE_MODEL_NAME to the served model name")
    record = probe(launch, plan, out, model)
    results.set_data({"role": record["role"], "passed": True})
    print(summary(record))
