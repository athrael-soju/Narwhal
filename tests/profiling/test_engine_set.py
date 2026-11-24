"""Check a proposed engine set against the profile store and first-token calibration."""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.config import EngineSpec, FleetConfig
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.membership import check_engine_set
from narwhal.profiling.model import Profile
from narwhal.profiling.probe import cli as profile_cli
from narwhal.profiling.store import ProfileStore
from narwhal.types import Role
from tests.fixtures import calibration_document, fleet, profile
from tests.profiling.test_profile_probe import measured_store

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def joined(cfg: FleetConfig, iid: str = "e6") -> list[EngineSpec]:
    """Return the configured engines plus one new decode engine."""
    return [*cfg.engines, EngineSpec(iid, f"http://{iid}:8000", role=Role.DECODE)]


def evidence(digest: str = DIGEST) -> GenerationEvidence:
    return GenerationEvidence(digest, {}, 100.0)


def live_generations(case: unittest.TestCase, digests: dict[str, str]) -> list[str]:
    """Answer every generation read from `digests`, DIGEST by default; return the reads."""
    reads: list[str] = []

    async def read(cfg, spec, **kwargs):
        reads.append(spec.iid)
        return evidence(digests.get(spec.iid, DIGEST))

    for module in ("membership", "calibration"):
        patcher = patch(f"narwhal.profiling.{module}.read_generation", new=read)
        patcher.start()
        case.addCleanup(patcher.stop)
    return reads


def measured(iid: str) -> Profile:
    return profile(iid, generation_digest=DIGEST)


class EngineSetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.engine_contract = None
        self.cfg.profiles_path.unlink()
        self.store = ProfileStore(self.cfg.profiles_path)
        for iid in ("e0", "e3"):
            self.store.put(measured(iid))
        self.digests: dict[str, str] = {}
        self.reads = live_generations(self, self.digests)

    async def test_the_store_must_hold_exactly_the_proposed_engines(self) -> None:
        proposed = joined(self.cfg)
        result = await check_engine_set(self.cfg, proposed)
        self.assertEqual(len(result.problems), 1)
        self.assertIn("no profile for e6", result.problems[0])
        self.assertIn("exactly the proposed engine set", result.problems[0])
        self.store.put(measured("e6"))
        result = await check_engine_set(self.cfg, proposed)
        self.assertEqual(result.problems, ())
        self.assertIsNotNone(result.profiles)
        self.assertIsNotNone(result.profiles.get("e6"))
        self.assertEqual(result.calibration.status, "uncalibrated")
        # A profile for an engine outside the set is stale.
        result = await check_engine_set(self.cfg, self.cfg.engines)
        self.assertIn("stale profiles for engines outside the proposed set: e6", result.problems[0])

    async def test_an_unreadable_store_names_the_file(self) -> None:
        self.cfg.profiles_path.write_text("{")
        result = await check_engine_set(self.cfg, joined(self.cfg))
        self.assertIsNone(result.profiles)
        self.assertIn(f"profile store {self.cfg.profiles_path} is unreadable", result.problems[0])

    async def test_each_live_engine_must_run_its_profiles_generation(self) -> None:
        self.store.put(measured("e6"))
        proposed = joined(self.cfg)
        self.digests["e6"] = OTHER
        result = await check_engine_set(self.cfg, proposed)
        self.assertEqual(
            result.problems,
            ("e6 profile generation differs from the live engine; reprofile before admission",),
        )
        self.reads.clear()
        # An engine out of service is not read.
        result = await check_engine_set(self.cfg, proposed, live={"e0", "e3"})
        self.assertEqual(result.problems, ())
        self.assertEqual(sorted(set(self.reads)), ["e0", "e3"])

    async def test_calibration_must_cover_every_pair_the_new_engine_forms(self) -> None:
        self.store.put(measured("e6"))
        proposed = joined(self.cfg)
        path = self.root / "calibration.json"
        self.cfg.first_token_calibration_path = path
        starts = {spec.iid: 100.0 for spec in proposed}
        generations = dict.fromkeys(starts, DIGEST)
        path.write_text(json.dumps(calibration_document(self.cfg, generations, starts)))
        result = await check_engine_set(self.cfg, proposed)
        text = " ".join(result.problems)
        self.assertIn("no samples for e6", text)
        self.assertIn("does not cover every directed pair", text)
        self.assertEqual(result.calibration.status, "rejected")
        candidate = replace(self.cfg, engines=proposed)
        path.write_text(json.dumps(calibration_document(candidate, generations, starts)))
        result = await check_engine_set(self.cfg, proposed)
        self.assertEqual(result.problems, ())
        self.assertEqual(result.calibration.status, "measured")
        self.assertEqual(set(result.calibration.engines), {"e0", "e3", "e6"})


