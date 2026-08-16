"""The engine dialect seam: the registry, vLLM's choices, degradations."""

from __future__ import annotations

import pytest

from narwhal.dialect import EngineDialect, VllmDialect, lookup


class _OpenaiOnlyDialect(EngineDialect):
    """A build with plain OpenAI routes and no exact-count endpoint."""

    name = "openai-only"
    tokenize_path = None
    prefill_incompatible = ()

    def tokenize_request(self, model, body):
        raise AssertionError("no route: never called")

    def tokenize_response(self, payload):
        raise AssertionError("no route: never called")

    def decode_probe_extras(self, tokens):
        return {}


def test_the_registry_names_the_known_dialects():
    assert isinstance(lookup("vllm"), VllmDialect)


def test_an_unknown_dialect_is_named_back():
    with pytest.raises(ValueError, match=r"unknown dialect 'sglangyet'.*vllm"):
        lookup("sglangyet")


def test_vllms_tokenize_round_trip():
    d = VllmDialect()
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    payload = d.tokenize_request("m", body)
    assert payload["messages"] == body["messages"], "chat bodies count as messages"
    assert "prompt" not in payload
    assert d.tokenize_request("m", {"prompt": "p"}) == {"model": "m", "prompt": "p"}
    assert d.tokenize_response({"count": 7}) == 7
    assert d.tokenize_response({"tokens": [1, 2, 3]}) is None
    assert d.tokenize_response({"count": "nine"}) is None


def test_vllm_holds_decode_with_min_tokens_and_ignore_eos():
    assert VllmDialect().decode_probe_extras(8) == {"min_tokens": 8, "ignore_eos": True}


def test_the_route_less_dialect_carries_no_tokenize_path():
    d = _OpenaiOnlyDialect()
    assert d.tokenize_path is None
    assert d.prefill_incompatible == ()


async def test_token_count_degrades_without_any_request():
    """No route means no probe: the caller takes its character estimate."""
    import httpx

    from narwhal.engine import EngineClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url.path}")

    c = EngineClient(transport=httpx.MockTransport(handler), dialect=_OpenaiOnlyDialect())
    assert await c.token_count("http://e0", {"prompt": "hello"}, 1.0) is None
    await c.aclose()


async def test_prefill_drops_what_the_dialect_names_incompatible():
    import httpx

    from narwhal.engine import EngineClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"kv_transfer_params": {"k": 1}}]})

    body = {"prompt": "x", "min_tokens": 9, "n": 4, "stream_options": {}, "best_of": 2}

    c = EngineClient(transport=httpx.MockTransport(handler))
    await c.prefill("http://e0", "/v1/completions", dict(body), {})
    for name in ("stream_options", "min_tokens", "n", "best_of"):
        assert name not in seen["body"], f"vLLM rejects {name} on a one-token prefill"

    c = EngineClient(transport=httpx.MockTransport(handler), dialect=_OpenaiOnlyDialect())
    await c.prefill("http://e0", "/v1/completions", dict(body), {})
    assert seen["body"]["min_tokens"] == 9, "an OpenAI-clean build carries the client's body"


async def test_the_health_route_is_the_dialects():
    import httpx

    from narwhal.engine import EngineClient

    hit = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit.append(request.url.path)
        return httpx.Response(200)

    c = EngineClient(transport=httpx.MockTransport(handler), dialect=_OpenaiOnlyDialect())
    assert await c.healthy("http://e0") is True
    assert hit == ["/health"]
    await c.aclose()


async def test_make_prompt_falls_back_to_the_character_ratio():
    """Without the route the profiler sizes by chars and says what the x is."""
    import httpx

    from narwhal import probe

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text, n = await probe.make_prompt(
            client, "http://e0", "m", 100, _OpenaiOnlyDialect(), chars_per_token=4.0
        )
    assert len(text) == 400
    assert n == 100


def test_a_config_carries_and_validates_the_dialect(tmp_path):
    import json

    from narwhal.config import FleetConfig

    path = tmp_path / "fleet.json"
    path.write_text(
        json.dumps(
            {
                "model": "m",
                "engines": [{"iid": "a", "url": "http://h:1"}],
                "slo": {"ttft_s": 10, "tpot_s": 0.125},
                "dialect": "vllm",
            }
        )
    )
    cfg = FleetConfig.load(path)
    assert cfg.dialect == "vllm"
    out = tmp_path / "saved.json"
    cfg.save(out)
    assert FleetConfig.load(out).dialect == "vllm", "the name survives a round trip"

    path.write_text(
        json.dumps(
            {
                "model": "m",
                "engines": [{"iid": "a", "url": "http://h:1"}],
                "slo": {"ttft_s": 10, "tpot_s": 0.125},
                "dialect": "madeup",
            }
        )
    )
    with pytest.raises(ValueError, match="unknown dialect"):
        FleetConfig.load(path)
