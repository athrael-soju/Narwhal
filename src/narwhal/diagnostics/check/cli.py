"""The narwhal-check command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib import resources
from pathlib import Path

import httpx

from ... import command_results as results
from ...cli_support import add_version_argument
from ...config import FleetConfig
from ...contracts import manifest
from ...profiling.calibration import calibrate
from .evidence import verify_directed_kv_evidence
from .preflight import run


def main(argv: list[str] | None = None) -> int:
    """Run the fleet-check CLI."""
    return results.invoke("narwhal-check", argv, _main, operation="check")


def _main(argv: list[str]) -> int:
    """Parse a fleet-check operation."""
    ap = argparse.ArgumentParser(
        description="Check running engines and their measured profiles before starting the "
        "router. By default, probe the full eligible directed KV mesh; retain it with "
        "--evidence-out or verify a saved mesh with --verify-evidence.",
    )
    add_version_argument(ap)
    results.add_format(ap)
    ap.add_argument(
        "--fleet", help="fleet config JSON; required for preflight and evidence verification"
    )
    ap.add_argument(
        "--ring",
        action="store_true",
        help="test rotating producer-consumer pairs (default: full eligible directed mesh)",
    )
    ap.add_argument(
        "--calibrate-first-token",
        action="store_true",
        help="measure directed first-token latency using a separate observation bound",
    )
    ap.add_argument(
        "--input-tokens",
        help="comma-separated input lengths, including the longest admitted input",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=100,
        help="fresh handoffs per pair and input length (100 required for qualifying evidence)",
    )
    ap.add_argument(
        "--observation-timeout-s",
        type=float,
        help="diagnostic first-token bound above the configured serving limit",
    )
    ap.add_argument(
        "--calibration-out",
        type=Path,
        help="fresh JSON path under runs/ for first-token calibration samples",
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="KV transfer probes per pair, clamped to at least 1 (default: %(default)s)",
    )
    ap.add_argument(
        "--no-kv",
        action="store_true",
        help="run reach, calibration, contract, profile, model, pace, tokenize and slo gates "
        "(default: include produce and consume)",
    )
    ap.add_argument(
        "--evidence-out",
        type=Path,
        help="fresh JSON path for process-bound full-mesh KV evidence; requires "
        "engine_contract and full KV mesh; exclusive with --ring, --no-kv and --verify-evidence "
        "(default: gate output only)",
    )
    ap.add_argument(
        "--verify-evidence",
        type=Path,
        help="verify saved KV evidence against the current fleet, profiles and live processes; "
        "requires engine_contract; exclusive with --evidence-out; "
        "--ring, --no-kv and --repeats apply to new probes "
        "(default: run preflight)",
    )
    ap.add_argument(
        "--print-example-config",
        action="store_true",
        help="print the annotated example fleet config and exit before fleet loading; "
        "takes precedence over --print-contract-versions (default: false)",
    )
    ap.add_argument(
        "--print-contract-versions",
        action="store_true",
        help="print the versioned machine-readable interface registry and exit before "
        "fleet loading (default: false)",
    )
    args = ap.parse_args(argv)

    if args.print_example_config:
        results.set_operation("print-example-config")
        example = resources.files("narwhal").joinpath("fleet.example.json").read_text()
        results.set_data(json.loads(example))
        print(example, end="")
        return 0
    if args.print_contract_versions:
        results.set_operation("print-contract-versions")
        results.set_data(manifest())
        print(json.dumps(manifest(), indent=2))
        return 0

    if args.fleet is None:
        ap.error("give --fleet")
    if args.calibrate_first_token:
        if args.input_tokens is None or args.observation_timeout_s is None:
            ap.error("calibration requires --input-tokens and --observation-timeout-s")
        if args.calibration_out is None:
            ap.error("calibration requires --calibration-out")
        if args.evidence_out is not None or args.verify_evidence is not None:
            ap.error("calibration is exclusive with directed KV evidence modes")
        if args.ring or args.no_kv or args.repeats != 1:
            ap.error("calibration always probes the full directed mesh with --samples")
    elif (
        args.input_tokens is not None
        or args.calibration_out is not None
        or args.observation_timeout_s is not None
        or args.samples != 100
    ):
        ap.error("calibration options require --calibrate-first-token")
    if args.evidence_out is not None and (args.ring or args.no_kv):
        ap.error("--evidence-out requires the full KV mesh")
    if args.evidence_out is not None and args.verify_evidence is not None:
        ap.error("choose --evidence-out or --verify-evidence")
    if args.evidence_out is not None:
        results.add_artifact("directed_kv_evidence", args.evidence_out)
    from ...cli_errors import failure

    try:
        cfg = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"load fleet {args.fleet}", exc, 2)
    if results.json_mode():
        results.protect_environment(cfg.engine_api_key_env)
    try:
        if args.calibrate_first_token:
            calibration_out = args.calibration_out
            input_tokens = args.input_tokens
            observation_timeout_s = args.observation_timeout_s
            if calibration_out is None or input_tokens is None or observation_timeout_s is None:
                raise ValueError("calibration options are incomplete")
            results.set_operation("calibrate-first-token")
            results.add_artifact("first_token_calibration", calibration_out)
            lengths = tuple(int(part) for part in input_tokens.split(","))
            return asyncio.run(
                calibrate(
                    cfg,
                    input_tokens=lengths,
                    samples_per_group=args.samples,
                    observation_timeout_s=observation_timeout_s,
                    out=calibration_out,
                )
            )
        if args.verify_evidence is not None:
            results.set_operation("verify-evidence")
            results.add_artifact("directed_kv_evidence", args.verify_evidence)
            problems = asyncio.run(
                verify_directed_kv_evidence(cfg, Path(args.fleet), args.verify_evidence)
            )
            results.set_data({"failed": problems})
            for problem in problems:
                results.record_error("evidence_gate_failed", problem, stage="verify-evidence")
                print(f"  FAIL  {problem}")
            if problems:
                return 1
            print("directed KV evidence matches the current fleet, profiles, and processes")
            return 0
        return asyncio.run(
            run(
                cfg,
                not args.ring,
                args.no_kv,
                args.repeats,
                evidence_out=args.evidence_out,
                fleet_path=Path(args.fleet),
            )
        )
    except ValueError as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"check fleet {args.fleet}", exc, 2)
    except (OSError, httpx.HTTPError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-check", f"check fleet {args.fleet}", exc, 1)


if __name__ == "__main__":
    sys.exit(main())