class ContractEngineSetTests(unittest.IsolatedAsyncioTestCase):
    """A fleet with an engine contract, which joins require."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.assertIsNotNone(self.cfg.engine_contract)
        self.cfg.profiles_path.unlink()
        self.store = ProfileStore(self.cfg.profiles_path)
        for iid in ("e0", "e3"):
            self.store.put(measured(iid))
        live_generations(self, {})

    async def test_a_nominal_profile_for_the_new_engine_is_refused(self) -> None:
        self.store.put(replace(profile("e6"), nominal=True))
        result = await check_engine_set(self.cfg, joined(self.cfg))
        self.assertIn(
            "e6 has a nominal profile; engine_contract requires a measured one", result.problems
        )
        self.store.put(measured("e6"))
        result = await check_engine_set(self.cfg, joined(self.cfg))
        self.assertEqual(result.problems, ())

    async def test_a_malformed_generation_answer_names_the_engine(self) -> None:
        self.store.put(measured("e6"))

        async def malformed(cfg, spec, **kwargs):
            if spec.iid == "e6":
                raise AttributeError("'list' object has no attribute 'get'")
            return evidence()

        with patch("narwhal.profiling.membership.read_generation", new=malformed):
            result = await check_engine_set(self.cfg, joined(self.cfg))
        self.assertEqual(len(result.problems), 1)
        self.assertTrue(result.problems[0].startswith("e6 profile generation unreadable"))

    async def test_a_failing_calibration_read_refuses_instead_of_raising(self) -> None:
        self.store.put(measured("e6"))
        failing = AsyncMock(side_effect=TypeError("bad identity"))
        with patch("narwhal.profiling.membership.verify_calibration", new=failing):
            result = await check_engine_set(self.cfg, joined(self.cfg))
        self.assertEqual(result.calibration.status, "rejected")
        self.assertEqual(result.problems, ("calibration check failed: TypeError: bad identity",))


class CandidateFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = fleet(self.root)
        self.cfg.engine_contract = None
        live_generations(self, {})

    def test_a_new_engine_profiled_against_a_candidate_fleet_merges_into_the_store(
        self,
    ) -> None:
        proposed = joined(self.cfg)
        candidate_path = self.root / "candidate.json"
        replace(self.cfg, engines=proposed).save(candidate_path)
        base = measured_store(self.root / "base.json", [measured("e0"), measured("e3")])

        async def sweep(cfg, *args, **kwargs):
            # The live sweep measures only the `--only` engine into `--out`.
            self.assertEqual([spec.iid for spec in cfg.engines], ["e0", "e3", "e6"])
            measured_store(cfg.profiles_path, [measured("e6")])
            return 0

        with patch.object(profile_cli, "run", new=AsyncMock(side_effect=sweep)) as run:
            with redirect_stdout(io.StringIO()):
                code = profile_cli.main(
                    [
                        "--fleet",
                        str(candidate_path),
                        "--only",
                        "e6",
                        "--out",
                        str(self.root / "e6.json"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(run.await_count, 1)
            # The sweep measures only the `--only` engine.
            self.assertEqual(run.await_args.args[1], {"e6"})
        merged = self.root / "merged.json"
        with redirect_stdout(io.StringIO()):
            code = profile_cli.main(
                [
                    "--fleet",
                    str(candidate_path),
                    "--merge",
                    str(base),
                    "--merge",
                    str(self.root / "e6.json"),
                    "--out",
                    str(merged),
                ]
            )
        self.assertEqual(code, 0)
        before = asyncio.run(check_engine_set(self.cfg, proposed))
        self.assertIn("no profile for e6", before.problems[0])
        merged.replace(self.cfg.profiles_path)
        after = asyncio.run(check_engine_set(self.cfg, proposed))
        self.assertEqual(after.problems, ())


class AdoptTests(unittest.TestCase):
    def test_adopt_replaces_rows_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = ProfileStore(root / "live.json")
            live.put(profile("e0"))
            fresh = ProfileStore(root / "fresh.json")
            fresh.put(profile("e0", ttft_c=1.0))
            fresh.put(profile("e6"))
            reader = live
            live.adopt(fresh)
            self.assertEqual(reader.get("e0"), profile("e0", ttft_c=1.0))
            self.assertEqual(reader.get("e6"), profile("e6"))
            self.assertEqual(reader.engine_set_diff(["e0", "e6"]), ([], []))


if __name__ == "__main__":
    unittest.main()
