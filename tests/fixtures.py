"""Fleet and profile fixtures for contract tests."""

from dataclasses import replace
from pathlib import Path

import httpx

from narwhal.config import FleetConfig
from narwhal.engines.attestation import EngineIdentity
from narwhal.engines.prefix import block_identities
from narwhal.engines.validation import validation_pairs
from narwhal.profiling import calibration
from narwhal.profiling.generation import identity_generation
from narwhal.profiling.model import Profile
from narwhal.profiling.probe import device_key
from narwhal.profiling.store import ProfileStore

ROOT = Path(__file__).resolve().parents[1]


def profile(iid="e0", **changes):
    """Return a measured decode domain with finite cost coefficients."""
    row = Profile(
        iid,
        0,
        0.001,
        0.01,
        0.000001,
        0.001,
        kv_capacity_tokens=100_000,
        tpot_request_slope=0.001,
        decode_min_requests=1,
        decode_max_requests=16,
        decode_min_kv_tokens=1,
        decode_max_kv_tokens=100_000,
        decode_fit_mape=0.01,
        decode_cv_mape=0.02,
    )
    return replace(row, **changes)


def warm(iid, **changes):
    """Return a profile with a warm fit that makes cached prefill cheap."""
    fit = {
        "cached_ttft_a": 1e-8,
        "cached_ttft_b": 0.0001,
        "cached_ttft_c": 0.01,
        "cached_ttft_d": 0.0,
        "cached_cv_mape": 0.05,
        "cached_min_prefix_tokens": 4,
        "cached_max_prefix_tokens": 64,
        "cached_min_suffix_tokens": 1,
        "cached_max_suffix_tokens": 64,
    }
    return replace(profile(iid, ttft_a=1e-6, ttft_b=0.002, ttft_c=0.01), **{**fit, **changes})


def put_warm(store, iid, **changes):
    """Store a warm profile for `iid` that keeps the stored generation digest."""
    digest = store.get(iid).generation_digest
    store.put(warm(iid, generation_digest=digest, **changes))


def hold_prefix(view, namespace, tokens, block, *, sequence=None):
    """Make a residency view hold every full block of `tokens`; return their identities."""
    identities = block_identities(namespace, tokens, block)
    view.known, view.block_size = True, block
    if sequence is not None:
        view.sequence = sequence
    view.groups = {"0": ("full_attention", None, set(identities))}
    return identities


def fleet(root, engines=("e0", "e3"), pinned=()):
    """Create a fleet of the named engines and write profiles under the supplied directory."""
    cfg = FleetConfig.load(ROOT / "tests/data/fleet.json")
    specs = {spec.iid: spec for spec in cfg.engines}
    cfg.engines = [
        replace(specs[iid], pin=True) if iid in pinned else specs[iid] for iid in engines
    ]
    cfg.profiles_path = root / "profiles.json"
    store = ProfileStore(cfg.profiles_path)
    for spec in cfg.engines:
        store.put(profile(spec.iid))
    return cfg


def calibration_document(cfg, generations, starts, seconds=0.25):
    """Return complete first-token calibration evidence with every sample at `seconds`."""
    pairs = validation_pairs(cfg.engines, mesh=True)
    rounds = calibration.calibration_rounds(
        pairs, {spec.iid: device_key(spec) for spec in cfg.engines}
    )
    numbers = {pair: number for number, members in enumerate(rounds, 1) for pair in members}
    p99, maximum, candidate = calibration.candidate_deadline([seconds] * 100)
    return {
        "schema": calibration.SCHEMA,
        "schema_version": 1,
        "captured_at_unix": 1000.0,
        "duration_s": 60.0,
        "status": "complete",
        "model": cfg.model,
        "contract_fingerprint": cfg.engine_contract.fingerprint() if cfg.engine_contract else None,
        "engine_urls": {spec.iid: spec.url for spec in cfg.engines},
        "generations": dict(generations),
        "process_starts": dict(starts),
        "changed_generations": [],
        "generation_errors": [],
        "input_tokens": [8],
        "samples_per_group": 100,
        "observation_timeout_s": 10.0,
        "configured_deadline_s": cfg.first_token_timeout_s,
        "candidate_deadline_s": candidate,
        "groups": [
            {
                "producer": src,
                "consumer": dst,
                "target_input_tokens": 8,
                "actual_input_tokens_min": 8,
                "actual_input_tokens_max": 8,
                "completed": 100,
                "failed": 0,
                "p99_seconds": p99,
                "maximum_seconds": maximum,
                "candidate_deadline_s": candidate,
            }
            for src, dst in pairs
        ],
        "attempts": [
            {
                "producer": src,
                "consumer": dst,
                "target_input_tokens": 8,
                "requested_output_tokens": 4,
                "attempt": attempt,
                "round": None if attempt == 1 else numbers[(src, dst)],
                "actual_input_tokens": 8,
                "prefill_seconds": 0.05,
                "first_token_seconds": seconds,
                "status": "completed",
            }
            for src, dst in pairs
            for attempt in range(1, 101)
        ],
    }


def bind_identity_profiles(router):
    """Bind contract-free profiles and serve identities whose start times tests can change."""
    assert router.cfg.engine_contract is None
    starts = dict.fromkeys((spec.iid for spec in router.cfg.engines), 100.0)
    by_url = {spec.url: spec.iid for spec in router.cfg.engines}

    def respond(request):
        iid = by_url[str(request.url).rsplit("/", 1)[0]]
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "fixture"})
        if request.url.path == "/metrics":
            return httpx.Response(200, text=f"process_start_time_seconds {starts[iid]}\n")
        raise AssertionError(request.url.path)

    for iid, start in starts.items():
        generation = identity_generation(EngineIdentity("fixture", start))
        router.profiles.put(replace(router.profiles.get(iid), generation_digest=generation.digest))
    router.lifecycle_transport = httpx.MockTransport(respond)
    return starts


def invalid_token_choices():
    """Output events that require exact-token consumers to reject their identity."""
    return [
        {"text": "x"},
        *({"text": "x", "token_ids": ids} for ids in (None, [], [True], [-1], [1.5], "1")),
        *({"delta": {field: "x"}} for field in ("reasoning", "reasoning_content", "refusal")),
        {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}},
        {"delta": {"function_call": {"arguments": "{}"}}},
    ]


def validation_topologies():
    """Enumerate two-to-four-engine role and pin combinations."""
    from itertools import product

    from narwhal.config import EngineSpec
    from narwhal.types import Role

    choices = [(role, pin) for role in Role for pin in (False, True)]
    for size in range(2, 5):
        for settings in product(choices, repeat=size):
            yield [
                EngineSpec(f"e{index}", f"http://stub-{index}", role=role, pin=pin)
                for index, (role, pin) in enumerate(settings)
            ]
