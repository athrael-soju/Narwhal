import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import msgpack
from starlette.requests import Request as HTTPRequest

from narwhal.backends import load
from narwhal.backends.sglang import attestation, discovery, plan, runtime
from narwhal.backends.sglang.identity import SglangIdentity
from narwhal.backends.sglang.role_switch import SglangRoleSwitch
from narwhal.deployment.launch_engine.check import check
from narwhal.deployment.launch_engine.plan import option
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.engines.attestation import fetch_engine_identity
from narwhal.engines.client import EngineClient
from narwhal.engines.kv_events import StoredBlocks
from narwhal.engines.stream import SseEvent, sse_token_bearing, sse_token_count
from narwhal.observability.journal import RunJournal
from narwhal.profiling.probe.engine import engine_context_limit
from narwhal.runtime.role_switch import restore_roles
from narwhal.serving import app as app_module
from narwhal.serving.completion import reassemble
from narwhal.serving.router.routing import NarwhalRouter
from narwhal.types import Phase, Request, Role
from tests.backends.fixtures.sglang import batch, page_stored
from tests.fixtures import fleet

BACKEND = load("sglang")
MODEL = "/model"
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


def serve(record, *, tp=1, kv_events=None):
    return plan.serve_args(
        {"runtime": record, "tensor_parallel_size": tp},
        model=MODEL,
        served_name="m",
        host="0.0.0.0",
        port=8000,
        kv_events=kv_events,
    )


