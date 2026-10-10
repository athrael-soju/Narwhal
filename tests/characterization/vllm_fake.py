import asyncio
import hashlib
import json
import re

import httpx

from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation

VERSION = "1.0.0"
PROCESS_START = 100.0
BLOCK_TOKENS = 16
CONTEXT_LIMIT = 8192
KV_CAPACITY = 4096
# Request headers that carry Narwhal's per-leg identity or the engine credential.
RECORDED_HEADERS = ("authorization", "x-request-id")
UUID_PREFIX = re.compile(r"^[0-9a-f]{32} ")
ENGINES = {"engine-0.invalid": "e0", "engine-3.invalid": "e3"}
# Measured durations and wall-clock times vary per run.
TIMED = {
    "captured_at_unix",
    "duration_s",
    "first_token_seconds",
    "prefill_seconds",
    "p99_seconds",
    "maximum_seconds",
    "candidate_deadline_s",
    "decode_seconds",
}


def prompt_label(prompt):
    prefixed = bool(UUID_PREFIX.match(prompt))
    text = UUID_PREFIX.sub("", prompt)
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    return f"<{'uuid ' if prefixed else ''}{len(text.split())} words sha256:{digest}>"


def normalized(value, salts):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "cache_salt" and isinstance(item, str):
                number = salts.setdefault(item, len(salts) + 1)
                out[key] = f"<salt {len(item)} chars #{number}>"
            elif key == "prompt" and isinstance(item, str):
                out[key] = prompt_label(item)
            else:
                out[key] = normalized(item, salts)
        return out
    if isinstance(value, list):
        return [normalized(item, salts) for item in value]
    return value


class TokenStream(httpx.AsyncByteStream):
    def __init__(self, tokens):
        self.tokens = tokens

    async def __aiter__(self):
        for index in range(self.tokens):
            await asyncio.sleep(0.001)
            finish = "length" if index == self.tokens - 1 else None
            choice = {"index": 0, "text": "x", "token_ids": [index + 1], "finish_reason": finish}
            yield f"data: {json.dumps({'choices': [choice]})}\n\n".encode()
        yield b"data: [DONE]\n\n"


class FakeVllm:
    def __init__(self, names, contract=None):
        self.names = names
        self.contract = contract
        self.requests = []
        self.salts = {}
        self.prefix_hits = dict.fromkeys(names.values(), 0)
        self.transfers = dict.fromkeys(names.values(), 0)
        self.cache = {iid: {} for iid in names.values()}

    def iid(self, request):
        return self.names[request.url.host]

    def record(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "engine": self.iid(request),
                "method": request.method,
                "path": request.url.path,
                "body": normalized(body, self.salts),
                "headers": {
                    name: request.headers[name]
                    for name in RECORDED_HEADERS
                    if name in request.headers
                },
            }
        )
        return body

    def metrics(self, iid):
        return (
            f"process_start_time_seconds {PROCESS_START}\n"
            f'vllm:cache_config_info{{block_size="{BLOCK_TOKENS}",'
            f'enable_prefix_caching="True",engine="0"}} 1.0\n'
            f'vllm:kv_cache_info{{engine="0",kv_cache_size_tokens="{KV_CAPACITY}"}} 1.0\n'
            f'vllm:prefix_cache_hits_total{{engine="0",model_name="test-model"}} '
            f"{float(self.prefix_hits[iid])}\n"
            f'vllm:nixl_xfer_time_seconds_count{{engine="0"}} {float(self.transfers[iid])}\n'
            f'vllm:nixl_xfer_time_seconds_sum{{engine="0"}} {0.01 * self.transfers[iid]}\n'
        )

    def cached_tokens(self, iid, salt, words):
        best = 0
        for earlier in self.cache[iid].get(salt, []):
            shared = 0
            for left, right in zip(earlier, words, strict=False):
                if left != right:
                    break
                shared += 1
            best = max(best, shared)
        # vLLM recomputes the last prompt token.
        best = min(best, len(words) - 1)
        self.cache[iid].setdefault(salt, []).append(words)
        return best // BLOCK_TOKENS * BLOCK_TOKENS

    def completion(self, iid, body):
        words = str(body.get("prompt", "")).split()
        hits = self.cached_tokens(iid, body.get("cache_salt"), words)
        self.prefix_hits[iid] += hits
        params = body.get("kv_transfer_params") or {}
        if body.get("stream"):
            if "remote_engine_id" in params:
                self.transfers[iid] += 1
            return httpx.Response(200, stream=TokenStream(body["max_tokens"]))
        response = {
            "id": "cmpl-fixture",
            "choices": [{"index": 0, "text": "x", "finish_reason": "length"}],
            "usage": {"prompt_tokens": len(words), "completion_tokens": body["max_tokens"]},
        }
        if params.get("do_remote_decode"):
            index = sorted(self.prefix_hits).index(iid)
            response["kv_transfer_params"] = {
                "do_remote_prefill": True,
                "do_remote_decode": False,
                "remote_block_ids": [[0, 1]],
                "remote_engine_id": f"{iid}-engine",
                "remote_host": "127.0.0.1",
                "remote_port": 5600 + index,
                "tp_size": 1,
            }
        return httpx.Response(200, json=response)

    def attestation(self):
        document = AttestationDocument(
            self.contract, dict.fromkeys(self.contract.fields(), "fixture")
        )
        return make_attestation(document, EngineIdentity(VERSION, PROCESS_START))

    def __call__(self, request):
        iid = self.iid(request)
        body = self.record(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200)
        if path == "/version":
            return httpx.Response(200, json={"version": VERSION})
        if path == "/metrics":
            return httpx.Response(200, text=self.metrics(iid))
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        if path == "/v1/attestation":
            return httpx.Response(200, json=self.attestation())
        if path == "/tokenize":
            count = len(str(body.get("prompt", "")).split())
            return httpx.Response(
                200,
                json={
                    "count": count,
                    "max_model_len": CONTEXT_LIMIT,
                    "tokens": list(range(count)),
                },
            )
        if path == "/v1/completions":
            return self.completion(iid, body)
        return httpx.Response(404)


def untimed(value):
    if isinstance(value, dict):
        return {
            key: "<seconds>" if key in TIMED and item is not None else untimed(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [untimed(item) for item in value]
    return value


def routed(handler):
    base = httpx.AsyncClient

    class Routed(base):
        def __init__(self, *args, transport=None, **kwargs):
            super().__init__(*args, transport=transport or httpx.MockTransport(handler), **kwargs)

    return Routed
