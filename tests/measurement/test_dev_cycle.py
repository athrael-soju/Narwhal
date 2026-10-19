"""Check role-history replay and keep overload acceptance separate from latency."""

import copy
import heapq
import io
import itertools
import json
import math
import statistics
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.dev.template import _read_template, reference
from narwhal.profiling.store import ProfileStore
from narwhal.scheduling.control import SLO, Thresholds
from narwhal.scheduling.controller import ReactiveController
from narwhal.scheduling.monitor import InstanceMonitor
from narwhal.scheduling.scheduler.placement import GlobalScheduler
from narwhal.types import Instance, Phase, Request, Role
from tests.fixtures import profile
from tools.measurement import dev_cycle

FOUR_SGLANG = "four-engine-cuda-sglang-v1.json"
ROLES = {"n1": Role.PREFILL, "n2": Role.PREFILL, "n3": Role.DECODE, "n4": Role.DECODE}
SPLITS = ((1, 3), (2, 2), (3, 1))


def small_gpu_profile(iid):
    """A profile with prefill and decode costs of the order a small GPU measures."""
    return profile(
        iid,
        ttft_a=1e-9,
        ttft_b=1.2e-5,
        ttft_c=0.01,
        tpot_slope=1e-7,
        tpot_intercept=0.008,
        tpot_request_slope=0.0005,
        decode_max_requests=2,
        decode_max_kv_tokens=20_000,
        kv_capacity_tokens=200_000,
    )


def colocated_store(path, prefill=1.0, decode=1.0, contention=0.0):
    """Write a profile store measured at 1P:3D, 2P:2D and 3P:1D on one shared GPU.

    The first engines take prefill at each split. `prefill` and `decode` scale every engine's
    costs, and each other engine in the same role adds `contention` of them.
    """
    store = ProfileStore(path)
    for split in SPLITS:
        fill = prefill * (1 + contention * (split[0] - 1))
        drain = decode * (1 + contention * (split[1] - 1))
        for index, iid in enumerate(ROLES):
            base = small_gpu_profile(iid)
            store.put(
                replace(
                    base,
                    ttft_a=base.ttft_a * fill,
                    ttft_b=base.ttft_b * fill,
                    ttft_c=base.ttft_c * fill,
                    tpot_slope=base.tpot_slope * drain,
                    tpot_intercept=base.tpot_intercept * drain,
                    tpot_request_slope=base.tpot_request_slope * drain,
                    colocated_group="gpu-0",
                    colocated_target_role=(Role.PREFILL if index < split[0] else Role.DECODE).value,
                    colocated_prefill_engines=split[0],
                    colocated_decode_engines=split[1],
                    colocated_prefill_rps=1.0,
                    colocated_decode_rps=1.0,
                )
            )
    return store


