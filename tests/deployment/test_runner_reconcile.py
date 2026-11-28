"""Interrupted attempts, Job adoption, stops, cancels, observation reuse and reasons."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.contracts import ContractVersionError
from narwhal.deployment import run_record
from narwhal.deployment.graph import INPUT, LIVE, Fact, Rerun, Scope, Stage, StageGraph
from narwhal.deployment.runner import (
    Adoptable,
    Cancelled,
    Input,
    Outcome,
    Runner,
    StageContext,
    StageFailed,
    StaticTopology,
    Stop,
    Unobserved,
)
from tests.fixtures import ROOT

# `link` reruns the stage that produces it, as a fabric route does; `identity` does not.
GRAPH = StageGraph(
    scopes=(Scope("W", "run", "Once per run", single=True), Scope("E", "engine", "Each engine")),
    relations=(),
    stages=(
        Stage("first", "W", "First", "first.md#first", produces=("identity",)),
        Stage("gate", "E", "Gate", "gate.md#gate", ("first",), produces=("link",)),
        Stage("third", "W", "Third", "third.md#third", ("gate",)),
    ),
    facts=(
        Fact("setting", INPUT, "W", "Setting", "The setting", (Rerun("gate"),)),
        Fact("other", INPUT, "W", "Other", "The other setting", (Rerun("third"),)),
        Fact("identity", LIVE, "W", "Identity", "A process", (Rerun("third"),)),
        Fact("link", LIVE, "E", "Link", "A route", (Rerun("gate"), Rerun("third"))),
    ),
)
TOPOLOGY = StaticTopology({"E": ("engine-1", "engine-2")})
AGE = {"link": 1800, "identity": 1800}


class Cluster:
    """Jobs by name in a file, so a runner that exits leaves them to the next one."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def jobs(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def create(self, name: str) -> bool:
        """Create a Job; False, as a 409 says, when it exists."""
        jobs = self.jobs()
        if name in jobs:
            return False
        self.path.write_text(json.dumps({**jobs, name: "active"}))
        return True

    def delete(self, name: str) -> None:
        jobs = self.jobs()
        jobs.pop(name, None)
        self.path.write_text(json.dumps(jobs))


class Rig:
    """A gate stage that runs one Job per engine, and the clock and probes around it."""

    def __init__(self, root: Path) -> None:
        self.directory = root / "run"
        self.cluster = Cluster(root / "cluster.json")
        self.inputs = {"setting": Input.value(1, "test"), "other": Input.value(1, "test")}
        self.links: dict[str, object] = {"engine-1": {"route": 1}, "engine-2": {"route": 1}}
        self.identity: object = {"pid": 1}
        self.probed: list[str] = []
        self.created: list[str] = []
        self.adopted: list[str] = []
        self.asked: list[dict] = []
        self.now = 1000.0
        # The engine whose gate ends the process after creating its Job.
        self.exit_in: str | None = None
        # Whether that gate ends the process before it deletes the earlier attempts' Jobs.
        self.exit_early = False
        self.job_name = lambda context: f"gate-{context.instance}-a{context.attempt}"
        # The engine whose gate waits for a cancel or for `release`.
        self.hold: str | None = None
        self.held = threading.Event()
        self.release = threading.Event()
        self.probe_answer = None

    def first(self, context: StageContext) -> Outcome:
        (context.directory / "out.json").write_text("{}")
        return Outcome(products={"identity": self.identity})

    def third(self, context: StageContext) -> Outcome:
        (context.directory / "out.json").write_text("{}")
        return Outcome()

    def gate(self, context: StageContext) -> Outcome:
        name = self.job_name(context)
        if context.instance == self.exit_in and self.exit_early:
            os._exit(3)
        # The Jobs of earlier attempts go before this attempt starts its own.
        for stale in context.stale_jobs:
            self.cluster.delete(stale)
            context.jobs_deleted([stale])
        context.record_jobs([name])
        (self.created if self.cluster.create(name) else self.adopted).append(name)
        if context.instance == self.exit_in:
            os._exit(3)
        if context.instance == self.hold:
            self.held.set()
            while not self.release.is_set():
                if context.cancelled:
                    self.cluster.delete(name)
                    raise Cancelled(f"{name} deleted")
                time.sleep(0.01)
        self.cluster.delete(name)
        (context.directory / "gate.json").write_text("{}")
        return Outcome(products={"link": self.links[context.instance]})

    def adopt(self, previous: dict) -> bool:
        self.asked.append(previous)
        jobs = self.cluster.jobs()
        return bool(previous["jobs"]) and all(job in jobs for job in previous["jobs"])

    def probe_link(self, instance: str, recorded: object) -> object:
        self.probed.append(f"link[{instance}]")
        if self.probe_answer is not None:
            return self.probe_answer(recorded)
        return self.links.get(instance)

    def probe_identity(self, instance: str, recorded: object) -> object:
        self.probed.append("identity")
        return self.identity

    def runner(self, max_age=None, **actions) -> Runner:
        return Runner(
            GRAPH,
            self.directory,
            target="kubernetes",
            revision="test",
            actions={
                "first": self.first,
                "gate": Adoptable(self.gate, self.adopt),
                "third": self.third,
                **actions,
            },
            topology=TOPOLOGY,
            probes={"link": self.probe_link, "identity": self.probe_identity},
            max_age=max_age,
            clock=lambda: self.now,
        )

    def run(self, max_age=None, **options):
        return self.runner(max_age).run(self.inputs, **options)

    def record(self) -> dict:
        return run_record.read(self.directory)

    def exit_during(self, instance: str, *, early: bool = False) -> None:
        """Run in another process that ends during `instance`'s gate, after creating its Job,
        or with `early` before it deletes the earlier attempts' Jobs. It uses this rig's
        setting."""
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.deployment.test_runner_reconcile",
                "exit",
                str(self.directory.parent),
                instance,
                "early" if early else "late",
                json.dumps(self.inputs["setting"].digest),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if process.returncode != 3:
            raise AssertionError(process.stderr)


