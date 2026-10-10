from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass
from typing import Any, TypeGuard
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
from .connector import KvConnector, KvHandoff, PrefillResult, RendezvousConnector
from .dialect import EngineDialect
from .stream import SseEvent, sse_batches, sse_error, sse_events, sse_token_bearing
from .wire import Dial, WireClient, WireResponse, dial_tcp


class EngineError(RuntimeError):
    def __init__(self, leg: str, url: str, status: int, detail: str) -> None:
        super().__init__(f"{leg} leg against {url} failed ({status}): {detail[:240]}")
        self.leg = leg
        self.url = url
        self.status = status
        self.detail = detail


class _GapBoundStream(httpx.AsyncByteStream):
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


# EngineError detail prefixes for failure shapes the breaker classifies explicitly;
# the router's controller risk note matches them too.
FIRST_OUTPUT_DETAIL = "no first token within"
STREAM_SILENCE_DETAIL = "engine went silent between tokens"
STREAM_UNTERMINATED_DETAIL = "stream ended before the [DONE] terminator"
STREAM_EMPTY_DETAIL = "stream ended with [DONE] before any token arrived"
NO_HANDOFF_DETAIL = "no handoff"
# A health timeout surfacing this far past its budget measured router scheduling delay.
LATE_TIMEOUT_FACTOR = 1.5

# The probe runs the plain completion route every dialect serves.
_PROBE_ENDPOINT = "/v1/completions"
_PROBE_PROMPT = "breaker verification"


def first_output_timeout(exc: BaseException) -> TypeGuard[EngineError]:
    return isinstance(exc, EngineError) and exc.detail.startswith(FIRST_OUTPUT_DETAIL)


def leg_failure_class(exc: BaseException) -> str | None:
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
class Tokenization:
    count: int
    token_ids: tuple[int, ...] | None


@dataclass(frozen=True)
class ProbeLeg:
    failed: str | None = None
    inconclusive: bool = False


@dataclass(frozen=True)
class InferenceProbe:
    prefill: ProbeLeg
    decode: ProbeLeg


def _status_class(status: int) -> str:
    return LEG_OVERLOAD if status in (408, 429) else LEG_INFERENCE_STATUS


