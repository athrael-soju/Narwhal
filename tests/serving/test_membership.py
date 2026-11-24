import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from narwhal.config import EngineSpec
from narwhal.serving.router.membership import Membership
from tests.fixtures import contract_free_router


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


if __name__ == "__main__":
    unittest.main()