def ran(result) -> set[str]:
    return {decision.key for decision in result.decisions if decision.ran}


def reasons(result, name: str) -> tuple[str, ...]:
    return next(decision.reasons for decision in result.decisions if decision.key == name)


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.rig = Rig(Path(temporary.name))


class InterruptedAttemptTests(Fixture):
    def test_a_restart_mid_job_resumes_the_attempt_and_adopts_its_job(self):
        self.rig.exit_during("engine-1")
        left = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual((left["status"], left["jobs"]), ("running", ["gate-engine-1-a1"]))
        self.assertIn("gate-engine-1-a1", self.rig.cluster.jobs())

        statuses = []
        write = run_record.write

        def recording(directory, body):
            entry = body["stages"].get("gate[engine-1]")
            statuses.append(entry and entry["status"])
            write(directory, body)

        with patch.object(run_record, "write", side_effect=recording):
            result = self.rig.run()
        self.assertIsNotNone(result.stale_lock)
        self.assertEqual(reasons(result, "gate[engine-1]"), ("resumes interrupted attempt 1",))
        self.assertEqual(self.rig.adopted, ["gate-engine-1-a1"])
        self.assertEqual(self.rig.created, ["gate-engine-2-a1"])
        self.assertEqual(self.rig.asked[0]["status"], "interrupted")
        self.assertIn("ended during this stage", self.rig.asked[0]["failure"]["message"])
        # The interruption is recorded, and the attempt is never recorded failed.
        self.assertIn("interrupted", statuses)
        self.assertNotIn("failed", statuses)
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual(
            (entry["status"], entry["attempt"], entry["directory"]),
            ("passed", 1, "stages/gate/engine-1/attempt-1"),
        )
        self.assertEqual(entry["started_at"], left["started_at"])
        self.assertEqual(entry["jobs"], ["gate-engine-1-a1"])
        self.assertEqual(entry["reasons"], ["no attempt yet"])

    def test_an_interrupted_attempt_whose_jobs_are_gone_runs_a_new_attempt(self):
        self.rig.exit_during("engine-1")
        self.rig.cluster.delete("gate-engine-1-a1")
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("attempt 1 interrupted",))
        self.assertEqual(self.rig.created, ["gate-engine-1-a2", "gate-engine-2-a1"])
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual((entry["status"], entry["attempt"]), ("passed", 2))
        self.assertEqual(entry["jobs"], ["gate-engine-1-a2"])

    def test_an_edit_while_the_runner_was_down_starts_a_new_attempt_instead_of_adopting(self):
        self.rig.exit_during("engine-1")
        self.rig.inputs["setting"] = Input.value(2, "test")
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("attempt 1 interrupted",))
        self.assertEqual(self.rig.adopted, [])
        # The Job built from the old setting is deleted, never adopted.
        self.assertNotIn("gate-engine-1-a1", self.rig.cluster.jobs())
        self.assertIn("gate-engine-1-a2", self.rig.created)
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["attempt"], 2)

    def test_an_attempt_interrupted_twice_is_still_adopted(self):
        self.rig.exit_during("engine-1")
        # This pass records the interruption, then stops before it reaches the gate.
        self.rig.run(until="first")
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["status"], "interrupted")
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("resumes interrupted attempt 1",))
        self.assertEqual(self.rig.adopted, ["gate-engine-1-a1"])

    def test_a_failed_adoption_check_stops_the_pass_before_any_job_starts_again(self):
        self.rig.exit_during("engine-1")

        def unreachable(previous):
            raise ConnectionError("API server unavailable")

        stub = self.rig.runner(gate=Adoptable(self.rig.gate, unreachable))
        with self.assertRaises(ConnectionError) as caught:
            stub.run(self.rig.inputs)
        self.assertIn(
            "adopting the interrupted attempt 1 of gate[engine-1]", caught.exception.__notes__
        )
        self.assertEqual(self.rig.created, [])
        record = self.rig.record()
        self.assertIsNone(record["lock"])
        self.assertEqual(record["stages"]["gate[engine-1]"]["status"], "interrupted")
        self.assertEqual(
            reasons(self.rig.run(), "gate[engine-1]"), ("resumes interrupted attempt 1",)
        )

    def test_an_action_without_adopt_runs_a_new_attempt(self):
        self.rig.exit_during("engine-1")
        result = self.rig.runner(gate=self.rig.gate).run(self.rig.inputs)
        self.assertEqual(reasons(result, "gate[engine-1]"), ("attempt 1 interrupted",))
        self.assertNotIn("gate-engine-1-a1", self.rig.cluster.jobs())
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["attempt"], 2)

    def test_a_second_crash_before_the_cleanup_keeps_the_first_attempts_job_named(self):
        self.rig.exit_during("engine-1")
        # An edit keeps attempt 1 from resuming; attempt 2 ends before it deletes that Job.
        self.rig.inputs["setting"] = Input.value(2, "test")
        self.rig.exit_during("engine-1", early=True)
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual((entry["attempt"], entry["jobs"]), (2, []))
        self.assertEqual(entry["stale_jobs"], ["gate-engine-1-a1"])
        self.rig.run()
        self.assertEqual(self.rig.cluster.jobs(), {})
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual((entry["attempt"], entry["stale_jobs"]), (3, []))

    def test_an_upstream_output_change_keeps_an_interrupted_attempt_from_resuming(self):
        self.rig.exit_during("engine-1")
        output = self.rig.record()["stages"]["first"]["outputs"][0]["path"]
        (self.rig.directory / output).write_text('{"changed": true}')
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("output of first changed",))
        self.assertEqual(self.rig.adopted, [])
        self.assertNotIn("gate-engine-1-a1", self.rig.cluster.jobs())
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["attempt"], 2)

    def test_a_changed_stage_definition_keeps_an_interrupted_attempt_from_resuming(self):
        self.rig.exit_during("engine-1")
        record = self.rig.record()
        record["stages"]["gate[engine-1]"]["definition"] = "sha256:" + "0" * 64
        run_record.write(self.rig.directory, record)
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("attempt 1 interrupted",))
        self.assertNotIn("gate-engine-1-a1", self.rig.cluster.jobs())

    def test_a_resumed_attempt_keeps_the_jobs_it_recorded_before(self):
        self.rig.exit_during("engine-1")
        self.rig.job_name = lambda context: f"next-{context.instance}"
        self.rig.run()
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual(entry["jobs"], ["gate-engine-1-a1", "next-engine-1"])


