"""The shared prefill estimate every scheduler consumer prices with."""

from __future__ import annotations

from ..profiling.model import Profile
from ..types import Request

__all__ = ["prefill_seconds"]


def prefill_seconds(profile: Profile, request: Request) -> float:
    """Price one request's prefill: warm fit for a cached prefix inside its domain, else cold."""
    cached = request.cached_tokens.get(profile.iid, 0)
    if cached > 0:
        warm = profile.cached_prefill_time(cached, request.input_len - cached)
        if warm is not None:
            return warm
    return profile.prefill_time(request.input_len)
