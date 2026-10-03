"""Profiling sweep grids and their bounds from live engine limits."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

# Candidate lengths are bounded by each live engine's reported context limit.
# 256, 1024 and 4096 end on a 16- and 512-token block boundary; the rest end between boundaries.
PREFILL_LENS = (256, 700, 1024, 1300, 2300, 4096, 4300, 8300, 12300, 16300)
DECODE_CONCURRENCY = (1, 4, 16, 48)
DECODE_INPUT_LENS = (512, 4096, 8192)
DECODE_TOKENS = 64
PREFILL_REPEATS = 3
# Warm sweep grid.
CACHED_PREFIX_LENS = (2048, 4096, 8192)
CACHED_SUFFIX_LENS = (700, 1300, 2600)
CACHED_REPEATS = 3


@dataclass(frozen=True)
class Sweep:
    """Prompt lengths, concurrency levels, and repetition counts for profiling."""

    prefill_lens: tuple[int, ...] = PREFILL_LENS
    decode_concurrency: tuple[int, ...] = DECODE_CONCURRENCY
    decode_tokens: int = DECODE_TOKENS
    prefill_repeats: int = PREFILL_REPEATS
    decode_input_lens: tuple[int, ...] = DECODE_INPUT_LENS
    decode_repeats: int = 1
    cached_prefix_lens: tuple[int, ...] = CACHED_PREFIX_LENS
    cached_suffix_lens: tuple[int, ...] = CACHED_SUFFIX_LENS
    cached_repeats: int = CACHED_REPEATS


def warm_cases(sweep: Sweep, max_model_len: int | None = None) -> list[tuple[int, int]]:
    """Return the warm (prefix, suffix) cases whose prompt and output token fit the context."""
    return [
        (p, q)
        for p in sweep.cached_prefix_lens
        for q in sweep.cached_suffix_lens
        if max_model_len is None or p + q + 1 < max_model_len
    ]


def bounded_sweep(sweep: Sweep, max_model_len: int, max_num_seqs: int | None = None) -> Sweep:
    """Keep candidate lengths and cohorts within the serving engine's limits."""
    prefill = tuple(n for n in sweep.prefill_lens if n + 1 < max_model_len)
    decode = tuple(n for n in sweep.decode_input_lens if n + sweep.decode_tokens < max_model_len)
    if len(set(prefill)) < 3 or len(set(decode)) < 2:
        raise ValueError(
            f"max_model_len {max_model_len} leaves too few sweep points; choose shorter "
            "--prefill-lens and --decode-input-lens"
        )
    concurrency = sweep.decode_concurrency
    if max_num_seqs is not None:
        concurrency = tuple(n for n in concurrency if n <= max_num_seqs)
        if max_num_seqs < max(sweep.decode_concurrency) and max_num_seqs not in concurrency:
            concurrency += (max_num_seqs,)
        if len(set(concurrency)) < 2:
            raise ValueError(
                f"max_num_seqs {max_num_seqs} leaves fewer than two decode concurrency "
                "points; adjust the engine launch policy before profiling"
            )
    # Prefix, suffix and one output token fit the context.
    shortest_suffix = min(sweep.cached_suffix_lens, default=0)
    shortest_prefix = min(sweep.cached_prefix_lens, default=0)
    prefixes = tuple(p for p in sweep.cached_prefix_lens if p + shortest_suffix + 1 < max_model_len)
    suffixes = tuple(s for s in sweep.cached_suffix_lens if shortest_prefix + s + 1 < max_model_len)
    return replace(
        sweep,
        prefill_lens=prefill,
        decode_input_lens=decode,
        decode_concurrency=concurrency,
        cached_prefix_lens=prefixes,
        cached_suffix_lens=suffixes,
    )


def load_sequence_limits(path: Path, engine_ids: set[str]) -> dict[str, int]:
    """Read the generated limits bound to the fleet's serving roles."""
    document = json.loads(path.read_text())
    if (
        not isinstance(document, dict)
        or document.get("schema") != "narwhal.profiling-limits"
        or document.get("schema_version") != 1
        or not isinstance(document.get("engines"), dict)
    ):
        raise ValueError(f"invalid profiling limits: {path}")
    limits = document["engines"]
    if set(limits) != engine_ids or any(
        type(value) is not int or value < 1 for value in limits.values()
    ):
        raise ValueError(
            f"profiling limits must name every fleet engine with a positive limit: {path}"
        )
    return limits
