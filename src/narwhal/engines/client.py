"""HTTP client for split prefill and decode requests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from ..types import (
    LEG_CONNECTION,
    LEG_INFERENCE_STATUS,
    LEG_KV_HANDOFF,
    LEG_OVERLOAD,
    LEG_STREAM,
    LEG_TIMEOUT,
)
from .connector import KvConnector, NixlConnector, PrefillResult
from .dialect import EngineDialect, VllmDialect
from .stream import sse_error, sse_token_bearing


class EngineError(RuntimeError):
    """Report an engine failure with the failed leg and endpoint."""

    def __init__(self, leg: str, url: str, status: int, detail: str) -> None:
        super().__init__(f"{leg} leg against {url} failed ({status}): {detail[:240]}")
        self.leg = leg
        self.url = url
        self.status = status
        self.detail = detail


class _GapBoundStream(httpx.AsyncByteStream):
    """Apply the current phase's timeout to raw transport reads."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream | httpx.SyncByteStream,
        timeout: Callable[[], float | None],
    ) -> None:
        if not isinstance(stream, httpx.AsyncByteStream):
            raise TypeError("decode requires an asynchronous response stream")
        self.stream = stream
        self.timeout = timeout

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = self.stream.__aiter__()
        while True:
            try:
                async with asyncio.timeout(self.timeout()):
                    chunk = await anext(chunks)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise httpx.ReadTimeout("decode transport gap exceeded its deadline") from exc
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


# Detail markers opening the EngineError detail of failure shapes the
# breaker classifies explicitly. The classifier below and the router's
# controller risk note key on these prefixes, so the raises use them too.
# (Names avoid the substring that trips the hardcoded-secret lint.)
FIRST_OUTPUT_DETAIL = "no first token within"
STREAM_SILENCE_DETAIL = "engine went silent between tokens"
STREAM_UNTERMINATED_DETAIL = "stream ended before the [DONE] terminator"
STREAM_EMPTY_DETAIL = "stream ended with [DONE] before any token arrived"
NO_HANDOFF_DETAIL = "no handoff"

# The probe runs the plain completion route every dialect serves.
_PROBE_ENDPOINT = "/v1/completions"
_PROBE_PROMPT = "breaker verification"


def leg_failure_class(exc: BaseException) -> str | None:
    """Return the breaker failure class, or None for local pool timeouts
    and non-overload 4xx responses.
    """
    if isinstance(exc, httpx.PoolTimeout):
        return None
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return LEG_CONNECTION
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return LEG_TIMEOUT
    if isinstance(exc, EngineError):
        if 400 <= exc.status < 500:
            return LEG_OVERLOAD if exc.status in (408, 429) else None
        detail = exc.detail
        if detail.startswith(FIRST_OUTPUT_DETAIL) or detail.startswith(STREAM_SILENCE_DETAIL):
            # Stream progress requires inference verification.
            return LEG_STREAM
        if exc.status == 200 and detail.startswith(NO_HANDOFF_DETAIL):
            return LEG_KV_HANDOFF
        if detail.startswith(STREAM_UNTERMINATED_DETAIL) or detail.startswith(STREAM_EMPTY_DETAIL):
            return LEG_STREAM
        return LEG_INFERENCE_STATUS
    # Unclassified failures require health verification.
    return LEG_TIMEOUT


@dataclass(frozen=True)
class ProbeLeg:
    """Result of one inference-probe leg.

    `failed` is the failure class or None; `inconclusive` marks a local
    control-pool timeout.
    """

    failed: str | None = None
    inconclusive: bool = False


@dataclass(frozen=True)
class InferenceProbe:
    """Outcome of the two-leg inference verification against one engine."""

    prefill: ProbeLeg
    decode: ProbeLeg


def _status_class(status: int) -> str:
    """Breaker class for a probe leg's non-200 answer."""
    return LEG_OVERLOAD if status in (408, 429) else LEG_INFERENCE_STATUS


