"""The connector seam: the wire today stays the wire, and a second
transport joins by instance, not by patch."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from narwhal.config import FleetConfig
from narwhal.connector import KvConnector, NixlConnector, lookup
from narwhal.engine import EngineClient


def _server(capture: dict, prefill_payload: dict, kv=None) -> EngineClient:
    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        capture["body"] = body
        if body.get("max_tokens") == 1 and not body.get("stream"):
            return httpx.Response(200, json=prefill_payload)
        stream = "\n".join(
            "data: " + json.dumps({"choices": [{"index": 0, "text": f"t{k}"}]})
            for k in range(int(body.get("max_tokens", 1)))
        )
        return httpx.Response(200, text=stream + "\ndata: [DONE]\n")

    return EngineClient(transport=httpx.MockTransport(handle), kv=kv)


NIXL_HANDOFF = {
    "choices": [
        {
            "index": 0,
            "text": "",
            "kv_transfer_params": {"remote_engine_id": "n3", "remote_block_ids": [1, 2, 3]},
        }
    ]
}


async def test_the_nixl_wire_is_what_it_always_was():
    capture: dict = {}
    engines = _server(capture, NIXL_HANDOFF)
    params = await engines.prefill("http://e0", "/v1/completions", {"prompt": "hi"}, {})
    await engines.aclose()
    assert capture["body"]["kv_transfer_params"] == {"do_remote_decode": True}
    assert capture["body"]["max_tokens"] == 1
    assert capture["body"]["stream"] is False
    assert params == {"remote_engine_id": "n3", "remote_block_ids": [1, 2, 3]}


async def test_the_decode_leg_carries_the_handoff_verbatim():
    capture: dict = {}
    engines = _server(capture, NIXL_HANDOFF)
    handoff = {"remote_engine_id": "n3", "remote_block_ids": [1, 2, 3]}
    async for _ in engines.decode(
        "http://e1", "/v1/completions", {"prompt": "hi", "max_tokens": 2}, {}, handoff
    ):
        pass
    await engines.aclose()
    assert capture["body"]["kv_transfer_params"] == handoff


async def test_an_uncrossed_decode_drops_any_stale_handoff_key():
    capture: dict = {}
    engines = _server(capture, NIXL_HANDOFF)
    async for _ in engines.decode(
        "http://e1",
        "/v1/completions",
        {"prompt": "hi", "max_tokens": 1, "kv_transfer_params": {"stale": True}},
        {},
        None,
    ):
        pass
    await engines.aclose()
    assert "kv_transfer_params" not in capture["body"]


class _EchoConnector(KvConnector):
    """A second transport that proves the seam halves by renaming the key."""

    name = "echo"
    param_key = "kv_echo"

    def prefill_params(self):
        return {"kv_echo": {"offer": True}}

    def extract(self, payload):
        return dict(payload.get("kv_echo") or {})

    def attach(self, body, params):
        body["kv_echo"] = params


async def test_the_seam_swaps_the_wire_with_the_instance():
    capture: dict = {}
    engines = _server(capture, {"kv_echo": {"today": "node7"}}, kv=_EchoConnector())
    params = await engines.prefill("http://e0", "/v1/completions", {"prompt": "hi"}, {})
    assert capture["body"]["kv_echo"] == {"offer": True}
    assert "kv_transfer_params" not in capture["body"]
    assert params == {"today": "node7"}
    async for _ in engines.decode(
        "http://e1", "/v1/completions", {"prompt": "hi", "max_tokens": 2}, {}, params
    ):
        pass
    await engines.aclose()
    assert capture["body"]["kv_echo"] == {"today": "node7"}


def test_lookup_names_the_known_connector():
    assert isinstance(lookup("nixl"), NixlConnector)
    with pytest.raises(ValueError, match=re.escape("unknown connector 's3': known ones are nixl")):
        lookup("s3")


def test_config_roundtrip_and_refusal(tmp_path: Path):
    good = {
        "model": "m",
        "engines": [{"iid": "i0", "url": "http://i0"}],
        "slo": {"ttft_s": 3.0, "tpot_s": 0.06},
        "connector": "nixl",
    }
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(good))
    cfg = FleetConfig.load(path)
    assert cfg.connector == "nixl"
    out = tmp_path / "rt.json"
    cfg.save(out)
    assert json.loads(out.read_text())["connector"] == "nixl"

    bad = dict(good, connector="s3")
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="unknown connector 's3'"):
        FleetConfig.load(path)
