"""Skip, rerun, interruption, tampering and locking of the stage runner on the deployment graph."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from narwhal.contracts import ContractVersionError, manifest
from narwhal.deployment import run_record, runner
from narwhal.deployment.deploy_graph import DEPLOY_GRAPH
from narwhal.deployment.graph import INPUT, LIVE, PRODUCT
from narwhal.deployment.hosts import RemoteTimeout
from narwhal.deployment.run_record import key
from narwhal.deployment.runner import (
    Command,
    Input,
    Outcome,
    RunCancelled,
    RunLocked,
    Runner,
    StageFailed,
    StaticTopology,
)
from tests.deployment import runner_harness
from tests.fixtures import ROOT

TOPOLOGY = StaticTopology(
    scopes={
        "H": ("node-1", "node-2", "router-host"),
        "E": ("engine-1", "engine-2"),
        "G": ("group-1", "group-2"),
        "X": ("node-1-to-node-2", "node-2-to-node-1"),
        "R": ("router-host",),
    },
    relations={
        "group": {"engine-1": ("group-1",), "engine-2": ("group-2",)},
        "group_edges": {"engine-1": ("node-1-to-node-2",), "engine-2": ("node-2-to-node-1",)},
    },
    # engine-1's backend captures a cache layout; engine-2's handoffs are measured.
    capabilities={"cache_capture": ("engine-1",), "transfer_measurement": ("engine-2",)},
)

ORDER = (
    "access",
    "discover",
    "package",
    "install",
    "engine-inspect",
    "engine-prepare",
    "engine-check",
    "engine-start",
    "engine-probe",
    "cache-capture",
    "transfer-measure",
    "cache-groups",
    "fabric-budget",
    "fabric-edge",
    "fleet-inventory",
    "attest-capture",
    "sidecar-start",
    "sidecar-verify",
    "finalize-fleet",
    "profile",
    "calibrate",
    "fleet-limits",
    "preflight",
    "lease-check",
    "router-start",
    "load-balancer",
    "monitoring",
    "tunnel",
    "trial",
)
SERVING = ("preflight", "router-start", "trial")

# The rows of the task 1 record, written out. "same" is the changed fact's instance, "others"
# every other instance of the stage, a relation name its related instances, and "all" every
# instance.
ROWS = {
    "access": {"all": ("access",)},
    "destinations": {"all": ("access", *ORDER[2:])},
    "discovery": {"all": ORDER[1:]},
    "revision": {"all": ORDER[2:]},
    "slo": {"all": ("fleet-limits", *SERVING)},
    "deadline_margin": {"all": ("fleet-limits", *SERVING)},
    "profiling": {"all": ("profile", *SERVING)},
    "calibration": {"all": ("calibrate", "fleet-limits", *SERVING)},
    "fabric_workload": {"all": ("transfer-measure", "fabric-budget", "fabric-edge")},
    "fabric_test": {"all": ("fabric-edge",)},
    "router": {"all": ("lease-check", "router-start", "load-balancer", "monitoring", "trial")},
    "monitoring": {"all": ("monitoring",)},
    "trial": {"all": ("trial",)},
    "launch": {"same": ("engine-check", "engine-start")},
    "engine_identity": {
        "same": (
            "engine-probe",
            "cache-capture",
            "transfer-measure",
            "attest-capture",
            "sidecar-start",
            "sidecar-verify",
        ),
        "all": ("finalize-fleet", "preflight", "router-start"),
    },
    "cache_geometry": {
        "all": ("cache-groups",),
        "group": ("fabric-budget",),
        "group_edges": ("fabric-edge",),
    },
    "handoff_geometry": {
        "all": ("cache-groups",),
        "group": ("fabric-budget",),
        "group_edges": ("fabric-edge",),
    },
    "link": {
        "same": ("fabric-edge",),
        "all": ("calibrate", "fleet-limits", *SERVING),
    },
    "binding": {"all": ("profile", "calibrate", "fleet-limits", *SERVING)},
    "engine_contract": {
        "others": ("attest-capture",),
        "all": (
            "finalize-fleet",
            "profile",
            "calibrate",
            "fleet-limits",
            "preflight",
            "router-start",
        ),
    },
    "sidecar_identity": {"same": ("sidecar-start", "sidecar-verify"), "all": ("finalize-fleet",)},
    "router_identity": {"same": ("router-start",)},
    "load_balancer_identity": {"all": ("load-balancer",)},
    "monitoring_identity": {"all": ("monitoring",)},
}


def facts(kind):
    return [name for name, fact in DEPLOY_GRAPH.facts.items() if fact.kind == kind]


def instances(stage):
    spec = DEPLOY_GRAPH.stages[stage]
    if DEPLOY_GRAPH.scopes[spec.scope].single:
        return ("",)
    names = TOPOLOGY.instances(spec.scope)
    if spec.capability is not None:
        names = tuple(name for name in names if TOPOLOGY.capable(spec.capability, name))
    return names


def scope_instances(scope):
    """A scope's instances, the one unnamed instance for a single scope."""
    return ("",) if DEPLOY_GRAPH.scopes[scope].single else TOPOLOGY.instances(scope)


def expected(fact, instance=""):
    """The stage instances a change of one fact instance reruns, from the written-out rows."""
    keys = set()
    for how, names in ROWS[fact].items():
        for stage in names:
            available = instances(stage)
            if how == "same":
                chosen = tuple(name for name in available if name == instance)
            elif how == "others":
                chosen = tuple(name for name in available if name != instance)
            elif how == "all":
                chosen = available
            else:
                chosen = tuple(TOPOLOGY.related(how, instance))
            keys |= {key(stage, name) for name in chosen}
    return keys


