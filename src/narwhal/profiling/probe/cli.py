"""The narwhal-profile command."""

from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path

import httpx

from ... import command_results as results
from ...cli_errors import failure
from ...cli_support import add_version_argument
from ...config import FleetConfig
from ..fitting import cached_fit_possible
from .fleet import run
from .neighbours import ColocatedWorkload
from .offline import merge_profiles, refit_saved_prefill
from .sweep import (
    CACHED_PREFIX_LENS,
    CACHED_SUFFIX_LENS,
    DECODE_CONCURRENCY,
    DECODE_INPUT_LENS,
    DECODE_TOKENS,
    PREFILL_LENS,
    PREFILL_REPEATS,
    Sweep,
    warm_cases,
)


def _int_list(text: str) -> tuple[int, ...]:
    """Parse comma-separated integers, skipping blank items."""
    return tuple(int(item) for item in text.split(",") if item.strip())


def main(argv: list[str] | None = None) -> int:
    """Run the profiling CLI."""
    return results.invoke("narwhal-profile", argv, _main, operation="profile")


def _main(argv: list[str]) -> int:
    """Parse one profiling operation."""
    ap = argparse.ArgumentParser(
        description="Measure live prefill/decode curves into fleet profiles.path, refit saved "
        "TTFT samples, or merge measured role mixes. Refits and merges use --out and write "
        "a .samples.json sidecar; sweep and neighbour options apply to live measurement.",
    )
    add_version_argument(ap)
    results.add_format(ap)
    ap.add_argument("--fleet", required=True, help="fleet config JSON")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="live-sweep instance ID; repeatable (default: all configured engines)",
    )
    ap.add_argument(
        "--refit-samples",
        type=Path,
        help="refit TTFT from a generation-bound sample sidecar covering the complete fleet, "
        "retaining decode fits; requires --out; exclusive with --only and --merge",
    )
    ap.add_argument(
        "--merge",
        type=Path,
        action="append",
        default=[],
        help="measured profile store with matching sidecar; repeat at least twice and supply "
        "--out; combined stores must cover the fleet; exclusive with --refit-samples, "
        "--only and --overwrite",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="fresh profile path required for --refit-samples and --merge; "
        "also writes PATH with suffix replaced by .samples.json",
    )
    ap.add_argument(
        "--limits",
        type=Path,
        help="generated max_num_seqs limits for live decode cohorts "
        "(default: use the requested concurrency points)",
    )
    ap.add_argument(
        "--observation-timeout-s",
        type=float,
        help="positive diagnostic HTTP timeout for live health, tokenization, prefill, "
        "decode, metrics and generation probes (default: each probe's built-in limit)",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="replace live-sweep profiles and sample sidecar; --only retains selected engines "
        "(default: require fresh outputs); refits always require fresh outputs",
    )
    ap.add_argument(
        "--prefill-lens",
        default=",".join(str(n) for n in PREFILL_LENS),
        help="comma-separated positive input token lengths; at least three usable distinct "
        "points within live max_model_len (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-concurrency",
        default=",".join(str(n) for n in DECODE_CONCURRENCY),
        help="comma-separated positive simultaneous stream counts; at least two usable "
        "distinct points after --limits (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-input-lens",
        default=",".join(str(n) for n in DECODE_INPUT_LENS),
        help="comma-separated positive input token lengths; at least two usable distinct "
        "points with room for --decode-tokens (default: %(default)s)",
    )
    ap.add_argument(
        "--cached-prefix-lens",
        default=",".join(str(n) for n in CACHED_PREFIX_LENS),
        help="comma-separated cached prefix lengths for the warm prefill sweep, which runs "
        "when the engine reuses a cached prefix; at least two, and at least five cases "
        "with --cached-suffix-lens (default: %(default)s)",
    )
    ap.add_argument(
        "--cached-suffix-lens",
        default=",".join(str(n) for n in CACHED_SUFFIX_LENS),
        help="comma-separated uncached suffix lengths for the warm prefill sweep; "
        "at least two (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-tokens",
        type=int,
        default=DECODE_TOKENS,
        help="output tokens per decode stream, at least 3 (default: %(default)s)",
    )
    ap.add_argument(
        "--decode-repeats",
        type=int,
        default=1,
        help="repetitions per decode input/concurrency point, at least 1 (default: %(default)s)",
    )
    ap.add_argument(
        "--prefill-repeats",
        type=int,
        default=PREFILL_REPEATS,
        help="repetitions per prefill length, at least 3; fit the median (default: %(default)s)",
    )
    ap.add_argument(
        "--colocated",
        action="store_true",
        help="load peers in each target's shared_device group during the live sweep; "
        "requires all five --neighbour-* options (default: target-only traffic)",
    )
    ap.add_argument(
        "--neighbour-prefill-rps",
        type=float,
        help="finite positive requests/second per prefill neighbour; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-rps",
        type=float,
        help="finite positive requests/second per decode neighbour; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-prefill-tokens",
        type=int,
        help="positive input tokens per prefill neighbour request (one output token); "
        "required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-input-tokens",
        type=int,
        help="positive input tokens per decode neighbour request; required with --colocated",
    )
    ap.add_argument(
        "--neighbour-decode-output-tokens",
        type=int,
        help="positive output tokens per decode neighbour request; input plus output must "
        "fit its live max_model_len; required with --colocated",
    )
    args = ap.parse_args(argv)
    if args.observation_timeout_s is not None and (
        not math.isfinite(args.observation_timeout_s) or args.observation_timeout_s <= 0
    ):
        ap.error("--observation-timeout-s must be finite and positive")
    if args.observation_timeout_s is not None and (args.merge or args.refit_samples):
        ap.error("--observation-timeout-s applies only to live profiling")
    try:
        cfg = FleetConfig.load(args.fleet)
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"load fleet {args.fleet}", exc, 2)
    if results.json_mode():
        results.protect_environment(cfg.engine_api_key_env)
        results.set_operation(
            "merge" if args.merge else "refit" if args.refit_samples else "profile"
        )
        output_path = args.out or cfg.profiles_path
        results.add_artifact("profiles", output_path)
        results.add_artifact("profile_samples", output_path.with_suffix(".samples.json"))
        results.set_data(
            {
                "engines": [
                    engine.iid for engine in cfg.engines if not args.only or engine.iid in args.only
                ]
            }
        )
    if any(
        path.resolve() == Path(args.fleet).resolve()
        or (path.exists() and path.samefile(args.fleet))
        for path in (cfg.profiles_path, cfg.profiles_path.with_suffix(".samples.json"))
    ):
        ap.error("profile outputs must not replace the fleet config")
    try:
        sweep = Sweep(
            prefill_lens=_int_list(args.prefill_lens),
            decode_concurrency=_int_list(args.decode_concurrency),
            decode_input_lens=_int_list(args.decode_input_lens),
            decode_tokens=args.decode_tokens,
            decode_repeats=args.decode_repeats,
            prefill_repeats=args.prefill_repeats,
            cached_prefix_lens=_int_list(args.cached_prefix_lens),
            cached_suffix_lens=_int_list(args.cached_suffix_lens),
        )
    except ValueError:
        ap.error(
            "--prefill-lens, --decode-input-lens, --decode-concurrency, --cached-prefix-lens "
            "and --cached-suffix-lens take comma-separated integers"
        )
    if not cached_fit_possible(warm_cases(sweep)):
        ap.error(
            "the warm prefill fit needs two distinct prefix lengths, two distinct suffix "
            "lengths and at least five prefix and suffix cases"
        )
    if any(value <= 0 for value in (*sweep.cached_prefix_lens, *sweep.cached_suffix_lens)):
        ap.error("cached prefix and suffix lengths must be positive")
    if len(set(sweep.prefill_lens)) < 3:
        ap.error("the sweep needs at least three distinct prefill lengths")
    if len(set(sweep.decode_input_lens)) < 2 or len(set(sweep.decode_concurrency)) < 2:
        ap.error(
            "the decode fit needs two distinct input lengths and two distinct concurrency steps"
        )
    if any(value <= 0 for value in (*sweep.prefill_lens, *sweep.decode_input_lens)):
        ap.error("profile lengths must be positive")
    if any(value < 1 for value in sweep.decode_concurrency):
        ap.error("decode concurrency must be at least 1")
    if sweep.decode_tokens < 3 or sweep.prefill_repeats < 3 or sweep.decode_repeats < 1:
        ap.error(
            "--decode-tokens needs at least 3 (two intervals need three tokens); "
            "--prefill-repeats at least 3 for a repeat median; --decode-repeats at least 1"
        )
    neighbour_values = (
        args.neighbour_prefill_rps,
        args.neighbour_decode_rps,
        args.neighbour_prefill_tokens,
        args.neighbour_decode_input_tokens,
        args.neighbour_decode_output_tokens,
    )
    if args.colocated:
        if any(
            value is None or not math.isfinite(value) or value <= 0 for value in neighbour_values
        ):
            ap.error("--colocated requires positive neighbour rates and token lengths")
        if any(type(value) is not int for value in neighbour_values[2:]):
            ap.error("neighbour token lengths must be integers")
        colocated_workload = ColocatedWorkload(*neighbour_values)
    else:
        if any(value is not None for value in neighbour_values):
            ap.error("neighbour load options require --colocated")
        colocated_workload = None
    try:
        if args.merge:
            if args.refit_samples is not None or args.out is None or args.only or args.overwrite:
                ap.error("--merge requires --out and cannot combine with refit, only, or overwrite")
            if len(args.merge) < 2:
                ap.error("--merge needs at least two measured profile stores")
            return merge_profiles(args.merge, args.out, {engine.iid for engine in cfg.engines})
        if args.refit_samples is not None or args.out is not None:
            if args.refit_samples is None or args.out is None or args.only:
                ap.error("--refit-samples requires --out and a complete fleet selection")
            return refit_saved_prefill(
                args.refit_samples, args.out, {engine.iid for engine in cfg.engines}
            )
        return asyncio.run(
            run(
                cfg,
                set(args.only) or None,
                sweep,
                overwrite=args.overwrite,
                limits_path=args.limits,
                colocated_workload=colocated_workload,
                observation_timeout_s=args.observation_timeout_s,
            )
        )
    except (OSError, ValueError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"profile fleet {args.fleet}", exc, 2)
    except (RuntimeError, httpx.HTTPError) as exc:
        if results.json_mode():
            raise
        return failure("narwhal-profile", f"profile fleet {args.fleet}", exc, 1)


if __name__ == "__main__":
    raise SystemExit(main())
