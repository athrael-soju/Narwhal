"""Size offered requests and match their prompts against engine residency."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from ...engines.client import EngineError
from ...engines.prefix import CacheNamespace, non_negative_ints, prefix_identities
from ...types import Instance, Request
from ..completion import cacheable_render
from ..retry import transient

if TYPE_CHECKING:
    from ...config.model import EngineContract
    from .routing import NarwhalRouter

# Prompts longer than this hash their block identities in a worker thread.
HASH_THREAD_TOKENS = 8192
# First and longest skip of an engine after a failed exact count; each failure doubles it.
TOKENIZE_BACKOFF_S = 1.0
TOKENIZE_BACKOFF_MAX_S = 30.0


def _hash_prompt(
    namespace: CacheNamespace | None, token_ids: Sequence[int], sizes: set[int]
) -> dict[int, list[bytes]]:
    """Return the block identities of the prompt minus its final token per block size.

    Invalid token IDs give an empty result.
    """
    if namespace is None:
        return {}
    try:
        return prefix_identities(namespace, token_ids, len(token_ids) - 1, sizes)
    except ValueError:
        return {}


class RequestSizer:
    """Count offered input tokens and read their prefix-cache evidence."""

    def __init__(self, router: NarwhalRouter) -> None:
        self.router = router
        self._turn = 0
        # Engine ID to consecutive failed exact counts and the time it may count again.
        self.backoff: dict[str, tuple[int, float]] = {}
        # The engine contract last fingerprinted and its fingerprint.
        self._contract: EngineContract | None = None
        self._fingerprint = ""

    async def size(
        self, body: dict[str, Any]
    ) -> tuple[int, dict[str, int], dict[str, int], dict[int, list[bytes]]]:
        """Return the input token count and the prefix-cache evidence for the prompt.

        A transient exact-count failure backs off its engine and moves the count to another
        live engine; the last or a non-transient failure raises EngineError.
        """
        prompt = body.get("prompt")
        if (
            "messages" not in body
            and isinstance(prompt, list)
            and prompt
            and non_negative_ints(prompt)
        ):
            return len(prompt), *(await self._cache_evidence(body, prompt))
        if self.router.cfg.tokenize:
            live = self.router.scheduler.live_instances()
            attempts = min(len(live), self.router.cfg.serving.max_attempts)
            tried: set[str] = set()
            # Each failed count moves to another live engine, up to `serving.max_attempts`.
            for attempt in range(1, attempts + 1):
                engine = self._tokenize_engine(live, tried)
                tried.add(engine.iid)
                try:
                    got = await self.router.engines.tokenize(
                        engine.url, body, self.router.cfg.tokenize_timeout_s, strict=True
                    )
                except EngineError as exc:
                    self._count_failed(engine, exc)
                    # An engine rejecting the prompt itself fails the count everywhere.
                    if attempt == attempts or not transient(exc):
                        raise
                    continue
                if got is None:
                    break
                self.backoff.pop(engine.iid, None)
                if got.token_ids is None:
                    return got.count, {}, {}, {}
                return got.count, *(await self._cache_evidence(body, got.token_ids))
        return self.estimate_length(body), {}, {}, {}

    def _count_failed(self, engine: Instance, exc: EngineError) -> None:
        """Back off `engine` and record breaker evidence for its failed count."""
        failures = self.backoff.get(engine.iid, (0, 0.0))[0] + 1
        delay = min(TOKENIZE_BACKOFF_S * 2 ** min(failures - 1, 16), TOKENIZE_BACKOFF_MAX_S)
        self.backoff[engine.iid] = (failures, self.router._clock() + delay)
        # A wrapped transport failure is the evidence; an engine status stands for itself.
        cause = exc.__cause__
        evidence = cause if isinstance(cause, httpx.HTTPError | TimeoutError) else exc
        self.router.verifier.leg_failed(engine.iid, evidence)

    def _tokenize_engine(self, live: list[Instance], tried: set[str]) -> Instance:
        """Pick the least-occupied untried live engine outside its count backoff.

        Ties rotate between requests.
        """
        now = self.router._clock()
        live = [i for i in live if i.iid not in tried]
        candidates = [i for i in live if self.backoff.get(i.iid, (0, 0.0))[1] <= now] or live
        self._turn += 1
        start = self._turn % len(candidates)
        ordered = candidates[start:] + candidates[:start]
        return min(ordered, key=lambda i: len(i.prefill) + len(i.decode))

    def prefix_cache_evidence(
        self, body: dict[str, Any], token_ids: Sequence[int]
    ) -> tuple[dict[str, int], dict[str, int], dict[int, list[bytes]]]:
        """Return cached tokens and residency sequence per engine, and the matched block identities.

        Identities cover the prompt minus its final token.
        """
        return self.router.residency.match(_hash_prompt(*self._evidence_inputs(body, token_ids)))

    async def _cache_evidence(
        self, body: dict[str, Any], token_ids: Sequence[int]
    ) -> tuple[dict[str, int], dict[str, int], dict[int, list[bytes]]]:
        """`prefix_cache_evidence`, hashing a long prompt in a worker thread."""
        inputs = self._evidence_inputs(body, token_ids)
        if len(inputs[1]) > HASH_THREAD_TOKENS:
            by_size = await asyncio.to_thread(_hash_prompt, *inputs)
        else:
            by_size = _hash_prompt(*inputs)
        return self.router.residency.match(by_size)

    def _evidence_inputs(
        self, body: dict[str, Any], token_ids: Sequence[int]
    ) -> tuple[CacheNamespace | None, Sequence[int], set[int]]:
        contract = self.router.cfg.engine_contract
        sizes = self.router.residency.block_sizes()
        if (
            contract is None
            or not sizes
            or len(token_ids) < 2
            or not cacheable_render(body)
            # Speculative decoding shortens vLLM's prefix hits by a block.
            or contract.speculative_config not in ("", "disabled")
        ):
            return None, (), set()
        if contract is not self._contract:
            self._contract, self._fingerprint = contract, contract.fingerprint()
        salt = body.get("cache_salt")
        namespace = CacheNamespace(
            self.router.cfg.model,
            self._fingerprint,
            None,
            salt if isinstance(salt, str) else None,
        )
        return namespace, token_ids, sizes

    def recheck_cache_evidence(self, request: Request, fresh_s: float = 0.0) -> None:
        """Refresh the request's cache evidence from current residency.

        Evidence checked within `fresh_s` stays while every engine behind it keeps a known view.
        """
        now = self.router._clock()
        checked = request.cache_checked_at
        if (
            checked is not None
            and 0.0 <= now - checked < fresh_s
            and all(
                (view := self.router.residency.views.get(iid)) is not None and view.known
                for iid in request.cached_tokens
            )
        ):
            return
        request.cache_checked_at = now
        before = dict(request.cached_tokens)
        cached, sequences, _ = self.router.residency.match(
            request.cache_identities, engines=request.cached_tokens.keys()
        )
        for iid in list(request.cached_tokens):
            if iid in cached:
                request.cached_tokens[iid] = cached[iid]
                if iid in sequences:
                    request.cache_sequences[iid] = sequences[iid]
            else:
                del request.cached_tokens[iid]
                request.cache_sequences.pop(iid, None)
        if request.cached_tokens != before:
            self.router.controller.demand.reprice_arrival(request)

    def estimate_length(self, body: dict[str, Any]) -> int:
        """Estimate offered input length locally."""
        raw = body.get("prompt")
        if raw is None:
            raw = "".join(str(m.get("content", "")) for m in body.get("messages", []) or [])
        if isinstance(raw, list):
            return len(raw)
        return max(1, int(len(str(raw)) / self.router.cfg.chars_per_token))