class JobRecordTests(Fixture):
    def test_a_job_is_recorded_before_it_is_created(self):
        seen = []
        create = self.rig.cluster.create

        def creating(name):
            entry = run_record.read(self.rig.directory)["stages"]["gate[engine-1]"]
            seen.append((name, list(entry["jobs"])))
            return create(name)

        with patch.object(self.rig.cluster, "create", side_effect=creating):
            self.rig.run(until="gate")
        self.assertEqual(seen[0], ("gate-engine-1-a1", ["gate-engine-1-a1"]))

    def test_a_job_name_is_recorded_once_and_must_be_a_dns_label(self):
        def gate(context):
            context.record_jobs(["job-a", "job-a"])
            context.record_jobs(["job-a", "job-b"])
            if context.instance == "engine-2":
                context.record_jobs(["Job_C"])
            return Outcome(products={"link": {}})

        with self.assertRaisesRegex(StageFailed, "DNS-1123 labels: Job_C"):
            self.rig.runner(gate=gate).run(self.rig.inputs)
        stages = self.rig.record()["stages"]
        self.assertEqual(stages["gate[engine-1]"]["jobs"], ["job-a", "job-b"])
        self.assertEqual(stages["gate[engine-2]"]["jobs"], ["job-a", "job-b"])

    def test_only_a_stale_job_can_be_confirmed_deleted(self):
        def gate(context):
            context.jobs_deleted(["gate-engine-1-a1"])
            return Outcome(products={"link": {}})

        with self.assertRaisesRegex(StageFailed, "gate-engine-1-a1 are not stale Jobs"):
            self.rig.runner(gate=gate).run(self.rig.inputs)

    def test_the_record_refuses_malformed_jobs_reasons_and_observations(self):
        self.rig.run()
        path = self.rig.directory / "run.json"
        document = json.loads(path.read_text())
        for field, value in (
            ("jobs", ["Not-A-Label"]),
            ("jobs", ["job-a", "job-a"]),
            ("stale_jobs", ["Not-A-Label"]),
            ("reasons", [""]),
            ("observed_at", {"unknown": 1.0}),
            ("observed_at", {"link": "now"}),
        ):
            changed = json.loads(json.dumps(document))
            changed["stages"]["gate[engine-1]"][field] = value
            path.write_text(json.dumps(changed))
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(ContractVersionError, field),
            ):
                run_record.read(self.rig.directory)
        changed = json.loads(json.dumps(document))
        changed["stages"]["gate[engine-1]"].update(status="interrupted", failure=None)
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ContractVersionError, "interrupted stage records its failure"):
            run_record.read(self.rig.directory)


