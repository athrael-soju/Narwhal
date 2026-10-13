import contextlib
import copy
import importlib
import io
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx

from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine.backend import EngineLauncher, cache_capture
from narwhal.deployment.launch_engine.plan import load, prepare
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.engines.kv_events import CacheEvent
from tests.deployment.fixtures import launcher_inputs
from tools.measurement import simulated_engine as sim


@dataclass(frozen=True)
class ContractFixtures:
    # A metrics scrape and the values the backend's mapping reads from it.
    metrics: str
    kv_capacity: int
    cache_block_tokens: int
    prefix_cache_hits: int
    transfer_totals: tuple[float, float] | None
    # Cache event batches as published, with the event type each one decodes to.
    kv_batches: list[tuple[bytes, list[type[CacheEvent] | None]]]
    malformed_kv_batch: bytes
    # A verified attestation payload and the limits the identity reader takes from it. Its
    # launch section is the producer launch that handoffs and role switches read.
    attestation: dict[str, Any]
    sequence_limit: int | None
    kv_lease: int | None
    memory_args: list[str]
    memory_fraction: str
    # The runtime section of a launch record.
    launch_runtime: dict[str, Any]
    # Stdout of the backend's runtime check probe for a launch plan.
    check_output: Callable[[dict], str]
    # Writes the evidence a started container's captures leave in its launch directory.
    attestation_evidence: Callable[[Path, dict, str], None]
    startup_log: str
    kv_event_args: list[str] = field(default_factory=list)
    no_kv_event_args: list[str] = field(default_factory=list)
    # Distributions an engine image reports to discovery, and the .env fields discovery reads.
    image_packages: dict[str, str] = field(default_factory=dict)
    discovery_settings: dict[str, str] = field(default_factory=dict)
    # Handoff body fields that take a new random value for each request.
    handoff_random_fields: tuple[str, ...] = ()


def fixtures(backend: str) -> ContractFixtures:
    return importlib.import_module(f"tests.backends.fixtures.{backend}").FIXTURES


CONTAINER_ID = "c" * 64
MODEL_CONFIG = {
    "architectures": ["FixtureForCausalLM"],
    "num_attention_heads": 8,
    "num_key_value_heads": 8,
    "hidden_size": 512,
    "num_hidden_layers": 4,
}
SHARED_DEVICE = {
    "group": "example-host:GPU-fixture",
    "gpu_uuid": "GPU-fixture",
    "device_allowance": 0.9,
}
# Docker labels, request IDs and probe prompt prefixes are random per run.
RANDOM_HEX = re.compile(r"\b[0-9a-f]{32}\b")
RANDOM_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def random_ids(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: random_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [random_ids(item) for item in value]
    if isinstance(value, str):
        return RANDOM_UUID.sub("<uuid>", RANDOM_HEX.sub("<hex>", value))
    return value


def launch_record(runtime: dict, shared: dict | None) -> dict:
    gpus = ["0"] if shared is not None else ["0", "1"]
    entry = {
        "accelerator": "synthetic-gpu",
        "gpu_ids": gpus,
        "tensor_parallel_size": len(gpus),
        "gpu_visibility_env": "CUDA_VISIBLE_DEVICES",
        "accelerator_devices": ["/dev/nvidiactl", *(f"/dev/nvidia{gpu}" for gpu in gpus)],
        "network_mode": "host",
        "transfer": {"transport": "ucx_tcp", "net_devices": "fabric0", "devices": []},
        "sources": dict.fromkeys(
            ("allocation", "devices", "transfer"), "synthetic approved launch record"
        ),
        "runtime": runtime,
        **({"shared_device": shared} if shared is not None else {}),
    }
    document = {
        "schema": "narwhal.engine-launch",
        "schema_version": 1,
        "engines": {"engine-1": entry},
    }
    return selected_launch(document, "engine-1", {})


def prepared(
    root: Path,
    launcher: EngineLauncher,
    fixture: ContractFixtures,
    *,
    native: bool,
    **environment: str,
) -> tuple[Path, dict, str]:
    """Prepare a two-GPU container launch, or a native launch on one shared GPU."""
    _, env = launcher_inputs(root)
    config = root / "model" / "config.json"
    config.write_text(json.dumps(MODEL_CONFIG))
    env["NARWHAL_MODEL_CONFIG_SHA256"] = digest(config)
    env[launcher.side_channel_port_env] = env.pop("NARWHAL_NIXL_SIDE_CHANNEL_PORT")
    capture = cache_capture(launcher)
    if capture is not None:
        env["NARWHAL_CACHE_CAPTURE_HOOK"] = str(capture.cache_hook)
        env["NARWHAL_CACHE_CAPTURE_HOOK_SHA256"] = digest(capture.cache_hook)
    runtime = copy.deepcopy(fixture.launch_runtime)
    shared = None
    if native:
        runtime["extra_args"] = [*runtime.get("extra_args", []), *fixture.memory_args]
        shared = {**SHARED_DEVICE, "gpu_memory_utilization": float(fixture.memory_fraction)}
        env.pop("NARWHAL_ENGINE_IMAGE")
        env["NARWHAL_MODEL_REVISION"] = "a" * 40
        env["NARWHAL_NODE_1_ATTESTATION_URL"] = "http://192.0.2.11:8010/v1/attestation"
    env.update(environment)
    Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(launch_record(runtime, shared)))
    run = root / "launch"
    with contextlib.redirect_stdout(io.StringIO()) as printed:
        prepare(run, env, backend="native" if native else "container")
    return run, load(run), printed.getvalue()


