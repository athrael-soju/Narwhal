import asyncio
import contextlib
import io
import socket
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends import load as load_backend
from narwhal.contracts import LIFECYCLE, MEMBERSHIP, versioned
from narwhal.profiling.probe import cli as profile_cli
from narwhal.profiling.probe import drain as drain_probe
from narwhal.profiling.probe import fleet as fleet_probe
from narwhal.serving import app as serving_app
from narwhal.types import Role
from tests.fixtures import fleet, profile
from tests.profiling.test_colocated_pairs import WORKLOAD, SglangGroup, sglang_engines, split_fleet
from tests.wire import routed

ROUTER = "http://router.invalid:8000"


def record(state="active", *, ready=False, accepts=True, resident=0, pending=0):
    return {
        "state": state,
        "draining": not accepts,
        "accepts_new": accepts,
        "ready_to_stop": ready,
        "resident": {"prefill": resident, "decode": 0},
        "pending_handoffs": pending,
    }


DRAINED = record("drained", ready=True, accepts=False)
# The test resolver's answers; every other host fails to resolve.
ADDRESSES = {"localhost": {"127.0.0.1", "::1"}, "127.0.0.1": {"127.0.0.1"}}


async def resolve(host, port):
    if host in ADDRESSES:
        return ADDRESSES[host]
    raise socket.gaierror(socket.EAI_NONAME, "not known")


def stub_dns(case, resolver=resolve):
    """Answer the drain check's host lookups from `resolver` for the rest of `case`."""
    patcher = patch.object(drain_probe, "_resolve", side_effect=resolver)
    patcher.start()
    case.addCleanup(patcher.stop)


