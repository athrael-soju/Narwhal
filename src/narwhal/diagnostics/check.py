"""Run fleet preflight gates from cheap health checks through KV transfer."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from hashlib import sha256
from importlib import resources
from pathlib import Path

import httpx

from .. import command_results as results
from ..cli_support import add_version_argument
from ..config import FleetConfig
from ..contracts import manifest
from ..engines.client import EngineClient
from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect
from ..engines.validation import validation_pairs
from ..profiling.calibration import calibrate
from ..profiling.generation import binding_digest, generation_problem
from .gates import (
    colocated_restart_risk,
    gate_calibration,
    gate_contract,
    gate_model,
    gate_pace,
    gate_reach,
    gate_tokenize,
)
from .profile_gates import gate_profile, gate_profile_generation, gate_slo
from .report import Report
from .transfer import (
    gate_consume,
    gate_produce,
    pair_snapshot,
    same_generation,
    verify_directed_kv_evidence,
)


async def run(
    cfg: FleetConfig,
    mesh: bool,
    skip_kv: bool,
    repeats: int = 1,
    *,
    report: Report | None = None,
    evidence_out: Path | None = None,
    fleet_path: Path | None = None,
) -> int:
    """Run every preflight gate and return a process exit code."""
    if evidence_out is not None:
        if not mesh or skip_kv or cfg.engine_contract is None or fleet_path is None:
            raise ValueError(
                "directed KV evidence requires a fleet file, full mesh, and engine contract"
            )
        if evidence_out.exists():
            raise ValueError(f"directed KV evidence already exists: {evidence_out}")
    fleet_hash = sha256(fleet_path.read_bytes()).hexdigest() if fleet_path is not None else None
    profile_hash = (
        sha256(cfg.profiles_path.read_bytes()).hexdigest()
        if evidence_out is not None and cfg.profiles_path.exists()
        else None
    )
    print(f"fleet: {len(cfg.engines)} engines, model {cfg.model}")
    print(f"slo:   ttft <= {cfg.slo.ttft_s}s, tpot <= {cfg.slo.tpot_s}s")
    rep = report or Report()
    # Preflight uses the serving timeouts.
    client = EngineClient(
        timeout_s=cfg.request_timeout_s,
        prefill_timeout_s=cfg.prefill_timeout_s,
        read_timeout_s=cfg.decode_read_timeout_s,
        max_connections=cfg.max_connections,
        control_connections=cfg.resolved_control_connections(),
        pool_timeout_s=cfg.pool_timeout_s,
        connect_timeout_s=cfg.connect_timeout_s,
        health_timeout_s=cfg.health_timeout_s,
        kv=lookup_connector(cfg.connector),
        dialect=lookup_dialect(cfg.dialect),
        model=cfg.model,
        engine_api_key=cfg.resolve_engine_key(),
    )
    try:
        live = await gate_reach(cfg, client, rep)
        if restart_risk := await colocated_restart_risk(cfg):
            rep.warn(restart_risk)
        await gate_calibration(cfg, rep)
        incompatible = await gate_contract(cfg, live, rep)
        store = gate_profile(cfg, rep)
        stale = await gate_profile_generation(cfg, store, live, rep)
        incompatible.update(stale)
        incompatible.update(await gate_model(cfg, live, rep))
        pace_store = store if not stale else None
        slow = await gate_pace(cfg, live, rep, pace_store)
        await gate_tokenize(cfg, live, client, rep)
        if skip_kv:
            rep.skip("produce and consume: skipped by --no-kv")
        elif slow or incompatible:
            # A stalled transfer can terminate a healthy peer's engine core.
            blocked = slow | incompatible
            names = ", ".join(sorted(blocked))
            rep.skip(f"produce and consume: pre-transfer gate failed on {names}")
        else:
            handoffs = await gate_produce(cfg, live, client, rep)
            if evidence_out is None:
                await gate_consume(cfg, live, handoffs, client, rep, mesh, repeats)
            else:
                await gate_consume(
                    cfg, live, handoffs, client, rep, mesh, repeats, evidence=rep.pairs
                )
        gate_slo(cfg, store, rep)
        if evidence_out is not None:
            expected = validation_pairs(cfg.engines, mesh=True)
            for src, dst in expected:
                passed = sum(
                    row.get("producer") == src
                    and row.get("consumer") == dst
                    and row.get("status") == "passed"
                    for row in rep.pairs
                )
                if passed < max(1, repeats):
                    rep.fail(f"{src} -> {dst}: no current passing directed KV evidence")
            first_generation: dict[str, dict[str, object]] = {}
            generation_failures: set[str] = set()
            for row in rep.pairs:
                for side in ("producer", "consumer"):
                    iid = str(row[side])
                    for phase in ("before", "after"):
                        snapshot = row.get(f"{side}_{phase}")
                        if isinstance(snapshot, dict):
                            first = first_generation.setdefault(iid, snapshot)
                            if not same_generation(first, snapshot):
                                generation_failures.add(
                                    f"{iid} process generation differs across directed KV probes"
                                )
            for iid, before in first_generation.items():
                try:
                    current = await pair_snapshot(cfg, iid)
                    if not same_generation(before, current):
                        generation_failures.add(f"{iid} process changed after directed KV probes")
                    for profile in store.profiles_for_engine(iid):
                        problem = generation_problem(
                            iid,
                            profile.generation_digest,
                            binding_digest(current),
                        )
                        if problem:
                            generation_failures.add(problem)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    generation_failures.add(f"{iid} current process verification failed: {exc}")
            for problem in sorted(generation_failures):
                rep.fail(problem)
            if fleet_path is None or sha256(fleet_path.read_bytes()).hexdigest() != fleet_hash:
                rep.fail("fleet changed during directed KV qualification")
            current_profile_hash = (
                sha256(cfg.profiles_path.read_bytes()).hexdigest()
                if cfg.profiles_path.exists()
                else None
            )
            if current_profile_hash != profile_hash or profile_hash is None:
                rep.fail("profile store changed during directed KV qualification")
    finally:
        await client.aclose()

    if evidence_out is not None:
        contract = cfg.engine_contract
        if fleet_path is None or contract is None:
            raise ValueError("directed KV evidence has no fleet file or engine contract")
        document = {
            "schema": "narwhal.directed-kv-evidence",
            "schema_version": 1,
            "captured_at_unix": time.time(),
            "fleet_sha256": fleet_hash,
            "profile_sha256": profile_hash,
            "model": cfg.model,
            "contract_fingerprint": contract.fingerprint(),
            "expected_pairs": [list(pair) for pair in validation_pairs(cfg.engines, mesh=True)],
            "repeats": max(1, repeats),
            "pairs": rep.pairs,
            "failed": rep.failed,
            "skipped": rep.skipped,
            "status": "passed" if not rep.failed and not rep.skipped else "failed",
        }
        evidence_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(evidence_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(document, output, indent=2)
            output.write("\n")
        print(f"directed KV evidence: {evidence_out}")

    results.set_data(
        {
            "failed": rep.failed,
            "skipped": rep.skipped,
            "warnings": rep.warnings,
            "pairs": rep.pairs,
            "first_token_calibration": rep.calibration,
        }
    )
    for problem in rep.failed:
        results.record_error("gate_failed", problem, stage="preflight")
    if rep.failed or (rep.skipped and evidence_out is not None):
        results.set_status("failed_gate")
    elif rep.skipped:
        results.set_status("degraded")
        results.record_error(
            "gates_skipped", "Preflight completed its selected gates", stage="preflight"
        )
    print()
    if rep.failed:
        print(f"{len(rep.failed)} gate(s) failed, {len(rep.skipped)} skipped")
        return 1
    if rep.skipped and evidence_out is not None:
        print(f"directed KV evidence incomplete: {len(rep.skipped)} gate(s) skipped")
        return 1
    if rep.skipped:
        print(f"all gates pass, {len(rep.skipped)} skipped")
        return 0
    print("all gates pass")
    return 0


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
        results.set_data(
            json.loads(resources.files("narwhal").joinpath("fleet.example.json").read_text())
        )
        print(resources.files("narwhal").joinpath("fleet.example.json").read_text(), end="")
        return 0
    if args.print_contract_versions:
        results.set_operation("print-contract-versions")
        results.set_data(manifest())
        print(json.dumps(manifest(), indent=2))
        return 0

    if args.fleet is None:
        ap.error("give --fleet")
        return 2
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
    from ..cli_errors import failure

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