def switching_engines(modes, posted):
    def handle(request):
        host = request.url.host
        if request.method == "GET":
            return httpx.Response(
                200, json={"internal_states": [{"disaggregation_mode": modes[host]}]}
            )
        role = json.loads(request.content)["new_role"]
        modes[host] = role
        posted.append(f"{host} {role}")
        return httpx.Response(200, json={"success": True, "message": "ok"})

    return handle


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
    async def test_context_limit_is_the_served_input_limit(self):
        paths = []

        async def answer(request):
            paths.append((request.method, request.url.path))
            return httpx.Response(
                200,
                json={"served_model_name": "m", "context_length": 1024, "max_req_input_len": 1018},
            )

        dialect = load("sglang").dialect
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            limit = await engine_context_limit(client, "http://e", "m", dialect)
            self.assertEqual(limit, 1018)
            self.assertEqual(paths, [("GET", "/server_info")])
            with self.assertRaisesRegex(RuntimeError, "no valid max_model_len for absent"):
                await engine_context_limit(client, "http://e", "absent", dialect)

    def test_the_context_length_bounds_a_larger_input_limit(self):
        payload = {"served_model_name": "m", "context_length": 1024, "max_req_input_len": 4000}
        self.assertEqual(load("sglang").dialect.context_limit(payload, "m"), 1024)
        self.assertIsNone(
            load("sglang").dialect.context_limit({**payload, "context_length": 0}, "m")
        )


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

    def test_the_version_read_sends_the_engine_key(self):
        requests = []

        def handle(request):
            requests.append((str(request.url), request.headers.get("authorization")))
            if request.url.host == "down":
                return httpx.Response(503)
            return httpx.Response(200, json={"version": " 1.0 ", "api_key": "secret"})

        async def version(base, headers=None):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                return await SglangIdentity().version(client, base, headers)

        self.assertEqual(
            asyncio.run(version("http://engine/", {"Authorization": "Bearer k"})), "1.0"
        )
        self.assertEqual(asyncio.run(version("http://engine")), "1.0")
        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(version("http://down"))
        self.assertEqual(
            requests,
            [
                ("http://engine/server_info", "Bearer k"),
                ("http://engine/server_info", None),
                ("http://down/server_info", None),
            ],
        )

    def test_the_request_cap_sets_the_sequence_limit(self):
        for args, expected in (
            (["--max-running-requests", "48"], 48),
            (["--context-length", "4096", "--max-running-requests=12"], 12),
            (["--max-running-requests"], None),
            (["--max-running-requests", "0"], None),
            (["--max-running-requests", "many"], None),
        ):
            with self.subTest(args=args):
                limit = SglangIdentity().sequence_limit({"launch": {"args": args}})
                self.assertEqual(limit, expected)

    def test_salt_and_adapter_key_the_first_root_block(self):
        payload = batch(page_stored([1, 2], [5, 6], lora_id="a", cache_salt="s"))
        (event,) = BACKEND.kv_events.decode_batch(payload)
        self.assertIsInstance(event, StoredBlocks)
        self.assertEqual(event.extra_keys, (("a", "s"), ("a",)))

    def test_a_batch_without_events_names_the_rank_layout(self):
        with self.assertRaises(ValueError) as caught:
            BACKEND.kv_events.decode_batch(msgpack.packb([1.0]))
        self.assertEqual(str(caught.exception), "event batch must be [timestamp, events, dp_rank]")


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
        with (
            patch("narwhal.backends.sglang.role_switch.IDLE_WAIT_S", 0.0),
            self.assertRaisesRegex(ValueError, "not idle"),
        ):
            await self.switch("decode", Role.PREFILL, {"success": False, "message": "not idle"})

    async def test_a_switch_waits_while_the_engine_finishes_its_transfers(self):
        posted = []
        refusal = "PD role switch rejected (prefill -> decode): instance is not idle"

        def handle(request):
            if request.method == "GET":
                state = {"disaggregation_mode": "prefill"}
                return httpx.Response(200, json={"internal_states": [state]})
            posted.append(json.loads(request.content))
            if len(posted) < 3:
                return httpx.Response(400, json={"success": False, "message": refusal})
            return httpx.Response(200, json={"success": True, "message": "ok"})

        with patch("narwhal.backends.sglang.role_switch.IDLE_POLL_S", 0.0):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                await BACKEND.role_switch.switch(client, "http://engine", Role.DECODE, LAUNCH)
        self.assertEqual(len(posted), 3)

    async def test_a_decode_engine_switches_again_for_a_producer_it_has_stale(self):
        modes = {"a": "decode", "b": "decode"}
        posted = []
        handle = switching_engines(modes, posted)
        switcher = SglangRoleSwitch()
        a, b = ("http://a", LAUNCH), ("http://b", LAUNCH)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            # b's endpoints predate this switcher.
            await switcher.place(client, a, b)
            self.assertEqual(posted, ["a prefill", "b prefill", "b decode"])
            posted.clear()
            await switcher.place(client, a, b)
            self.assertEqual(posted, [])
            # a enters prefill again, so b still holds a's earlier endpoint.
            await switcher.switch(client, a[0], Role.DECODE, LAUNCH)
            await switcher.place(client, a, b)
            self.assertEqual(posted, ["a decode", "a prefill", "b prefill", "b decode"])
            posted.clear()
            # b switched since it last took a handoff from a's current generation.
            await switcher.place(client, b, a)
            await switcher.place(client, a, b)
            self.assertEqual(posted, ["b prefill", "a decode", "a prefill", "b decode"])

    async def test_restored_roles_leave_no_stale_endpoint(self):
        modes = {"a": "decode", "b": "prefill", "c": "decode"}
        posted = []
        handle = switching_engines(modes, posted)
        switcher = SglangRoleSwitch()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            self.assertIs(await switcher.role(client, "http://a"), Role.DECODE)
            await restore_roles(
                switcher,
                client,
                {
                    "http://a": (Role.PREFILL, LAUNCH),
                    "http://b": (Role.DECODE, LAUNCH),
                    "http://c": (Role.DECODE, LAUNCH),
                },
            )
        # b switched after a entered prefill; c may hold a's earlier endpoint.
        self.assertEqual(posted, ["a prefill", "b decode", "c prefill", "c decode"])
        self.assertEqual(modes, {"a": "prefill", "b": "decode", "c": "decode"})

    async def test_the_switcher_reads_the_engine_role_without_its_key(self):
        requests = []

        def handle(request):
            requests.append(
                (request.method, str(request.url), request.headers.get("authorization"))
            )
            if request.url.host == "down":
                return httpx.Response(503)
            if request.method == "GET":
                return httpx.Response(
                    200, json={"internal_states": [{"disaggregation_mode": "prefill"}]}
                )
            return httpx.Response(200, json={"success": True})

        switcher = SglangRoleSwitch()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            self.assertIs(await switcher.role(client, "http://engine/"), Role.PREFILL)
            await switcher.switch(client, "http://engine/", Role.DECODE, {})
            with self.assertRaises(httpx.HTTPStatusError):
                await switcher.role(client, "http://down")
            with self.assertRaises(httpx.HTTPStatusError):
                await switcher.switch(client, "http://down/", Role.DECODE)
        self.assertEqual(
            requests,
            [
                ("GET", "http://engine/server_info", None),
                ("GET", "http://engine/server_info", None),
                ("POST", "http://engine/pd_role_switch", None),
                ("GET", "http://down/server_info", None),
                ("GET", "http://down/server_info", None),
            ],
        )
        self.assertEqual((switcher._generations, switcher._received), ({}, {"http://engine": {}}))

    def test_mooncake_is_the_only_connector_and_switches_roles(self):
        self.assertEqual(list(BACKEND.connectors), ["mooncake"])
        self.assertIsNotNone(BACKEND.role_switcher("mooncake"))


