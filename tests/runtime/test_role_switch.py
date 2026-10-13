"""Each backend declares its role model; shared code tracks engine-held role generations."""

import unittest

from narwhal.backends import load, names
from narwhal.runtime.role_switch import EngineHeldRoles, FixedRoles, RoleSwitcher, restore_roles
from narwhal.types import Role, RoleModel

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


class DeclaredRoleModelTests(unittest.TestCase):
    def test_every_backend_declares_a_role_model_for_every_connector(self):
        for name in names():
            backend = load(name)
            for connector in backend.connectors:
                with self.subTest(backend=name, connector=connector):
                    switcher = backend.role_switcher(connector)
                    self.assertIsInstance(switcher, RoleSwitcher)
                    self.assertIsInstance(switcher.model, RoleModel)
                    self.assertEqual(switcher.engine_held, switcher.model is RoleModel.ENGINE)
                    if switcher.requires_idle:
                        self.assertIs(switcher.model, RoleModel.ENGINE)

    def test_a_connector_the_backend_cannot_switch_keeps_fixed_roles(self):
        for name in names():
            backend = load(name)
            connectors = backend.role_switch.connectors
            if connectors is None:
                continue
            with self.subTest(backend=name):
                switcher = backend.role_switcher("unswitched")
                self.assertIsInstance(switcher, FixedRoles)
                self.assertIs(switcher.model, RoleModel.FIXED)


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


class RouterAndFixedRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_and_fixed_roles_never_switch_or_go_stale(self):
        for name in names():
            switcher = load(name).role_switch
            if switcher.engine_held:
                continue
            for roles in (switcher, FixedRoles()):
                with self.subTest(backend=name, model=roles.model):
                    await roles.place(None, ("http://a", None), ("http://b", None))
                    self.assertFalse(roles.stale_handoff("http://a", "http://b"))
                    self.assertIsNone(await roles.role(None, "http://a"))


if __name__ == "__main__":
    unittest.main()
