"""Check the router's residency view against a live sidecar application."""

import unittest

import httpx

from narwhal.config import EngineSpec, FleetConfig
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, build_app
from narwhal.engines.kv_events import CacheCleared, StoredBlocks
from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.engines.residency import ResidencyIndex
from narwhal.runtime.residency import ResidencySubscriptions
from tests.fixtures import ROOT

SIDECAR = "http://sidecar:8010"


def stored(hashes, tokens, *, parent=None, group=0, kind="full_attention"):
    return StoredBlocks(tuple(hashes), parent, tuple(tokens), 4, group=group, kind=kind)


class ResidencySubscriptionTests(unittest.IsolatedAsyncioTestCase):
    """The router follows ordered changes and falls back to a fresh snapshot."""

    def setUp(self):
        contract = FleetConfig.load(ROOT / "tests/data/fleet.json").engine_contract
        self.document = AttestationDocument(contract, dict.fromkeys(contract.fields(), "test"))
        self.identity = EngineIdentity(contract.vllm_version, 100.0)
        self.namespace = CacheNamespace("model", contract.fingerprint())
        self.start = 100.0
        self.index = ResidencyIndex("model", contract.fingerprint())
        self.spec = EngineSpec("e1", "http://engine", attestation_url=SIDECAR + "/v1/attestation")
        self.subscriptions = ResidencySubscriptions([self.spec])
        self.client = self.sidecar(self.index)

    def engine(self, request):
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": self.identity.vllm_version})
        return httpx.Response(200, text=f"process_start_time_seconds {self.start}\n")

    def sidecar(self, index):
        app = build_app(
            self.document,
            "http://engine",
            self.identity,
            transport=httpx.MockTransport(self.engine),
            residency=index,
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=SIDECAR)
        self.addAsyncCleanup(client.aclose)
        return client

    def names(self, tokens):
        return block_identities(self.namespace, tuple(tokens), 4)

    async def test_router_follows_changes_and_matches_the_engine_view(self):
        prompt = range(12)
        self.index.mark_empty()
        await self.subscriptions.refresh(self.client)
        view = self.subscriptions.view("e1")
        self.assertEqual((view.known, view.sequence, view.resyncs), (True, -1, 1))
        # Hybrid layout: the Mamba group names only the block its attention peer named.
        self.index.apply(
            0,
            [stored([3], prompt, kind="mamba"), stored([1, 2, 3], prompt, group=1)],
        )
        await self.subscriptions.refresh(self.client)
        names = self.names(prompt)
        self.assertEqual((view.sequence, view.resyncs), (0, 1))
        self.assertEqual(view.cached_prefix_blocks(names), 3)
        self.assertEqual(view.cached_prefix_blocks(names), self.index.cached_prefix_blocks(names))
        self.index.apply(1, [CacheCleared()])
        await self.subscriptions.refresh(self.client)
        self.assertEqual(view.cached_prefix_blocks(names), 0)
        self.assertEqual(view.resyncs, 1)

    async def test_gaps_restarts_and_process_changes_resynchronise_or_go_cold(self):
        prompt = range(8)
        self.index.apply(0, [stored([1, 2], prompt)])
        await self.subscriptions.refresh(self.client)
        view = self.subscriptions.view("e1")
        self.assertEqual(view.cached_prefix_blocks(self.names(prompt)), 2)
        # A gap at the sidecar leaves only a snapshot, which reports unknown residency.
        self.index.apply(5, [])
        await self.subscriptions.refresh(self.client)
        self.assertFalse(view.known)
        self.assertIn("sequence gap", view.reason)
        self.assertEqual(view.cached_prefix_blocks(self.names(prompt)), 0)
        self.index.apply(6, [CacheCleared()])
        self.index.apply(7, [stored([1, 2], prompt)])
        await self.subscriptions.refresh(self.client)
        self.assertTrue(view.known)
        # A restarted sidecar serves a new epoch; the router takes its snapshot.
        resyncs = view.resyncs
        self.client = self.sidecar(self.index)
        await self.subscriptions.refresh(self.client)
        self.assertEqual(view.resyncs, resyncs + 1)
        self.assertEqual(view.cached_prefix_blocks(self.names(prompt)), 2)
        # A replaced engine process ends the sidecar's evidence.
        self.start = 101.0
        await self.subscriptions.refresh(self.client)
        self.assertFalse(view.known)
        self.assertIn("residency refresh failed", view.reason)

    async def test_an_unreachable_sidecar_leaves_only_its_engine_cold(self):
        self.index.mark_empty()
        app = httpx.ASGITransport(app=self.sidecar(self.index)._transport.app)

        async def route(request):
            if request.url.host == "down":
                raise httpx.ConnectError("refused", request=request)
            return await app.handle_async_request(request)

        subscriptions = ResidencySubscriptions(
            [
                self.spec,
                EngineSpec("e2", "http://engine", attestation_url="http://down:1/v1/attestation"),
            ]
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
            await subscriptions.refresh(client)
        self.assertTrue(subscriptions.view("e1").known)
        self.assertIn("ConnectError", subscriptions.view("e2").reason)
        self.assertEqual(set(subscriptions.snapshot()), {"e1", "e2"})

    async def test_engines_without_residency_are_priced_cold(self):
        cold = self.sidecar(None)
        await self.subscriptions.refresh(cold)
        view = self.subscriptions.view("e1")
        self.assertEqual((view.known, view.reason), (False, "engine publishes no cache events"))
        bare = ResidencySubscriptions([EngineSpec("e2", "http://engine")])
        await bare.refresh(cold)
        self.assertEqual(bare.view("e2").reason, "engine has no attestation sidecar")
