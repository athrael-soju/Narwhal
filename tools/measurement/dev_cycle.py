"""Replay the template's workloads through a verified four-engine development fleet."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
import uuid
from pathlib import Path

import httpx

from narwhal.config import FleetConfig
from narwhal.config.serialization import document
from narwhal.dev import lifecycle
from narwhal.provenance import stamp
from tools.measurement import load_trial as trial


def plan(root):
    instance = lifecycle.instance(root)
    template = lifecycle.read(root / "template.json")
    recipe = template["role_cycle"]
    if recipe["schema_version"] != 1 or instance["engine_count"] != 4:
        raise ValueError("role cycle requires recipe version 1 and four engines")
    for phase in recipe["phases"]:
        if not phase["name"] or any(c not in "abcdefghijklmnopqrstuvwxyz-" for c in phase["name"]):
            raise ValueError("phase names require lowercase letters and hyphens")
    return instance, template


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
    # The burst exercises overload; preserve its SLO result alongside movement acceptance.
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
        "require_slo": phase["require_slo"],
        "phase_pass": valid and (report["candidate_pass"] or not phase["require_slo"]),
    }


async def replay(client, out, instance, template):
    recipe = template["role_cycle"]
    timeout = recipe["timeout_s"]
    base = instance["router_url"]
    await trial.drain(client, base, timeout)
    # Expire verification's short arithmetic request from the rolling demand window.
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
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, value in (("template", template), ("fleet", effective), ("instance", instance)):
            trial.private_json(out / f"{name}.json", value)
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
