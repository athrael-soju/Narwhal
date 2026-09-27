"""Keep remote profile activation behind idle and persisted lifecycle checks."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from narwhal import command_results
from narwhal.contracts import HANDOFF, STATE, versioned
from narwhal.deployment import ssh_gates as gates


class RemoteGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.idle = versioned(
            STATE,
            {
                "admission": dict.fromkeys(
                    ("inflight", "queued", "waiting_prefill", "waiting_decode"), 0
                ),
                "serving": {"http_retained": 0},
                "resident": {"a": {"prefill": 0, "decode": 0}, "b": {"prefill": 0, "decode": 0}},
                "ha": {"standby": False, "epoch": 0},
            },
        )
        self.handoff = versioned(
            HANDOFF,
            {
                "epoch": 0,
                "engines": ["a", "b"],
                "ejected": [],
                "lifecycle": {
                    "engine_restart_policy": "individual",
                    "wave_id": "",
                    "process_starts": {"a": 100, "b": 100},
                    "records": [
                        {
                            "iid": "b",
                            "state": "drained",
                            "wave_id": "",
                            "old_process_start": 100,
                            "requested_at": 101,
                            "restart_required": True,
                        }
                    ],
                },
            },
        )
        self.request = {
            "operation": "router_capture",
            "url": "http://127.0.0.1:8000",
            "engine_id": "b",
            "fleet": str(self.root / "fleet.json"),
            "deadline_seconds": 10,
            "preserved_handoff": str(self.root / "preserved.json"),
            "resume_handoff": str(self.root / "resume.json"),
        }

    def test_idle_requires_complete_zero_integer_counters_and_standalone_router(self):
        gates.router_idle(self.idle, {"a", "b"})
        for category, key, value in (
            ("admission", "queued", 1),
            ("serving", "http_retained", 1),
            ("admission", "inflight", False),
            ("ha", "standby", True),
            ("ha", "epoch", 1),
        ):
            altered = copy.deepcopy(self.idle)
            altered[category][key] = value
            with self.subTest(category=category, key=key), self.assertRaises(ValueError):
                gates.router_idle(altered, {"a", "b"})
        for resident in (
            {"a": {"prefill": 0, "decode": 0}},
            {"a": {"prefill": 0, "decode": 0}, "b": {"prefill": 0, "decode": 1}},
        ):
            with self.assertRaises(ValueError):
                gates.router_idle({**self.idle, "resident": resident}, {"a", "b"})

    def test_missing_or_released_hold_is_rejected(self):
        self.assertEqual(gates.verify_hold(self.handoff, "b")["old_process_start"], 100)
        for field, value in (
            ("state", "active"),
            ("old_process_start", None),
            ("old_process_start", True),
            ("restart_required", False),
            ("wave_id", "wave-x"),
        ):
            altered = copy.deepcopy(self.handoff)
            altered["lifecycle"]["records"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                gates.verify_hold(altered, "b")

    async def exchange(self, documents, request=None):
        fleet = SimpleNamespace(engines=[SimpleNamespace(iid="a"), SimpleNamespace(iid="b")])
        with (
            patch.object(gates.FleetConfig, "load", return_value=fleet),
            patch.object(
                gates,
                "_get",
                AsyncMock(side_effect=[json.dumps(row).encode() for row in documents]),
            ),
        ):
            return await gates.router_handoff(request or self.request)

    async def test_capture_preserves_two_copies_after_both_idle_observations(self):
        result = await self.exchange([self.idle, self.handoff, self.idle])
        self.assertTrue(result["idle"])
        self.assertEqual(
            Path(self.request["preserved_handoff"]).read_bytes(),
            Path(self.request["resume_handoff"]).read_bytes(),
        )
        self.assertEqual(Path(self.request["preserved_handoff"]).stat().st_mode & 0o777, 0o600)

    async def test_work_arriving_during_capture_prevents_snapshot_publication(self):
        busy = copy.deepcopy(self.idle)
        busy["admission"]["inflight"] = 1
        with self.assertRaisesRegex(ValueError, "not idle"):
            await self.exchange([self.idle, self.handoff, busy])
        self.assertFalse(Path(self.request["preserved_handoff"]).exists())

    async def test_resume_requires_same_original_process_and_a_placement_hold(self):
        Path(self.request["preserved_handoff"]).write_text(json.dumps(self.handoff))
        request = {**self.request, "operation": "router_resume_check"}
        lifecycle = {
            "router": {"controls_fleet": True},
            "engines": {"b": {"accepts_new": False, "draining": True, "state": "drained"}},
        }
        result = await self.exchange([self.idle, self.handoff, lifecycle, self.idle], request)
        self.assertTrue(result["idle"])
        changed = copy.deepcopy(self.handoff)
        changed["lifecycle"]["records"][0]["old_process_start"] = 200
        with self.assertRaisesRegex(ValueError, "changed the persisted"):
            await self.exchange([self.idle, changed], request)
        lifecycle["engines"]["b"]["accepts_new"] = True
        with self.assertRaisesRegex(ValueError, "placement hold"):
            await self.exchange([self.idle, self.handoff, lifecycle], request)

    def test_router_start_uses_explicit_journal_and_resume_flag(self):
        with patch("narwhal.cli.serve", return_value=0) as serve:
            gates.perform(
                {
                    "operation": "router_serve",
                    "fleet": "/fleet.json",
                    "port": 8000,
                    "journal": "/journal.jsonl",
                    "resume": True,
                }
            )
        self.assertEqual(
            serve.call_args.args[0],
            [
                "--fleet",
                "/fleet.json",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--journal",
                "/journal.jsonl",
                "--resume",
            ],
        )

    def test_failed_installed_command_retains_its_structured_result(self):
        destination = self.root / "result.json"

        def main(arguments):
            return command_results.invoke("narwhal-check", arguments, lambda _: 1)

        code = gates.command_result({"result_path": str(destination)}, main, ["--format", "json"])
        self.assertEqual(code, 1)
        result = json.loads(destination.with_name("command-result.json").read_bytes())
        self.assertEqual(result["status"], "failed_gate")
        self.assertEqual(result["exit_code"], code)
        self.assertTrue(result["errors"])
