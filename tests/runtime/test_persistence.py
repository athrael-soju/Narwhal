"""Check handoff restoration and journal persistence under storage failures."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.observability.journal import RunJournal
from narwhal.runtime import state
from narwhal.runtime.lifecycle import LifecycleError
from narwhal.serving.app import create_app
from narwhal.types import Role
from tests.fixtures import fleet


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    """Real routers restore local handoffs with synthetic engine identities."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.engines[0].pin = True
        self.router = create_app(self.cfg).state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.router.lifecycle.process_starts = {"e0": 100, "e3": 100}
        self.doc = state.snapshot(self.router)
        self.path = self.root / "handoff.json"

    def test_atomic_round_trip_and_failed_replace_cleanup(self):
        """Failed replacement preserves the previous handoff and removes its temporary file."""
        state.write(self.path, self.doc)
        self.assertEqual(state.load(self.path), self.doc)
        before = self.path.read_bytes()
        with (
            patch("narwhal.runtime.state.os.replace", side_effect=OSError("disk")),
            self.assertRaisesRegex(OSError, "disk"),
        ):
            state.write(self.path, {**self.doc, "run": "next"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_missing_and_torn_files_return_an_absent_handoff(self):
        """Read recovery treats missing, truncated and invalid-UTF8 files as absent."""
        self.assertIsNone(state.load(self.path))
        for content in (b"{", b"\xff"):
            self.path.write_bytes(content)
            self.assertIsNone(state.load(self.path))

    def test_restore_preserves_pins_counters_and_ejection(self):
        """Configured pins survive restoration while cumulative counters and holds transfer."""
        doc = copy.deepcopy(self.doc)
        doc["roles"] = {"e0": "decode", "e3": "prefill", "stale": "invalid"}
        doc["counters"] = {
            "served": 8,
            "failed": 2,
            "unserved": 3,
            "refused": 4,
            "rejected": 5,
            "cancelled": 6,
        }
        doc["ejected"] = ["e3"]
        doc["inference_sources"] = {"e3": ["e0"]}
        report = state.apply(self.router, doc)
        self.assertTrue(report.applied)
        self.assertEqual(self.router.monitor.instances["e0"].role, Role.PREFILL)
        self.assertEqual(self.router.monitor.instances["e3"].role, Role.PREFILL)
        self.assertEqual(
            (
                self.router.served,
                self.router.failed,
                self.router.refused,
                self.router.rejected,
                self.router.cancelled,
            ),
            (8, 2, 4, 5, 6),
        )
        self.assertIn("e3", self.router.scheduler.ejected)
        self.assertEqual(self.router._inference_sources["e3"], {"e0"})

    def test_restore_rejects_fleet_policy_and_identity_gaps(self):
        """An incompatible handoff leaves the replacement router's counters untouched."""
        for kind in ("absent", "fleet", "policy", "identity"):
            doc = copy.deepcopy(self.doc)
            doc["counters"]["served"] = 99
            if kind == "absent":
                doc = None
            elif kind == "fleet":
                doc["engines"] = ["e0"]
                doc["lifecycle"]["process_starts"] = {"e0": 100}
            elif kind == "policy":
                doc["lifecycle"]["engine_restart_policy"] = "whole_wave"
            else:
                doc["lifecycle"]["process_starts"] = {}
            with self.subTest(kind=kind):
                self.assertFalse(state.apply(self.router, doc).applied)
                self.assertEqual(self.router.served, 0)

    def test_handoff_validation_rejects_malformed_identity_and_risk(self):
        """Invalid handoff fields raise ValueError."""
        for section, field, value in (
            (None, "epoch", True),
            (None, "holder", 1),
            ("lifecycle", "records", [None]),
            ("lifecycle", "wave_id", 1),
            ("lifecycle", "process_starts", {"e0": float("nan")}),
            (None, "inference_sources", {"e0": ["stale"]}),
            (None, "demand_risk", {"kind": "failure", "age_s": -1, "events": {}}),
            (None, "demand_risk", {"kind": "failure", "age_s": 0, "events": {"failure": True}}),
        ):
            doc = copy.deepcopy(self.doc)
            (doc if section is None else doc[section])[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                state.validate(doc)

    def test_drain_rejects_unknown_incomplete_wave_and_invalid_deadlines(self):
        """Lifecycle actions validate their engine set before creating holds."""
        for engines, wave, deadline in (
            (["stale"], False, 1),
            ([], False, 1),
            (["e0"], True, 1),
            (["e0"], False, 0),
            (["e0"], False, float("nan")),
        ):
            with self.subTest(engines=engines, wave=wave, deadline=deadline):
                with self.assertRaises(LifecycleError):
                    self.router.lifecycle.begin(engines, wave=wave, deadline_s=deadline)
                self.assertEqual(self.router.lifecycle.records, {})

    def test_blocked_identity_capture_can_retry_the_same_drain(self):
        """Recording a process identity lets an idle engine finish draining."""
        lifecycle = self.router.lifecycle
        self.assertTrue(lifecycle.begin(["e3"], wave=False, deadline_s=10))
        lifecycle.record_old_identity("e3", None, "identity unavailable")
        self.assertEqual(lifecycle.records["e3"].state, "blocked")
        self.assertFalse(lifecycle.begin(["e3"], wave=False, deadline_s=10))
        lifecycle.record_old_identity("e3", 100)
        self.assertEqual(lifecycle.records["e3"].state, "drained")


class JournalTests(unittest.TestCase):
    """RunJournal appends versioned request records."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_appended_sessions_keep_distinct_run_ids_and_contracts(self):
        """Each writer stamps provenance and tags its own request rows."""
        path = self.root / "journal.jsonl"
        for run in ("first", "second"):
            journal = RunJournal(path, run=run)
            journal.write({"rid": "before-open"})
            journal.open()
            journal.write({"rid": run})
            journal.close()
            journal.close()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual([rows[1]["run"], rows[3]["run"]], ["first", "second"])
        self.assertEqual(rows[1]["schema"], "narwhal.journal")
