import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
import msgpack

from narwhal.backends import load
from narwhal.backends.sglang import attestation, plan, runtime
from narwhal.backends.sglang.identity import SglangIdentity
from narwhal.engines.attestation import fetch_engine_identity
from narwhal.engines.client import EngineClient
from narwhal.engines.kv_events import StoredBlocks
from narwhal.observability.journal import RunJournal
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Role
from tests.fixtures import fleet

BACKEND = load("sglang")
LAUNCH = {
    "bootstrap": {"bootstrap_host": "192.0.2.11", "bootstrap_port": 5600},
    "decode_cuda_graph_memory_gb": 4.0,
}


def runtime_record(**changes):
    return {
        "backend": "sglang",
        "expected_packages": {"sglang": "0.5.21", "mooncake-transfer-engine": "0.3.9"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "decode_cuda_graph_memory_gb": 4.0,
        **changes,
    }


class ConnectorTests(unittest.TestCase):
    def test_both_legs_carry_the_producer_bootstrap_and_one_room(self):
        kv = BACKEND.connector("mooncake")
        rendezvous = kv.rendezvous(LAUNCH)
        self.assertEqual(rendezvous["bootstrap_host"], "192.0.2.11")
        self.assertEqual(rendezvous["bootstrap_port"], 5600)
        self.assertLess(rendezvous["bootstrap_room"], 2**63)
        self.assertNotEqual(kv.rendezvous(LAUNCH)["bootstrap_room"], rendezvous["bootstrap_room"])
        body = {"model": "m", "prompt": "p", "max_completion_tokens": 7}
        decode = kv.decode_body(body, rendezvous)
        self.assertEqual((decode["max_tokens"], decode["stream"]), (7, True))
        self.assertNotIn("max_completion_tokens", decode)

    def test_the_prefill_leg_generates_one_token(self):
        client = EngineClient(kv=BACKEND.connector("mooncake"), dialect=BACKEND.dialect, model="m")
        rendezvous = {"bootstrap_room": 1}
        leg = client._prefill_leg(
            {"model": "m", "prompt": "p", "max_completion_tokens": 9}, rendezvous
        )
        self.assertEqual((leg["max_tokens"], leg["bootstrap_room"]), (1, 1))
        self.assertNotIn("max_completion_tokens", leg)
        tagged, headers = client._tag(leg, {"x-request-id": "client"}, "rid-prefill")
        self.assertEqual(tagged["rid"], "rid-prefill")
        self.assertEqual(headers, {"x-request-id": "client"})


class IdentityAndEventTests(unittest.TestCase):
    def test_process_start_comes_from_the_host(self):
        self.assertFalse(SglangIdentity.reports_process_start)

        async def read(**kwargs):
            return await fetch_engine_identity(
                "http://engine",
                reader=SglangIdentity(),
                transport=httpx.MockTransport(
                    lambda request: (
                        httpx.Response(200, json={"version": "1.0", "api_key": "secret"})
                        if request.url.path == "/server_info"
                        else httpx.Response(200, json={"process_start_time_seconds": 42.5})
                    )
                ),
                **kwargs,
            )

        with self.assertRaisesRegex(ValueError, "sidecar"):
            asyncio.run(read())
        self.assertEqual(asyncio.run(read(process=lambda: 7.0)).process_start_time_seconds, 7.0)
        identity = asyncio.run(read(attestation_url="http://sidecar:9/v1/attestation"))
        self.assertEqual((identity.version, identity.process_start_time_seconds), ("1.0", 42.5))

    def test_salt_and_adapter_key_the_first_root_block(self):
        payload = msgpack.packb(
            [
                1.0,
                [
                    {
                        "type": "BlockStored",
                        "block_hashes": [1, 2],
                        "parent_block_hash": None,
                        "token_ids": [5, 6],
                        "block_size": 1,
                        "lora_id": "a",
                        "cache_salt": "s",
                    }
                ],
                0,
            ]
        )
        (event,) = BACKEND.kv_events.decode_batch(payload)
        self.assertIsInstance(event, StoredBlocks)
        self.assertEqual(event.extra_keys, (("a", "s"), ("a",)))


class RoleSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def switch(self, mode, role, result=None):
        posted = []

        def handle(request):
            if request.method == "GET":
                state = {"disaggregation_mode": mode, "decode_cuda_graph_memory_gb": 0.0}
                return httpx.Response(200, json={"internal_states": [state], "api_key": "k"})
            posted.append(json.loads(request.content))
            return httpx.Response(200, json=result or {"success": True, "message": "ok"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await BACKEND.role_switch.switch(client, "http://engine", role, LAUNCH)
        return posted

    async def test_an_engine_in_the_role_needs_no_switch(self):
        self.assertEqual(await self.switch("prefill", Role.PREFILL), [])

    async def test_a_decode_switch_reserves_graph_memory(self):
        posted = await self.switch("prefill", Role.DECODE)
        self.assertEqual(posted, [{"new_role": "decode", "decode_cuda_graph_memory_gb": 4.0}])

    async def test_a_refused_switch_raises(self):
        with self.assertRaisesRegex(ValueError, "not idle"):
            await self.switch("decode", Role.PREFILL, {"success": False, "message": "not idle"})

    def test_only_mooncake_switches_roles(self):
        self.assertIsNotNone(BACKEND.role_switcher("mooncake"))
        self.assertIsNone(BACKEND.role_switcher("nixl"))


class LaunchTests(unittest.TestCase):
    def test_runtime_requires_pins_for_its_transfer_backend(self):
        plan.validate_runtime(runtime_record())
        for record, message in (
            (runtime_record(connector="ib"), "connector"),
            (runtime_record(expected_packages={"sglang": "0.5.21"}), "mooncake"),
            (runtime_record(decode_cuda_graph_memory_gb=0), "decode_cuda_graph_memory_gb"),
            (runtime_record(extra_args=["--page-size", "64"]), "--page-size"),
            (runtime_record(environment={"SGLANG_HOST_IP": "x"}), "SGLANG_HOST_IP"),
            (runtime_record(role="decode"), "runtime.role is set by the router"),
            (
                runtime_record(
                    connector="nixl",
                    expected_packages={"sglang": "0.5.21", "nixl": "1.5.0"},
                    decode_cuda_graph_memory_gb=None,
                ),
                "runtime.role must be prefill or decode",
            ),
            (runtime_record(extra_args=["--max-running-requests", "512"]), "from 1 to 256"),
            (
                runtime_record(
                    extra_args=["--max-running-requests", "8", "--max-running-requests", "9"]
                ),
                "set once",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                plan.validate_runtime(record)
        # Images install CUDA builds of the transfer engine under suffixed names.
        cuda_build = {"sglang": "0.5.21", "mooncake-transfer-engine-cuda13": "0.3.13"}
        plan.validate_runtime(runtime_record(expected_packages=cuda_build))
        self.assertEqual(
            plan.transfer_package("mooncake", cuda_build), "mooncake-transfer-engine-cuda13"
        )
        self.assertIsNone(
            plan.transfer_package("mooncake", {**cuda_build, "mooncake-transfer-engine": "0.3.9"})
        )
        self.assertIsNone(
            plan.transfer_package("mooncake", {"mooncake-transfer-engine-extra": "1"})
        )
        plan.validate_runtime(
            runtime_record(
                connector="nixl",
                expected_packages={"sglang": "0.5.21", "nixl": "1.5.0"},
                decode_cuda_graph_memory_gb=None,
                role="decode",
            )
        )

    def test_a_fixed_role_engine_launches_in_its_role(self):
        runtime = runtime_record(
            connector="nixl",
            expected_packages={"sglang": "0.5.21", "nixl": "1.5.0"},
            role="decode",
        )
        runtime.pop("decode_cuda_graph_memory_gb", None)
        args, transfer = plan.serve_args(
            {"runtime": runtime, "tensor_parallel_size": 1},
            model="/model",
            served_name="m",
            host="0.0.0.0",
            port=8000,
            kv_events=None,
        )
        self.assertEqual(args[args.index("--disaggregation-mode") + 1], "decode")
        self.assertNotIn("--enable-pd-role-switch", args)
        self.assertEqual((transfer["launch_role"], transfer["role_switch"]), ("decode", False))

    def test_every_engine_launches_as_prefill_with_the_pins(self):
        record = {"runtime": runtime_record(), "tensor_parallel_size": 2}
        events = {"endpoint": "ipc:///e/events.sock", "replay_endpoint": "ipc:///e/replay.sock"}
        args, transfer = plan.serve_args(
            record, model="/model", served_name="m", host="0.0.0.0", port=8000, kv_events=events
        )
        self.assertEqual(args[:3], ["-m", "launch_engine", "serve"])
        for name, value in (
            ("--disaggregation-mode", "prefill"),
            ("--disaggregation-transfer-backend", "mooncake"),
            ("--page-size", "1"),
            ("--attention-backend", "triton"),
            ("--max-running-requests", "256"),
            ("--stream-interval", "1"),
            ("--tp-size", "2"),
        ):
            self.assertEqual(plan.option(args, name), value, name)
        self.assertIn("--enable-pd-role-switch", args)
        self.assertEqual(json.loads(plan.option(args, "--kv-events-config"))["publisher"], "zmq")
        self.assertEqual(
            transfer,
            {
                "transfer_backend": "mooncake",
                "launch_role": "prefill",
                "role_switch": True,
                "decode_cuda_graph_memory_gb": 4.0,
            },
        )
        self.assertNotIn("/model", attestation.launch_args(args))

    def test_a_launch_may_lower_the_request_cap(self):
        record = {
            "runtime": runtime_record(extra_args=["--max-running-requests", "64"]),
            "tensor_parallel_size": 1,
        }
        plan.validate_runtime(record["runtime"])
        args, _ = plan.serve_args(
            record, model="/model", served_name="m", host="0.0.0.0", port=8000, kv_events=None
        )
        self.assertEqual(plan.option(args, "--max-running-requests"), "64")

    def test_the_wrapper_adds_the_bootstrap_port_and_key(self):
        command = runtime.server_command(
            ["--port", "8000"],
            {runtime.BOOTSTRAP_PORT_ENV: "5600", runtime.API_KEY_ENV: "secret"},
        )
        self.assertEqual(command[1:3], ["-m", "sglang.launch_server"])
        self.assertEqual(
            command[3:],
            ["--port", "8000", "--disaggregation-bootstrap-port", "5600", "--api-key", "secret"],
        )

    def test_model_dimensions_come_from_the_model_config(self):
        config = {
            "architectures": ["M"],
            "text_config": {
                "num_attention_heads": 8,
                "num_key_value_heads": 2,
                "hidden_size": 1024,
                "num_hidden_layers": 4,
            },
        }
        self.assertEqual(
            attestation.model_contract(config),
            {
                "contract": {"kv_heads": 2, "head_size": 128, "hidden_layers": 4},
                "model_architecture": "M",
            },
        )


class RouterRoleTests(unittest.IsolatedAsyncioTestCase):
    def router(self, connector, handle):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = fleet(Path(folder.name), engines=("e0", "e3", "e4"))
        cfg.backend, cfg.connector, cfg.dialect = "sglang", connector, "sglang"
        journal = RunJournal(Path(folder.name) / "journal.jsonl")
        journal.open()
        self.addCleanup(journal.close)
        return NarwhalRouter(cfg, journal, httpx.MockTransport(handle))

    async def test_fixed_roles_pin_every_engine(self):
        router = self.router("nixl", lambda request: httpx.Response(500))
        self.assertEqual(router.scheduler.pinned, frozenset(router.monitor.instances))
        self.assertTrue(router.scheduler.availability.roles_bound)
        self.assertIsNone(router.scheduler.roles.flip(Role.PREFILL, "test", bypass_dwell=True))

    async def test_a_flip_holds_the_engine_until_its_switch_completes(self):
        released = asyncio.Event()
        posted = []

        async def handle(request):
            if request.method == "GET":
                return httpx.Response(
                    200, json={"internal_states": [{"disaggregation_mode": "decode"}]}
                )
            posted.append(json.loads(request.content))
            await released.wait()
            return httpx.Response(200, json={"success": True})

        router = self.router("mooncake", handle)
        decode = [i for i in router.monitor.instances.values() if i.role is Role.DECODE]
        moved = router.scheduler.roles.flip(
            Role.PREFILL, "test", candidate=decode[0], bypass_cooldown=True, bypass_dwell=True
        )
        self.assertIsNotNone(moved)
        self.assertIn(moved.iid, router.scheduler.availability.switching)
        self.assertNotIn(moved, router.scheduler.live_instances())
        await asyncio.sleep(0.05)
        released.set()
        await asyncio.gather(*router.role_switches)
        self.assertEqual(posted, [{"new_role": "prefill"}])
        self.assertNotIn(moved.iid, router.scheduler.availability.switching)
        self.assertNotIn(moved.iid, router.scheduler.ejected)

    async def test_a_busy_engine_is_no_switch_donor(self):
        router = self.router("mooncake", lambda request: httpx.Response(500))
        self.assertTrue(router.scheduler.switch_requires_idle)
        for inst in router.monitor.instances.values():
            inst.decode["r"] = object()
        donor, reason = router.scheduler.roles.planned_donor(Role.PREFILL, bypass_dwell=True)
        self.assertIsNone(donor)
        self.assertIn("idle", reason)