class Stages:
    """Answer the subprocesses a launch runs: Docker commands and the runtime check probe."""

    def __init__(self, plan: dict, output: str) -> None:
        self.plan = plan
        self.output = output
        self.calls: list[dict] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        environment = kwargs.get("env")
        self.calls.append(
            {
                # A probe script passed inline is recorded line by line.
                "command": [arg.splitlines() if "\n" in arg else arg for arg in command],
                "stage": kwargs.get("stage"),
                "log": str(kwargs.get("log")),
                **({} if environment is None else {"env": dict(sorted(environment.items()))}),
            }
        )
        stdout = self.output
        if command[:3] == ["docker", "image", "inspect"]:
            stdout = json.dumps([{"Id": self.plan["image"], "RepoDigests": []}])
        elif command[:2] in (["docker", "create"], ["docker", "start"]):
            stdout = CONTAINER_ID
        return subprocess.CompletedProcess(command, 0, stdout, "")


def running_container(image_id: str) -> contextlib.AbstractContextManager:
    inspected = {"Id": CONTAINER_ID, "Image": image_id, "State": {"Running": True}}
    return patch(
        "narwhal.deployment.attestation_contract.evidence.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, json.dumps(inspected), ""),
    )


class Engines:
    """Engines that answer each backend's routes as its simulated engine does.

    Each request is recorded. A rendezvous leg waits for its peer leg, and an engine serves
    only its live role, so a pair needs the backend's role switch first.
    """

    def __init__(self, backend: str, random_fields: tuple[str, ...]) -> None:
        self.protocol = sim.PROTOCOLS[backend]
        self.rooms = sim.Rooms(bootstrap_timeout_s=5.0)
        self.engines: dict[str, SimpleNamespace] = {}
        self.random_fields = random_fields
        self.calls: list[dict] = []
        self.answered: set[int] = set()
        self.values: dict[tuple[str, object], int] = {}

    def engine(self, host: str) -> SimpleNamespace:
        if host not in self.engines:
            self.engines[host] = SimpleNamespace(
                iid=host,
                port=8000,
                role="prefill",
                rooms=self.rooms,
                transfers=0,
                transfer_seconds=0.0,
            )
        return self.engines[host]

    def placeholder(self, name: str, value: object) -> str:
        number = self.values.setdefault((name, value), len(self.values) + 1)
        return f"<{name} {number}>"

    def recorded(self, request: httpx.Request, body: object) -> dict:
        if isinstance(body, dict):
            body = {
                key: self.placeholder(key, value) if key in self.random_fields else value
                for key, value in body.items()
            }
        headers = {
            name: request.headers[name]
            for name in ("authorization", "x-request-id")
            if name in request.headers
        }
        return random_ids(
            {
                "engine": request.url.host,
                "method": request.method,
                "path": request.url.path,
                "headers": headers,
                "body": body,
            }
        )

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        engine = self.engine(request.url.host)
        body = json.loads(request.content) if request.content else None
        index = len(self.calls)
        self.calls.append({**self.recorded(request, body), "answered": set(self.answered)})
        try:
            return await self.reply(engine, request, body)
        finally:
            self.answered.add(index)

    async def reply(
        self, engine: SimpleNamespace, request: httpx.Request, body: Any
    ) -> httpx.Response:
        route = self.protocol.route(engine, request.method, request.url.path, request.content)
        if route is None:
            refused = await self.protocol.admit(engine, body)
            if refused is None and body.get("stream"):
                refused = self.protocol.decode_error(body)
            route = refused
        if route is not None:
            return httpx.Response(
                route.status, content=route.body, headers={"content-type": route.content_type}
            )
        if body.get("stream"):
            chat = request.url.path.endswith("/chat/completions")
            ids = body.get("return_token_ids")
            frames = [
                {"index": 0, "delta": {"content": "Hi"}} if chat else {"index": 0, "text": "Hi"},
                {"index": 0, "delta": {}, "finish_reason": "length"}
                if chat
                else {"index": 0, "text": "", "finish_reason": "length"},
            ]
            if ids:
                frames[0]["token_ids"], frames[1]["token_ids"] = [7], []
            wire = "".join(
                "data: " + json.dumps({"id": "fixture", "choices": [frame]}) + "\n\n"
                for frame in frames
            )
            return httpx.Response(
                200, text=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
            )
        rid = self.protocol.request_id(request.headers, body) or "fixture"
        reply = {
            "id": "fixture",
            "choices": [{"index": 0, "text": "t", "finish_reason": "length"}],
            **self.protocol.prefill_fields(engine, rid, body, 3),
        }
        return httpx.Response(200, json=reply)

    def _take(self) -> list[dict]:
        calls, self.calls = self.calls, []
        self.answered.clear()
        return calls

    def take(self) -> list[dict]:
        return [strip(call) for call in self._take()]

    def handoff(self) -> dict:
        """The control calls, then each leg, and whether decode started after prefill ended."""
        calls = self._take()
        legs = [i for i, call in enumerate(calls) if call["path"].startswith("/v1/")]
        prefill = [i for i in legs if not (calls[i]["body"] or {}).get("stream")]
        decode = [i for i in legs if (calls[i]["body"] or {}).get("stream")]
        if len(prefill) != 1 or len(decode) != 1:
            raise AssertionError(f"expected one prefill and one decode leg: {calls}")
        sequential = prefill[0] in calls[decode[0]]["answered"]
        return {
            "control": [strip(call) for i, call in enumerate(calls) if i not in legs],
            "prefill": strip(calls[prefill[0]]),
            "decode": strip(calls[decode[0]]),
            "order": "prefill completes before decode" if sequential else "legs overlap",
        }


def strip(call: dict) -> dict:
    return {key: value for key, value in call.items() if key != "answered"}
