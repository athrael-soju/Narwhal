"""Workload synthesis: the three-phase trace, replayed traces, and prompts.

`bench` drives these against a router; the simulator and the walk harnesses
replay them without one. Splitting the workload from the driver keeps one
definition of the trace for every consumer.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .profiler import ProfileStore

# the study's methodology §B, "Phase Inversion": the
# instantaneous optimal P/D ratio has to cross the static split's fixed ratio,
# and the phases have to be long relative to migration cost or "adaptive may
# thrash and lose". Migration here is a relabel gated by `cooldown_s`
# (default 10 s), so 300 s phases are 30 cooldowns long.
#
#   P1 summarization  long in, short out   prefill-heavy
#   P2 chat/reasoning short in, long out   decode-heavy
#   P3 mixed          both                 the honesty check
# The band the protocol gives P1, output 200 to 500, is not prefill-bound on
# every fleet: near ISL 12,000 decode can bind in that phase instead, its
# rate then below the prefill ceiling, and the optimum never crosses. Short
# outputs are what make prefill bind there.
#
# The phases also differ in capacity by more than an order of magnitude, so one
# arrival rate cannot stress both. Each phase carries its own multiplier on the
# swept rate, which is the protocol's rate-multiplier idea applied per phase.
# Multipliers put each phase at a comparable fraction of its own knee at one
# base rate. The shipped constants are one such set of knees: about
# 7.7 req/s of P1 (prefill-bound), 0.49 of P2 (decode-bound) and 29 of P3,
# so P1 runs at 16x P2's rate. They are the fallback only -
# derive_segments below re-prices the multipliers from any fleet's own fitted
# curves, and narwhal-bench uses it whenever a profile store is present.
# The model is validated by the capacity math: at 4.4 req/s the prefill
# phase asks 140.6 instance-seconds of prefill, which a 1P5D fleet's 60
# cannot serve and a 3P3D's 180 can.
PHASE_SECONDS = 300.0
SEGMENTS = (
    (PHASE_SECONDS, (8000, 16000), (30, 60), 16.0),
    (PHASE_SECONDS, (300, 800), (2000, 8000), 1.0),
    (PHASE_SECONDS, (300, 800), (200, 500), 4.0),
)


Segments = tuple[tuple[float, tuple[int, int], tuple[int, int], float], ...]


def derive_segments(
    store: ProfileStore, tpot_slo_s: float, phase_seconds: float = PHASE_SECONDS
) -> Segments | None:
    """The shipped bands with multipliers re-priced from this fleet's fits.

    The baked SEGMENTS multipliers are one fleet's knee ratios
    (16x is 7.7 vs 0.49 req/s); any fleet with a profile store prices
    its own instead. Per band, the per-engine knee is
    min(prefill rate, decode rate): prefill retires 1/t_p(mean isl) requests
    a second, decode holds T*/(isl + osl/2) streams - T* the resident-token
    ceiling at the TPOT target, isl + osl/2 a stream's mid-life residency -
    each retiring osl tokens at the SLO gap. The multipliers are knee
    ratios (fleet size cancels): the prefill-heavy band at its ratio to the
    decode-heavy band, the light band at the geometric mean, keeping it a
    recovery phase rather than a third saturation. On the reference profile
    this derives (19.4, 1.0, 4.4), close to the shipped (16, 1, 4).

    None whenever the store cannot price a band; the caller keeps the
    shipped numbers.
    """
    tstar = store.mean_max_tokens(tpot_slo_s)
    if not tstar or tstar <= 0.0:
        return None

    def knee(isl_band: tuple[int, int], osl_band: tuple[int, int]) -> float | None:
        isl = (isl_band[0] + isl_band[1]) / 2.0
        osl = (osl_band[0] + osl_band[1]) / 2.0
        t_p = store.mean_prefill_time(int(isl))
        if not t_p or t_p <= 0.0:
            return None
        streams = tstar / (isl + osl / 2.0)
        return min(1.0 / t_p, streams / (osl * tpot_slo_s))

    (_, isl1, osl1, _), (_, isl2, osl2, _), (_, isl3, osl3, _) = SEGMENTS
    heavy, ref = knee(isl1, osl1), knee(isl2, osl2)
    if heavy is None or ref is None or ref <= 0.0:
        return None
    r = heavy / ref
    if not 1.0 <= r <= 1000.0:
        return None
    return (
        (phase_seconds, isl1, osl1, round(r, 1)),
        (phase_seconds, isl2, osl2, 1.0),
        (phase_seconds, isl3, osl3, round(r**0.5, 1)),
    )


def scale_segments(segments: Segments, phase_seconds: float) -> Segments:
    """Same shape, different phase length."""
    return tuple((phase_seconds, isl, osl, mult) for _, isl, osl, mult in segments)


def parse_segments(spec: str) -> Segments:
    """`dur:isl_lo-isl_hi:osl_lo-osl_hi:mult`, comma-separated, one per phase.

    The shape of SEGMENTS as a flag, so a driver can put a different workload
    through the same sweep without editing this file. Durations are taken as
    written: a caller that names its phases has already chosen their length.
    """
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            dur, isl, osl, mult = part.split(":")
            isl_lo, isl_hi = (int(x) for x in isl.split("-"))
            osl_lo, osl_hi = (int(x) for x in osl.split("-"))
            out.append((float(dur), (isl_lo, isl_hi), (osl_lo, osl_hi), float(mult)))
        except ValueError as exc:
            raise ValueError(
                f"segment {part!r} is not dur:isl_lo-isl_hi:osl_lo-osl_hi:mult"
            ) from exc
    if not out:
        raise ValueError("no segments in spec")
    return tuple(out)


def load_trace(path: Path, rate: float) -> list[tuple[float, int, int, tuple[int, int] | None]]:
    """Timestamped JSONL rows `{"at", "input_len", "output_len"}`, rate-scaled.

    §6.1's replay method: "we multiply the timestamps by a constant to simulate
    varying request rates", so `rate` is that constant and 2.0 arrives twice as
    fast as recorded. Azure's LLM inference traces convert to this shape.

    Rows may carry `prefix_id` and `prefix_len` (the shared-prefix traces of
    the affinity ablation); they ride through as the fourth element so the prompt
    synthesizer can give same-prefix requests identical heads.
    """
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [
        (
            r["at"] / rate,
            int(r["input_len"]),
            int(r["output_len"]),
            (int(r["prefix_id"]), int(r["prefix_len"]))
            if "prefix_id" in r and "prefix_len" in r
            else None,
        )
        for r in rows
    ]


def make_trace(
    rate: float, seed: int, segments: Segments = SEGMENTS
) -> list[tuple[float, int, int]]:
    """The three-phase trace at `rate`, deterministic in `seed`."""
    rng = random.Random(seed)  # noqa: S311 - deterministic trace synthesis, not cryptography
    out: list[tuple[float, int, int]] = []
    t = 0.0
    for dur, isl, osl, mult in segments:
        end = t + dur
        while True:
            t += rng.expovariate(rate * mult)
            if t >= end:
                t = end
                break
            out.append((t, rng.randint(*isl), rng.randint(*osl)))
    return out


# How many tokens one repetition of the filler costs is a property of the
# serving tokenizer, so the driver calibrates it at startup against the live
# engine (set_tokens_per_repeat; narwhal-bench reads usage.prompt_tokens
# through the router). The default is the reference tokenizer's measurement -
# 4,560 repetitions gave 4,467 tokens - kept only as the fallback when no
# calibration ran. A character-ratio guess under-sizes prefill, leaving the
# prefill-heavy phase far lighter than the trace asks for; a wrong ratio
# silently mis-sizes every prompt.
FILLER = "benchmark "
TOKENS_PER_REPEAT = 0.98
_tokens_per_repeat = TOKENS_PER_REPEAT


def set_tokens_per_repeat(ratio: float) -> None:
    """Calibrate prompt sizing to the serving tokenizer; non-positive is ignored."""
    global _tokens_per_repeat
    if ratio > 0.0:
        _tokens_per_repeat = ratio


def _prompt_of(tokens: int, prefix: tuple[int, int] | None = None, nonce: str = "") -> str:
    """Filler text sized to `tokens`; distinct, stable heads per prefix.

    With a `(prefix_id, prefix_len)` pair, the first `prefix_len` tokens are
    a per-prefix tag block: identical across requests of the same prefix
    (their common text, down to the byte, for the engine's cache and the
    router's affinity key alike) and diverging from every other prefix
    within the first few characters. The tag repeat is treated as one token,
    the same convention the filler measured to within 2%.

    `nonce` marks the tail as this request's own. Without it the tail is
    pure filler, so two same-prefix requests whose lengths coincide - or a
    shorter one following a longer one - produce identical or fully contained
    prompts, and on an engine image that asserts on full-cache hits such a
    request is terminal. A trace replayed with prefix caching on must never
    offer a prompt that another request already cached in full. The nonce
    sits after the shared head, so head byte-identity (the thing the cache
    game measures) is untouched; callers that omit it get the historical
    text, since a caching-off fleet cannot observe the difference.
    """
    mark = f"t{nonce} " if nonce else ""
    if prefix is None:
        return mark + FILLER * max(1, round(tokens / _tokens_per_repeat))
    pid, plen = prefix
    plen = min(plen, tokens)
    tag = f"ctx{pid} "
    head = tag * max(1, round(plen / _tokens_per_repeat))
    tail_tokens = tokens - plen
    if tail_tokens <= 0:
        return head
    return head + mark + FILLER * max(1, round(tail_tokens / _tokens_per_repeat))
