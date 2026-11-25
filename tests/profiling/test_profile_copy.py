"""Copy a member's measured profile to a new engine with the same generation and KV capacity."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from io import StringIO
from pathlib import Path

import httpx

from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.profiling.generation import read_generation
from narwhal.profiling.membership import check_engine_set
from narwhal.profiling.probe.offline import copy_profile, merge_profiles
from narwhal.profiling.store import ProfileStore
from tests.fixtures import fleet, profile

LAUNCH = {"image_id": "sha256:" + "a" * 64, "args": ["--max-num-seqs", "64"]}
CAPACITY = 100_000


class ProfileCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        # The candidate fleet adds e1 to the members e0 and e3.
        self.cfg = fleet(self.root, engines=("e0", "e1", "e3"))
        self.by_host = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}
        contract = self.cfg.engine_contract
        document = AttestationDocument(
            contract, dict.fromkeys(contract.fields(), "fixture"), launch=LAUNCH
        )
        self.documents = {spec.iid: document for spec in self.cfg.engines}
        self.capacity = dict.fromkeys(self.documents, CAPACITY)
        self.transport = httpx.MockTransport(self.respond)
        self.cfg.profiles_path.unlink()
        store = ProfileStore(self.cfg.profiles_path)
        evidence = {}
        for iid in ("e0", "e3"):
            spec = next(spec for spec in self.cfg.engines if spec.iid == iid)
            generation = await read_generation(self.cfg, spec, transport=self.transport)
            row = replace(profile(iid), generation_digest=generation.digest)
            store.put(row)
            evidence[iid] = {"profile": asdict(row), "generation_evidence": generation.document}
        samples = {"method_version": 2, "engines": evidence}
        self.cfg.profiles_path.with_suffix(".samples.json").write_text(json.dumps(samples))

    def respond(self, request):
        iid = self.by_host[request.url.host]
        identity = EngineIdentity(self.cfg.engine_contract.engine_version, 100.0)
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": identity.version})
        if request.url.path == "/metrics":
            text = (
                f"process_start_time_seconds {identity.process_start_time_seconds}\n"
                f'vllm:cache_config_info{{kv_cache_size_tokens="{self.capacity[iid]}"}} 1\n'
            )
            return httpx.Response(200, text=text)
        if request.url.path == "/v1/attestation":
            return httpx.Response(200, json=make_attestation(self.documents[iid], identity))
        raise AssertionError(request.url.path)

    async def copy(self, source="e0", target="e1", name="copied.json"):
        with redirect_stdout(StringIO()):
            return await copy_profile(
                self.cfg, source, target, self.root / name, transport=self.transport
            )

    async def test_a_matching_engine_joins_with_the_members_profile(self):
        before = await check_engine_set(self.cfg, self.cfg.engines, transport=self.transport)
        self.assertIn("no profile for e1", before.problems[0])
        self.assertEqual(await self.copy(), 0)
        copied = ProfileStore(self.root / "copied.json")
        source = ProfileStore(self.cfg.profiles_path).get("e0")
        self.assertEqual(copied.get("e1"), replace(source, iid="e1"))
        sidecar = json.loads((self.root / "copied.samples.json").read_text())
        self.assertEqual(sidecar["engines"]["e1"]["copied_from"]["iid"], "e0")
        # The copy merges into the fleet's store like a swept profile, and the join checks pass.
        merged = self.root / "merged.json"
        with redirect_stdout(StringIO()):
            merge_profiles(
                [self.cfg.profiles_path, self.root / "copied.json"],
                merged,
                {"e0", "e1", "e3"},
            )
        self.cfg.profiles_path = merged
        after = await check_engine_set(self.cfg, self.cfg.engines, transport=self.transport)
        self.assertEqual(after.problems, ())

    async def test_an_engine_with_a_different_launch_is_refused(self):
        self.documents["e1"] = replace(
            self.documents["e1"], launch={**LAUNCH, "args": ["--max-num-seqs", "32"]}
        )
        with self.assertRaisesRegex(ValueError, "launch or model files differ"):
            await self.copy()
        self.assertFalse((self.root / "copied.json").exists())

    async def test_an_engine_with_different_model_files_is_refused(self):
        launch = {**LAUNCH, "model_tree_sha256": "b" * 64}
        self.documents["e1"] = replace(self.documents["e1"], launch=launch)
        with self.assertRaisesRegex(ValueError, "launch or model files differ"):
            await self.copy()

    async def test_an_engine_with_a_different_kv_capacity_is_refused(self):
        self.capacity["e1"] = CAPACITY // 2
        with self.assertRaisesRegex(ValueError, "KV capacity is 50000 tokens"):
            await self.copy()
        self.assertFalse((self.root / "copied.json").exists())

    async def test_a_fleet_without_an_engine_contract_is_refused(self):
        self.cfg.engine_contract = None
        with self.assertRaisesRegex(ValueError, "requires a complete engine_contract"):
            await self.copy()

    async def test_the_source_needs_a_measured_profile_with_evidence(self):
        with self.assertRaisesRegex(ValueError, "holds no profile for e5"):
            await self.copy(source="e5")
        self.cfg.profiles_path.with_suffix(".samples.json").write_text(
            json.dumps({"method_version": 2, "engines": {}})
        )
        with self.assertRaisesRegex(ValueError, "lacks matching measurement evidence"):
            await self.copy()

    async def test_the_output_must_be_fresh(self):
        (self.root / "copied.json").write_text("{}")
        with self.assertRaises(FileExistsError):
            await self.copy()


if __name__ == "__main__":
    unittest.main()
