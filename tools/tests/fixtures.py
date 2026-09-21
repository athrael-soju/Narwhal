"""Shared CPU fleet and profile fixtures for contract-level tests."""

from dataclasses import replace
from pathlib import Path

from narwhal.config import FleetConfig
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore

ROOT = Path(__file__).resolve().parents[2]


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


def fleet(root):
    """Create a two-engine stub fleet and write profiles under the supplied directory."""
    cfg = FleetConfig.load(ROOT / "config/fleet.stub.json")
    cfg.engines = [cfg.engines[0], cfg.engines[3]]
    cfg.profiles_path = root / "profiles.json"
    store = ProfileStore(cfg.profiles_path)
    for spec in cfg.engines:
        store.put(profile(spec.iid))
    return cfg


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