class EngineClient:
    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        prefill_timeout_s: float = 120.0,
        read_timeout_s: float = 10.0,
        max_connections: int = 768,
        control_connections: int = 2,
        pool_timeout_s: float = 5.0,
        connect_timeout_s: float = 10.0,
        health_timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        kv: KvHandoff,
        dialect: EngineDialect,
        model: str = "",
        engine_api_key: str | None = None,
        dial: Dial = dial_tcp,
    ) -> None:
        self.kv = kv
        self.dialect = dialect
        # The served model name used in inference-probe requests.
        self.model = model
        # Bounds gaps between transport chunks; 0 disables the bound.
        self._read_timeout = read_timeout_s if read_timeout_s > 0 else None
        # 0 is the unvalidated config's derive marker; a standalone client uses 2.
        control_connections = control_connections if control_connections > 0 else 2
        self.control_connections = control_connections
        self._data_timeout = httpx.Timeout(
            timeout_s, connect=connect_timeout_s, read=self._read_timeout, pool=pool_timeout_s
        )
        self._wire = WireClient(
            max_connections=max_connections,
            max_keepalive=max(1, max_connections // 2),
            connect_timeout_s=connect_timeout_s,
            pool_timeout_s=pool_timeout_s,
            dial=dial,
            keepalive_expiry_s=dialect.keepalive_expiry_s,
        )
        self._control = httpx.AsyncClient(
            timeout=self._data_timeout,
            limits=httpx.Limits(
                max_connections=control_connections,
                max_keepalive_connections=max(1, control_connections // 2),
                keepalive_expiry=dialect.keepalive_expiry_s,
            ),
            transport=transport,
        )
        self._prefill_timeout = prefill_timeout_s
        self._health_timeout = health_timeout_s
        self._connect_timeout = connect_timeout_s
        self._pool_timeout = pool_timeout_s
        # A caller-supplied authorization header overrides the engine credential.
        self._engine_api_key = engine_api_key

    def _auth(self, headers: dict[str, str] | None) -> dict[str, str]:
        out = dict(headers) if headers else {}
        if self._engine_api_key is not None and "authorization" not in (
            name.lower() for name in out
        ):
            out["authorization"] = f"Bearer {self._engine_api_key}"
        return out

    async def aclose(self) -> None:
        await self._wire.aclose()
        await self._control.aclose()

    def _phase_timeout(self, budget_s: float) -> httpx.Timeout:
        return httpx.Timeout(
            budget_s,
            connect=min(self._connect_timeout, budget_s),
            pool=min(self._pool_timeout, budget_s),
        )

    async def healthy(self, url: str) -> bool | None:
        loop = asyncio.get_running_loop()
        # Lateness counts from the first connection event, after any wait for the pool.
        started = [loop.time()]

        async def trace(event: str, info: dict[str, Any]) -> None:
            if len(started) == 1:
                started.append(loop.time())

        try:
            r = await self._control.get(
                f"{url}{self.dialect.health_path}",
                timeout=self._phase_timeout(self._health_timeout),
                headers=self._auth(None),
                extensions={"trace": trace},
            )
        except httpx.PoolTimeout:
            return None
        except httpx.TimeoutException:
            if loop.time() - started[-1] > self._health_timeout * LATE_TIMEOUT_FACTOR:
                return None
            return False
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    async def token_count(
        self, url: str, body: dict[str, Any], timeout_s: float, *, strict: bool = False
    ) -> int | None:
        result = await self.tokenize(url, body, timeout_s, strict=strict)
        return None if result is None else result.count

    async def tokenize(
        self, url: str, body: dict[str, Any], timeout_s: float, *, strict: bool = False
    ) -> Tokenization | None:
        if self.dialect.tokenize_path is None:
            return None
        try:
            async with asyncio.timeout(timeout_s):
                r = await self._wire.post(
                    f"{url}{self.dialect.tokenize_path}",
                    self.dialect.tokenize_request(body.get("model"), body),
                    self._auth(None),
                    timeout_s=timeout_s,
                )
        except (httpx.TimeoutException, TimeoutError) as exc:
            if strict:
                raise EngineError(
                    "tokenize", url, 504, f"exact count exceeded {timeout_s:g}s"
                ) from exc
            return None
        except httpx.HTTPError as exc:
            if strict:
                raise EngineError("tokenize", url, 502, str(exc)) from exc
            return None
        if r.status_code != 200:
            if strict:
                raise EngineError("tokenize", url, r.status_code, r.text[:200])
            return None
        try:
            payload = r.json()
        except ValueError:
            if strict:
                raise EngineError("tokenize", url, 502, "invalid JSON response") from None
            return None
        count = self.dialect.tokenize_response(payload)
        if count is None:
            if strict:
                raise EngineError("tokenize", url, 502, "response has no valid token count")
            return None
        token_ids = self.dialect.tokenize_token_ids(payload)
        return Tokenization(count, None if token_ids is None else tuple(token_ids))

    @property
    def descriptor_kv(self) -> KvConnector:
        if not isinstance(self.kv, KvConnector):
            raise TypeError(f"connector {self.kv.name!r} has no prefill descriptor")
        return self.kv

    def _prefill_leg(
        self, body: dict[str, Any], rendezvous: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        leg = {**body, "max_tokens": 1, "stream": False}
        if isinstance(self.kv, RendezvousConnector):
            leg = self.kv.prefill_body(leg, rendezvous or {})
        else:
            leg.update(self.descriptor_kv.prefill_params())
        for name in self.dialect.prefill_incompatible:
            leg.pop(name, None)
        return leg

    def _tag(
        self, leg: dict[str, Any], headers: dict[str, str], request_id: str | None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if request_id is None:
            return leg, headers
        extra_headers, fields = self.dialect.request_id(request_id)
        names = {name.lower() for name in extra_headers}
        kept = {k: v for k, v in headers.items() if k.lower() not in names}
        return {**leg, **fields}, {**kept, **extra_headers}

    async def prefill(
        self,
        url: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        request_id: str | None = None,
    ) -> PrefillResult:
        r = await self._post_prefill(url, endpoint, self._prefill_leg(body), headers, request_id)
        try:
            return self.descriptor_kv.prefill_result(
                r.json(), url=url, endpoint=endpoint, request_id=request_id
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise EngineError(
                "prefill",
                url,
                200,
                f"{NO_HANDOFF_DETAIL}: invalid {self.kv.name} descriptor: {exc}",
            ) from exc

    async def rendezvous_prefill(
        self,
        url: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        rendezvous: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> None:
        leg = self._prefill_leg(body, rendezvous)
        await self._post_prefill(url, endpoint, leg, headers, request_id)

    async def _post_prefill(
        self,
        url: str,
        endpoint: str,
        leg: dict[str, Any],
        headers: dict[str, str],
        request_id: str | None,
    ) -> WireResponse:
        leg, headers = self._tag(leg, headers, request_id)
        io_started = False

        def started() -> None:
            nonlocal io_started
            io_started = True

        try:
            async with asyncio.timeout(self._prefill_timeout):
                r = await self._wire.post(
                    f"{url}{endpoint}",
                    leg,
                    self._auth(headers),
                    timeout_s=self._prefill_timeout,
                    on_connection=started,
                )
        except TimeoutError as exc:
            if not io_started:
                raise httpx.PoolTimeout(
                    f"prefill waited for a connection until its {self._prefill_timeout:g}s "
                    "elapsed deadline"
                ) from exc
            raise httpx.ReadTimeout(
                f"prefill exceeded its {self._prefill_timeout:g}s elapsed deadline"
            ) from exc
        if r.status_code != 200:
            raise EngineError("prefill", url, r.status_code, r.text)
        return r

    async def decode(
        self,
        url: str,
        endpoint: str,
        body: dict[str, Any],
        headers: dict[str, str],
        kv_params: PrefillResult | None,
        first_token_timeout_s: float | None = None,
        *,
        request_id: str | None = None,
        rendezvous: dict[str, Any] | None = None,
    ) -> AsyncGenerator[list[SseEvent], None]:
        try:
            if isinstance(self.kv, RendezvousConnector):
                leg = self.kv.decode_body({**body, "stream": True}, rendezvous or {})
            else:
                leg = self.descriptor_kv.decode_body(body, kv_params, url=url, endpoint=endpoint)
        except (ValueError, TypeError) as exc:
            raise EngineError("decode", url, 502, f"invalid continuation: {exc}") from exc
        leg, headers = self._tag(leg, headers, request_id)

        budget_s = first_token_timeout_s or 0.0
        deadline = asyncio.get_running_loop().time() + budget_s if budget_s > 0 else None
        first = True  # no generated token observed yet

        def gap() -> float | None:
            # The first-token deadline replaces the read gap for headers and pre-token reads.
            return None if first and deadline is not None else self._read_timeout

        async with AsyncExitStack() as stack:
            opening = stack.enter_async_context(
                self._wire.stream(f"{url}{endpoint}", leg, self._auth(headers), gap=gap)
            )
            try:
                remaining = (
                    max(0.0, deadline - asyncio.get_running_loop().time()) if deadline else None
                )
                r = await asyncio.wait_for(opening, timeout=remaining)
            except TimeoutError as exc:
                raise EngineError("decode", url, 504, _first_token_detail(budget_s, 0)) from exc
            if r.status_code != 200:
                first = False  # Error bodies retain the ordinary transport-gap bound.
                detail = (await r.aread()).decode("utf-8", "replace")
                raise EngineError("decode", url, r.status_code, detail)
            batches = await stack.enter_async_context(aclosing(sse_batches(r.aiter_bytes())))
            metadata = 0  # pre-token frames that carried no token
            while True:
                try:
                    if first and deadline is not None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        batch = await asyncio.wait_for(anext(batches), timeout=remaining)
                    else:
                        try:
                            batch = await anext(batches)
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
                    raise EngineError("decode", url, 502, STREAM_UNTERMINATED_DETAIL) from None
                except TimeoutError as exc:
                    raise EngineError(
                        "decode",
                        url,
                        504,
                        _first_token_detail(budget_s, metadata),
                    ) from exc
                relayed: list[SseEvent] = []
                for event in batch:
                    upstream_error = sse_error(event)
                    if upstream_error is not None:
                        if relayed:
                            yield relayed
                        status, detail = upstream_error
                        raise EngineError("decode", url, status, detail)
                    if event.done:
                        if first:
                            if relayed:
                                yield relayed
                            raise EngineError("decode", url, 502, STREAM_EMPTY_DETAIL)
                        relayed.append(event)
                        yield relayed
                        return
                    if first:
                        if sse_token_bearing(event, self.dialect):
                            first = False
                        else:
                            metadata += 1
                    relayed.append(event)
                yield relayed

    async def probe_inference(
        self,
        url: str,
        *,
        prefill_url: str | None = None,
        deadline_s: float | None = None,
        producer: Mapping[str, Any] | None = None,
    ) -> InferenceProbe | None:
        if not self.model:
            return None
        prompt = f"{uuid4().hex} {_PROBE_PROMPT}"
        if isinstance(self.kv, RendezvousConnector):
            return await self._probe_rendezvous(
                self.kv, url, prefill_url or url, producer or {}, prompt, deadline_s
            )
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

    async def _probe_rendezvous(
        self,
        kv: RendezvousConnector,
        url: str,
        prefill_url: str,
        producer: Mapping[str, Any],
        prompt: str,
        deadline_s: float | None,
    ) -> InferenceProbe:
        rendezvous = kv.rendezvous(producer)

        async def bounded(leg: Awaitable[ProbeLeg], limit: float | None, late: str) -> ProbeLeg:
            try:
                async with asyncio.timeout(limit):
                    return await leg
            except TimeoutError:
                return ProbeLeg(failed=late)

        decode_limit = min(deadline_s or math.inf, kv.decode_wait_s)
        prefill, decode = await asyncio.gather(
            bounded(
                self._probe_prefill(prefill_url, prompt=prompt, rendezvous=rendezvous),
                deadline_s,
                LEG_TIMEOUT,
            ),
            bounded(
                self._probe_decode(url, prompt=prompt, rendezvous=rendezvous),
                decode_limit,
                LEG_STREAM,
            ),
        )
        if prefill.failed or prefill.inconclusive:
            # Decode cannot finish without its prefill leg, so its result says nothing.
            decode = ProbeLeg(inconclusive=True)
        return InferenceProbe(prefill=prefill, decode=decode)

    async def _probe_prefill(
        self,
        url: str,
        *,
        handoff: list[PrefillResult] | None = None,
        prompt: str = _PROBE_PROMPT,
        rendezvous: dict[str, Any] | None = None,
    ) -> ProbeLeg:
        body = self._prefill_leg({"model": self.model, "prompt": prompt}, rendezvous)
        try:
            r = await self._control.post(
                f"{url}{_PROBE_ENDPOINT}",
                json=body,
                timeout=self._phase_timeout(self._prefill_timeout),
                headers=self._auth(None),
            )
        except httpx.PoolTimeout:
            return ProbeLeg(inconclusive=True)
        except httpx.HTTPError as exc:
            return ProbeLeg(failed=leg_failure_class(exc))
        if r.status_code != 200:
            return ProbeLeg(failed=_status_class(r.status_code))
        if rendezvous is not None:
            return ProbeLeg()
        try:
            result = self.descriptor_kv.prefill_result(
                r.json(), url=url, endpoint=_PROBE_ENDPOINT, request_id=None
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # A 200 whose payload resists reading carries no readable handoff.
            return ProbeLeg(failed=LEG_KV_HANDOFF)
        if handoff is not None:
            handoff.append(result)
        return ProbeLeg()

    async def _probe_decode(
        self,
        url: str,
        *,
        kv_params: PrefillResult | None = None,
        prompt: str = _PROBE_PROMPT,
        rendezvous: dict[str, Any] | None = None,
    ) -> ProbeLeg:
        body = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": 1,
            "stream": True,
            # A model may end this prompt at once.
            **self.dialect.decode_probe_extras(1),
        }
        if rendezvous is not None and isinstance(self.kv, RendezvousConnector):
            body = self.kv.decode_body(body, rendezvous)
        else:
            body = self.descriptor_kv.decode_body(
                body, kv_params, url=url, endpoint=_PROBE_ENDPOINT
            )
        timeouts = self._control.timeout.as_dict()
        # probe_inference's per-leg deadline bounds the wait for the first token.
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
                async with aclosing(sse_events(r.aiter_bytes())) as events:
                    async for event in events:
                        if not bearing and sse_token_bearing(event, self.dialect):
                            bearing = True
                        if event.done:
                            # The terminal marker is evidence only after output arrived.
                            return ProbeLeg() if bearing else ProbeLeg(failed=LEG_STREAM)
                return ProbeLeg(failed=LEG_STREAM)
        except httpx.PoolTimeout:
            return ProbeLeg(inconclusive=True)
        except httpx.HTTPError as exc:
            return ProbeLeg(failed=leg_failure_class(exc))


def _first_token_detail(budget_s: float, metadata: int) -> str:
    if metadata:
        frames = f"{metadata} metadata frame" + ("s" if metadata != 1 else "")
        return f"{FIRST_OUTPUT_DETAIL} {budget_s:g}s: {frames} but no token"
    return f"{FIRST_OUTPUT_DETAIL} {budget_s:g}s: no frames at all"
