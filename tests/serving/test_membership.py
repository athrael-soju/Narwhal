import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import EngineSpec
from narwhal.serving import app as serving_app
from narwhal.serving.router.membership import Membership
from tests.fixtures import contract_free_router, fleet


def spec(iid: str) -> EngineSpec:
    return EngineSpec(iid, f"http://{iid}:8000")


class MembershipTests(unittest.TestCase):
    def test_changes_return_a_successor_one_generation_higher(self) -> None:
        start = Membership.initial([spec("e0"), spec("e1")])
        grown = start.joined(spec("e2"))
        shrunk = grown.left("e0")
        self.assertEqual((start.generation, grown.generation, shrunk.generation), (0, 1, 2))
        self.assertEqual(start.ids, ["e0", "e1"])
        self.assertEqual(grown.ids, ["e0", "e1", "e2"])
        self.assertEqual(shrunk.ids, ["e1", "e2"])
        self.assertEqual(shrunk.spec("e2").url, "http://e2:8000")
        self.assertNotIn("e0", shrunk)
        self.assertEqual(len(shrunk), 2)

    def test_a_snapshot_cannot_change_in_place(self) -> None:
        current = Membership.initial([spec("e0")])
        with self.assertRaises(FrozenInstanceError):
            current.generation = 5  # type: ignore[misc]
        current.by_id().pop("e0")
        self.assertIn("e0", current)
        with self.assertRaises(FrozenInstanceError):
            current.spec("e0").pin = True  # type: ignore[misc]

    def test_duplicates_and_unknown_engines_are_refused(self) -> None:
        current = Membership.initial([spec("e0")])
        with self.assertRaisesRegex(ValueError, "already a member"):
            current.joined(spec("e0"))
        with self.assertRaisesRegex(ValueError, "not a member"):
            current.left("e9")
        with self.assertRaisesRegex(ValueError, "duplicate engine id"):
            Membership.initial([spec("e0"), spec("e0")])
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            Membership(-1, ())


class RouterMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_monitor_keys_follow_the_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            self.assertEqual(router.membership.generation, 0)
            self.assertEqual(router.membership.ids, [e.iid for e in router.cfg.engines])
            self.assertEqual(router.membership.ids, list(router.monitor.instances))
            self.assertEqual(set(router.residency.views), set(router.membership.ids))

    async def test_profile_set_check_follows_a_replaced_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = contract_free_router(self, Path(tmp))
            keep = tuple(spec for spec in router.membership if spec.iid in ("e0", "e3"))
            router.membership = Membership(1, keep)
            self.assertEqual(router.profile_set_diff(), ([], ["e1", "e2", "e4", "e5"]))


class MembershipRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_wave_defaults_and_health_follow_the_snapshot_in_sorted_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = fleet(root, engines=("e3", "e0"))
            cfg.state_path = root / "handoff.json"
            app = serving_app.create_app(cfg)
            router = app.state.router
            self.addAsyncCleanup(router.engines.aclose)
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://router"
            )
            self.addAsyncCleanup(client.aclose)
            self.assertEqual(router.membership.ids, ["e3", "e0"])
            response = await client.get("/health")
            self.assertEqual(response.json()["instances"], 2)
            with patch.object(
                serving_app,
                "capture_process_identities",
                new=AsyncMock(return_value=({"e0": 100, "e3": 100}, {})),
            ) as capture:
                response = await client.post("/narwhal/lifecycle/drain", json={"wave": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(capture.call_args.args[1], ["e0", "e3"])
            started = [
                event["iid"]
                for event in router.lifecycle.events
                if event["action"] == "drain_started"
            ]
            self.assertEqual(started, ["e0", "e3"])


if __name__ == "__main__":
    unittest.main()
