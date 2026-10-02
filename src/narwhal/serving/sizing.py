"""Request sizing: token counts and prefix-cache evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ..engines.client import EngineError
from ..engines.prefix import CacheNamespace, block_identities
from ..types import Instance, Request
from .completion import cacheable_render

if TYPE_CHECKING:
    from .router import NarwhalRouter


# Prompts at least this long hash their block identities in a worker thread.
HASH_THREAD_TOKENS = 8192
# First and longest skip of an engine after a failed exact count; each failure doubles it.
TOKENIZE_BACKOFF_S = 1.0
TOKENIZE_BACKOFF_MAX_S = 30.0


def _hash_prompt(
    namespace: CacheNamespace | None, reusable: Sequence[int], sizes: set[int]
) -> dict[int, list[bytes]]:
    """Return the prompt's block identities per block size; empty for invalid token IDs."""
    if namespace is None:
        return {}
    try:
        return {size: block_identities(namespace, reusable, size) for size in sizes}
    except ValueError:
        return {}


async def size(
    router: NarwhalRouter, body: dict[str, Any]
) -> tuple[int, dict[str, int], dict[str, int], dict[int, list[bytes]]]:
    """Return the input token count and the prefix-cache evidence for the prompt.

    A failed exact count raises EngineError and backs off that engine.
    """
    prompt = body.get("prompt")
    if (
        "messages" not in body
        and isinstance(prompt, list)
        and prompt
        and all(type(token) is int and token >= 0 for token in prompt)
    ):
        return len(prompt), *(await cache_evidence(router, body, prompt))
    if router.cfg.tokenize:
        live = router.scheduler.live_instances()
        if live:
            engine = _tokenize_engine(router, live)
            try:
                got = await router.engines.tokenize(
                    engine.url, body, router.cfg.tokenize_timeout_s, strict=True
                )
            except EngineError:
                failures = router._tokenize_backoff.get(engine.iid, (0, 0.0))[0] + 1
                delay = min(TOKENIZE_BACKOFF_S * 2 ** min(failures - 1, 16), TOKENIZE_BACKOFF_MAX_S)
                router._tokenize_backoff[engine.iid] = (failures, router._clock() + delay)
                raise
            if got is not None:
                router._tokenize_backoff.pop(engine.iid, None)
                ids = got.token_ids
                if ids is None:
                    return got.count, {}, {}, {}
                return got.count, *(await cache_evidence(router, body, ids))
    return estimate_length(router, body), {}, {}, {}


def _tokenize_engine(router: NarwhalRouter, live: list[Instance]) -> Instance:
    """Pick the least-occupied live engine outside its count backoff, rotating among ties."""
    now = router._clock()
    candidates = [
        i for i in live if router._tokenize_backoff.get(i.iid, (0, 0.0))[1] <= now
    ] or live
    router._tokenize_turn += 1
    start = router._tokenize_turn % len(candidates)
    ordered = candidates[start:] + candidates[:start]
    return min(ordered, key=lambda i: len(i.prefill) + len(i.decode))


def prefix_cache_evidence(
    router: NarwhalRouter, body: dict[str, Any], token_ids: Sequence[int]
) -> tuple[dict[str, int], dict[str, int], dict[int, list[bytes]]]:
    """Return cached tokens and residency sequence per engine, and the matched block identities.

    Identities cover the prompt minus its final token.
    """
    return router.residency.match(_hash_prompt(*_evidence_inputs(router, body, token_ids)))


async def cache_evidence(
    router: NarwhalRouter, body: dict[str, Any], token_ids: Sequence[int]
) -> tuple[dict[str, int], dict[str, int], dict[int, list[bytes]]]:
    """`prefix_cache_evidence`, hashing a long prompt in a worker thread."""
    inputs = _evidence_inputs(router, body, token_ids)
    if len(inputs[1]) >= HASH_THREAD_TOKENS:
        by_size = await asyncio.to_thread(_hash_prompt, *inputs)
    else:
        by_size = _hash_prompt(*inputs)
    return router.residency.match(by_size)


def _evidence_inputs(
    router: NarwhalRouter, body: dict[str, Any], token_ids: Sequence[int]
) -> tuple[CacheNamespace | None, Sequence[int], set[int]]:
    contract = router.cfg.engine_contract
    if (
        contract is None
        or len(token_ids) < 2
        or not cacheable_render(body)
        # Speculative decoding shortens vLLM's prefix hits by a block.
        or contract.speculative_config not in ("", "disabled")
    ):
        return None, (), set()
    salt = body.get("cache_salt")
    namespace = CacheNamespace(
        router.cfg.model, contract.fingerprint(), None, salt if isinstance(salt, str) else None
    )
    return namespace, token_ids[:-1], router.residency.block_sizes()


def recheck_cache_evidence(router: NarwhalRouter, request: Request, fresh_s: float = 0.0) -> None:
    """Refresh the request's cache evidence from current residency.

    Evidence checked within `fresh_s` stays while every engine behind it keeps a known view.
    """
    now = router._clock()
    checked = request.cache_checked_at
    if (
        checked is not None
        and 0.0 <= now - checked < fresh_s
        and all(
            (view := router.residency.views.get(iid)) is not None and view.known
            for iid in request.cached_tokens
        )
    ):
        return
    request.cache_checked_at = now
    before = dict(request.cached_tokens)
    cached, sequences, _ = router.residency.match(
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
        router.controller.demand.reprice_arrival(request)


def estimate_length(router: NarwhalRouter, body: dict[str, Any]) -> int:
    """Estimate offered input length locally."""
    raw = body.get("prompt")
    if raw is None:
        raw = "".join(str(m.get("content", "")) for m in body.get("messages", []) or [])
    if isinstance(raw, list):
        return len(raw)
    return max(1, int(len(str(raw)) / router.cfg.chars_per_token))
