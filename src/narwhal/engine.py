"""The HTTP protocol for splitting one request across two stateless engines (Arrow §5.2).

Requires every vLLM engine to run the connector the fleet config names
(default `NixlConnector` with `kv_role: kv_both`), so any instance is
eligible for either phase.

Two calls to the same OpenAI-compatible endpoint, over one shared connection
pool:

1. Prefill: the client's body with `max_tokens` forced to 1, `stream` off,
   plus the configured connector's handshake (`connector.prefill_params`).
   Returns the handoff the connector read out of the response.
2. Decode: the client's body carrying that handoff, attached by the
   connector's own convention. The KV moves engine to engine over the
   transport; only the handle passes through this process.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .connector import KvConnector, NixlConnector
from .dialect import EngineDialect, VllmDialect


class EngineError(RuntimeError):
    """An engine refused or failed a leg. Carries the leg for the log line."""

    def __init__(self, leg: str, url: str, status: int, detail: str) -> None:
        super().__init__(f"{leg} leg against {url} failed ({status}): {detail[:240]}")
        self.leg = leg
        self.url = url
        self.status = status
        self.detail = detail


class EngineClient:
    """One shared connection pool over every engine in the fleet."""

    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        prefill_timeout_s: float = 120.0,
        read_timeout_s: float = 60.0,
        max_connections: int = 512,
        pool_timeout_s: float = 5.0,
        connect_timeout_s: float = 10.0,
        health_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        kv: KvConnector | None = None,
        dialect: EngineDialect | None = None,
    ) -> None:
        # `transport` is the seam for testing the split against a stub engine.
        # `read` bounds the gap between received chunks, so a stalled stream is
        # caught in seconds while a healthy one runs as long as it needs.
        self.kv = kv or NixlConnector()
        self.dialect = dialect or VllmDialect()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_s, connect=connect_timeout_s, read=read_timeout_s, pool=pool_timeout_s
            ),
            # Half the pool kept warm: enough that steady traffic never pays
            # a handshake, without holding every slot open on an idle fleet.
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max(1, max_connections // 2),
            ),
            transport=transport,
        )
        self._prefill_timeout = prefill_timeout_s
        self._health_timeout = health_timeout_s

    async def aclose(self) -> None:
        """Close the pooled client and every connection it holds."""
        await self._client.aclose()

    # -- readiness ------------------------------------------------------

    async def healthy(self, url: str) -> bool:
        """Whether `url` answers the dialect's health route with 200 in the budget."""
        try:
            r = await self._client.get(
                f"{url}{self.dialect.health_path}", timeout=self._health_timeout
            )
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def token_count(self, url: str, body: dict[str, Any], timeout_s: float) -> int | None:
        """Exact input length from the engine's own tokenizer.

        Worth the round trip: prefill cost is quadratic in this number (Arrow §3.1).
        The budget is the caller's, because this wait is charged to a request
        the router has not placed yet. A dialect with no exact-count route
        answers None outright, and the caller falls back to its estimate.
        """
        if self.dialect.tokenize_path is None:
            return None
        try:
            r = await self._client.post(
                f"{url}{self.dialect.tokenize_path}",
                json=self.dialect.tokenize_request(body.get("model"), body),
                timeout=timeout_s,
            )
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            return self.dialect.tokenize_response(r.json())
        except ValueError:
            return None

    # -- the two legs ---------------------------------------------------

    async def prefill(
        self, url: str, endpoint: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Run the prefill leg and return the handoff for the decode leg."""
        leg = {
            **body,
            "max_tokens": 1,
            "stream": False,
            **self.kv.prefill_params(),
        }
        for name in self.dialect.prefill_incompatible:
            leg.pop(name, None)

        r = await self._client.post(
            f"{url}{endpoint}", json=leg, headers=headers, timeout=self._prefill_timeout
        )
        if r.status_code != 200:
            raise EngineError("prefill", url, r.status_code, r.text)

        params = self.kv.extract(r.json())
        if not params:
            raise EngineError(
                "prefill",
                url,
                200,
                f"no handoff the {self.kv.name} connector can read in the prefill "
                f"response; this build is not running {type(self.kv).__name__}, or is "
                "running it with a consumer-only role",
            )
        return params

    async def decode(
        self,
        url: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        kv_params: dict[str, Any] | None,
        first_token_timeout_s: float | None = None,
    ) -> AsyncIterator[str]:
        """Stream the decode leg, yielding raw SSE lines.

        Always streamed, whatever the client asked for, because the monitor is
        defined on the token stream (Arrow §5.2); the server reassembles if needed.
        `kv_params` is None when both legs landed on the same instance, which
        needs no transfer.

        `first_token_timeout_s` bounds the wait for the first line only. Arrow §4.3
        splits the stream there: `q2` and `q3` precede the first token and are
        "highly unpredictable", while `p_j` after it comes from the profiled
        decode curve. So the two halves take different deadlines.
        """
        leg = {**body, "stream": True}
        if kv_params:
            self.kv.attach(leg, kv_params)
        else:
            leg.pop(self.kv.param_key, None)

        async with self._client.stream("POST", f"{url}{endpoint}", json=leg, headers=headers) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")
                raise EngineError("decode", url, r.status_code, detail)
            lines = r.aiter_lines()
            first = True
            while True:
                try:
                    if first and first_token_timeout_s:
                        line = await asyncio.wait_for(anext(lines), timeout=first_token_timeout_s)
                    else:
                        line = await anext(lines)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise EngineError(
                        "decode",
                        url,
                        504,
                        f"no first token within {first_token_timeout_s:g}s",
                    ) from exc
                if line:
                    first = False
                    yield line


def sse_text(line: str) -> str:
    """The generated text one SSE line carries, for opt-in payload capture."""
    if not line.startswith("data:"):
        return ""
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        obj = json.loads(payload)
    except ValueError:
        return ""
    parts = []
    for choice in obj.get("choices", []) or []:
        text = choice.get("text")
        if text is None:
            text = (choice.get("delta") or {}).get("content")
        if text:
            parts.append(text)
    return "".join(parts)


def sse_token_count(line: str) -> int:
    """Output tokens carried by one SSE line: one generation step per choice."""
    if not line.startswith("data:"):
        return 0
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return 0
    try:
        obj = json.loads(payload)
    except ValueError:
        return 0
    n = 0
    for choice in obj.get("choices", []) or []:
        text = choice.get("text")
        if text is None:
            text = (choice.get("delta") or {}).get("content")
        if text:
            n += 1
    return n
