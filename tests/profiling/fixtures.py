"""Reusable synthetic profiling sweeps and engine metric scrapes."""

import contextlib
import io
from unittest.mock import AsyncMock, patch

from narwhal.profiling import probe


@contextlib.contextmanager
def patched_profile_sweeps(
    prefill, decode, *, hits, block=None, warm=([], "none"), capacity=100_000
):
    """Replace the sweeps and scrapes behind `probe.profile_instance`; yield the warm sweep.

    A list of `hits` is returned one value per scrape.
    """
    counter = (
        AsyncMock(side_effect=hits) if isinstance(hits, list) else AsyncMock(return_value=hits)
    )
    with (
        patch.object(probe, "probe_prefill", AsyncMock(return_value=prefill)),
        patch.object(probe, "probe_decode", AsyncMock(return_value=decode)),
        patch.object(probe, "kv_capacity", AsyncMock(return_value=capacity)),
        patch.object(probe, "cache_block_tokens", AsyncMock(return_value=block)),
        patch.object(probe, "prefix_cache_hits", counter),
        patch.object(probe, "probe_cached_prefill", AsyncMock(return_value=warm)) as sweep,
        contextlib.redirect_stdout(io.StringIO()),
    ):
        yield sweep