def ran(result):
    return {decision.key for decision in result.decisions if decision.ran}


def runs(result):
    """How many times each stage instance ran."""
    return Counter(decision.key for decision in result.decisions if decision.ran)


PROCESSES = (
    "launch",
    "engine_identity",
    "sidecar_identity",
    "router_identity",
    "load_balancer_identity",
    "monitoring_identity",
)


class Fleet:
    """Stub stage bodies: each attempt writes its evidence and returns the fleet's values."""

    def __init__(self):
        self.inputs = {name: Input.value(name, "test") for name in facts(INPUT)}
        self.products = {}
        self.live = {}
        self.started = 0
        self.interrupt = None
        self.cleaned = []
        # Live facts whose stage starts a new process, with a new identity, on every rerun.
        self.fresh = ()

    def action(self, stage):
        spec = DEPLOY_GRAPH.stages[stage]

        def run(context):
            (context.directory / "evidence.json").write_text(
                json.dumps({"stage": stage, "instance": context.instance})
            )
            if self.interrupt is not None and self.interrupt[:2] == (stage, context.instance):
                try:
                    signal.raise_signal(self.interrupt[2])
                    time.sleep(5)
                finally:
                    (context.directory / "cleanup.json").write_text("{}")
                    self.cleaned.append(key(stage, context.instance))
            products = {}
            for fact in spec.produces:
                name = (fact, context.instance)
                if DEPLOY_GRAPH.facts[fact].kind == LIVE:
                    if self.live.get(name) is None or fact in self.fresh:
                        self.started += 1
                        self.live[name] = {"started": self.started}
                    products[fact] = self.live[name]
                else:
                    products[fact] = self.products.setdefault(name, {"generation": 0})
            return Outcome(products=products)

        return run

    def probe(self, fact):
        return lambda instance, recorded: self.live.get((fact, instance))

    def runner(self, directory):
        return Runner(
            DEPLOY_GRAPH,
            directory,
            target="ssh",
            revision="test",
            actions={name: self.action(name) for name in DEPLOY_GRAPH.stages},
            topology=TOPOLOGY,
            probes={fact: self.probe(fact) for fact in facts(LIVE)},
        )

    def run(self, directory, **options):
        return self.runner(directory).run(self.inputs, **options)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"
        self.fleet = Fleet()

    def baseline(self):
        first = self.fleet.run(self.directory)
        everything = {key(stage, name) for stage in ORDER for name in instances(stage)}
        self.assertEqual(ran(first), everything)
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def record(self):
        return run_record.read(self.directory)

    def test_the_graph_holds_the_rows_the_tests_pin(self):
        self.assertEqual(DEPLOY_GRAPH.order(), ORDER)
        self.assertEqual(set(DEPLOY_GRAPH.facts), set(ROWS))

    def test_unchanged_run_skips_every_stage(self):
        self.baseline()
        record = self.record()
        self.assertIsNone(record["lock"])
        self.assertEqual(record["stale_locks"], [])
        self.assertTrue(all(entry["status"] == "passed" for entry in record["stages"].values()))
        self.assertNotIn("cache-capture[engine-2]", record["stages"])
        self.assertEqual(record["graph_sha256"], DEPLOY_GRAPH.digest())
        self.assertEqual(os.stat(self.directory / "run.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.directory).st_mode & 0o777, 0o700)
        engine = record["stages"]["engine-check[engine-2]"]
        self.assertEqual(engine["directory"], "stages/engine-check/engine-2/attempt-1")
        self.assertEqual(
            [output["path"] for output in engine["outputs"]],
            ["stages/engine-check/engine-2/attempt-1/evidence.json"],
        )

    def test_each_input_reruns_the_stages_its_row_names(self):
        self.baseline()
        for fact in facts(INPUT):
            with self.subTest(fact=fact):
                self.fleet.inputs[fact] = Input.value(fact + "-changed", "test")
                self.assertEqual(ran(self.fleet.run(self.directory)), expected(fact))
                self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_each_live_change_reruns_the_stages_its_row_names(self):
        self.baseline()
        for fact in facts(LIVE):
            for instance in scope_instances(DEPLOY_GRAPH.facts[fact].scope):
                with self.subTest(fact=fact, instance=instance):
                    self.fleet.live[(fact, instance)] = {"restarted": fact + instance}
                    self.assertEqual(ran(self.fleet.run(self.directory)), expected(fact, instance))
                    self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_a_restarted_process_that_passed_its_check_is_recorded_without_rerunning_its_stage(
        self,
    ):
        self.baseline()
        restarted = {"started": "again"}
        probes = self.fleet.probe

        def refreshing(fact):
            if fact != "sidecar_identity":
                return probes(fact)
            return lambda instance, recorded: (
                runner.Refreshed(restarted) if instance == "engine-1" else recorded
            )

        with patch.object(self.fleet, "probe", refreshing):
            self.assertEqual(
                ran(self.fleet.run(self.directory)),
                {"sidecar-verify[engine-1]", "finalize-fleet[router-host]"},
            )
        entry = self.record()["stages"]["sidecar-start[engine-1]"]
        self.assertEqual(entry["products"]["sidecar_identity"], restarted)
        self.fleet.live[("sidecar_identity", "engine-1")] = restarted
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_inspect_reports_each_live_fact_and_records_a_checked_restart(self):
        self.baseline()
        self.fleet.live[("router_identity", "router-host")] = None
        self.fleet.live[("link", "node-1-to-node-2")] = {"route": "changed"}
        build = self.fleet.runner(self.directory)
        sidecar = build.probes["sidecar_identity"]
        build.probes["sidecar_identity"] = lambda instance, recorded: (
            runner.Refreshed({"new": instance})
            if instance == "engine-2"
            else sidecar(instance, recorded)
        )
        _, checks = build.inspect(refresh=False)
        states = {check.key: check.state for check in checks}
        self.assertEqual(states["router_identity[router-host]"], "gone")
        self.assertEqual(states["link[node-1-to-node-2]"], "changed")
        self.assertEqual(states["sidecar_identity[engine-2]"], "restarted and checked")
        self.assertEqual(states["engine_identity[engine-1]"], "holds")
        unchanged = self.record()["stages"]["sidecar-start[engine-2]"]["products"]
        self.assertNotEqual(unchanged["sidecar_identity"], {"new": "engine-2"})
        build.inspect(refresh=True)
        refreshed = self.record()["stages"]["sidecar-start[engine-2]"]["products"]
        self.assertEqual(refreshed["sidecar_identity"], {"new": "engine-2"})

    def test_plan_decides_like_run_without_running_or_recording(self):
        build = self.fleet.runner(self.directory)
        decisions = {d.key: d.reasons for d in build.plan(self.fleet.inputs).decisions}
        self.assertEqual(decisions["access[node-1]"], ("no attempt yet",))
        self.assertEqual(decisions["discover"], ("no attempt yet",))
        self.assertFalse(self.directory.exists())
        self.baseline()
        before = (self.directory / "run.json").read_bytes()
        self.fleet.inputs["slo"] = Input.value("changed", "test")
        planned = self.fleet.runner(self.directory).plan(self.fleet.inputs)
        running = {d.key: d.reasons for d in planned.decisions if d.ran}
        self.assertEqual(running["fleet-limits"], ("slo changed",))
        self.assertEqual(running["preflight[router-host]"], ("slo changed",))
        self.assertEqual(set(running), expected("slo"))
        self.assertNotIn("calibrate", running)
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        self.assertEqual(
            {d.key for d in self.fleet.run(self.directory).decisions if d.ran},
            expected("slo"),
        )

    def assert_plan_matches_run(self, probes=None):
        """Plan names every stage instance the next run runs, and only a product that may
        change makes it name more."""
        build = self.fleet.runner(self.directory)
        if probes:
            build.probes.update(probes)
        planned = {d.key: d.reasons for d in build.plan(self.fleet.inputs).decisions if d.ran}
        build = self.fleet.runner(self.directory)
        if probes:
            build.probes.update(probes)
        ran_now = {d.key for d in build.run(self.fleet.inputs).decisions if d.ran}
        self.assertLessEqual(ran_now, set(planned))
        for name in set(planned) - ran_now:
            self.assertRegex(planned[name][0], r"^runs after \S+ if \S+ changes$", name)
        return planned, ran_now

    def test_plan_names_what_run_runs_after_each_change(self):
        self.baseline()
        # Each rerun of a stage that starts a process starts a new one, as on a host.
        self.fleet.fresh = PROCESSES
        for fact in facts(INPUT):
            with self.subTest(input=fact):
                self.fleet.inputs[fact] = Input.value(fact + "-planned", "test")
                self.assert_plan_matches_run()
        for fact in facts(LIVE):
            for instance in scope_instances(DEPLOY_GRAPH.facts[fact].scope):
                for value in ({"planned": fact + instance}, None):
                    with self.subTest(fact=fact, instance=instance, gone=value is None):
                        self.fleet.live[(fact, instance)] = value
                        self.assert_plan_matches_run()

    def test_plan_reruns_the_readers_of_a_process_a_rerun_replaces(self):
        self.baseline()
        # The launch directory is gone while the engine still answers with its old identity.
        # The rerun of engine-start starts a new process, so its readers run after it.
        self.fleet.fresh = PROCESSES
        self.fleet.live[("launch", "engine-1")] = None
        planned, _ = self.assert_plan_matches_run()
        self.assertEqual(
            planned["engine-probe[engine-1]"],
            ("runs after engine-start[engine-1] if engine_identity[engine-1] changes",),
        )
        self.assertNotIn("engine-probe[engine-2]", planned)

    def test_plan_skips_the_stage_of_a_checked_restart_as_run_does(self):
        self.baseline()
        for fact, instance in (
            ("sidecar_identity", "engine-1"),
            ("router_identity", "router-host"),
        ):
            with self.subTest(fact=fact):
                restarted = {"restarted": fact}

                def refreshed(probed, recorded, fact=fact, instance=instance, value=restarted):
                    if probed == instance and recorded != value:
                        return runner.Refreshed(value)
                    return self.fleet.live.get((fact, probed))

                planned, _ = self.assert_plan_matches_run({fact: refreshed})
                self.assertNotIn(key(DEPLOY_GRAPH.producer(fact), instance), planned)
                self.fleet.live[(fact, instance)] = restarted

    def test_plan_invalidates_only_the_instances_run_would(self):
        self.baseline()
        entry = self.record()["stages"]["engine-check[engine-1]"]
        (self.directory / entry["outputs"][0]["path"]).write_text("{}")
        planned, _ = self.assert_plan_matches_run()
        self.assertIn("engine-start[engine-1]", planned)
        self.assertNotIn("engine-start[engine-2]", planned)

    def test_a_live_process_that_is_gone_reruns_the_stage_that_started_it(self):
        self.baseline()
        for fact in facts(LIVE):
            producer = DEPLOY_GRAPH.producer(fact)
            instance = scope_instances(DEPLOY_GRAPH.facts[fact].scope)[0]
            with self.subTest(fact=fact):
                self.fleet.live[(fact, instance)] = None
                result = self.fleet.run(self.directory)
                self.assertEqual(ran(result), {key(producer, instance)} | expected(fact, instance))
                decision = next(d for d in result.decisions if d.key == key(producer, instance))
                self.assertIn(f"{key(fact, instance)} is gone", decision.reasons)
                self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_each_product_change_reruns_the_stages_its_row_names(self):
        self.baseline()
        for fact in facts(PRODUCT):
            producer = DEPLOY_GRAPH.producer(fact)
            engine = instances(producer)[0]
            with self.subTest(fact=fact, engine=engine):
                # An engine restart reruns the producer, which then reports a changed value.
                restart = expected("engine_identity", engine)
                self.assertIn(key(producer, engine), restart)
                self.fleet.live[("engine_identity", engine)] = {"restarted": fact}
                self.fleet.products[(fact, engine)] = {"generation": fact}
                result = self.fleet.run(self.directory)
                self.assertEqual(ran(result), restart | expected(fact, engine))
                self.assertEqual(max(runs(result).values()), 1)
                self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_a_later_engines_contract_change_reattests_the_engines_before_it(self):
        self.baseline()
        self.fleet.live[("engine_identity", "engine-2")] = {"restarted": True}
        self.fleet.products[("engine_contract", "engine-2")] = {"generation": 1}
        result = self.fleet.run(self.directory)
        self.assertEqual(
            ran(result),
            expected("engine_identity", "engine-2") | expected("engine_contract", "engine-2"),
        )
        self.assertIn("attest-capture[engine-1]", ran(result))
        decision = [d for d in result.decisions if d.key == "attest-capture[engine-1]"][-1]
        self.assertEqual(decision.reasons, ("engine_contract[engine-2] changed",))
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_an_upgrade_of_every_engine_attests_each_once(self):
        self.baseline()
        for engine in TOPOLOGY.instances("E"):
            self.fleet.live[("engine_identity", engine)] = {"restarted": engine}
            self.fleet.products[("engine_contract", engine)] = {"generation": 1}
        result = self.fleet.run(self.directory)
        counts = runs(result)
        self.assertEqual(counts["attest-capture[engine-1]"], 1)
        self.assertEqual(counts["attest-capture[engine-2]"], 1)
        self.assertEqual(max(counts.values()), 1)
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_an_interrupted_reattestation_resumes_on_the_next_run(self):
        self.baseline()
        self.fleet.live[("engine_identity", "engine-2")] = {"restarted": True}
        self.fleet.products[("engine_contract", "engine-2")] = {"generation": 1}
        self.fleet.interrupt = ("attest-capture", "engine-1", signal.SIGTERM)
        with self.assertRaises(RunCancelled):
            self.fleet.run(self.directory)
        self.fleet.interrupt = None
        result = self.fleet.run(self.directory)
        self.assertIn("attest-capture[engine-1]", ran(result))
        self.assertNotIn("attest-capture[engine-2]", ran(result))
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_engine_restart_with_unchanged_binding_repeats_only_its_row(self):
        self.baseline()
        self.fleet.live[("engine_identity", "engine-2")] = {"restarted": True}
        self.assertEqual(
            ran(self.fleet.run(self.directory)),
            {
                "engine-probe[engine-2]",
                "transfer-measure[engine-2]",
                "attest-capture[engine-2]",
                "sidecar-start[engine-2]",
                "sidecar-verify[engine-2]",
                "finalize-fleet[router-host]",
                "preflight[router-host]",
                "router-start[router-host]",
            },
        )

    def tampered(self):
        self.baseline()
        evidence = self.directory / "stages/engine-check/engine-1/attempt-1/evidence.json"
        evidence.write_text("{}")
        later = set()
        for stage in DEPLOY_GRAPH.downstream("engine-check"):
            same = DEPLOY_GRAPH.stages[stage].scope == "E"
            names = tuple(n for n in instances(stage) if not same or n == "engine-1")
            later |= {key(stage, name) for name in names}
        return evidence, later

    def test_a_tampered_output_reruns_its_stage_and_every_stage_after_it(self):
        evidence, later = self.tampered()
        result = self.fleet.run(self.directory)
        self.assertEqual(ran(result), {"engine-check[engine-1]"} | later)
        decision = next(d for d in result.decisions if d.key == "engine-check[engine-1]")
        self.assertEqual(
            decision.reasons,
            ("output stages/engine-check/engine-1/attempt-1/evidence.json changed",),
        )
        self.assertEqual(
            self.record()["stages"]["engine-check[engine-1]"]["directory"],
            "stages/engine-check/engine-1/attempt-2",
        )
        self.assertEqual(evidence.read_text(), "{}")
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_a_tampered_output_reruns_its_dependants_after_a_stop(self):
        _, later = self.tampered()
        self.assertEqual(
            ran(self.fleet.run(self.directory, until="engine-check")), {"engine-check[engine-1]"}
        )
        record = self.record()
        invalidated = {
            name for name, entry in record["stages"].items() if entry["status"] == "invalidated"
        }
        self.assertEqual(invalidated, later)
        self.assertEqual(
            record["stages"]["trial"]["invalidation"], "output of engine-check[engine-1] changed"
        )
        self.assertEqual(ran(self.fleet.run(self.directory)), later)
        self.assertEqual(ran(self.fleet.run(self.directory)), set())

    def test_a_widened_output_mode_counts_as_a_changed_output(self):
        self.baseline()
        (self.directory / "stages/trial/attempt-1/evidence.json").chmod(0o644)
        self.assertEqual(ran(self.fleet.run(self.directory)), {"trial"})

    def test_a_changed_stage_definition_reruns_that_stage(self):
        self.baseline()
        record = self.record()
        record["stages"]["monitoring"]["definition"] = "sha256:" + "0" * 64
        run_record.write(self.directory, record)
        self.assertEqual(ran(self.fleet.run(self.directory)), {"monitoring"})

    def test_interruption_at_each_stage_records_cleanup_and_resumes_there(self):
        for index, stage in enumerate(ORDER):
            name = instances(stage)[0]
            sig = (signal.SIGINT, signal.SIGTERM)[index % 2]
            with self.subTest(stage=stage, signal=sig.name), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "run"
                fleet = Fleet()
                fleet.interrupt = (stage, name, sig)
                with self.assertRaises(RunCancelled) as caught:
                    fleet.run(directory)
                self.assertEqual(caught.exception.key, key(stage, name))
                self.assertEqual(fleet.cleaned, [key(stage, name)])
                record = run_record.read(directory)
                self.assertIsNone(record["lock"])
                entry = record["stages"][key(stage, name)]
                self.assertEqual(entry["status"], "cancelled")
                self.assertEqual(entry["failure"]["recovery"], DEPLOY_GRAPH.stages[stage].page)
                self.assertTrue((directory / entry["directory"] / "cleanup.json").exists())
                before = set(ORDER[:index])
                self.assertTrue(
                    all(
                        value["status"] == "passed"
                        for value in record["stages"].values()
                        if value["stage"] in before
                    )
                )
                fleet.interrupt = None
                result = fleet.run(directory)
                # The interrupted instance is the stage's first, so its whole stage and every
                # later stage run; every earlier stage is skipped.
                self.assertEqual(
                    ran(result),
                    {key(later, other) for later in ORDER[index:] for other in instances(later)},
                )
                decision = next(d for d in result.decisions if d.key == key(stage, name))
                self.assertEqual(decision.reasons, ("attempt 1 cancelled",))
                self.assertEqual(
                    run_record.read(directory)["stages"][key(stage, name)]["attempt"], 2
                )

    def signal_during_write(self, sig, when):
        """Raise `sig` inside the first record write that `when` selects; it is held until after."""
        write = run_record.write
        fired = []

        def interrupting(directory, body):
            if not fired and when(body):
                fired.append(True)
                signal.raise_signal(sig)
            write(directory, body)

        return patch.object(runner.run_record, "write", side_effect=interrupting)

    def test_a_signal_during_the_running_write_cancels_that_attempt(self):
        def running(body):
            entry = body["stages"].get("engine-check[engine-1]")
            return entry is not None and entry["status"] == "running"

        with self.signal_during_write(signal.SIGTERM, running), self.assertRaises(RunCancelled):
            self.fleet.run(self.directory)
        record = self.record()
        self.assertIsNone(record["lock"])
        entry = record["stages"]["engine-check[engine-1]"]
        self.assertEqual(entry["status"], "cancelled")
        result = self.fleet.run(self.directory)
        decision = next(d for d in result.decisions if d.key == "engine-check[engine-1]")
        self.assertEqual(decision.reasons, ("attempt 1 cancelled",))

    def test_a_signal_during_the_passed_write_keeps_the_pass(self):
        def passed(body):
            entry = body["stages"].get("engine-check[engine-1]")
            return entry is not None and entry["status"] == "passed"

        with self.signal_during_write(signal.SIGINT, passed), self.assertRaises(KeyboardInterrupt):
            self.fleet.run(self.directory)
        record = self.record()
        self.assertIsNone(record["lock"])
        self.assertEqual(record["stages"]["engine-check[engine-1]"]["status"], "passed")
        self.assertNotIn("engine-check[engine-2]", record["stages"])
        result = self.fleet.run(self.directory)
        self.assertNotIn("engine-check[engine-1]", ran(result))
        self.assertIn("engine-check[engine-2]", ran(result))

    def test_a_signal_during_the_opening_write_leaves_no_stale_lock(self):
        def opening(body):
            return body["lock"] is not None and not body["stages"]

        with (
            self.signal_during_write(signal.SIGTERM, opening),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.fleet.run(self.directory, until="access")
        self.assertIsNone(self.record()["lock"])
        result = self.fleet.run(self.directory, until="access")
        self.assertIsNone(result.stale_lock)
        self.assertEqual(self.record()["stale_locks"], [])

    def test_a_signal_during_the_closing_write_leaves_an_unlocked_record(self):
        with (
            self.signal_during_write(signal.SIGTERM, lambda body: body["lock"] is None),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.fleet.run(self.directory, until="access")
        self.assertIsNone(self.record()["lock"])
        self.assertEqual(ran(self.fleet.run(self.directory, until="access")), set())

    def test_processes_are_recorded_when_a_cancel_follows_the_action(self):
        started = {"pid": 4242, "boot_id": "boot", "start_ticks": 7}
        seen = []

        def start(context):
            seen.append(context.previous)
            return Outcome(processes=(started,))

        stub = self.fleet.runner(self.directory)
        stub.actions["access"] = start
        with (
            patch.object(runner._Run, "_outputs", side_effect=KeyboardInterrupt),
            self.assertRaises(RunCancelled),
        ):
            stub.run(self.fleet.inputs, until="access")
        entry = self.record()["stages"]["access[node-1]"]
        self.assertEqual((entry["status"], entry["processes"]), ("cancelled", [started]))
        stub.run(self.fleet.inputs, until="access")
        self.assertEqual(seen[1]["processes"], [started])
        self.assertEqual(self.record()["stages"]["access[node-1]"]["status"], "passed")

    def test_a_stage_failure_stops_the_run_and_names_the_recovery_section(self):
        def fail(context):
            raise ValueError("engine port 8000 is taken")

        stub = self.fleet.runner(self.directory)
        stub.actions["engine-inspect"] = fail
        with self.assertRaises(StageFailed) as caught:
            stub.run(self.fleet.inputs)
        self.assertEqual(caught.exception.key, "engine-inspect[engine-1]")
        self.assertIsNone(caught.exception.stale_lock)
        self.assertIn(
            "deploy/03-Validate-Engines.md#inspecting-every-engine-host", str(caught.exception)
        )
        record = self.record()
        self.assertEqual(record["stages"]["engine-inspect[engine-1]"]["status"], "failed")
        self.assertNotIn("engine-prepare[engine-1]", record["stages"])

    def test_until_stops_after_the_named_stage(self):
        result = self.fleet.run(self.directory, until="install")
        self.assertEqual(
            {decision.key.split("[")[0] for decision in result.decisions},
            {"access", "discover", "package", "install"},
        )
        self.assertEqual(self.record()["until"], "install")

    def test_inputs_and_actions_must_match_the_graph(self):
        stub = self.fleet.runner(self.directory)
        with self.assertRaisesRegex(ValueError, "missing slo"):
            stub.run({k: v for k, v in self.fleet.inputs.items() if k != "slo"})
        del stub.actions["trial"]
        with self.assertRaisesRegex(ValueError, "no action for stages: trial"):
            stub.run(self.fleet.inputs)
        self.assertEqual(
            stub.run(self.fleet.inputs, until="tunnel").run_id, self.record()["run_id"]
        )
        with self.assertRaisesRegex(ValueError, "probes must cover"):
            Runner(
                DEPLOY_GRAPH,
                self.directory,
                target="ssh",
                revision="test",
                actions={},
                topology=TOPOLOGY,
                probes={},
            )

    def test_another_target_is_refused(self):
        self.fleet.run(self.directory, until="access")
        stub = self.fleet.runner(self.directory)
        stub.target = "kubernetes"
        with self.assertRaisesRegex(ValueError, "holds a ssh run"):
            stub.run(self.fleet.inputs)

    def test_a_run_directory_other_users_can_enter_is_refused(self):
        self.fleet.run(self.directory, until="access")
        self.directory.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "mode 0750.*chmod 700"):
            self.fleet.run(self.directory, until="access")

    def test_a_temporary_record_left_by_a_crash_is_removed(self):
        self.fleet.run(self.directory, until="access")
        leftover = self.directory / f".run.json-{'a' * 32}"
        leftover.write_text("{")
        self.assertEqual(ran(self.fleet.run(self.directory, until="access")), set())
        self.assertFalse(leftover.exists())

    def test_a_running_stage_the_graph_no_longer_has_is_recorded_as_failed(self):
        self.fleet.run(self.directory, until="access")
        record = self.record()
        entry = dict(record["stages"]["access[node-1]"])
        entry.update(stage="retired", instance="", status="running", ended_at=None)
        record["stages"]["retired"] = entry
        run_record.write(self.directory, record)
        self.fleet.run(self.directory, until="access")
        retired = self.record()["stages"]["retired"]
        self.assertEqual(retired["status"], "failed")
        self.assertIn("no longer has", retired["failure"]["message"])
        self.assertEqual(retired["failure"]["recovery"], "Deploy.md#rerunning-after-a-change")


class LiveOutputTests(unittest.TestCase):
    """Files that a stage's processes keep writing, such as their logs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"

    @staticmethod
    def serve(context):
        (context.directory / "out.json").write_text("{}")
        (context.directory / "process.log").write_text("started\n")
        (context.directory / "slots").mkdir()
        (context.directory / "slots" / "slot-0.bin").write_bytes(b"0")
        return Outcome(live=(context.directory / "process.log", context.directory / "slots"))

    def test_a_growing_live_output_leaves_its_stage_passed(self):
        stub = runner_harness.runner(self.directory, first=self.serve)
        stub.run(runner_harness.INPUTS)
        entry = run_record.read(self.directory)["stages"]["first"]
        self.assertEqual(
            entry["live_outputs"],
            ["stages/first/attempt-1/process.log", "stages/first/attempt-1/slots"],
        )
        self.assertEqual(
            [output["path"] for output in entry["outputs"]], ["stages/first/attempt-1/out.json"]
        )
        attempt = self.directory / "stages/first/attempt-1"
        with (attempt / "process.log").open("a") as log:
            log.write("served a request\n")
        (attempt / "slots" / "slot-0.bin").write_bytes(b"1")
        self.assertEqual(ran(stub.run(runner_harness.INPUTS)), set())
        (attempt / "out.json").write_text('{"changed": true}')
        self.assertIn("first", ran(stub.run(runner_harness.INPUTS)))

    def test_a_live_output_outside_the_attempt_directory_fails_the_stage(self):
        def outside(context):
            return Outcome(live=(context.directory.parent / "elsewhere.log",))

        with self.assertRaises(StageFailed) as caught:
            runner_harness.runner(self.directory, first=outside).run(runner_harness.INPUTS)
        self.assertIn("is not inside the attempt directory", str(caught.exception.__cause__))


class CommandStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"
        environment = patch.dict(
            os.environ,
            {"NARWHAL_STAGE_CLEANUP_GRACE_SECONDS": "0.2", "NARWHAL_STAGE_KILL_GRACE_SECONDS": "1"},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def command(self, code):
        return Command(lambda context: [sys.executable, "-c", code])

    def test_a_command_stage_records_its_command_and_its_evidence(self):
        runner_harness.runner(self.directory, second=self.command("print('checked')")).run(
            runner_harness.INPUTS
        )
        entry = run_record.read(self.directory)["stages"]["second[engine-1]"]
        self.assertEqual(entry["command"]["exit_code"], 0)
        self.assertEqual(entry["command"]["log"], "stages/second/engine-1/attempt-1/command.log")
        names = {Path(output["path"]).name for output in entry["outputs"]}
        self.assertIn("command.log", names)
        self.assertTrue(any(name.endswith(".stage.json") for name in names))

    def test_a_failing_command_fails_its_stage(self):
        stub = runner_harness.runner(self.directory, second=self.command("raise SystemExit(3)"))
        with self.assertRaisesRegex(StageFailed, "exited 3"):
            stub.run(runner_harness.INPUTS)
        entry = run_record.read(self.directory)["stages"]["second[engine-1]"]
        self.assertEqual((entry["status"], entry["command"]["exit_code"]), ("failed", 3))

    def test_sigterm_during_a_command_stops_its_processes_and_records_the_cleanup(self):
        stub = runner_harness.runner(
            self.directory, second=self.command("import time; time.sleep(60)")
        )
        attempt = self.directory / "stages/second/engine-1/attempt-1"

        def terminate():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not list(attempt.glob("*.stage.json")):
                time.sleep(0.02)
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=terminate, daemon=True).start()
        with self.assertRaises(RunCancelled):
            stub.run(runner_harness.INPUTS)
        entry = run_record.read(self.directory)["stages"]["second[engine-1]"]
        self.assertEqual(entry["status"], "cancelled")
        context = entry["failure"]["context"]
        self.assertEqual(context["status"], "cancelled")
        self.assertEqual(context["cleanup"]["surviving_processes"], {})
        self.assertFalse(any(_alive(int(pid)) for pid in context["processes"]))
        self.assertTrue(Path(context["evidence"]).exists())


class RemoteTimeoutTests(unittest.TestCase):
    def test_a_host_command_past_its_limit_records_the_stage_as_timed_out(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "run"

            def hung(context):
                raise RemoteTimeout("node-1", "second", "management access", Path("log"), 2.0)

            with self.assertRaises(StageFailed):
                runner_harness.runner(directory, second=hung).run(runner_harness.INPUTS)
            entry = run_record.read(directory)["stages"]["second[engine-1]"]
            self.assertEqual(entry["status"], "timeout")
            self.assertIn("exceeded the 2s second time limit", entry["failure"]["message"])


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"

    def harness(self, mode):
        return subprocess.Popen(
            [sys.executable, "-m", "tests.deployment.runner_harness", mode, str(self.directory)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def crash(self):
        crashed = self.harness("crash")
        self.assertEqual(crashed.wait(timeout=30), -signal.SIGKILL)
        return crashed

    def test_a_second_runner_is_refused_with_the_owner(self):
        holder = self.harness("hold")
        self.addCleanup(holder.kill)
        held = self.directory.parent / "held"
        deadline = time.monotonic() + 20
        while not held.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.assertRaises(RunLocked) as caught:
            runner_harness.runner(self.directory).run(runner_harness.INPUTS)
        self.assertEqual(caught.exception.owner["pid"], holder.pid)
        self.assertIn(f"pid {holder.pid}", str(caught.exception))
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=20)
        record = run_record.read(self.directory)
        self.assertIsNone(record["lock"])
        self.assertEqual(record["stages"]["second[engine-1]"]["status"], "cancelled")
        result = runner_harness.runner(self.directory).run(runner_harness.INPUTS)
        self.assertIsNone(result.stale_lock)
        self.assertEqual(ran(result), {"second[engine-1]", "second[engine-2]", "third"})

    def test_a_refused_runner_names_no_owner_it_cannot_confirm(self):
        runner_harness.runner(self.directory).run(runner_harness.INPUTS)
        record = run_record.read(self.directory)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        owner = {**run_record.owner(), "pid": ended.pid}
        record["lock"] = {"owner": owner, "acquired_at": time.time()}
        run_record.write(self.directory, record)
        with run_record.locked(self.directory):
            with self.assertRaises(RunLocked) as caught:
                runner_harness.runner(self.directory).run(runner_harness.INPUTS)
            self.assertIsNone(caught.exception.owner)
            (self.directory / "run.json").write_text("{")
            with self.assertRaises(RunLocked) as caught:
                runner_harness.runner(self.directory).run(runner_harness.INPUTS)
            self.assertIsNone(caught.exception.owner)

    def test_a_stale_lock_is_reported_with_its_owner_and_its_helpers_stopped(self):
        crashed = self.crash()
        attempt = self.directory / "stages/second/engine-1/attempt-1"
        evidence = json.loads(next(attempt.glob("helper.log.*.stage.json")).read_text())
        helpers = [int(pid) for pid in evidence["processes"]]
        self.assertTrue(any(_alive(pid) for pid in helpers))
        result = runner_harness.runner(self.directory).run(runner_harness.INPUTS)
        self.assertEqual(result.stale_lock["pid"], crashed.pid)
        self.assertFalse(any(_alive(pid) for pid in helpers))
        self.assertEqual(ran(result), {"second[engine-1]", "second[engine-2]", "third"})
        record = run_record.read(self.directory)
        entry = record["stages"]["second[engine-1]"]
        self.assertEqual((entry["status"], entry["attempt"]), ("passed", 2))
        self.assertEqual([lock["owner"]["pid"] for lock in record["stale_locks"]], [crashed.pid])

    def test_a_stale_lock_survives_a_failing_run(self):
        crashed = self.crash()

        def fail(context):
            raise ValueError("still broken")

        with self.assertRaises(StageFailed) as caught:
            runner_harness.runner(self.directory, second=fail).run(runner_harness.INPUTS)
        self.assertEqual(caught.exception.stale_lock["pid"], crashed.pid)
        self.assertIn(f"pid {crashed.pid}", "".join(caught.exception.__notes__))
        record = run_record.read(self.directory)
        self.assertIsNone(record["lock"])
        self.assertEqual([lock["owner"]["pid"] for lock in record["stale_locks"]], [crashed.pid])

    def test_a_recorded_owner_that_still_runs_is_refused(self):
        runner_harness.runner(self.directory).run(runner_harness.INPUTS)
        record = run_record.read(self.directory)
        record["lock"] = {"owner": run_record.owner(), "acquired_at": time.time()}
        run_record.write(self.directory, record)
        with self.assertRaisesRegex(RunLocked, "runs without holding run.lock"):
            runner_harness.runner(self.directory).run(runner_harness.INPUTS)


class HeldWriteTests(unittest.TestCase):
    def test_a_signal_another_thread_receives_during_the_opening_write_leaves_no_stale_lock(self):
        # The kernel may deliver SIGINT to any thread that leaves it unblocked, and Python then
        # raises KeyboardInterrupt in the main thread.
        waiting = threading.Event()
        bystander = threading.Thread(target=waiting.wait, daemon=True)
        bystander.start()
        self.addCleanup(waiting.set)
        real = run_record.write
        writes = []

        def write(directory, record):
            writes.append(record.get("lock"))
            if len(writes) == 1:
                signal.pthread_kill(bystander.ident, signal.SIGINT)
                time.sleep(0.2)
                sum(range(1000))
            real(directory, record)

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "run"
            with (
                patch("narwhal.deployment.run_record.write", write),
                self.assertRaises(KeyboardInterrupt),
            ):
                runner_harness.runner(directory).run(runner_harness.INPUTS)
            self.assertIsNone(run_record.read(directory)["lock"])
            result = runner_harness.runner(directory).run(runner_harness.INPUTS)
            self.assertIsNone(result.stale_lock)


class HeldHandlerTests(unittest.TestCase):
    """A signal that lands while `_held()` swaps handlers leaves both handlers restored."""

    def setUp(self):
        def interrupt(signum, frame):
            raise KeyboardInterrupt(signal.Signals(signum).name)

        self.handler = interrupt
        for held in (signal.SIGINT, signal.SIGTERM):
            self.addCleanup(signal.signal, held, signal.getsignal(held))
            signal.signal(held, interrupt)

    def held_with_a_signal_after(self, calls, signum):
        real = signal.signal
        made = []

        def injecting(held, handler):
            made.append(held)
            previous = real(held, handler)
            if len(made) == calls:
                os.kill(os.getpid(), signum)
            return previous

        with (
            patch.object(signal, "signal", injecting),
            self.assertRaises(KeyboardInterrupt),
            runner._held(),
        ):
            pass
        for held in (signal.SIGINT, signal.SIGTERM):
            self.assertIs(signal.getsignal(held), self.handler, signal.Signals(held).name)

    def test_a_signal_while_the_handlers_are_put_back(self):
        self.held_with_a_signal_after(3, signal.SIGINT)

    def test_a_signal_while_the_hold_begins(self):
        self.held_with_a_signal_after(1, signal.SIGTERM)


class RunRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "run"
        runner_harness.runner(self.directory).run(runner_harness.INPUTS)

    def test_the_record_carries_its_contract_and_is_listed_in_the_manifest(self):
        document = json.loads((self.directory / "run.json").read_text())
        self.assertEqual(
            (document["schema"], document["schema_version"]), ("narwhal.deployment-run", 1)
        )
        self.assertEqual(
            manifest()["contracts"]["deployment_run"]["schema"], "narwhal.deployment-run"
        )

    def test_a_record_of_another_version_or_shape_is_refused(self):
        path = self.directory / "run.json"
        document = json.loads(path.read_text())
        path.write_text(json.dumps({**document, "schema_version": 2}))
        with self.assertRaisesRegex(ContractVersionError, "newer than"):
            run_record.read(self.directory)
        for field, value in (("status", "skipped"), ("directory", "/tmp/elsewhere")):
            changed = json.loads(json.dumps(document))
            changed["stages"]["third"][field] = value
            path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaisesRegex(ContractVersionError, field):
                run_record.read(self.directory)
        changed = json.loads(json.dumps(document))
        changed["stages"]["third"]["outputs"][0]["path"] = "stages/../../escape"
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ContractVersionError, "outputs"):
            run_record.read(self.directory)

    def test_the_record_reaches_the_disk_before_the_rename_and_the_rename_after_it(self):
        record = run_record.read(self.directory)
        events = []
        fsync = os.fsync
        replace = Path.replace

        def recording_fsync(descriptor):
            events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            fsync(descriptor)

        def recording_replace(path, target):
            events.append("rename")
            return replace(path, target)

        with (
            patch("narwhal.deployment.stages.os.fsync", side_effect=recording_fsync),
            patch.object(Path, "replace", autospec=True, side_effect=recording_replace),
        ):
            run_record.write(self.directory, record)
        self.assertEqual(events, ["file", "rename", "directory"])
        self.assertEqual(
            sorted(p.name for p in self.directory.iterdir()), ["run.json", "run.lock", "stages"]
        )


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in {"Z", "X"}


if __name__ == "__main__":
    unittest.main()
