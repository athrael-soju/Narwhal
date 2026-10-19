"""Replay the template's workloads through a verified four-engine development fleet."""

from __future__ import annotations

import argparse
import asyncio
import itertools
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
from narwhal.types import Role
from tools.measurement import load_trial as trial

# A phase target sets its load in engine-equivalents of the profiles the controller prices at
# the split where the phase starts, so the same recipe raises the same controller load on any GPU.
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


def split_profiles(store, roles, split):
    """Return the profiles the controller prices at a split, one per engine.

    The engines take the measured role assignment nearest their configured `roles`.
    """
    prefill, decode = split
    iids = list(roles)
    pools = sorted(
        itertools.combinations(iids, prefill),
        key=lambda pool: sum((iid in pool) != (roles[iid] is Role.PREFILL) for iid in iids),
    )
    for pool in pools:
        assigned = {iid: Role.PREFILL if iid in pool else Role.DECODE for iid in iids}
        rows = store.profiles_for_split(iids, prefill, decode, assigned)
        if rows:
            return rows
    raise ValueError(f"the run's profiles price no {prefill}P:{decode}D split")


def prices(phase, rows, slo):
    """Return a phase request's mean prefill seconds and per-engine decode requests/s."""
    input_tokens, output_tokens = phase["input_tokens"], phase["output_tokens"]
    prefill_s = sum(p.prefill_time(input_tokens) for p in rows) / len(rows)
    if not prefill_s > 0:
        raise ValueError(f"phase {phase['name']}: the profiles predict no prefill time")
    # Decode capacity per engine at the TPOT budget, priced at the mid-stream context.
    decode_rps = sum(
        p.decode_rps(slo["tpot_s"], input_tokens + output_tokens / 2, output_tokens) for p in rows
    ) / len(rows)
    return prefill_s, decode_rps


def sized(phase, split, rows, template, limit):
    """Return the phase with its rate, request count and in-flight cap set from its target.

    `rows` are the profiles the controller prices at `split`, where the phase starts, and
    `limit` is the router's in-flight limit.
    """
    target = phase.get("target")
    if target is None:
        return phase
    slo = template["slo"]
    reactive = template["controller"]["reactive"]
    prefill_s, decode_rps = prices(phase, rows, slo)
    kind, value = next(iter(target.items()))
    if kind == "prefill_backlog_s":
        # The whole backlog arrives at once, so every request is in flight together.
        requests = min(math.ceil(value / prefill_s), limit)
        backlog_s = requests * prefill_s
        if backlog_s / split[0] <= slo["ttft_s"]:
            raise ValueError(
                f"phase {phase['name']} queues {backlog_s / split[0]:.3g} s of prefill work on "
                f"each of {split[0]} prefill engines, within the {slo['ttft_s']:g} s TTFT budget"
            )
        return {
            **phase,
            "requests": requests,
            "max_inflight": requests,
            "sizing": {
                "split": list(split),
                "prefill_s": prefill_s,
                "prefill_backlog_s": backlog_s,
            },
        }
    if kind == "decode_engines":
        if not decode_rps > 0:
            raise ValueError(f"phase {phase['name']}: the profiles price no decode capacity")
        rate = value * decode_rps
    else:
        rate = value / prefill_s
    if rate * reactive["window_s"] < reactive["min_arrivals"]:
        raise ValueError(
            f"phase {phase['name']} offers {rate:.3g} requests/s, fewer than "
            f"{reactive['min_arrivals']} per {reactive['window_s']:g} s demand window"
        )
    return {
        **phase,
        "rate_rps": rate,
        "requests": math.ceil(rate * phase["duration_s"]),
        # The cap stays above the rate times the slowest latency the SLO allows.
        "max_inflight": max(
            phase["max_inflight"],
            math.ceil(rate * (slo["ttft_s"] + phase["output_tokens"] * slo["tpot_s"])),
        ),
        "sizing": {
            "split": list(split),
            "prefill_s": prefill_s,
            "decode_rps": decode_rps,
            "prefill_engines": rate * prefill_s,
            "decode_engines": rate / decode_rps if decode_rps > 0 else None,
        },
    }