class RouterDrainTests(unittest.IsolatedAsyncioTestCase):
    """An SGLang sweep reads the router's drain state before it switches any engine."""

    def setUp(self):
        stub_dns(self)
        switcher = load_backend("sglang").role_switcher("mooncake")
        for state in (switcher._received, switcher._generations):
            saved = dict(state)
            state.clear()
            self.addCleanup(lambda state=state, saved=saved: (state.clear(), state.update(saved)))

    def fleet(self, root):
        cfg = fleet(root, engines=("e0", "e1", "e2", "e3"))
        cfg.backend, cfg.connector = "sglang", "mooncake"
        cfg.profiles_path = root / "swept.json"
        return cfg

    async def sweep(self, members=None, *, cfg=None, controls=True, router=None, **options):
        """Profile e0, whose peer is e1, behind a router whose members map ID to record.

        By default the router serves e1 to e3 and e0 is the joining engine.
        """
        if members is None:
            members = {"e1": record(), "e2": record(), "e3": record()}
        with tempfile.TemporaryDirectory() as folder:
            cfg = cfg or self.fleet(Path(folder))
            cfg.profiles_path = Path(folder) / "swept.json"
            group = SglangGroup(cfg)
            urls = {spec.iid: spec.url for spec in cfg.engines}
            self.requests = []

            async def handle(request):
                host, path = request.url.host, request.url.path
                self.requests.append((host, path))
                if host == "router.invalid":
                    if router is not None:
                        return router(path)
                    if path == "/narwhal/engines":
                        engines = [
                            {"iid": iid, "url": urls.get(iid, f"http://{iid}"), "role": "decode"}
                            for iid in members
                        ]
                        return httpx.Response(
                            200, json=versioned(MEMBERSHIP, {"generation": 1, "engines": engines})
                        )
                    body = {"router": {"controls_fleet": controls}, "engines": members}
                    return httpx.Response(200, json=versioned(LIFECYCLE, body))
                return await group.handle(request)

            async def measured(client, iid, *args, roles, **kwargs):
                await roles(Role.PREFILL)
                await roles(Role.DECODE)
                return profile(iid)

            self.measure = AsyncMock(side_effect=measured)
            options.setdefault("split_lanes", False)
            only = options.pop("only", {"e0"})
            with (
                sglang_engines(cfg, SimpleNamespace(handle=handle), self.measure),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    outcome = await fleet_probe.run(cfg, only, **options)
                except (RuntimeError, ValueError) as exc:
                    outcome = exc
            self.stored = cfg.profiles_path.exists()
        self.engine_paths = {path for host, path in self.requests if host != "router.invalid"}
        return outcome

    def assert_refused(self, outcome, kind, message):
        self.assertIsInstance(outcome, kind)
        self.assertRegex(str(outcome), message)
        # The engines received nothing: no role read, switch, cache reset or request.
        self.assertEqual(self.engine_paths, set())
        self.measure.assert_not_called()
        self.assertFalse(self.stored)

    async def test_an_only_sweep_needs_a_router_url_or_no_router(self):
        outcome = await self.sweep()
        self.assert_refused(outcome, ValueError, "pass --router-url .* or --no-router")
        self.assertEqual(self.requests, [])

    async def test_an_undrained_peer_is_refused_before_any_engine_request(self):
        outcome = await self.sweep(router_url=ROUTER)
        self.assert_refused(
            outcome,
            RuntimeError,
            r"router at http://router.invalid:8000 has not drained: peer e1 is active\. "
            r"Drain each one with POST /narwhal/lifecycle/drain",
        )
        self.assertNotIn("e0 is", str(outcome))
        self.assertEqual(
            [path for _, path in self.requests], ["/narwhal/engines", "/narwhal/lifecycle"]
        )

    async def test_a_drained_peer_passes_and_the_router_is_read_first(self):
        outcome = await self.sweep(
            {"e1": DRAINED, "e2": record(), "e3": record()}, router_url=ROUTER
        )
        self.assertEqual(outcome, 0)
        self.assertTrue(self.stored)
        self.assertEqual(
            [path for _, path in self.requests[:2]], ["/narwhal/engines", "/narwhal/lifecycle"]
        )
        self.assertIn("/pd_role_switch", self.engine_paths)

    async def test_a_blocked_peer_held_out_of_placement_passes(self):
        blocked = record("blocked", accepts=False)
        outcome = await self.sweep({"e1": blocked, "e2": record()}, router_url=ROUTER)
        self.assertEqual(outcome, 0)

    async def test_peers_the_router_can_still_reach_are_refused(self):
        cases = {
            "draining": record("draining", accepts=False, resident=2),
            "drained with a KV read pending": record("drained", accepts=False),
            "blocked with resident requests": record("blocked", accepts=False, resident=1),
            "blocked in placement": record("blocked"),
            "blocked with a KV read pending": record("blocked", accepts=False, pending=1),
            "blocked on a router without pending handoffs": {
                key: value
                for key, value in record("blocked", accepts=False).items()
                if key != "pending_handoffs"
            },
            "validating": record("validating", accepts=False),
            # The breaker's automatic recovery can return an ejected engine to placement.
            "ejected": record("active", accepts=False),
            "without a lifecycle record": None,
        }
        for name, peer in cases.items():
            with self.subTest(name):
                members = {"e1": peer} if peer is not None else {}
                if peer is None:
                    # The router lists e1 as a member, but its lifecycle document omits it.
                    outcome = await self.sweep(
                        {"e1": record()},
                        router_url=ROUTER,
                        router=self.membership_without_lifecycle("e1"),
                    )
                    self.assert_refused(outcome, RuntimeError, r"peer e1 is unknown")
                    continue
                outcome = await self.sweep(members, router_url=ROUTER)
                self.assert_refused(outcome, RuntimeError, rf"peer e1 is {peer['state']}")

    def membership_without_lifecycle(self, iid):
        def router(path):
            if path == "/narwhal/engines":
                engine = {"iid": iid, "url": "http://engine-1.invalid", "role": "decode"}
                body = {"generation": 1, "engines": [engine]}
                return httpx.Response(200, json=versioned(MEMBERSHIP, body))
            body = {"router": {"controls_fleet": True}, "engines": {}}
            return httpx.Response(200, json=versioned(LIFECYCLE, body))

        return router

    async def test_a_member_under_another_id_is_matched_by_url(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = self.fleet(Path(folder))
            peer = cfg.engines[1]

            def router(path):
                if path == "/narwhal/engines":
                    engine = {"iid": "n2", "url": peer.url + "/", "role": "decode"}
                    body = {"generation": 3, "engines": [engine]}
                    return httpx.Response(200, json=versioned(MEMBERSHIP, body))
                body = {"router": {"controls_fleet": True}, "engines": {"n2": record()}}
                return httpx.Response(200, json=versioned(LIFECYCLE, body))

            outcome = await self.sweep(cfg=cfg, router=router, router_url=ROUTER + "/")
        self.assert_refused(outcome, RuntimeError, r"peer e1 \(router member n2\) is active")

    async def test_a_held_member_whose_peer_is_a_serving_member(self):
        """A replacement: the router holds the target, and its peer serves the fleet."""
        replaced = record("drained", ready=True, accepts=False)
        members = {"e0": replaced, "e1": record(), "e2": record(), "e3": record()}
        outcome = await self.sweep(members, router_url=ROUTER)
        self.assert_refused(outcome, RuntimeError, r"has not drained: peer e1 is active\.")
        self.assertIn("lists that engine right after it", str(outcome))
        # A spare engine outside the fleet, listed right after the target, takes the peer leg.
        with tempfile.TemporaryDirectory() as folder:
            cfg = self.fleet(Path(folder))
            e0, *rest = cfg.engines
            spare = replace(rest[0], iid="spare", url="http://spare.invalid:8000")
            cfg.engines = [e0, spare, *rest]
            # The sweep's own output is captured, so the notice is read from the print call.
            with patch.object(drain_probe, "print", create=True) as printed:
                outcome = await self.sweep(members, cfg=cfg, router_url=ROUTER)
        self.assertEqual(outcome, 0)
        self.assertTrue(self.stored)
        self.assertIn("spare.invalid", {host for host, _ in self.requests})
        printed.assert_called_once_with(
            f"  router {ROUTER} has no member with the ID or URL of: spare"
        )
        # In a whole-wave drain every member is held, so the fleet's own peer serves.
        wave = dict.fromkeys(("e0", "e1", "e2", "e3"), DRAINED)
        self.assertEqual(await self.sweep(wave, router_url=ROUTER), 0)

    async def test_a_serving_target_is_named_without_the_peer_prefix(self):
        outcome = await self.sweep({"e0": record(), "e1": DRAINED}, router_url=ROUTER)
        self.assert_refused(outcome, RuntimeError, r"has not drained: e0 is active\.")

    async def test_a_standby_router_is_refused(self):
        outcome = await self.sweep({"e1": DRAINED}, controls=False, router_url=ROUTER)
        self.assert_refused(outcome, RuntimeError, "does not control the fleet")

    async def test_an_unreadable_router_is_refused(self):
        for name, response in (
            ("unreachable", httpx.ConnectError("refused")),
            ("error status", httpx.Response(503)),
            ("other schema", httpx.Response(200, json={"schema": "narwhal.state"})),
        ):
            with self.subTest(name):

                def router(path, response=response):
                    if isinstance(response, Exception):
                        raise response
                    return response

                outcome = await self.sweep(router=router, router_url=ROUTER)
                self.assert_refused(
                    outcome, RuntimeError, "cannot read http://router.invalid:8000/narwhal/engines"
                )

    async def test_no_router_sweeps_without_reading_a_router(self):
        outcome = await self.sweep(no_router=True)
        self.assertEqual(outcome, 0)
        self.assertNotIn("router.invalid", {host for host, _ in self.requests})

    async def test_a_full_sweep_needs_no_router_option(self):
        outcome = await self.sweep(only=None)
        self.assertEqual(outcome, 0)
        self.assertNotIn("router.invalid", {host for host, _ in self.requests})

    async def test_the_two_router_options_are_exclusive(self):
        outcome = await self.sweep(router_url=ROUTER, no_router=True)
        self.assert_refused(outcome, ValueError, "mutually exclusive")

    async def test_a_busy_group_neighbour_is_refused_under_colocated_only(self):
        with tempfile.TemporaryDirectory() as folder:
            # e0 and e1 prefill, e2 and e3 decode; e0 pairs with e2, and e1 and e3 carry load.
            cfg = split_fleet(Path(folder), 2)
        members = {"e1": DRAINED, "e2": DRAINED, "e3": record()}
        outcome = await self.sweep(members, cfg=cfg, router_url=ROUTER, colocated_workload=WORKLOAD)
        self.assert_refused(outcome, RuntimeError, r"has not drained: peer e3 is active\.")
        # The target's peers come from its GPU group, so a spare outside the fleet cannot serve.
        self.assertNotIn("outside the router's fleet", str(outcome))
        # The same refusal without --colocated offers the spare.
        outcome = await self.sweep({"e1": record()}, router_url=ROUTER)
        self.assertIn("lists that engine right after it", str(outcome))

    async def test_vllm_and_trtllm_only_sweeps_need_no_router_option(self):
        class Stop(Exception):
            pass

        for backend, connector in (("vllm", "nixl"), ("trtllm", "nixl"), ("trtllm", "ucx")):
            with self.subTest(backend=backend, connector=connector):
                with tempfile.TemporaryDirectory() as folder:
                    cfg = fleet(Path(folder), engines=("e0", "e1"))
                    cfg.backend, cfg.connector = backend, connector
                    cfg.profiles_path = Path(folder) / "swept.json"
                    check = AsyncMock()
                    with (
                        patch.object(fleet_probe, "refuse_undrained", new=check),
                        # The sweep's first engine contact comes after the drain check.
                        patch.object(fleet_probe, "paired_transport", side_effect=Stop),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaises(Stop),
                    ):
                        await fleet_probe.run(cfg, {"e0"}, router_url=ROUTER)
                check.assert_not_called()

    async def test_the_lane_splitting_parent_checks_every_engine_before_the_lanes_start(self):
        calls = []

        async def check(router_url, engines, targets, timeout_s, **kwargs):
            calls.append(("check", [spec.iid for spec in engines]))

        async def lanes(cfg, lanes, *args, **kwargs):
            calls.append(("lanes", [[spec.iid for spec in lane] for lane in lanes]))
            return 0

        with tempfile.TemporaryDirectory() as folder:
            cfg = self.fleet(Path(folder))
            with (
                patch.object(fleet_probe, "refuse_undrained", side_effect=check),
                patch.object(fleet_probe, "_run_lanes_apart", side_effect=lanes),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = await fleet_probe.run(cfg, {"e0", "e1", "e2", "e3"}, router_url=ROUTER)
        self.assertEqual(code, 0)
        engines = ["e0", "e1", "e2", "e3"]
        self.assertEqual(calls, [("check", engines), ("lanes", [["e0", "e1"], ["e2", "e3"]])])

    def test_lane_workers_skip_the_check_their_parent_ran(self):
        with patch.object(fleet_probe, "run", new=AsyncMock(return_value=0)) as run:
            fleet_probe._lane_worker(SimpleNamespace(), {"e0"}, None, None, None, None, [])
        self.assertIs(run.await_args.kwargs["drain_checked"], True)


class MemberMatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stub_dns(self)

    async def test_a_member_under_another_spelling_of_its_host_is_matched(self):
        def router(request):
            if request.url.path == "/narwhal/engines":
                engine = {"iid": "n2", "url": "http://127.0.0.1:8100", "role": "decode"}
                body = {"generation": 1, "engines": [engine]}
                return httpx.Response(200, json=versioned(MEMBERSHIP, body))
            body = {"router": {"controls_fleet": True}, "engines": {"n2": record()}}
            return httpx.Response(200, json=versioned(LIFECYCLE, body))

        with tempfile.TemporaryDirectory() as folder:
            spec = replace(fleet(Path(folder)).engines[0], iid="e1", url="http://localhost:8100")
        with (
            patch.object(drain_probe.httpx, "AsyncClient", routed(router)),
            self.assertRaisesRegex(RuntimeError, r"peer e1 \(router member n2\) is active\."),
        ):
            await drain_probe.refuse_undrained(ROUTER, [spec], set(), 5.0)
        # Another port on the same host is another engine.
        other = replace(spec, url="http://localhost:8101", iid="e2")
        with (
            patch.object(drain_probe.httpx, "AsyncClient", routed(router)),
            patch.object(drain_probe, "print", create=True) as printed,
        ):
            await drain_probe.refuse_undrained(ROUTER, [other], set(), 5.0)
        printed.assert_called_once_with(
            f"  router {ROUTER} has no member with the ID or URL of: e2"
        )

    async def test_an_unresolvable_host_matches_by_name_and_port(self):
        endpoints = await drain_probe._endpoints("https://engine-1.invalid", 5.0)
        self.assertEqual(endpoints, {("engine-1.invalid", 443)})
        endpoints = await drain_probe._endpoints("http://localhost:8100", 5.0)
        self.assertEqual(endpoints, {("localhost", 8100), ("127.0.0.1", 8100), ("::1", 8100)})

    async def test_each_lookup_is_bounded_and_the_lookups_run_together(self):
        started = []

        async def slow(host, port):
            started.append(host)
            await asyncio.sleep(10)
            return {"192.0.2.1"}

        stub_dns(self, slow)

        def router(request):
            if request.url.path == "/narwhal/engines":
                engines = [
                    {"iid": f"m{index}", "url": f"http://member-{index}.invalid:8000"}
                    for index in range(4)
                ]
                body = {"generation": 1, "engines": engines}
                return httpx.Response(200, json=versioned(MEMBERSHIP, body))
            members = {f"m{index}": DRAINED for index in range(4)}
            body = {"router": {"controls_fleet": True}, "engines": members}
            return httpx.Response(200, json=versioned(LIFECYCLE, body))

        with tempfile.TemporaryDirectory() as folder:
            spec = replace(fleet(Path(folder)).engines[0], iid="e9", url="http://new.invalid:8000")
        begun = time.monotonic()
        with (
            patch.object(drain_probe.httpx, "AsyncClient", routed(router)),
            patch.object(drain_probe, "print", create=True) as printed,
        ):
            await drain_probe.refuse_undrained(ROUTER, [spec], {"e9"}, 0.2)
        # Five lookups that each time out at 0.2 s end together, and fall back to name and port.
        self.assertLess(time.monotonic() - begun, 0.8)
        self.assertEqual(len(started), 5)
        printed.assert_called_once_with(
            f"  router {ROUTER} has no member with the ID or URL of: e9"
        )


class LiveRouterTests(unittest.IsolatedAsyncioTestCase):
    """The check reads the documents a real router serves."""

    def setUp(self):
        stub_dns(self)

    async def test_a_peer_passes_once_the_router_drains_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = fleet(root)
            cfg.state_path = root / "handoff.json"
            app = serving_app.create_app(cfg)
            self.addAsyncCleanup(app.state.router.engines.aclose)
            transport = httpx.ASGITransport(app=app)
            base = httpx.AsyncClient

            class Routed(base):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, transport=transport, **kwargs)

            peer, other = cfg.engines[0], cfg.engines[-1]
            joining = replace(peer, iid="e9", url="http://joining.invalid:8000")

            async def check(engines, targets):
                with patch.object(drain_probe.httpx, "AsyncClient", Routed):
                    await drain_probe.refuse_undrained("http://router", engines, targets, 5.0)

            with self.assertRaisesRegex(RuntimeError, rf"peer {peer.iid} is active\."):
                await check([joining, peer], {"e9"})
            with patch.object(
                serving_app,
                "capture_process_identities",
                new=AsyncMock(return_value=({peer.iid: 100}, {})),
            ):
                async with Routed(base_url="http://router") as client:
                    response = await client.post(
                        "/narwhal/lifecycle/drain", json={"engines": [peer.iid]}
                    )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["engines"][peer.iid]["pending_handoffs"], 0)
            await check([joining, peer], {"e9"})
            app.state.router.scheduler.eject(other.iid, "liveness")
            with self.assertRaisesRegex(RuntimeError, rf"peer {other.iid} is active\."):
                await check([peer, other], set())


class RouterOptionTests(unittest.TestCase):
    def run_cli(self, *args):
        with (
            tempfile.TemporaryDirectory() as folder,
            contextlib.redirect_stderr(io.StringIO()) as errors,
            patch.object(profile_cli, "run", new=AsyncMock(return_value=0)) as run,
        ):
            cfg = fleet(Path(folder))
            path = Path(folder) / "fleet.json"
            cfg.save(path)
            try:
                code = profile_cli.main(["--fleet", str(path), *args])
            except SystemExit as exc:
                code = exc.code
        return code, errors.getvalue(), run

    def test_the_router_options_reach_the_sweep(self):
        code, _, run = self.run_cli("--only", "e0", "--router-url", ROUTER)
        self.assertEqual(code, 0)
        self.assertEqual(run.await_args.kwargs["router_url"], ROUTER)
        self.assertIs(run.await_args.kwargs["no_router"], False)
        code, _, run = self.run_cli("--only", "e0", "--no-router")
        self.assertIs(run.await_args.kwargs["no_router"], True)

    def test_invalid_router_options_exit_2(self):
        for args, message in (
            (("--router-url", ROUTER, "--no-router"), "not allowed with argument"),
            (("--router-url", "router:8000"), "http:// or https:// URL"),
            (("--no-router", "--merge", "a.json", "--out", "b.json"), "only to live profiling"),
        ):
            with self.subTest(args=args):
                code, errors, run = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertIn(message, errors)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
