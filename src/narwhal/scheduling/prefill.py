"""The shared prefill estimate every scheduler consumer prices with."""

from __future__ import annotations

from ..profiling.model import Profile
from ..types import Instance, Request

__all__ = [
    "batched_prefill_seconds",
    "prefill_seconds",
    "resident_prefill_seconds",
    "warm_prefill_time",
]


def warm_prefill_time(profile: Profile, input_len: int, cached: int) -> float | None:
    """Return the warm-fit price of a prompt with `cached` prefix tokens, else None."""
    if cached > 0:
        return profile.cached_prefill_time(cached, input_len - cached)
    return None


def prefill_seconds(profile: Profile, request: Request) -> float:
    """Price one request's prefill: warm fit for a cached prefix inside its domain, else cold."""
    warm = warm_prefill_time(profile, request.input_len, request.cached_tokens.get(profile.iid, 0))
    if warm is not None:
        return warm
    return profile.prefill_time(request.input_len)


def resident_prefill_seconds(profile: Profile, inst: Instance) -> float:
    """Price the prefill work resident on `inst`."""
    return sum(prefill_seconds(profile, r) for r in inst.prefill.values())


def batched_prefill_seconds(profile: Profile, input_len: int, cached: int = 0) -> float:
    """Price one prompt's share of an engine's batched prefill.

    The fit's constant term is a fixed time per prefill batch, so a batch of prompts pays it
    once. The share is the cold price at `input_len`, or the warm price with `cached` prefix
    tokens inside the warm domain, without that constant term.
    """
    warm = warm_prefill_time(profile, input_len, cached)
    if warm is not None and profile.cached_ttft_c is not None:
        return max(0.0, warm - profile.cached_ttft_c)
    return max(0.0, profile.prefill_time(input_len) - profile.ttft_c)
