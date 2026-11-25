"""Router journal extracts for sessions and load jobs."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from tests.fleet_control.test_service_core import FakeRunner, python_hook
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    DEFAULT_JOURNAL_MAX_BYTES,
    ConfigError,
    ControlConfig,
    RouterEndpoint,
    load_config,
)
from tools.fleet_control.journal import (
    CREATED,
    MISSING,
    NOT_CONFIGURED,
    PARTIAL,
    ROTATED,
    Mark,
    extract,
    extract_all,
    mark,
    marks,
)
from tools.fleet_control.service import ControlService

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
TOKEN = "fake-control-token-" + "0" * 24


def row(terminal: str | None = None, **fields: Any) -> bytes:
    document = {"schema": "narwhal.journal", "schema_version": 1, "run": "r1", **fields}
    if terminal is not None:
        document["terminal"] = terminal
    return (json.dumps(document) + "\n").encode()


def append(path: Path, *lines: bytes) -> None:
    with open(path, "ab") as stream:
        stream.write(b"".join(lines))


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.journal = self.root / "journal.jsonl"
        self.session = self.root / "session"
        self.session.mkdir()

    def run_extract(self, start: Mark | None, max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES) -> Any:
        return extract(self.journal, start, self.session, "job-001.jsonl", max_bytes)

    def copied(self) -> bytes:
        return (self.session / "journal/job-001.jsonl").read_bytes()

    def test_only_rows_after_the_mark_are_copied_with_terminal_counts(self) -> None:
        before = row("completed")
        append(self.journal, b'{"meta": {"schema": "narwhal.journal"}}\n', before)
        start = mark(self.journal)
        after = [row("completed"), row("refused"), row(event="eject"), row("completed")]
        append(self.journal, *after)
        result = self.run_extract(start)
        self.assertEqual(self.copied(), b"".join(after))
        assert start is not None
        self.assertEqual(
            result,
            {
                "extract": "journal/job-001.jsonl",
                "start_offset": start.offset,
                "end_offset": start.offset + len(b"".join(after)),
                "lines": 4,
                "bytes": len(b"".join(after)),
                "terminal": {"completed": 2, "refused": 1},
                "notes": [],
            },
        )
        mode = stat.S_IMODE((self.session / "journal/job-001.jsonl").stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE((self.session / "journal").stat().st_mode), 0o700)

    def test_a_partial_last_line_is_left_out(self) -> None:
        append(self.journal, row("completed"))
        start = mark(self.journal)
        append(self.journal, row("failed"), b'{"terminal": "comp')
        result = self.run_extract(start)
        self.assertEqual(self.copied(), row("failed"))
        self.assertEqual(result["lines"], 1)
        self.assertEqual(result["notes"], [PARTIAL])

    def test_a_replaced_journal_is_copied_from_its_start(self) -> None:
        append(self.journal, row("completed"), row("completed"))
        start = mark(self.journal)
        replacement = self.root / "new.jsonl"
        append(replacement, row("expired"))
        os.replace(replacement, self.journal)
        result = self.run_extract(start)
        self.assertEqual(self.copied(), row("expired"))
        self.assertEqual(result["start_offset"], 0)
        self.assertEqual(result["notes"], [ROTATED])

    def test_a_shrunken_journal_is_copied_from_its_start(self) -> None:
        append(self.journal, row("completed"), row("completed"))
        start = mark(self.journal)
        with open(self.journal, "r+b") as stream:
            stream.truncate(0)
        append(self.journal, row("rejected"))
        result = self.run_extract(start)
        self.assertEqual(self.copied(), row("rejected"))
        self.assertEqual(result["terminal"], {"rejected": 1})
        self.assertEqual(result["notes"], [ROTATED])

    def test_a_journal_created_after_the_mark_is_copied_from_its_start(self) -> None:
        start = mark(self.journal)
        self.assertEqual(start, Mark(None, 0))
        append(self.journal, row("completed"))
        result = self.run_extract(start)
        self.assertEqual(self.copied(), row("completed"))
        self.assertEqual(result["notes"], [CREATED])

    def test_a_missing_journal_is_noted(self) -> None:
        result = self.run_extract(Mark(1, 0))
        self.assertEqual(result, {"extract": None, "notes": [MISSING]})
        self.assertFalse((self.session / "journal").exists())

    def test_an_unconfigured_journal_is_noted(self) -> None:
        self.assertIsNone(mark(None))
        result = extract(None, None, self.session, "session.jsonl", 1024)
        self.assertEqual(result, {"extract": None, "notes": [NOT_CONFIGURED]})

    def test_the_extract_stops_at_the_size_cap_on_a_line_boundary(self) -> None:
        start = mark(self.journal)
        lines = [row("completed", n=index) for index in range(10)]
        append(self.journal, *lines)
        cap = len(lines[0]) * 3 + 5
        result = self.run_extract(start, max_bytes=cap)
        self.assertEqual(self.copied(), b"".join(lines[:3]))
        self.assertEqual(result["lines"], 3)
        self.assertEqual(result["notes"], [CREATED, f"truncated at {cap} bytes"])


class JournalConfigTests(unittest.TestCase):
    def load(self, router: dict[str, Any]) -> ControlConfig:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet-control.local.json"
            document = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}}
            path.write_text(json.dumps({**document, "router": router}))
            return load_config(path, {})

    def test_the_journal_is_optional_and_may_not_exist_yet(self) -> None:
        self.assertIsNone(self.load({}).router.journal)
        router = self.load({"journal": "/nonexistent/narwhal/journal.jsonl"}).router
        self.assertEqual(router.journal, Path("/nonexistent/narwhal/journal.jsonl"))
        self.assertEqual(router.journal_max_bytes, 256 * 1024 * 1024)

    def test_a_relative_journal_or_invalid_cap_is_refused(self) -> None:
        for router, problem in (
            ({"journal": "runs/journal.jsonl"}, "router.journal must be the absolute path"),
            ({"journal": 5}, "router.journal must be the absolute path"),
            ({"journal_max_bytes": 0}, "router.journal_max_bytes must be a positive"),
            ({"journal_max_bytes": 1.5}, "router.journal_max_bytes must be a positive"),
        ):
            with self.subTest(router=router), self.assertRaises(ConfigError) as caught:
                self.load(router)
            self.assertIn(problem, str(caught.exception))

    def test_the_example_configuration_names_a_journal(self) -> None:
        config = load_config(ROOT / "config/fleet-control.example.json", {})
        self.assertIsNotNone(config.router.journal)

    def test_a_router_pair_lists_each_routers_journal(self) -> None:
        paths = ["/shared/router-a/journal.jsonl", "/shared/router-b/journal.jsonl"]
        router = self.load({"journals": paths}).router
        self.assertIsNone(router.journal)
        self.assertEqual(router.journal_paths, tuple(Path(path) for path in paths))
        single = self.load({"journal": paths[0]}).router
        self.assertEqual((single.journals, single.journal_paths), ((), (Path(paths[0]),)))
        self.assertEqual(self.load({}).router.journal_paths, ())

    def test_an_invalid_journal_list_is_refused(self) -> None:
        pair = ["/a/journal.jsonl", "/b/journal.jsonl"]
        listed = "router.journals must list the distinct absolute journal paths"
        for router, problem in (
            ({"journals": "/a/journal.jsonl"}, listed),
            ({"journals": ["/a/journal.jsonl"]}, listed),
            ({"journals": ["/a/journal.jsonl", "b/journal.jsonl"]}, listed),
            ({"journals": ["/a/journal.jsonl", "/a/journal.jsonl"]}, listed),
            ({"journals": ["/a/journal.jsonl", "/a/../a//journal.jsonl"]}, listed),
            ({"journals": pair, "journal": pair[0]}, "set router.journal or router.journals"),
        ):
            with self.subTest(router=router), self.assertRaises(ConfigError) as caught:
                self.load(router)
            self.assertIn(problem, str(caught.exception))


class PairExtractTests(unittest.TestCase):
    """One extract from the journals of both routers of a pair."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.journals = (root / "router-a.jsonl", root / "router-b.jsonl")
        self.session = root / "session"
        self.session.mkdir()

    def run_extract(self, starts: Any, max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES) -> Any:
        return extract_all(self.journals, starts, self.session, "session.jsonl", max_bytes)

    def test_each_journal_is_copied_in_turn_after_its_own_mark(self) -> None:
        first, second = self.journals
        append(first, row("completed", n=0))
        starts = marks(self.journals)
        assert starts is not None
        self.assertEqual(starts[1], Mark(None, 0))
        old = [row("completed", run="a"), row("cancelled", run="a")]
        append(first, *old)
        new = [row("completed", run="b")]
        append(second, *new)
        result = self.run_extract(starts)
        self.assertEqual((self.session / "journal/session.jsonl").read_bytes(), b"".join(old + new))
        old_bytes, new_bytes = len(b"".join(old)), len(b"".join(new))
        self.assertEqual(
            result,
            {
                "extract": "journal/session.jsonl",
                "lines": 3,
                "bytes": old_bytes + new_bytes,
                "terminal": {"cancelled": 1, "completed": 2},
                "notes": [f"{second}: {CREATED}"],
                "journals": [
                    {
                        "journal": str(first),
                        "start_offset": starts[0].offset,
                        "end_offset": starts[0].offset + old_bytes,
                        "lines": 2,
                        "bytes": old_bytes,
                        "notes": [],
                    },
                    {
                        "journal": str(second),
                        "start_offset": 0,
                        "end_offset": new_bytes,
                        "lines": 1,
                        "bytes": new_bytes,
                        "notes": [CREATED],
                    },
                ],
            },
        )

    def test_the_size_cap_covers_both_journals(self) -> None:
        starts = marks(self.journals)
        line = row("completed")
        append(self.journals[0], line, line)
        append(self.journals[1], line, line)
        result = self.run_extract(starts, max_bytes=len(line) * 3)
        self.assertEqual(result["lines"], 3)
        self.assertEqual(
            [entry["notes"] for entry in result["journals"]],
            [[CREATED], [CREATED, f"truncated at {len(line) * 3} bytes"]],
        )
        first, second = (str(path) for path in self.journals)
        self.assertEqual(
            result["notes"],
            [
                f"{first}: {CREATED}",
                f"{second}: {CREATED}",
                f"{second}: truncated at {len(line) * 3} bytes",
            ],
        )

    def test_a_rotated_journal_is_named_in_the_top_level_notes(self) -> None:
        first, second = self.journals
        append(first, row("completed"), row("completed"))
        append(second, row("completed"))
        starts = marks(self.journals)
        with open(first, "r+b") as stream:
            stream.truncate(0)
        append(first, row("rejected"))
        result = self.run_extract(starts)
        self.assertEqual(result["notes"], [f"{first}: {ROTATED}"])
        self.assertEqual(result["terminal"], {"rejected": 1})

    def test_a_missing_journal_is_noted_and_the_other_copied(self) -> None:
        starts = marks(self.journals)
        append(self.journals[1], row("completed"))
        result = self.run_extract(starts)
        self.assertEqual(result["lines"], 1)
        self.assertEqual(
            result["journals"][0], {"journal": str(self.journals[0]), "notes": [MISSING]}
        )
        self.assertEqual(
            result["notes"], [f"{self.journals[0]}: {MISSING}", f"{self.journals[1]}: {CREATED}"]
        )
        self.assertEqual(self.run_extract(None), {"extract": None, "notes": [NOT_CONFIGURED]})

    def test_no_journal_present_copies_nothing(self) -> None:
        result = self.run_extract(marks(self.journals))
        self.assertEqual(result["extract"], None)
        self.assertEqual(result["notes"], [f"{path}: {MISSING}" for path in self.journals])
        self.assertFalse((self.session / "journal").exists())


class SessionJournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.journal = self.root / "router/journal.jsonl"
        self.journal.parent.mkdir()
        self.runs = self.root / "runs"
        config = ControlConfig(
            FLEET,
            {"restore": python_hook("print('restored')")},
            runs_dir=self.runs,
            router=RouterEndpoint(journal=self.journal),
        )
        self.runner = FakeRunner()
        self.service = ControlService(config, runner=self.runner, env={"PATH": "/usr/bin:/bin"})
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.service, TOKEN)),
            base_url="http://control",
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.service.close)

    async def job_settled(self) -> None:
        assert self.service.jobs is not None
        for _ in range(100):
            if not self.service.jobs.busy:
                return
            await asyncio.sleep(0.01)
        self.fail("the load job did not finish")

    async def test_jobs_and_the_session_record_their_journal_extracts(self) -> None:
        append(self.journal, row("completed", before="session"))
        response = await self.client.post("/api/session")
        self.assertEqual(response.status_code, 201, response.text)
        directory = self.runs / "sessions" / response.json()["result"]["session"]
        warmup = row("completed", phase="before job")
        append(self.journal, warmup)

        self.assertEqual((await self.client.post("/api/jobs", json={"rate": 1})).status_code, 201)
        first = [row("completed"), row("failed")]
        append(self.journal, *first)
        self.runner.release.set()
        await self.job_settled()
        job = (await self.client.get("/api/jobs/current")).json()
        self.assertEqual(job["journal"]["extract"], "journal/job-001.jsonl")
        self.assertEqual(job["journal"]["terminal"], {"completed": 1, "failed": 1})
        self.assertEqual((directory / "journal/job-001.jsonl").read_bytes(), b"".join(first))

        self.runner.release.clear()
        self.assertEqual((await self.client.post("/api/jobs", json={"rate": 2})).status_code, 201)
        second = [row("cancelled")]
        append(self.journal, *second)
        stop = await self.client.post("/api/jobs/current/stop")
        self.assertEqual(stop.status_code, 200, stop.text)
        stopped = stop.json()["result"]["job"]
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["journal"]["terminal"], {"cancelled": 1})
        self.assertEqual((directory / "journal/job-002.jsonl").read_bytes(), b"".join(second))

        end = await self.client.post("/api/session/end")
        self.assertEqual(end.status_code, 200, end.text)
        journal = end.json()["result"]["journal"]
        expected = warmup + b"".join(first) + b"".join(second)
        self.assertEqual((directory / "journal/session.jsonl").read_bytes(), expected)
        self.assertEqual(journal["extract"], "journal/session.jsonl")
        self.assertEqual(journal["lines"], 4)
        self.assertEqual(journal["terminal"], {"cancelled": 1, "completed": 2, "failed": 1})
        self.assertEqual(journal["notes"], [])

        record = json.loads((directory / "run.json").read_text())
        completions = [a for a in record["actions"] if a["action"] == "job.complete"]
        self.assertEqual(
            [a["result"]["journal"]["extract"] for a in completions],
            ["journal/job-001.jsonl", "journal/job-002.jsonl"],
        )
        self.assertEqual(record["actions"][-1]["result"]["journal"], journal)

    async def test_a_session_across_a_takeover_keeps_both_routers_rows(self) -> None:
        standby = self.root / "router-b/journal.jsonl"
        standby.parent.mkdir()
        router = RouterEndpoint(journals=(self.journal, standby))
        config = dataclasses.replace(self.service.config, router=router)
        service = ControlService(config, env={"PATH": "/usr/bin:/bin"})
        append(self.journal, row("completed", run="a"))
        append(standby, row("rejected", run="b"))
        await service.start_session()
        # router-a serves until it stops, then router-b takes over and serves.
        before = row("completed", run="a")
        append(self.journal, before)
        after = [row("completed", run="b"), row("completed", run="b")]
        append(standby, *after)
        ended = await service.end_session()
        assert ended.result is not None
        journal = ended.result["journal"]
        self.assertEqual(journal["terminal"], {"completed": 3})
        self.assertEqual(
            [entry["journal"] for entry in journal["journals"]], [str(self.journal), str(standby)]
        )
        (directory,) = (self.runs / "sessions").iterdir()
        self.assertEqual(
            (directory / "journal/session.jsonl").read_bytes(), before + b"".join(after)
        )

    async def test_a_missing_journal_does_not_fail_the_session(self) -> None:
        self.assertEqual((await self.client.post("/api/session")).status_code, 201)
        end = await self.client.post("/api/session/end")
        self.assertEqual(end.status_code, 200, end.text)
        self.assertEqual(end.json()["result"]["journal"], {"extract": None, "notes": [MISSING]})


if __name__ == "__main__":
    unittest.main()