def effective(path, max_connections=768, queue_capacity=0):
    """Return the effective-config fields that size a role cycle."""
    return {
        "engines": [{"iid": iid, "role": role.value} for iid, role in ROLES.items()],
        "profiles": {"path": str(path)},
        "serving": {"max_connections": max_connections, "queue_capacity": queue_capacity},
    }


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.recipe = reference()["role_cycle"]
        self.before = {
            "pools": {"prefill": ["n1", "n2"], "decode": ["n3", "n4"]},
            "flips": [],
            "control": {"advisory": False},
            "journal_run": "router-1",
            "lifecycle": {"process_starts": {"n1": 1, "n2": 2, "n3": 3, "n4": 4}},
        }
        self.after = copy.deepcopy(self.before)
        self.after["flips"] = [
            {"iid": iid, "to": role, "by": "reactive", "at": index}
            for index, (iid, role) in enumerate(
                [("n2", "decode"), ("n2", "prefill"), ("n3", "prefill"), ("n3", "decode")]
            )
        ]
        self.reports = [{"phase_pass": True}] * 3

    def assess(self):
        return dev_cycle.assess(self.before, self.after, self.reports, self.recipe)

    def test_captures_short_lived_split_between_state_snapshots(self):
        result = self.assess()
        self.assertEqual(result["observed_splits"], [[2, 2], [1, 3], [2, 2], [3, 1], [2, 2]])
        self.assertTrue(result["passed"])

    def test_rejects_missing_split_manual_move_and_process_restart(self):
        original = copy.deepcopy(self.after)
        for change in ("missing", "manual", "router", "engine"):
            with self.subTest(change=change):
                self.after = copy.deepcopy(original)
                if change == "missing":
                    self.after["flips"] = self.after["flips"][:2]
                elif change == "manual":
                    self.after["flips"][0]["by"] = "operator"
                elif change == "router":
                    self.after["journal_run"] = "router-2"
                else:
                    self.after["lifecycle"]["process_starts"]["n1"] = 42
                self.assertFalse(self.assess()["passed"])

    def test_rejects_lost_history_and_failed_steady_phase(self):
        self.before["flips"] = [{"at": -1}]
        self.assertFalse(self.assess()["passed"])
        self.before["flips"] = []
        self.reports[0] = {"phase_pass": False}
        result = self.assess()
        self.assertTrue(result["role_cycle_pass"])
        self.assertFalse(result["passed"])

    def test_burst_retains_failed_latency_and_rejects_transport_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "summary.json").write_text(
                json.dumps(
                    {
                        "client_schedule_valid": True,
                        "completed": 1,
                        "candidate_pass": False,
                    }
                )
            )
            rows = [
                {"outcome": "completed"},
                {
                    "outcome": "http_error",
                    "status": 429,
                    "error_body": "TTFT budget",
                },
            ]

            def save():
                (directory / "requests.jsonl").write_text("\n".join(map(json.dumps, rows)))

            save()
            burst = self.recipe["phases"][2]
            result = dev_cycle.phase_report(directory, burst)
            self.assertTrue(result["phase_pass"])
            self.assertFalse(result["candidate_pass"])
            self.assertFalse(
                dev_cycle.phase_report(directory, self.recipe["phases"][0])["phase_pass"]
            )
            rows[1] = {"outcome": "transport_error"}
            save()
            self.assertFalse(dev_cycle.phase_report(directory, burst)["phase_pass"])

    def test_command_saves_three_phases_and_checks_config_before_traffic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = reference()
            fleet = {
                "schema": "narwhal.fleet",
                "schema_version": 2,
                "model": spec["model"]["served_name"],
                "controller": copy.deepcopy(spec["controller"]),
                "slo": spec["slo"],
                "engines": [
                    {
                        "iid": f"n{i}",
                        "url": f"http://127.0.0.1:{18100 + i}",
                        "role": "prefill" if i < 3 else "decode",
                    }
                    for i in range(1, 5)
                ],
            }
            instance = {
                "schema": "narwhal.dev-instance",
                "schema_version": 1,
                "python_executable": sys.executable,
                "engine_count": 4,
                "router_url": "http://test",
            }
            for name, value in (("template", spec), ("fleet", fleet), ("instance", instance)):
                (root / f"{name}.json").write_text(json.dumps(value))
            seen = []

            async def run_trial(client, base, args):
                workload = json.loads(args.workload.read_text())
                self.assertEqual(args.run_seed, 0)
                seen.append((workload["input_tokens"], workload["seed"], args.requests))
                (args.out / "summary.json").write_text(
                    json.dumps(
                        {
                            "client_schedule_valid": True,
                            "completed": args.requests,
                            "candidate_pass": len(seen) < 3,
                        }
                    )
                )
                (args.out / "requests.jsonl").write_text('{"outcome":"completed"}\n')
                return 0 if len(seen) < 3 else 2

            with (
                redirect_stdout(io.StringIO()),
                patch.object(
                    dev_cycle.lifecycle,
                    "status",
                    return_value={"status": "ready", "run": str(root)},
                ),
                patch.object(
                    dev_cycle.trial,
                    "drain",
                    new=AsyncMock(side_effect=[self.before, self.before, self.after]),
                ),
                patch.object(dev_cycle.trial, "run_trial", side_effect=run_trial) as run,
                patch.object(dev_cycle.asyncio, "sleep", new=AsyncMock()),
            ):
                out = root / "results"
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--out", str(out)]), 0)
                self.assertEqual(seen, [(256, 1731, 24), (3840, 1730, 35), (3840, 1730, 12)])
                result = json.loads((out / "summary.json").read_text())
                self.assertTrue(result["passed"])
                self.assertFalse(result["phases"][2]["candidate_pass"])
                self.assertLessEqual(result["grafana_range"]["from"], result["grafana_range"]["to"])
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--dry-run"]), 0)
                run.assert_awaited()
            fleet["controller"]["reactive"]["step_s"] = 5
            (root / "fleet.json").write_text(json.dumps(fleet))
            with self.assertRaisesRegex(ValueError, "controller differs"):
                dev_cycle.check_config(root / "fleet.json", spec)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.spec = _read_template(FOUR_SGLANG)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "profiles.json"
        # Each split prices its own costs: prefill slows as engines join the prefill pool.
        self.store = colocated_store(self.path, contention=0.25)
        self.phases = {
            phase["name"]: phase
            for phase in dev_cycle.sized_recipe(self.spec, effective(self.path))["phases"]
        }

    def rows(self, split):
        return [
            p
            for p in self.store.all_profiles()
            if (p.colocated_prefill_engines, p.colocated_decode_engines) == split
        ]

    def test_sglang_template_matches_the_reference_cycle_and_controller(self):
        recipe = self.spec["role_cycle"]
        self.assertEqual(self.spec["runtime"]["backend"], "sglang")
        self.assertEqual(self.spec["allocation"]["engine_count"], 4)
        self.assertIn("neighbour_prefill_rps", self.spec["profile"])
        for key in ("controller", "slo"):
            self.assertEqual(self.spec[key], reference()[key])
        self.assertEqual(recipe["expected_splits"], reference()["role_cycle"]["expected_splits"])
        self.assertEqual(
            [phase["name"] for phase in recipe["phases"]],
            [phase["name"] for phase in reference()["role_cycle"]["phases"]],
        )
        for phase in recipe["phases"]:
            dev_cycle.check_target(phase)

    def test_each_phase_takes_the_prices_of_the_split_it_starts_from(self):
        tpot_s = self.spec["slo"]["tpot_s"]
        decode = self.phases["decode"]
        capacity = statistics.fmean(p.decode_rps(tpot_s, 256 + 64, 128) for p in self.rows((2, 2)))
        self.assertEqual(decode["sizing"]["split"], [2, 2])
        self.assertAlmostEqual(decode["rate_rps"], 1.8 * capacity)
        steady = self.phases["prefill-steady"]
        prefill_s = statistics.fmean(p.prefill_time(3840) for p in self.rows((1, 3)))
        self.assertEqual(steady["sizing"]["split"], [1, 3])
        self.assertAlmostEqual(steady["rate_rps"], 0.22 / prefill_s)
        burst = self.phases["prefill-burst"]
        prefill_s = statistics.fmean(p.prefill_time(3840) for p in self.rows((2, 2)))
        self.assertEqual(burst["sizing"]["split"], [2, 2])
        self.assertEqual(burst["requests"], math.ceil(3.0 / prefill_s))
        self.assertAlmostEqual(burst["sizing"]["prefill_backlog_s"], burst["requests"] * prefill_s)
        self.assertEqual(burst["max_inflight"], burst["requests"])

    def test_steady_prefill_band_follows_the_controller_settings(self):
        path = self.directory / "uniform.json"
        store = colocated_store(path)
        steady = self.spec["role_cycle"]["phases"][1]
        reactive = self.spec["controller"]["reactive"]
        utilization, margin = reactive["utilization"], reactive["movement_margin"]
        low, high = dev_cycle.target_band(
            steady,
            (1, 3),
            (2, 2),
            lambda split: dev_cycle.split_profiles(store, ROLES, split),
            self.spec,
        )
        # A third prefill engine lowers the prefill load P / (2u) by P / (6u).
        self.assertAlmostEqual(high, 6 * utilization * margin)
        prefill_s = small_gpu_profile("n1").prefill_time(3840)
        decode_s = 1 / small_gpu_profile("n1").decode_rps(self.spec["slo"]["tpot_s"], 3840.5, 1)
        self.assertAlmostEqual(low, reactive["demand_floor"] * prefill_s / (prefill_s + decode_s))
        self.assertGreater(low, 2 * utilization * margin)
        for value in (0.9, 0.1):
            spec = copy.deepcopy(self.spec)
            spec["role_cycle"]["phases"][1]["target"] = {"prefill_engines": value}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "lies outside"):
                dev_cycle.sized_recipe(spec, effective(path))

    def test_burst_stays_within_the_router_in_flight_limit(self):
        prefill_s = self.phases["prefill-burst"]["sizing"]["prefill_s"]
        self.assertGreater(self.phases["prefill-burst"]["requests"], 30)
        burst = dev_cycle.sized_recipe(self.spec, effective(self.path, 28, 2))["phases"][2]
        self.assertEqual((burst["requests"], burst["max_inflight"]), (30, 30))
        self.assertAlmostEqual(burst["sizing"]["prefill_backlog_s"], 30 * prefill_s)
        with self.assertRaisesRegex(ValueError, "within the 1 s TTFT budget"):
            dev_cycle.sized_recipe(self.spec, effective(self.path, 18, 2))

    def test_phases_draw_distinct_prompts(self):
        # The trial's warm-up request takes the seed after the phase's last request.
        ranges = sorted((p["seed"], p["seed"] + p["requests"] + 1) for p in self.phases.values())
        for first, second in itertools.pairwise(ranges):
            self.assertLessEqual(first[1], second[0])
        spec = copy.deepcopy(self.spec)
        phases = spec["role_cycle"]["phases"]
        phases[2]["seed"] = phases[1]["seed"] + self.phases["prefill-steady"]["requests"]
        with self.assertRaisesRegex(ValueError, "prefill-steady and prefill-burst repeat prompts"):
            dev_cycle.sized_recipe(spec, effective(self.path))

    def test_targets_scale_with_the_profiles(self):
        path = self.directory / "faster.json"
        colocated_store(path, prefill=0.25, decode=0.25, contention=0.25)
        quick = {
            phase["name"]: phase
            for phase in dev_cycle.sized_recipe(self.spec, effective(path))["phases"]
        }
        for name, key in (("decode", "decode_engines"), ("prefill-steady", "prefill_engines")):
            with self.subTest(name):
                self.assertGreater(quick[name]["rate_rps"], self.phases[name]["rate_rps"])
                self.assertAlmostEqual(quick[name]["sizing"][key], self.phases[name]["sizing"][key])

    def test_fixed_phases_stay_unchanged_without_reading_profiles(self):
        spec = reference()
        missing = Path(tempfile.gettempdir()) / "narwhal-missing-profiles.json"
        self.assertIs(dev_cycle.sized_recipe(spec, effective(missing)), spec["role_cycle"])

    def test_rejects_invalid_targets_and_light_phases(self):
        base = self.spec["role_cycle"]["phases"][0]
        for change, message in (
            ({"target": {"decode_engines": 1, "prefill_engines": 1}}, "names one of"),
            ({"target": {"engines": 1}}, "names one of"),
            ({"target": {"decode_engines": 0}}, "positive number"),
            ({"target": {"decode_engines": True}}, "positive number"),
            ({"duration_s": 0}, "positive duration_s"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                dev_cycle.check_target({**base, **change})
        with self.assertRaisesRegex(ValueError, "requires a positive rate_rps"):
            dev_cycle.check_target({**self.spec["role_cycle"]["phases"][2], "rate_rps": 0})
        light = {**base, "target": {"decode_engines": 0.001}}
        with self.assertRaisesRegex(ValueError, "per 30 s demand window"):
            dev_cycle.sized(light, (2, 2), self.rows((2, 2)), self.spec, 768)
        missing = self.directory / "missing.json"
        with self.assertRaisesRegex(ValueError, "price no 2P:2D split"):
            dev_cycle.sized_recipe(self.spec, effective(missing))

    def test_command_sizes_phases_from_the_run_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self.spec
            colocated_store(root / "profiles.json", contention=0.25)
            fleet = {
                "schema": "narwhal.fleet",
                "schema_version": 2,
                "model": spec["model"]["served_name"],
                "controller": copy.deepcopy(spec["controller"]),
                "slo": spec["slo"],
                "engines": [
                    {
                        "iid": iid,
                        "url": f"http://127.0.0.1:{18100 + index}",
                        "role": role.value,
                    }
                    for index, (iid, role) in enumerate(ROLES.items())
                ],
                "profiles": {"path": str(root / "profiles.json")},
            }
            instance = {
                "schema": "narwhal.dev-instance",
                "schema_version": 1,
                "python_executable": sys.executable,
                "engine_count": 4,
                "router_url": "http://test",
            }
            for name, value in (("template", spec), ("fleet", fleet), ("instance", instance)):
                (root / f"{name}.json").write_text(json.dumps(value))
            before = {
                "pools": {"prefill": ["n1", "n2"], "decode": ["n3", "n4"]},
                "flips": [],
                "control": {"advisory": False},
                "journal_run": "router-1",
                "lifecycle": {"process_starts": {"n1": 1, "n2": 2, "n3": 3, "n4": 4}},
            }
            after = copy.deepcopy(before)
            after["flips"] = [
                {"iid": iid, "to": role, "by": "reactive", "at": index}
                for index, (iid, role) in enumerate(
                    [("n2", "decode"), ("n2", "prefill"), ("n3", "prefill"), ("n3", "decode")]
                )
            ]
            offered = []

            async def run_trial(client, base, args):
                offered.append((args.rate, args.requests, args.max_inflight))
                (args.out / "summary.json").write_text(
                    json.dumps(
                        {"client_schedule_valid": True, "completed": 1, "candidate_pass": True}
                    )
                )
                (args.out / "requests.jsonl").write_text('{"outcome":"completed"}\n')

            with (
                redirect_stdout(io.StringIO()),
                patch.object(
                    dev_cycle.lifecycle,
                    "status",
                    return_value={"status": "ready", "run": str(root)},
                ),
                patch.object(
                    dev_cycle.trial, "drain", new=AsyncMock(side_effect=[before, before, after])
                ),
                patch.object(dev_cycle.trial, "run_trial", side_effect=run_trial),
                patch.object(dev_cycle.asyncio, "sleep", new=AsyncMock()),
            ):
                out = root / "results"
                self.assertEqual(dev_cycle.main(["--instance", str(root), "--out", str(out)]), 0)
                expected = [
                    tuple(
                        self.phases[name][key] for key in ("rate_rps", "requests", "max_inflight")
                    )
                    for name in ("decode", "prefill-steady", "prefill-burst")
                ]
                self.assertEqual(offered, expected)
                saved = json.loads((out / "phases.json").read_text())
                self.assertEqual([phase["requests"] for phase in saved], [e[1] for e in expected])
                self.assertEqual(
                    [phase["sizing"]["split"] for phase in saved], [[2, 2], [1, 3], [2, 2]]
                )
                self.assertEqual(json.loads((out / "template.json").read_text()), spec)
                summary = json.loads((out / "summary.json").read_text())
                self.assertTrue(summary["passed"])
                self.assertAlmostEqual(summary["phases"][0]["sizing"]["decode_engines"], 1.8)
                # A store that predicts no prefill time stops the replay before traffic.
                colocated_store(root / "profiles.json", prefill=0.0)
                errors = io.StringIO()
                with redirect_stderr(errors):
                    zero = root / "zero"
                    self.assertEqual(
                        dev_cycle.main(["--instance", str(root), "--out", str(zero)]), 1
                    )
                self.assertIn(
                    "phase decode: the profiles predict no prefill time", errors.getvalue()
                )
                self.assertFalse(zero.exists())


# Arguments the router passes from the template's reactive settings.
REACTIVE_ARGS = (
    "window_s",
    "confirmations",
    "utilization",
    "min_arrivals",
    "demand_floor",
    "movement_margin",
    "step_s",
    "evidence_span_s",
    "evidence_max_span_s",
    "evidence_min_arrivals",
    "demand_rise_tolerance",
)


class CycleSim:
    """Serve sized phases through the real reactive controller on a fake clock.

    Each prefill engine serves its requests in arrival order at its profile's prefill time, and
    a request decodes at its decode engine's token interval for the batch it joins. Engines
    price and serve at the profiles measured for the live split. Observed pool load reads zero,
    as in tests/scheduling/test_departure.py.
    """

    def __init__(self, store, spec):
        self.now = 0.0
        clock = lambda: self.now
        controller, reactive = spec["controller"], spec["controller"]["reactive"]
        self.monitor = InstanceMonitor(clock=clock, profiles=store)
        for iid, role in ROLES.items():
            self.monitor.add(Instance(iid, f"http://{iid}", role))
        roles = self.monitor.instances
        store.bind_role_mix(
            dict.fromkeys(ROLES, "gpu-0"),
            lambda group: tuple(
                sum(inst.role is role for inst in roles.values())
                for role in (Role.PREFILL, Role.DECODE)
            ),
            lambda iid: roles[iid].role,
        )
        self.events = []
        self.scheduler = GlobalScheduler(
            self.monitor,
            store,
            SLO(spec["slo"]["ttft_s"], spec["slo"]["tpot_s"]),
            Thresholds(**controller["thresholds"]),
            clock=clock,
            min_prefill=controller["min_prefill"],
            min_decode=controller["min_decode"],
            on_control_event=self.events.append,
        )
        self.scheduler.switch_hold_s = 3 * reactive["step_s"]
        self.controller = ReactiveController(
            self.monitor,
            self.scheduler,
            clock=clock,
            **{key: reactive[key] for key in REACTIVE_ARGS},
        )
        # The replay clears the demand window before its first phase.
        self.start = reactive["window_s"] + reactive["step_s"]
        self.step_s = reactive["step_s"]
        self.next_step = 0.0
        self.free_at = dict.fromkeys(ROLES, 0.0)
        self.queue = []
        self.order = itertools.count()

    def advance(self, until):
        while True:
            done = self.queue[0][0] if self.queue else math.inf
            if min(done, self.next_step) > until:
                break
            if done <= self.next_step:
                self.complete()
            else:
                self.now = self.next_step
                self.controller.sample()
                self.controller.step()
                self.next_step += self.step_s
        self.now = until

    def arrive(self, at, input_len, output_len):
        self.advance(at)
        self.controller.saw_arrival(input_len, wanted_len=output_len, at=at)
        engine = min(
            self.scheduler.live_instances(Role.PREFILL),
            key=lambda inst: (max(self.free_at[inst.iid], at), inst.iid),
        )
        done = max(self.free_at[engine.iid], at)
        done += self.scheduler.profiles.get(engine.iid).prefill_time(input_len)
        self.free_at[engine.iid] = done
        order = next(self.order)
        request = Request(f"r{order}", input_len, wanted_len=output_len, arrived_at=at)
        self.monitor.dispatched(engine.iid, request)
        heapq.heappush(self.queue, (done, order, engine.iid, request))

    def complete(self):
        at, order, iid, request = heapq.heappop(self.queue)
        self.now = at
        self.monitor.finished(iid, request.rid)
        if request.phase is Phase.PREFILL:
            request.phase = Phase.DECODE
            engine = min(
                self.scheduler.live_instances(Role.DECODE),
                key=lambda inst: (len(inst.decode), inst.iid),
            )
            self.monitor.dispatched(engine.iid, request)
            batch = len(engine.decode)
            interval = self.scheduler.profiles.get(engine.iid).token_interval(
                batch * (request.input_len + request.wanted_len / 2), batch
            )
            done = at + (request.wanted_len - 1) * interval
            heapq.heappush(self.queue, (done, order, engine.iid, request))

    def run(self, phases, gap_s, idle_s):
        """Offer each phase once the previous one drains; return when each phase starts."""
        starts = {}
        at = self.start
        for phase in phases:
            starts[phase["name"]] = at
            for index in range(phase["requests"]):
                self.arrive(
                    at + index / phase["rate_rps"], phase["input_tokens"], phase["output_tokens"]
                )
            while self.queue:
                self.advance(self.queue[0][0])
            at = self.now + gap_s
        self.advance(at + idle_s)
        return starts

    def moves(self):
        return [
            (event["current_prefill"], event["prefill"], event["at"])
            for event in self.events
            if event["event"] == "controller_decision" and event.get("result") == "applied"
        ]


class RoleCycleControllerTests(unittest.TestCase):
    """The sized SGLang phases move the real controller through the expected splits."""

    def test_sized_phases_drive_the_controller_through_the_cycle(self):
        spec = _read_template(FOUR_SGLANG)
        splits = spec["role_cycle"]["expected_splits"]
        expected = [(a[0], b[0]) for a, b in itertools.pairwise(splits[:4])]
        # Prefill and decode cost scales, and the share each same-role neighbour adds.
        for prefill, decode, contention in (
            (1.0, 1.0, 0.0),
            (0.5, 2.0, 0.0),
            (2.0, 0.5, 0.0),
            (0.5, 0.5, 0.25),
            (2.0, 2.0, 0.25),
        ):
            with (
                self.subTest(prefill=prefill, decode=decode, contention=contention),
                tempfile.TemporaryDirectory() as temporary,
            ):
                path = Path(temporary) / "profiles.json"
                colocated_store(path, prefill, decode, contention)
                phases = dev_cycle.sized_recipe(spec, effective(path))["phases"]
                sim = CycleSim(ProfileStore(path), spec)
                with patch.object(sim.scheduler, "pool_load", return_value=0.0):
                    starts = sim.run(phases, gap_s=1.0, idle_s=10.0)
                moves = sim.moves()
                self.assertEqual([move[:2] for move in moves[:3]], expected)
                # Decode load moves an engine to decode, and prefill-steady load moves it back
                # and holds 2P:2D until the burst moves a third engine to prefill.
                self.assertLess(moves[0][2], starts["prefill-steady"])
                self.assertGreaterEqual(moves[1][2], starts["prefill-steady"])
                self.assertLess(moves[1][2], starts["prefill-burst"])
                self.assertGreaterEqual(moves[2][2], starts["prefill-burst"])


if __name__ == "__main__":
    unittest.main()