class StopTests(Fixture):
    def in_thread(self, stop: Stop) -> tuple[threading.Thread, dict]:
        """Run the pass in a worker thread, as the controller does."""
        outcome: dict = {}

        def work():
            try:
                outcome["result"] = self.rig.run(stop=stop)
            except BaseException as error:  # the test reports it
                outcome["error"] = error

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.rig.held.wait(10))
        return thread, outcome

    def test_a_stop_ends_the_pass_at_the_next_instance_boundary(self):
        stop = Stop()
        self.rig.hold = "engine-1"
        thread, outcome = self.in_thread(stop)
        stop.request()
        time.sleep(0.1)
        self.assertTrue(thread.is_alive(), "the running instance finishes first")
        self.rig.release.set()
        thread.join(10)
        result = outcome["result"]
        self.assertTrue(result.stopped)
        self.assertEqual(ran(result), {"first", "gate[engine-1]"})
        record = self.rig.record()
        self.assertIsNone(record["lock"])
        self.assertEqual(record["stages"]["gate[engine-1]"]["status"], "passed")
        self.assertNotIn("gate[engine-2]", record["stages"])
        self.assertEqual(ran(self.rig.run()), {"gate[engine-2]", "third"})

    def test_cancelling_the_running_stage_deletes_its_jobs_and_records_it_cancelled(self):
        stop = Stop()
        self.rig.hold = "engine-1"
        thread, outcome = self.in_thread(stop)
        stop.cancel("gate")
        thread.join(10)
        self.assertTrue(outcome["result"].stopped)
        self.assertEqual(self.rig.cluster.jobs(), {})
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual(entry["status"], "cancelled")
        self.assertEqual(entry["failure"]["message"], "gate-engine-1-a1 deleted")
        self.assertNotIn("third", self.rig.record()["stages"])
        self.rig.hold = None
        result = self.rig.run()
        self.assertEqual(reasons(result, "gate[engine-1]"), ("attempt 1 cancelled",))
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["attempt"], 2)

    def test_cancelling_another_stage_waits_for_the_instance_boundary(self):
        stop = Stop()
        self.rig.hold = "engine-1"
        thread, outcome = self.in_thread(stop)
        stop.cancel("third")
        time.sleep(0.1)
        self.assertTrue(thread.is_alive(), "the running gate is not cancelled")
        self.rig.release.set()
        thread.join(10)
        self.assertTrue(outcome["result"].stopped)
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["status"], "passed")

    def test_a_cancel_no_stop_asked_for_fails_the_stage(self):
        def gate(context):
            raise Cancelled("cancelled on its own")

        with self.assertRaises(StageFailed):
            self.rig.runner(gate=gate).run(self.rig.inputs, stop=Stop())
        self.assertEqual(self.rig.record()["stages"]["gate[engine-1]"]["status"], "failed")


