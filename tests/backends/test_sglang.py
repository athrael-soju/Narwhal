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
from narwhal.engines.stream import SseEvent, sse_token_bearing, sse_token_count
from narwhal.observability.journal import RunJournal
from narwhal.profiling.probe.engine import engine_context_limit
from narwhal.serving.completion import reassemble
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


ARGS = ["--max-running-requests", "64"]


class RequestCapTests(unittest.TestCase):
    def test_a_served_cap_below_the_request_is_rejected(self):
        log = (
            "max_running_requests is capped to 59 by the mamba state cache\n"
            "max_total_num_tokens=777296, max_running_requests=59, context_len=32768\n"
        )
        with self.assertRaisesRegex(ValueError, "at most 59 running requests.*64"):
            attestation.check_request_cap(ARGS, log)

    def test_the_requested_cap_or_no_cap_line_passes(self):
        attestation.check_request_cap(ARGS, "max_running_requests=64, context_len=32768\n")
        attestation.check_request_cap(ARGS, "no scheduler summary\n")


class ChatTokenTests(unittest.TestCase):
    def test_streamed_chat_counts_chunks_without_token_ids(self):
        dialect = load("sglang").dialect
        self.assertTrue(dialect.streams_token_ids("/v1/completions"))
        self.assertFalse(dialect.streams_token_ids("/v1/chat/completions"))
        self.assertTrue(load("vllm").dialect.streams_token_ids("/v1/chat/completions"))
        chunks = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"reasoning_content": "Think"}}]},
            {"choices": [{"delta": {"content": "Red"}}]},
        ]
        events = [SseEvent("data: " + json.dumps(chunk), chunk) for chunk in chunks]
        self.assertEqual([sse_token_count(event) for event in events], [0, 1, 1])
        self.assertEqual(
            [sse_token_bearing(event, dialect, "/v1/chat/completions") for event in events],
            [False, True, True],
        )


class PrefixHitTests(unittest.TestCase):
    def test_an_engine_without_a_cache_hit_reads_zero_hits(self):
        metrics = load("sglang").metrics
        running = 'sglang:num_running_reqs{model_name="m"} 0.0\n'
        self.assertEqual(metrics.prefix_cache_hits(running), 0)
        hits = 'sglang:cached_tokens_total{cache_source="device",model_name="m"} 352.0\n'
        self.assertEqual(metrics.prefix_cache_hits(running + hits), 352)
        self.assertIsNone(metrics.prefix_cache_hits('vllm:num_requests_running{engine="0"} 0\n'))


class ContextLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_limit_reads_the_served_model(self):
        paths = []

        async def answer(request):
            paths.append((request.method, request.url.path))
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "other", "max_model_len": 4096},
                        {"id": "m", "max_model_len": 32768},
                    ],
                },
            )

        dialect = load("sglang").dialect
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            limit = await engine_context_limit(client, "http://e", "m", dialect)
            self.assertEqual(limit, 32768)
            self.assertEqual(paths, [("GET", "/v1/models")])
            with self.assertRaisesRegex(RuntimeError, "no valid max_model_len for absent"):
                await engine_context_limit(client, "http://e", "absent", dialect)


class ReassemblyTests(unittest.TestCase):
    def test_a_stop_ended_stream_keeps_the_matched_stop(self):
        dialect = load("sglang").dialect
        for endpoint, piece in (
            ("/v1/completions", {"text": "Red"}),
            ("/v1/chat/completions", {"delta": {"content": "Red"}}),
        ):
            chunks = [
                {"choices": [{"index": 0, **piece, "finish_reason": None, "matched_stop": None}]},
                {"choices": [{"index": 0, "finish_reason": "stop", "matched_stop": 151645}]},
            ]
            events = [SseEvent("data: " + json.dumps(chunk), chunk) for chunk in chunks]
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "unsupported field"):
                    reassemble(events, endpoint=endpoint)
                choice = reassemble(events, endpoint=endpoint, choice_fields=dialect.choice_fields)[
                    "choices"
                ][0]
                self.assertEqual(
                    (choice["finish_reason"], choice["matched_stop"]), ("stop", 151645)
                )


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

    def test_a_switching_engine_without_the_radix_cache_needs_no_decode_radix_flag(self):
        runtime = runtime_record(extra_args=["--disable-radix-cache"])
        args, _ = plan.serve_args(
            {"runtime": runtime, "tensor_parallel_size": 1},
            model="/model",
            served_name="m",
            host="0.0.0.0",
            port=8000,
            kv_events=None,
        )
        self.assertIn("--enable-pd-role-switch", args)
        self.assertNotIn("--disaggregation-decode-enable-radix-cache", args)

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
        self.assertIn("--disaggregation-decode-enable-radix-cache", args)
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