class EngineClient:
    """Pooled HTTP client with separate request and control connections.

    Prefill, decode and tokenization share the data pool. Health and recovery
    probes use the reserved control pool.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        prefill_timeout_s: float = 120.0,
        read_timeout_s: float = 60.0,
        max_connections: int = 512,
        control_connections: int = 2,
        pool_timeout_s: float = 5.0,
        connect_timeout_s: float = 10.0,
        health_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        kv: KvConnector | None = None,
        dialect: EngineDialect | None = None,
        model: str = "",
        engine_api_key: str | None = None,
    ) -> None:
        # The read timeout bounds gaps between transport chunks. The decode
        # leg adds a first-token deadline; request lifecycle owns the total deadline.
        self.kv = kv or NixlConnector()
        self.dialect = dialect or VllmDialect()
        # The served model name used in inference-probe requests.
        self.model = model
        # 0 disables the chunk-gap bound.
        self._read_timeout = read_timeout_s if read_timeout_s > 0 else None
        # An unvalidated config passes 0 to mean derive; a standalone caller
        # has no fleet context, so it keeps the small explicit budget.
        control_connections = control_connections if control_connections > 0 else 2
        self.control_connections = control_connections
        self._data = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_s, connect=connect_timeout_s, read=self._read_timeout, pool=pool_timeout_s
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max(1, max_connections // 2),
            ),
            transport=transport,
        )
        self._control = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_s, connect=connect_timeout_s, read=self._read_timeout, pool=pool_timeout_s
            ),
            limits=httpx.Limits(
                max_connections=control_connections,
                max_keepalive_connections=max(1, control_connections // 2),
            ),
            transport=transport,
        )
        self._prefill_timeout = prefill_timeout_s
        self._health_timeout = health_timeout_s
        # Attach the engine credential to each leg and probe. A caller-supplied
        # authorization header takes precedence.
        self._engine_api_key = engine_api_key

    def _auth(self, headers: dict[str, str] | None) -> dict[str, str]:
        out = dict(headers) if headers else {}
        if self._engine_api_key is not None and "authorization" not in (
            name.lower() for name in out
        ):
            out["authorization"] = f"Bearer {self._engine_api_key}"
        return out

    async def aclose(self) -> None:
        """Close all pooled connections."""
        await self._data.aclose()
        await self._control.aclose()

    async def healthy(self, url: str) -> bool | None:
        """Probe the engine through the control pool.

        `True` is a passing health check. `False` is an endpoint-side
        failure: connect or read timeout, connect error, or a non-200
        answer. `None` means the local control pool was exhausted, which is
        inconclusive and says nothing about the engine.
        """
        try:
            r = await self._control.get(
                f"{url}{self.dialect.health_path}",
                timeout=self._health_timeout,
                headers=self._auth(None),
            )
        except httpx.PoolTimeout:
            return None
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def token_count(self, url: str, body: dict[str, Any], timeout_s: float) -> int | None:
        """Ask the engine for the exact input length within `timeout_s`.

        Returns None when the dialect lacks a tokenizer route or the request fails.
        """
        if self.dialect.tokenize_path is None:
            return None
        try:
            r = await self._data.post(
                f"{url}{self.dialect.tokenize_path}",
                headers=self._auth(None),
                json=self.dialect.tokenize_request(body.get("model"), body),
                timeout=timeout_s,
            )
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        return self.dialect.tokenize_response(payload)

    def _prefill_leg(self, body: dict[str, Any]) -> dict[str, Any]:
        """Build the forced one-token prefill leg out of a request body."""
        leg = {
            **body,
            "max_tokens": 1,
            "stream": False,
            **self.kv.prefill_params(),
        }
        for name in self.dialect.prefill_incompatible:
            leg.pop(name, None)
        return leg

    async def prefill(
        self, url: str, endpoint: str, body: dict[str, Any], headers: dict[str, str]
    ) -> PrefillResult:
        """Run prefill and bind the handoff to its producer and request ID."""
        leg = self._prefill_leg(body)

        r = await self._data.post(
            f"{url}{endpoint}",
            json=leg,
            headers=self._auth(headers),
            timeout=self._prefill_timeout,
        )
        if r.status_code != 200:
            raise EngineError("prefill", url, r.status_code, r.text)

        try:
            return self.kv.prefill_result(
                r.json(),
                url=url,
                endpoint=endpoint,
                request_id=next(
                    (v for k, v in headers.items() if k.lower() == "x-request-id"), None
                ),
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise EngineError(
                "prefill",
                url,
                200,
                f"{NO_HANDOFF_DETAIL}: invalid {self.kv.name} descriptor: {exc}",
            ) from exc

    async def decode(
        self,
        url: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        kv_params: PrefillResult | None,
        first_token_timeout_s: float | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream raw SSE lines from the decode leg.

        The first-token deadline starts before opening the HTTP stream.
        After the first generated token, the read timeout bounds gaps between
        transport chunks. Same-engine decode strips transfer parameters.
        """
        try:
            leg = self.kv.decode_body(body, kv_params, url=url, endpoint=endpoint)
        except (ValueError, TypeError) as exc:
            raise EngineError("decode", url, 502, f"invalid continuation: {exc}") from exc

        budget_s = first_token_timeout_s or 0.0
        deadline = asyncio.get_running_loop().time() + budget_s if budget_s > 0 else None
        first = True  # no generated token observed yet
        # HTTPX applies its read timeout to headers and pre-token body reads
        # too. Use the first-output budget there and raw-chunk gaps afterward.
        timeouts = self._data.timeout.as_dict()
        if deadline is not None:
            timeouts["read"] = None
        async with AsyncExitStack() as stack:
            opening = stack.enter_async_context(
                self._data.stream(
                    "POST",
                    f"{url}{endpoint}",
                    json=leg,
                    headers=self._auth(headers),
                    timeout=httpx.Timeout(**timeouts),
                )
            )
            try:
                remaining = (
                    max(0.0, deadline - asyncio.get_running_loop().time()) if deadline else None
                )
                r = await asyncio.wait_for(opening, timeout=remaining)
            except TimeoutError as exc:
                raise EngineError("decode", url, 504, _first_token_detail(budget_s, 0)) from exc
            if deadline is not None:
                r.stream = _GapBoundStream(r.stream, lambda: None if first else self._read_timeout)
            if r.status_code != 200:
                first = False  # Error bodies retain the ordinary transport-gap bound.
                detail = (await r.aread()).decode("utf-8", "replace")
                raise EngineError("decode", url, r.status_code, detail)
            lines = r.aiter_lines()
            metadata = 0  # pre-token frames that carried no token
            done = False
            while True:
                try:
                    if first and deadline is not None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        line = await asyncio.wait_for(anext(lines), timeout=remaining)
                    else:
                        try:
                            line = await anext(lines)
                        except httpx.ReadTimeout as exc:
                            if first:
                                raise
                            detail = STREAM_SILENCE_DETAIL
                            if self._read_timeout is not None:
                                detail += (
                                    f": nothing arrived "
                                    f"within the {self._read_timeout:g}s read timeout"
                                )
                            raise EngineError("decode", url, 504, detail) from exc
                except StopAsyncIteration:
                    if done:
                        return
                    raise EngineError("decode", url, 502, STREAM_UNTERMINATED_DETAIL) from None
                except TimeoutError as exc:
                    raise EngineError(
                        "decode",
                        url,
                        504,
                        _first_token_detail(budget_s, metadata),
                    ) from exc
                if line:
                    upstream_error = sse_error(line)
                    if upstream_error is not None:
                        status, detail = upstream_error
                        raise EngineError("decode", url, status, detail)
                    if line.startswith("data:") and line[5:].strip() == "[DONE]":
                        if first:
                            raise EngineError("decode", url, 502, STREAM_EMPTY_DETAIL)
                        done = True
                    elif first:
                        if sse_token_bearing(line, self.dialect):
                            first = False
                        else:
                            metadata += 1
                    yield line
                    if done:
                        return

    async def probe_inference(
        self, url: str, *, prefill_url: str | None = None, deadline_s: float | None = None
    ) -> InferenceProbe | None:
        """Verify an engine's inference path through the control pool.

        Prefill must return a KV handoff; decode must emit a token followed by
        [DONE]. `prefill_url` selects the producer for a crossed transfer probe.
        Each probe uses a unique prompt and each leg has its own deadline.
        Returns None when the model is unspecified.
        """
        if not self.model:
            return None
        prompt = f"{uuid4().hex} {_PROBE_PROMPT}"
        handoff: list[PrefillResult] = []
        try:
            async with asyncio.timeout(deadline_s):
                prefill = await self._probe_prefill(
                    prefill_url or url, handoff=handoff, prompt=prompt
                )
        except TimeoutError:
            prefill = ProbeLeg(failed=LEG_TIMEOUT)
        if prefill_url is not None and (prefill.failed or prefill.inconclusive):
            # A producer failure provides no evidence about the consumer.
            return InferenceProbe(prefill=prefill, decode=ProbeLeg(inconclusive=True))
        try:
            async with asyncio.timeout(deadline_s):
                decode = await self._probe_decode(
                    url, kv_params=handoff[0] if prefill_url and handoff else None, prompt=prompt
                )
        except TimeoutError:
            decode = ProbeLeg(failed=LEG_STREAM)
        return InferenceProbe(prefill=prefill, decode=decode)

    async def _probe_prefill(
        self, url: str, *, handoff: list[PrefillResult] | None = None, prompt: str = _PROBE_PROMPT
    ) -> ProbeLeg:
        """Run the prefill leg of the inference probe on the control pool."""
        body = self._prefill_leg({"model": self.model, "prompt": prompt})
        try:
            r = await self._control.post(
                f"{url}{_PROBE_ENDPOINT}",
                json=body,
                timeout=self._prefill_timeout,
                headers=self._auth(None),
            )
        except httpx.PoolTimeout:
            return ProbeLeg(inconclusive=True)
        except httpx.HTTPError as exc:
            return ProbeLeg(failed=leg_failure_class(exc))
        if r.status_code != 200:
            return ProbeLeg(failed=_status_class(r.status_code))
        try:
            result = self.kv.prefill_result(
                r.json(), url=url, endpoint=_PROBE_ENDPOINT, request_id=None
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # A 200 whose payload resists reading carries no readable handoff.
            return ProbeLeg(failed=LEG_KV_HANDOFF)
        if handoff is not None:
            handoff.append(result)
        return ProbeLeg()

    async def _probe_decode(
        self, url: str, *, kv_params: PrefillResult | None = None, prompt: str = _PROBE_PROMPT
    ) -> ProbeLeg:
        """Run the streamed decode leg of the inference probe on the control pool."""
        body = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": 1,
            "stream": True,
        }
        body = self.kv.decode_body(body, kv_params, url=url, endpoint=_PROBE_ENDPOINT)
        timeouts = self._control.timeout.as_dict()
        # Use probe_inference's absolute per-leg deadline while waiting
        # for the first token.
        timeouts["read"] = None
        try:
            async with self._control.stream(
                "POST",
                f"{url}{_PROBE_ENDPOINT}",
                json=body,
                headers=self._auth(None),
                timeout=httpx.Timeout(**timeouts),
            ) as r:
                if r.status_code != 200:
                    return ProbeLeg(failed=_status_class(r.status_code))
                bearing = False
                r.stream = _GapBoundStream(
                    r.stream, lambda: None if not bearing else self._read_timeout
                )
                async for line in r.aiter_lines():
                    if not bearing and sse_token_bearing(line, self.dialect):
                        bearing = True
                    if line.startswith("data:") and line[5:].strip() == "[DONE]":
                        # The terminal marker is evidence only after output arrived.
                        return ProbeLeg() if bearing else ProbeLeg(failed=LEG_STREAM)
                return ProbeLeg(failed=LEG_STREAM)
        except httpx.PoolTimeout:
            return ProbeLeg(inconclusive=True)
        except httpx.HTTPError as exc:
            return ProbeLeg(failed=leg_failure_class(exc))


def _first_token_detail(budget_s: float, metadata: int) -> str:
    """Name which pre-token phase spent the absolute first-token deadline."""
    if metadata:
        frames = f"{metadata} metadata frame" + ("s" if metadata != 1 else "")
        return f"{FIRST_OUTPUT_DETAIL} {budget_s:g}s: {frames} but no token"
    return f"{FIRST_OUTPUT_DETAIL} {budget_s:g}s: no frames at all"