def target_band(phase, start, end, rows_at, template):
    """Return the target range that moves the controller from `start` to `end` and holds it.

    A pool's load is its demand over its engine count times `utilization`, and the objective is
    the larger pool load. The controller moves one engine when the move lowers the objective by
    at least `movement_margin` and leaves the pool that gives up the engine at or below
    `shrink`, and it holds while demand stays below `demand_floor`. Demand scales with the
    request rate, so each rule bounds the target. `rows_at` returns a split's profiles.
    """
    controller = template["controller"]
    reactive, shrink = controller["reactive"], controller["thresholds"]["shrink"]
    utilization, margin = reactive["utilization"], reactive["movement_margin"]
    step = end[0] - start[0]
    if abs(step) != 1 or sum(start) != sum(end):
        raise ValueError(f"phase {phase['name']} target requires a one-engine move")

    def loads(split):
        # Prefill and decode pool loads per request/s.
        prefill_s, decode_rps = prices(phase, rows_at(split), template["slo"])
        if not decode_rps > 0:
            raise ValueError(
                f"phase {phase['name']}: the profiles price no decode capacity at "
                f"{split[0]}P:{split[1]}D"
            )
        return prefill_s / (split[0] * utilization), 1 / (decode_rps * split[1] * utilization)

    gain = max(loads(start)) - max(loads(end))
    if not gain > 0:
        raise ValueError(
            f"phase {phase['name']} load does not favour {end[0]}P:{end[1]}D over "
            f"{start[0]}P:{start[1]}D"
        )
    prefill_s, decode_rps = prices(phase, rows_at(start), template["slo"])
    low = max(margin / gain, reactive["demand_floor"] / (prefill_s + 1 / decode_rps))
    # The prefill pool gives up an engine on a move to decode, the decode pool on a move to
    # prefill.
    high = shrink / loads(end)[0 if step < 0 else 1]
    beyond = (end[0] + step, end[1] - step)
    floors = (max(1, controller["min_prefill"]), max(1, controller["min_decode"]))
    if beyond[0] >= floors[0] and beyond[1] >= floors[1]:
        further = max(loads(end)) - max(loads(beyond))
        if further > 0:
            high = min(high, margin / further)
    unit = prefill_s if "prefill_engines" in phase["target"] else 1 / decode_rps
    return low * unit, high * unit


def sized_recipe(template, effective):
    """Size each target phase from the profiles the controller prices at its starting split."""
    recipe = template["role_cycle"]
    if not any("target" in phase for phase in recipe["phases"]):
        return recipe
    splits = [tuple(split) for split in recipe["expected_splits"]]
    if len(splits) <= len(recipe["phases"]):
        raise ValueError("role cycle requires an expected split after each phase")
    store = ProfileStore(Path(effective["profiles"]["path"]))
    roles = {engine["iid"]: Role(engine["role"]) for engine in effective["engines"]}
    priced = {}

    def rows_at(split):
        if split not in priced:
            priced[split] = split_profiles(store, roles, split)
        return priced[split]

    limit = effective["serving"]["max_connections"] + effective["serving"]["queue_capacity"]
    phases = []
    for index, phase in enumerate(recipe["phases"]):
        if "target" in phase:
            start, end = splits[index], splits[index + 1]
            phase = sized(phase, start, rows_at(start), template, limit)
            if "prefill_backlog_s" not in phase["target"]:
                low, high = target_band(phase, start, end, rows_at, template)
                value = next(iter(phase["target"].values()))
                if not low <= value < high:
                    raise ValueError(
                        f"phase {phase['name']} target {value:g} lies outside {low:.3g} to "
                        f"{high:.3g}, where its load moves the controller from "
                        f"{start[0]}P:{start[1]}D to {end[0]}P:{end[1]}D and holds it there"
                    )
                phase["sizing"]["target_band"] = [low, high]
        phases.append(phase)
    # Request i's prompt draws from seed + i and the warm-up's from seed + requests, so
    # overlapping ranges repeat cached prompts.
    ranges = sorted((p["seed"], p["seed"] + p["requests"] + 1, p["name"]) for p in phases)
    for (_, last, first), (seed, _, second) in itertools.pairwise(ranges):
        if seed < last:
            raise ValueError(f"phases {first} and {second} repeat prompts from overlapping seeds")
    return {**recipe, "phases": phases}


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
        recipe = sized_recipe(template, effective)
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
