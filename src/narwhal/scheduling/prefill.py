"""The shared prefill estimate every scheduler consumer prices with."""

from __future__ import annotations

from ..profiling.model import Profile
from ..types import Instance, Request

__all__ = ["prefill_seconds", "resident_prefill_seconds", "warm_prefill_time"]


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
