"""Reusable synthetic profiling sweeps and engine metric scrapes."""

import contextlib
import io
from unittest.mock import AsyncMock, patch

from narwhal.profiling.probe import live, warm


@contextlib.contextmanager
def patched_profile_sweeps(
    prefill_samples,
    decode_samples,
    *,
    hits,
    block=None,
    warm_result=([], "none"),
    capacity=100_000,
):
    """Replace the sweeps and scrapes behind `live.profile_instance`; yield the warm sweep.

    A list of `hits` is returned one value per scrape.
    """
    counter = (
        AsyncMock(side_effect=hits) if isinstance(hits, list) else AsyncMock(return_value=hits)
    )
    with (
        patch.object(live, "probe_prefill", AsyncMock(return_value=prefill_samples)),
        patch.object(live, "probe_decode", AsyncMock(return_value=decode_samples)),
        patch.object(live, "kv_capacity", AsyncMock(return_value=capacity)),
        patch.object(live, "cache_block_tokens", AsyncMock(return_value=block)),
        patch.object(warm, "prefix_cache_hits", counter),
        patch.object(live, "prefix_cache_hits", counter),
        patch.object(live, "probe_cached_prefill", AsyncMock(return_value=warm_result)) as sweep,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        yield sweep