class ObservationTests(Fixture):
    def test_a_link_observed_within_its_maximum_age_is_not_probed_again(self):
        self.rig.run(AGE)
        entry = self.rig.record()["stages"]["gate[engine-1]"]
        self.assertEqual(entry["observed_at"], {"link": 1000.0})
        self.rig.now = 2799.0
        self.assertEqual(ran(self.rig.run(AGE)), set())
        self.assertEqual(self.rig.probed, [])
        self.rig.now = 2800.0
        self.assertEqual(ran(self.rig.run(AGE)), set())
        self.assertEqual(sorted(self.rig.probed), ["identity", "link[engine-1]", "link[engine-2]"])
        self.assertEqual(
            self.rig.record()["stages"]["gate[engine-1]"]["observed_at"], {"link": 2800.0}
        )
        self.rig.probed.clear()
        self.rig.now = 3000.0
        self.rig.run(AGE)
        self.assertEqual(self.rig.probed, [])

    def test_a_fact_without_a_maximum_age_is_probed_on_every_pass(self):
        self.rig.run()
        self.rig.run()
        self.assertEqual(sorted(self.rig.probed), ["identity", "link[engine-1]", "link[engine-2]"])

    def test_a_changed_link_found_after_its_maximum_age_reruns_its_stages(self):
        self.rig.run(AGE)
        self.rig.links["engine-1"] = {"route": 2}
        self.rig.now = 1500.0
        self.assertEqual(ran(self.rig.run(AGE)), set())
        self.rig.now = 3000.0
        result = self.rig.run(AGE)
        self.assertEqual(ran(result), {"gate[engine-1]", "third"})
        self.assertEqual(reasons(result, "gate[engine-1]"), ("link[engine-1] changed",))

    def test_a_changed_observation_is_probed_again_on_the_next_pass(self):
        # A changed identity reruns only `third`, so the record still holds the old value.
        self.rig.run(AGE)
        self.rig.identity = {"pid": 2}
        self.rig.now = 3000.0
        self.assertEqual(ran(self.rig.run(AGE)), {"third"})
        self.assertNotIn("identity", self.rig.record()["stages"]["first"]["observed_at"])
        self.rig.probed.clear()
        self.rig.now = 3001.0
        self.assertEqual(ran(self.rig.run(AGE)), set())
        self.assertIn("identity", self.rig.probed)

    def test_a_clock_that_went_back_probes_again(self):
        self.rig.run(AGE)
        self.rig.now = 900.0
        self.rig.run(AGE)
        self.assertIn("link[engine-1]", self.rig.probed)

    def test_an_unobserved_link_on_a_real_pass_reruns_its_stage(self):
        self.rig.run()
        self.rig.probe_answer = Unobserved
        self.assertIn("gate[engine-1]", ran(self.rig.run()))

    def test_plan_reuses_a_young_observation_as_run_does(self):
        self.rig.run(AGE)
        self.rig.probe_answer = Unobserved
        self.rig.now = 2000.0
        plan = self.rig.runner(AGE).plan(self.rig.inputs)
        decision = next(d for d in plan.decisions if d.key == "gate[engine-1]")
        self.assertEqual((decision.ran, decision.unobserved), (False, ()))
        self.rig.now = 3000.0
        plan = self.rig.runner(AGE).plan(self.rig.inputs)
        decision = next(d for d in plan.decisions if d.key == "gate[engine-1]")
        self.assertEqual((decision.ran, decision.unobserved), (False, ("link[engine-1]",)))

    def test_a_maximum_age_must_name_a_live_fact_and_be_positive(self):
        with self.assertRaisesRegex(ValueError, "not live: setting"):
            self.rig.runner({"setting": 10})
        for age in (0, -1, float("inf")):
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, "positive"):
                self.rig.runner({"link": age})


class ReasonTests(Fixture):
    def test_each_attempt_records_why_it_ran(self):
        self.rig.run()
        stages = self.rig.record()["stages"]
        self.assertEqual(stages["gate[engine-1]"]["reasons"], ["no attempt yet"])
        self.rig.inputs["setting"] = Input.value(2, "test")
        self.rig.links["engine-2"] = {"route": 2}
        self.rig.run()
        stages = self.rig.record()["stages"]
        self.assertEqual(stages["gate[engine-1]"]["reasons"], ["setting changed"])
        self.assertEqual(
            stages["gate[engine-2]"]["reasons"], ["link[engine-2] changed", "setting changed"]
        )
        self.assertEqual(stages["third"]["reasons"], ["link[engine-2] changed"])
        # A skipped stage keeps the reasons it last ran for.
        self.rig.run()
        self.assertEqual(
            self.rig.record()["stages"]["gate[engine-1]"]["reasons"], ["setting changed"]
        )


def _exit(root: Path, instance: str, when: str, setting: str) -> None:
    rig = Rig(root)
    rig.exit_in = instance
    rig.exit_early = when == "early"
    rig.inputs["setting"] = Input(json.loads(setting), "test")
    rig.run()


if __name__ == "__main__":
    if sys.argv[1:2] == ["exit"]:
        _exit(Path(sys.argv[2]), *sys.argv[3:6])
    else:
        unittest.main()
