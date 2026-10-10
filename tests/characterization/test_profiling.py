import asyncio
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import FleetConfig
from narwhal.engines.client import EngineClient
from narwhal.engines.dialect import lookup
from narwhal.profiling import calibration
from narwhal.profiling.probe.decode import probe_decode
from narwhal.profiling.probe.engine import (
    cache_block_tokens,
    count_tokens,
    engine_context_limit,
    kv_capacity,
    parse_cache_block_tokens,
    parse_kv_capacity,
    parse_prefix_cache_hits,
    prefix_cache_hits,
)
from narwhal.profiling.probe.neighbours import ColocatedWorkload, NeighbourLoad
from narwhal.profiling.probe.prefill import probe_prefill
from narwhal.profiling.probe.sweep import Sweep
from narwhal.profiling.probe.warm import probe_cached_prefill
from narwhal.types import Role
from tests.characterization.golden import assert_golden
from tests.characterization.vllm_fake import ENGINES, FakeVllm, routed, untimed
from tests.fixtures import ROOT
from tests.wire import engine_transports

E0 = "http://engine-0.invalid:8000"
E3 = "http://engine-3.invalid:8000"


def distinct(requests):
    seen = []
    for request in requests:
        shape = json.loads(re.sub(r" #[0-9]+>", ">", json.dumps(request)))
        if shape not in seen:
            seen.append(shape)
    return seen


class ProfilingCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = FakeVllm(ENGINES)
        self.dialect = lookup("vllm")
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def client(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.engine))
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_engine_metrics_and_context_limit(self):
        client = self.client()
        self.engine.prefix_hits["e0"] = 48
        read = {
            "kv_capacity": await kv_capacity(client, E0),
            "cache_block_tokens": await cache_block_tokens(client, E0),
            "prefix_cache_hits": await prefix_cache_hits(client, E0),
            "context_limit": await engine_context_limit(client, E0, "test-model", self.dialect),
            "token_count": await count_tokens(client, E0, "test-model", "a b c", self.dialect),
        }
        texts = {
            "ranks": (
                'vllm:kv_cache_info{engine="0",kv_cache_size_tokens="1000"} 1\n'
                'vllm:kv_cache_info{engine="1",kv_cache_size_tokens="900.0"} 1\n'
                'vllm:cache_config_info{block_size="16",engine="0"} 1\n'
                'vllm:cache_config_info{block_size="16",engine="1"} 1\n'
                'vllm:prefix_cache_hits_total{engine="0",model_name="m"} 12.0\n'
                'vllm:prefix_cache_hits_created{engine="0",model_name="m"} 1.7e9\n'
                'vllm:prefix_cache_hits_total{engine="1",model_name="m"} 30.0\n'
            ),
            "mixed_blocks": (
                'vllm:cache_config_info{block_size="16",engine="0"} 1\n'
                'vllm:cache_config_info{block_size="32",engine="1"} 1\n'
            ),
            "unlabelled": "vllm:prefix_cache_hits_total 5\n",
            "absent": "vllm:num_requests_running 0\n",
        }
        parsed = {
            name: {
                "kv_capacity": parse_kv_capacity(text),
                "cache_block_tokens": parse_cache_block_tokens(text),
                "prefix_cache_hits": parse_prefix_cache_hits(text),
            }
            for name, text in texts.items()
        }
        assert_golden(
            self,
            "profiling_engine_metrics",
            {"read": read, "parsed": parsed, "requests": self.engine.requests},
        )

    async def test_probe_requests(self):
        client = self.client()
        probes = {}
        await probe_prefill(client, E0, "test-model", (32, 48), 2, self.dialect)
        probes["prefill"] = self.engine.requests
        self.engine.requests, self.engine.salts = [], {}
        await probe_decode(client, E0, "test-model", (1, 2), 8, self.dialect, input_lens=(32,))
        probes["decode"] = distinct(self.engine.requests)
        self.engine.requests, self.engine.salts = [], {}
        samples, stopped = await probe_cached_prefill(
            client,
            E0,
            "test-model",
            Sweep(cached_prefix_lens=(64,), cached_suffix_lens=(16,), cached_repeats=1),
            self.dialect,
            max_model_len=8192,
            block_tokens=16,
        )
        probes["warm"] = self.engine.requests
        self.engine.requests, self.engine.salts = [], {}
        load = NeighbourLoad(
            client,
            [("e0", E0, Role.PREFILL), ("e3", E3, Role.DECODE)],
            "test-model",
            self.dialect,
            3.8,
            ColocatedWorkload(100.0, 100.0, 32, 32, 4),
        )
        await load.start()
        await asyncio.sleep(0.06)
        await load.stop()
        probes["neighbours"] = distinct(self.engine.requests)
        warm = [
            {key: sample[key] for key in ("state", "prefix_tokens", "suffix_tokens")}
            for sample in samples
        ]
        assert_golden(
            self,
            "profiling_probe_requests",
            {"probes": probes, "warm_samples": warm, "warm_stopped": stopped},
        )

    async def test_calibration_requests_and_document(self):
        cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
        cfg.engines = [cfg.engines[0], cfg.engines[3]]
        cfg.first_token_timeout_s = 0.01
        self.engine.contract = cfg.engine_contract
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "calibration.json"
            with (
                patch.object(
                    calibration,
                    "EngineClient",
                    side_effect=lambda **kwargs: EngineClient(
                        **kwargs, **engine_transports(self.engine)
                    ),
                ),
                patch("httpx.AsyncClient", routed(self.engine)),
            ):
                code = await calibration.calibrate(
                    cfg,
                    input_tokens=(32,),
                    samples_per_group=1,
                    observation_timeout_s=1.0,
                    out=out,
                )
            document = json.loads(out.read_text())
        for request in self.engine.requests:
            request_id = request["headers"].get("x-request-id")
            if request_id is not None:
                request["headers"]["x-request-id"] = re.sub(r"[0-9a-f]{32}", "<id>", request_id)
        assert_golden(
            self,
            "profiling_calibration",
            {"exit_code": code, "document": untimed(document), "requests": self.engine.requests},
        )
