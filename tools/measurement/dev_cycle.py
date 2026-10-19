"""Replay the template's workloads through a verified four-engine development fleet."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import sys
import time
import uuid
from pathlib import Path

import httpx

from narwhal.config import FleetConfig
from narwhal.config.serialization import document
from narwhal.dev import lifecycle
from narwhal.profiling.store import ProfileStore
from narwhal.provenance import stamp
from tools.measurement import load_trial as trial

# A phase target sets its load in engine-equivalents of the fleet's profiles, so the
# same recipe raises the same controller load on any GPU.
TARGETS = ("decode_engines", "prefill_engines", "prefill_backlog_s")


def plan(root):
    instance = lifecycle.instance(root)
    template = lifecycle.read(root / "template.json")
    recipe = template["role_cycle"]
    if recipe["schema_version"] != 1 or instance["engine_count"] != 4:
        raise ValueError("role cycle requires recipe version 1 and four engines")
    for phase in recipe["phases"]:
        if not phase["name"] or any(c not in "abcdefghijklmnopqrstuvwxyz-" for c in phase["name"]):
            raise ValueError("phase names require lowercase letters and hyphens")
        check_target(phase)
    return instance, template


def check_target(phase):
    target = phase.get("target")
    if target is None:
        return
    if not isinstance(target, dict) or len(target) != 1 or next(iter(target)) not in TARGETS:
        raise ValueError(f"phase {phase['name']} target names one of {', '.join(TARGETS)}")
    value = next(iter(target.values()))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"phase {phase['name']} target requires a positive number")
    required = ("rate_rps",) if "prefill_backlog_s" in target else ("duration_s", "max_inflight")
    for key in required:
        if not phase.get(key, 0) > 0:
            raise ValueError(f"phase {phase['name']} target requires a positive {key}")


def sized(phase, profiles, template):
    """Return the phase with its rate, request count and in-flight cap set from its target."""
    target = phase.get("target")
    if target is None:
        return phase
    if not profiles:
        raise ValueError(f"phase {phase['name']} target requires the run's profiles")
    slo = template["slo"]
    reactive = template["controller"]["reactive"]
    input_tokens, output_tokens = phase["input_tokens"], phase["output_tokens"]
    prefill_s = sum(p.prefill_time(input_tokens) for p in profiles) / len(profiles)
    # Decode capacity per engine at the TPOT budget, priced at the mid-stream context.
    decode_rps = sum(
        p.decode_rps(slo["tpot_s"], input_tokens + output_tokens / 2, output_tokens)
        for p in profiles
    ) / len(profiles)
    kind, value = next(iter(target.items()))
    if kind == "prefill_backlog_s":
        # The whole backlog arrives at once, so every request is in flight together.
        requests = math.ceil(value / prefill_s)
        rate, inflight = phase["rate_rps"], requests
    else:
        if kind == "decode_engines":
            if decode_rps <= 0:
                raise ValueError(f"phase {phase['name']}: the profiles price no decode capacity")
            rate = value * decode_rps
        else:
            rate = value / prefill_s
        requests = math.ceil(rate * phase["duration_s"])
        # The cap stays above the rate times the slowest latency the SLO allows.
        inflight = max(
            phase["max_inflight"],
            math.ceil(rate * (slo["ttft_s"] + output_tokens * slo["tpot_s"])),
        )
        if rate * reactive["window_s"] < reactive["min_arrivals"]:
            raise ValueError(
                f"phase {phase['name']} offers {rate:.3g} requests/s, fewer than "
                f"{reactive['min_arrivals']} per {reactive['window_s']:g} s demand window"
            )
    return {
        **phase,
        "rate_rps": rate,
        "requests": requests,
        "max_inflight": inflight,
        "sizing": {
            "prefill_s": prefill_s,
            "decode_rps": decode_rps,
            "prefill_engines": rate * prefill_s,
            "decode_engines": rate / decode_rps if decode_rps > 0 else None,
        },
    }


def sized_recipe(template, profiles_path):
    recipe = template["role_cycle"]
    if not any("target" in phase for phase in recipe["phases"]):
        return recipe
    profiles = ProfileStore(Path(profiles_path)).all_profiles()
    return {**recipe, "phases": [sized(phase, profiles, template) for phase in recipe["phases"]]}


def check_config(path, template):
    effective = document(FleetConfig.load(path))
    for key in ("controller", "slo"):
        if effective[key] != template[key]:
            raise ValueError(f"{key} differs from the replay template: {path}")
    if effective["model"] != template["model"]["served_name"]:
        raise ValueError("fleet model differs from the replay template")
    return effective


def assess(before, after, reports, recipe):
    roles = {iid: role for role, ids in before["pools"].items() for iid in ids}
    splits = [[len(before["pools"]["prefill"]), len(before["pools"]["decode"])]]
    previous = before["flips"]
    history = after["flips"]
    intact = history[: len(previous)] == previous
    flips = history[len(previous) :] if intact else []
    for flip in flips:
        roles[flip["iid"]] = flip["to"]
        splits.append([list(roles.values()).count(role) for role in ("prefill", "decode")])
    same_processes = (
        before["journal_run"] == after["journal_run"]
        and before["lifecycle"]["process_starts"] == after["lifecycle"]["process_starts"]
    )
    movement = (
        intact
        and same_processes
        and all(flip["by"] == "reactive" for flip in flips)
        and splits == recipe["expected_splits"]
    )
    return {
        "passed": movement and all(report["phase_pass"] for report in reports),
        "role_cycle_pass": movement,
        "same_processes": same_processes,
        "observed_splits": splits,
        "expected_splits": recipe["expected_splits"],
        "flips": flips,
        "phases": reports,
    }


def phase_report(directory, phase):
    report = lifecycle.read(directory / "summary.json")
    rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
    # HTTP 429 TTFT admission rejections count as valid outcomes.
    valid = (
        report["client_schedule_valid"]
        and report["completed"] > 0
        and all(
            row["outcome"] == "completed"
            or (
                row["outcome"] == "http_error"
                and row["status"] == 429
                and "TTFT" in row.get("error_body", "")
            )
            for row in rows
        )
    )
    return {
        "name": phase["name"],
        **report,
        **({"sizing": phase["sizing"]} if "sizing" in phase else {}),
        "require_slo": phase["require_slo"],
        "phase_pass": valid and (report["candidate_pass"] or not phase["require_slo"]),
    }


async def replay(client, out, instance, template):
    recipe = template["role_cycle"]
    timeout = recipe["timeout_s"]
    base = instance["router_url"]
    await trial.drain(client, base, timeout)
    # Earlier requests leave the rolling demand window after window_s + step_s.
    delay = template["controller"]["reactive"]["window_s"]
    delay += template["controller"]["reactive"]["step_s"]
    print(f"Clearing demand history for {delay:g} seconds.", flush=True)
    await asyncio.sleep(delay)
    before = await trial.drain(client, base, timeout)
    trial.private_json(out / "state-before.json", before)
    initial = [len(before["pools"][role]) for role in ("prefill", "decode")]
    if initial != recipe["expected_splits"][0] or before["control"]["advisory"]:
        raise ValueError("replay requires an active controller starting at 2P:2D")
    reports = []
    for index, phase in enumerate(recipe["phases"]):
        print(f"Running {phase['name']}.", flush=True)
        directory = out / f"phase-{index + 1}-{phase['name']}"
        directory.mkdir(mode=0o700)
        workload = {
            "schema": 1,
            "kind": "synthetic-token-length",
            "model": template["model"]["served_name"],
            "token_pool": recipe["token_pool"],
            **{key: phase[key] for key in ("input_tokens", "output_tokens", "seed")},
        }
        source = out / f"workload-{index + 1}.json"
        trial.private_json(source, workload)
        args = argparse.Namespace(
            out=directory,
            workload=source,
            run_id=uuid.uuid4().hex,
            run_seed=0,
            requests=phase["requests"],
            rate=phase["rate_rps"],
            max_inflight=phase["max_inflight"],
            timeout=timeout,
            max_lag=recipe["max_schedule_lag_s"],
            ttft=template["slo"]["ttft_s"],
            tpot=template["slo"]["tpot_s"],
            attainment=1.0,
        )
        await trial.run_trial(client, base, args)
        reports.append(phase_report(directory, phase))
    after = await trial.drain(client, base, timeout)
    trial.private_json(out / "state-after.json", after)
    result = assess(before, after, reports, recipe)
    result.update(
        started_at=lifecycle.read(out / "manifest.json")["started_at"], ended_at=time.time()
    )
    result["grafana_range"] = {
        "from": int(result["started_at"] * 1000),
        "to": int(result["ended_at"] * 1000),
    }
    trial.private_json(out / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path, default=Path("runs/dev"))
    parser.add_argument("--out", type=Path, help="Fresh directory for the replay records")
    parser.add_argument("--dry-run", action="store_true", help="Check and print the saved recipe")
    args = parser.parse_args(argv)
    root = args.instance.expanduser().resolve()
    out = (args.out or root / f"cycle-{uuid.uuid4().hex[:12]}").resolve()
    try:
        instance, template = plan(root)
        configured = check_config(root / "fleet.json", template)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "slo": template["slo"],
                        "controller": configured["controller"],
                        "role_cycle": template["role_cycle"],
                    },
                    indent=2,
                )
            )
            return 0
        status = lifecycle.status(root)
        if status["status"] != "ready":
            raise ValueError("run dev up and dev verify before replay")
        run = Path(status["run"])
        effective = check_config(run / "fleet.json", template)
        recipe = sized_recipe(template, effective["profiles"]["path"])
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, value in (
            ("template", template),
            ("fleet", effective),
            ("instance", instance),
            ("phases", recipe["phases"]),
        ):
            trial.private_json(out / f"{name}.json", value)
        template = {**template, "role_cycle": recipe}
        trial.private_json(
            out / "manifest.json",
            {
                **stamp()["meta"],
                "host": platform.node(),
                "python": platform.python_version(),
                "httpx": httpx.__version__,
                "command": sys.argv,
                "started_at": time.time(),
                "load_helper_sha256": trial.digest(Path(trial.__file__)),
                "cycle_helper_sha256": trial.digest(Path(__file__)),
                "run": str(run),
                "template_sha256": trial.digest(root / "template.json"),
            },
        )

        async def execute():
            async with httpx.AsyncClient(
                timeout=template["role_cycle"]["timeout_s"], trust_env=False
            ) as client:
                return await replay(client, out, instance, template)

        result = asyncio.run(execute())
        print(json.dumps(result, indent=2))
        print(f"Replay records: {out}")
        return 0 if result["passed"] else 2
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        print(f"Role cycle: {exc}. Records: {out}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