class LaunchTests(unittest.TestCase):
    def test_runtime_requires_pins_for_its_transfer_backend(self):
        plan.validate_runtime(runtime_record())
        for record, message in (
            (runtime_record(connector="ib"), "connector"),
            (runtime_record(connector="nixl"), "runtime.connector must be one of mooncake"),
            (runtime_record(expected_packages={"sglang": "0.5.21"}), "mooncake"),
            (runtime_record(decode_cuda_graph_memory_gb=0), "decode_cuda_graph_memory_gb"),
            (runtime_record(extra_args=["--page-size", "64"]), "--page-size"),
            (runtime_record(environment={"SGLANG_HOST_IP": "x"}), "SGLANG_HOST_IP"),
            (runtime_record(environment={"SGLANG_SECRET": "x"}), "SGLANG_SECRET"),
            (runtime_record(environment={"VLLM_FOO": "1"}), "VLLM_FOO"),
            (runtime_record(role="decode"), "runtime.role is set by the router"),
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
        plan.validate_runtime(runtime_record(environment={"SGLANG_FOO": "1"}))
        # Images install CUDA builds of the transfer engine under suffixed names.
        cuda_build = {"sglang": "0.5.21", "mooncake-transfer-engine-cuda13": "0.3.13"}
        plan.validate_runtime(runtime_record(expected_packages=cuda_build))
        plan.validate_runtime(
            runtime_record(
                extra_args=[
                    "--mamba-radix-cache-strategy",
                    "no_buffer",
                    "--disable-overlap-schedule",
                ]
            )
        )
        self.assertEqual(
            plan.transfer_package("mooncake", cuda_build), "mooncake-transfer-engine-cuda13"
        )
        self.assertIsNone(
            plan.transfer_package("mooncake", {**cuda_build, "mooncake-transfer-engine": "0.3.9"})
        )
        self.assertIsNone(
            plan.transfer_package("mooncake", {"mooncake-transfer-engine-extra": "1"})
        )
        # Discovery pins the same CUDA builds that validation accepts.
        package = "mooncake-transfer-engine-cuda13"
        self.assertTrue(re.fullmatch(discovery.IMAGE_PACKAGES, package), package)
        self.assertEqual(plan.transfer_package("mooncake", {package: "1"}), package)
        self.assertIsNone(re.fullmatch(discovery.IMAGE_PACKAGES, "mooncake-transfer-engine-extra"))

    def test_a_switching_engine_without_the_radix_cache_needs_no_decode_radix_flag(self):
        args, _ = serve(runtime_record(extra_args=["--disable-radix-cache"]))
        self.assertIn("--enable-pd-role-switch", args)
        self.assertNotIn("--disaggregation-decode-enable-radix-cache", args)

    def test_extra_args_cannot_configure_cache_events(self):
        with self.assertRaisesRegex(ValueError, "unsupported or incomplete runtime option"):
            plan.validate_runtime(
                runtime_record(extra_args=["--kv-events-config", '{"publisher": "null"}'])
            )

    def test_every_engine_launches_as_prefill_with_the_pins(self):
        events = {"endpoint": "ipc:///e/events.sock", "replay_endpoint": "ipc:///e/replay.sock"}
        args, transfer = serve(runtime_record(), tp=2, kv_events=events)
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
            self.assertEqual(option(args, name), value, name)
        self.assertIn("--enable-pd-role-switch", args)
        self.assertIn("--disaggregation-decode-enable-radix-cache", args)
        self.assertEqual(json.loads(option(args, "--kv-events-config"))["publisher"], "zmq")
        self.assertEqual(
            transfer,
            {
                "transfer_backend": "mooncake",
                "launch_role": "prefill",
                "role_switch": True,
                "decode_cuda_graph_memory_gb": 4.0,
            },
        )
        self.assertNotIn(MODEL, attestation.launch_args(args))

    def test_a_launch_may_lower_the_request_cap(self):
        record = runtime_record(extra_args=["--max-running-requests", "64"])
        plan.validate_runtime(record)
        args, _ = serve(record)
        self.assertEqual(option(args, "--max-running-requests"), "64")

    def test_peers_reach_the_bootstrap_server_on_the_bind_address(self):
        launcher = load("sglang").launcher()
        native = launcher.engine_env("192.0.2.11", 5600, "", "127.0.0.1")
        self.assertEqual(launcher.side_channel_address(native), ("127.0.0.1", 5600))
        self.assertEqual(native["SGLANG_HOST_IP"], "192.0.2.11")
        for wildcard in ("0.0.0.0", "::"):
            env = launcher.engine_env("192.0.2.11", 5600, "", wildcard)
            self.assertEqual(launcher.side_channel_address(env), ("192.0.2.11", 5600))

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

    def test_model_dimensions_need_a_checked_plan(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        run = Path(folder.name)
        (run / "launch.json").write_text(json.dumps({"engine": "sglang"}))
        (run / "checked.json").write_text(json.dumps({"plan_sha256": "0" * 64}))
        with self.assertRaisesRegex(ValueError, "another serving plan"):
            load("sglang").launcher().model_dimensions(run, {"engine": "sglang"})
        self.assertFalse((run / "model-dimensions.json").exists())


IMAGE = "registry.example/sglang@sha256:" + "a" * 64
PINS = {"sglang": "0.5.21", "mooncake-transfer-engine": "0.3.9"}


class RuntimeCheckTests(unittest.TestCase):
    def prepared(self, backend: str) -> tuple[Path, dict]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        model = root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        sockets = root / "events" / "engine"
        launch = {
            "engine": "sglang",
            "backend": backend,
            "expected_packages": PINS,
            "args": ["-m", "launch_engine", "serve", "--model-path", str(model)],
            "model_dir": str(model),
            "kv_events": {
                "socket_dir": str(sockets),
                "endpoint": f"ipc://{sockets}/events.sock",
                "replay_endpoint": f"ipc://{sockets}/replay.sock",
            },
        }
        if backend == "native":
            launch.update(python_executable="/opt/sglang/bin/python", model_path=str(model))
        else:
            launch.update(image=IMAGE, common=["--network", "host"])
        run = root / "launch"
        run.mkdir()
        (run / "launch.json").write_text(json.dumps(launch))
        env = "engine.env" if backend == "native" else "container.env"
        (run / env).write_text("SGLANG_HOST_IP=192.0.2.11\n")
        return run, launch

    def endpoints(self, launch):
        return {key: launch["kv_events"][key] for key in ("endpoint", "replay_endpoint")}

    def checked(
        self, run, launch, *, version="0.5.21", events=None, returncode=0, digests=(IMAGE,)
    ):
        record = {
            "sglang_version": version,
            "prefix_caching": True,
            "kv_events": events or self.endpoints(launch),
        }
        output = runtime.SETTINGS_TAG + json.dumps(record) + "\n"
        self.calls = []

        def stage(command, **kwargs):
            self.calls.append((list(command), kwargs))
            if command[:3] == ["docker", "image", "inspect"]:
                inspection = [{"Id": "sha256:" + "b" * 64, "RepoDigests": list(digests)}]
                return subprocess.CompletedProcess(command, 0, json.dumps(inspection), "")
            return subprocess.CompletedProcess(command, returncode, output, "loading\nboom\n")

        with (
            patch("narwhal.deployment.stages.run", side_effect=stage),
            contextlib.redirect_stdout(io.StringIO()) as printed,
        ):
            check(run, launch)
        return printed.getvalue()

    def test_a_native_check_runs_the_hook_and_records_the_interpreter(self):
        run, launch = self.prepared("native")
        printed = self.checked(run, launch)
        [(command, kwargs)] = self.calls
        self.assertEqual(
            command,
            [
                launch["python_executable"],
                str(run / "hook/launch_engine.py"),
                "_check",
                json.dumps(PINS),
                json.dumps(launch["args"][3:]),
            ],
        )
        self.assertEqual(kwargs["env"], {**os.environ, "SGLANG_HOST_IP": "192.0.2.11"})
        self.assertEqual(kwargs["stage"], "native-runtime-check")
        self.assertEqual(kwargs["log"], run / "runtime-check.log")
        attempt = json.loads((run / "runtime-check.log").read_text())
        self.assertEqual(attempt["plan_sha256"], digest(run / "launch.json"))
        # write_once compares bytes, so a re-check needs this exact serialization.
        evidence = json.dumps(
            {
                "plan_sha256": digest(run / "launch.json"),
                "sglang_version": "0.5.21",
                "prefix_caching": True,
                "kv_events": self.endpoints(launch),
                "backend": "native",
                "python_executable": launch["python_executable"],
                "expected_packages": PINS,
            }
        )
        self.assertEqual((run / "checked.json").read_text(), evidence)
        sockets = launch["kv_events"]["socket_dir"]
        self.assertEqual(
            printed,
            "Runtime identity, package pins and server arguments passed.\n"
            f"The radix cache is on; cache events publish under {sockets}.\n",
        )
        self.checked(run, launch)
        self.assertEqual((run / "checked.json").read_text(), evidence)
        with self.assertRaisesRegex(ValueError, "^runtime identity changed; prepare a fresh"):
            self.checked(run, launch, version="0.5.22")

    def test_a_container_check_inspects_the_image_then_runs_the_hook(self):
        run, launch = self.prepared("container")
        self.checked(run, launch)
        log = run / "image-check.log"
        (inspect, inspected), (command, ran) = self.calls
        self.assertEqual(inspect, ["docker", "image", "inspect", IMAGE])
        self.assertEqual((inspected["stage"], inspected["log"]), ("docker-image", log))
        owner = json.loads((run / "docker-owner.json").read_text())["label"]
        self.assertEqual(command[:4], ["docker", "run", "--label", f"io.narwhal.launch={owner}"])
        self.assertEqual(
            command[6:],
            [
                "--rm",
                "--network",
                "host",
                "--entrypoint",
                "python3",
                IMAGE,
                "/narwhal-hooks/launch_engine.py",
                "_check",
                json.dumps(PINS),
                json.dumps(launch["args"][3:]),
            ],
        )
        self.assertEqual((ran["stage"], ran["log"]), ("docker-run", log))
        self.assertNotIn("env", ran)
        self.assertEqual(json.loads(log.read_text())["plan_sha256"], digest(run / "launch.json"))
        self.assertEqual(
            (run / "checked.json").read_text(),
            json.dumps(
                {
                    "plan_sha256": digest(run / "launch.json"),
                    "sglang_version": "0.5.21",
                    "prefix_caching": True,
                    "kv_events": self.endpoints(launch),
                    "image_id": "sha256:" + "b" * 64,
                }
            ),
        )

    def test_a_check_stops_at_the_first_failed_step(self):
        run, launch = self.prepared("container")
        (Path(launch["model_dir"]) / "config.json").write_text('{"auto_map": {"x": "y"}}')
        with self.assertRaisesRegex(ValueError, "^Model metadata requires --trust-remote-code"):
            self.checked(run, launch)
        self.assertEqual(self.calls, [])
        (Path(launch["model_dir"]) / "config.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "^local image identity differs from the launch"):
            self.checked(run, launch, digests=())
        self.assertEqual(
            [command[:3] for command, _ in self.calls], [["docker", "image", "inspect"]]
        )
        run, launch = self.prepared("native")
        failed = f"^native runtime check exited 1: boom; inspect {re.escape(str(run))}"
        with self.assertRaisesRegex(ValueError, failed + "/runtime-check.log$"):
            self.checked(run, launch, returncode=1)
        moved = {**self.endpoints(launch), "endpoint": "tcp://*:5557"}
        with self.assertRaisesRegex(ValueError, "^runtime cache-event endpoints differ"):
            self.checked(run, launch, events=moved)
        self.assertFalse((run / "checked.json").exists())


IMAGE_ID = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
PROCESS = {"pid": 1234, "boot_id": "boot", "start_ticks": 5678}
# The evidence file behind each attested contract field.
SOURCES = {
    "engine_version": "server-info.json",
    "image_digest": "checked.json",
    "transfer_version": "launch.json",
    "model_architecture": "model-dimensions.json",
    "model_dtype": "server-info.json",
    "kv_heads": "model-dimensions.json",
    "head_size": "model-dimensions.json",
    "hidden_layers": "model-dimensions.json",
    "attention_backend": "server-info.json",
    "kv_cache_dtype": "server-info.json",
    "connector": "launch.json",
    "speculative_config": "server-info.json",
}


class EngineDocumentTests(unittest.TestCase):
    def evidence(self, backend: str, connector: dict) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        run = Path(folder.name)
        native = backend == "native"
        launch = {
            "engine": "sglang",
            "backend": backend,
            "image": "" if native else IMAGE,
            "endpoint": "http://192.0.2.11:8000",
            "args": [
                *("-m", "launch_engine", "serve", "--model-path", "/model", "--port", "8000"),
                *("--page-size", "1", "--max-running-requests", "64"),
            ],
            "connector": connector,
            "expected_packages": PINS,
            "model_config_sha256": "1" * 64,
            "revision": "main",
            "model_revision": "2" * 40,
            "launch_sha256": "3" * 64,
            "launcher_sha256": "4" * 64,
            "launcher_code_sha256": "5" * 64,
        }
        checked = {"sglang_version": "0.5.21", "image_id": IMAGE_ID}
        identity: dict = {"image_id": IMAGE_ID, "container_id": CONTAINER_ID}
        if native:
            launch["python_executable"] = "/opt/sglang/bin/python"
            checked = {
                "sglang_version": "0.5.21",
                "backend": "native",
                "python_executable": launch["python_executable"],
                "expected_packages": PINS,
            }
            identity = {"process": PROCESS}
        (run / "launch.json").write_text(json.dumps(launch))
        plan_hash = digest(run / "launch.json")
        (run / "checked.json").write_text(json.dumps({"plan_sha256": plan_hash, **checked}))
        dimensions = {
            "contract": {"kv_heads": 2, "head_size": 128, "hidden_layers": 4},
            "model_architecture": "M",
            "plan_sha256": plan_hash,
            "image": launch["image"],
            "revision": "main",
            "model_config_sha256": "1" * 64,
        }
        (run / "model-dimensions.json").write_text(json.dumps(dimensions))
        server = {
            "version": "0.5.21",
            "dtype": "bfloat16",
            "kv_cache_dtype": "auto",
            "attention_backend": "triton",
            "page_size": 1,
            "disaggregation_transfer_backend": "mooncake",
            "speculative_algorithm": None,
            "max_running_requests": 64,
        }
        record = {"server": server, "plan_sha256": plan_hash, **identity}
        (run / "server-info.json").write_text(json.dumps(record))
        (run / ("engine.env" if native else "container.env")).write_text(
            "NARWHAL_SGLANG_BOOTSTRAP_HOST=192.0.2.11\nNARWHAL_SGLANG_BOOTSTRAP_PORT=5600\n"
        )
        (run / "startup.log").write_text("max_running_requests=64, context_len=32768\n")
        return run

    def test_the_document_holds_the_contract_its_sources_and_the_launch(self):
        switching = {
            "transfer_backend": "mooncake",
            "launch_role": "prefill",
            "role_switch": True,
            "decode_cuda_graph_memory_gb": 4.0,
        }
        fixed = {"transfer_backend": "mooncake", "launch_role": "decode", "role_switch": False}
        for backend, connector in (("container", switching), ("native", fixed)):
            with self.subTest(backend=backend):
                native = backend == "native"
                run = self.evidence(backend, connector)
                live, identity = (
                    ("live_native", PROCESS) if native else ("live_container", CONTAINER_ID)
                )
                with patch.object(attestation, live, return_value=identity):
                    document = attestation.engine_document(run, run / "startup.log")
                image_id = "" if native else IMAGE_ID
                expected = {
                    "schema": "narwhal.attestation",
                    "schema_version": 1,
                    "contract": {
                        "engine_version": "0.5.21",
                        "image_digest": image_id,
                        "transfer_version": "0.3.9",
                        "connector_version": 0,
                        "model_architecture": "M",
                        "model_dtype": "bfloat16",
                        "kv_heads": 2,
                        "head_size": 128,
                        "hidden_layers": 4,
                        "attention_backend": "triton",
                        "kv_cache_dtype": "auto",
                        "cross_layers_blocks": None,
                        "hybrid_kv_cache_manager": None,
                        "connector": "mooncake",
                        "kv_role": "",
                        "transfer_mode": "",
                        "speculative_config": "disabled",
                        "enforce_handshake_compat": None,
                    },
                    "sources": {
                        field: f"{(run / name).resolve()} sha256:{digest(run / name)}"
                        for field, name in SOURCES.items()
                        if not (native and field == "image_digest")
                    },
                    "launch": {
                        "args": [
                            *("-m", "launch_engine", "serve", "--model-path", "--port"),
                            *("--page-size", "1", "--max-running-requests", "64"),
                        ],
                        "image_id": image_id,
                        "expected_packages": PINS,
                        "model_config_sha256": "1" * 64,
                        "model_revision": "2" * 40,
                        "launch_sha256": "3" * 64,
                        "launcher_code_sha256": "5" * 64,
                        "bootstrap": {"bootstrap_host": "192.0.2.11", "bootstrap_port": 5600},
                        "role_switch": connector["role_switch"],
                        **({} if native else {"decode_cuda_graph_memory_gb": 4.0}),
                    },
                }
                self.assertEqual(document, expected)
                # The document is written in this key order.
                self.assertEqual(json.dumps(document), json.dumps(expected))

    def server_info(self, payload: dict):
        requests: list[httpx.Request] = []
        client = httpx.Client

        def answer(request):
            requests.append(request)
            return httpx.Response(200, json=payload)

        def connect(**kwargs):
            return client(transport=httpx.MockTransport(answer), **kwargs)

        return requests, patch("narwhal.backends.sglang.attestation.httpx.Client", connect)

    def test_the_server_capture_binds_the_engine_the_document_checks(self):
        connector = {"transfer_backend": "mooncake", "launch_role": "decode", "role_switch": False}
        for backend in ("container", "native"):
            with self.subTest(backend=backend):
                native = backend == "native"
                run = self.evidence(backend, connector)
                with (run / ("engine.env" if native else "container.env")).open("a") as env:
                    env.write("NARWHAL_SGLANG_API_KEY=secret\n")
                captured = json.loads((run / "server-info.json").read_text())
                (run / "server-info.json").unlink()
                requests, serving = self.server_info(captured["server"])
                live, identity, start = (
                    ("live_native", PROCESS, "native_start")
                    if native
                    else ("live_container", CONTAINER_ID, "container_start")
                )
                with (
                    patch.object(attestation, live, return_value=identity),
                    patch.object(attestation, start, return_value=100.0) as started,
                    serving,
                ):
                    path = attestation.capture_server(run)
                    with self.assertRaisesRegex(ValueError, "^Server evidence already exists"):
                        attestation.capture_server(run)
                    attestation.engine_document(run, run / "startup.log")
                    started.assert_called_once_with(PROCESS["pid"] if native else CONTAINER_ID)
                    [request] = requests
                    self.assertEqual(str(request.url), "http://192.0.2.11:8000/server_info")
                    self.assertEqual(request.headers["Authorization"], "Bearer secret")
                    binding = (
                        {"process": PROCESS}
                        if native
                        else {"image_id": IMAGE_ID, "container_id": CONTAINER_ID}
                    )
                    record = {
                        "server": captured["server"],
                        "process_start_time_seconds": 100.0,
                        "plan_sha256": digest(run / "launch.json"),
                        **binding,
                    }
                    self.assertEqual(path.read_text(), json.dumps(record, indent=2) + "\n")
                    field = "process" if native else "container_id"
                    record[field] = {**PROCESS, "start_ticks": 1} if native else "d" * 64
                    path.write_text(json.dumps(record))
                    with self.assertRaisesRegex(
                        ValueError, f"^Server evidence {field} differs from the checked serving"
                    ):
                        attestation.engine_document(run, run / "startup.log")


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
            inst.decode["r"] = Request("r", 128, phase=Phase.DECODE)
        donor, reason = router.scheduler.roles.planned_donor(Role.PREFILL, bypass_dwell=True)
        self.assertIsNone(donor)
        self.assertIn("idle", reason)
        # Pricing a split holds no engine; only a move does.
        self.assertEqual(router.scheduler.held_for_switch(), set())
        router.scheduler.roles.planned_donor(Role.PREFILL, bypass_dwell=True, hold=True)
        self.assertEqual(len(router.scheduler.held_for_switch()), 1)


class StreamRefusedFieldTests(unittest.IsolatedAsyncioTestCase):
    async def body(self, path: str, payload: dict) -> object:
        raw = json.dumps(payload).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
        router = SimpleNamespace(
            cfg=SimpleNamespace(serving=SimpleNamespace(max_request_bytes=1 << 20)),
            engines=SimpleNamespace(dialect=load("sglang").dialect),
        )
        return await app_module._completion_body(router, HTTPRequest(scope, receive))

    async def test_chat_refuses_fields_sglang_rejects_on_a_stream(self):
        for name in ("return_token_ids", "return_prompt_token_ids", "return_meta_info"):
            with self.subTest(field=name):
                response = await self.body("/v1/chat/completions", {"messages": [], name: True})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    json.loads(response.body)["error"]["message"],
                    f"{name} is not supported on /v1/chat/completions by sglang",
                )
        payload = {"prompt": "x", "return_token_ids": True}
        self.assertEqual(await self.body("/v1/completions", payload), payload)
