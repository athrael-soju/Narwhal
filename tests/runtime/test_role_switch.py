"""Shared code tracks the role generations of engines that hold their roles."""

import unittest

from narwhal.runtime.role_switch import EngineHeldRoles, restore_roles
from narwhal.types import Role

LAUNCH = {"bootstrap": {"bootstrap_port": 5600}}


class FakeEngines(EngineHeldRoles):
    """Engines that hold a role until a switch changes it; posted switches are recorded."""

    def __init__(self, modes: dict[str, Role]) -> None:
        super().__init__()
        self.modes = modes
        self.posted: list[str] = []

    async def change_role(self, client, base, role, launch):
        host = base.removeprefix("http://")
        if self.modes[host] is role:
            return False
        self.modes[host] = role
        self.posted.append(f"{host} {role}")
        return True

    async def role(self, client, base):
        return self.modes[base.rstrip("/").removeprefix("http://")]


class EngineHeldGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_decode_engine_switches_again_for_a_producer_it_has_stale(self):
        switcher = FakeEngines({"a": Role.DECODE, "b": Role.DECODE})
        a, b = ("http://a", LAUNCH), ("http://b", LAUNCH)
        # b's endpoints predate this switcher.
        await switcher.place(None, a, b)
        self.assertEqual(switcher.posted, ["a prefill", "b prefill", "b decode"])
        switcher.posted.clear()
        await switcher.place(None, a, b)
        self.assertEqual(switcher.posted, [])
        # a enters prefill again, so b still holds a's earlier endpoint.
        await switcher.switch(None, a[0], Role.DECODE, LAUNCH)
        await switcher.place(None, a, b)
        self.assertEqual(switcher.posted, ["a decode", "a prefill", "b prefill", "b decode"])
        switcher.posted.clear()
        # b switched since it last took a handoff from a's current generation.
        await switcher.place(None, b, a)
        await switcher.place(None, a, b)
        self.assertEqual(switcher.posted, ["b prefill", "a decode", "a prefill", "b decode"])

    async def test_restored_roles_leave_no_stale_endpoint(self):
        modes = {"a": Role.DECODE, "b": Role.PREFILL, "c": Role.DECODE}
        switcher = FakeEngines(modes)
        await restore_roles(
            switcher,
            None,
            {
                "http://a": (Role.PREFILL, LAUNCH),
                "http://b": (Role.DECODE, LAUNCH),
                "http://c": (Role.DECODE, LAUNCH),
            },
        )
        # b switched after a entered prefill; c may hold a's earlier endpoint.
        self.assertEqual(switcher.posted, ["a prefill", "b decode", "c prefill", "c decode"])
        self.assertEqual(modes, {"a": Role.PREFILL, "b": Role.DECODE, "c": Role.DECODE})

    async def test_an_engine_already_in_the_role_keeps_its_generation(self):
        switcher = FakeEngines({"a": Role.PREFILL})
        await switcher.switch(None, "http://a/", Role.PREFILL)
        self.assertEqual((switcher._generations, switcher._received), ({}, {}))
        self.assertTrue(switcher.stale_handoff("http://a", "http://d"))


if __name__ == "__main__":
    unittest.main()
