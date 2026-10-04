"""Fleet configuration and profiles for simulated engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.engines.attestation import EngineIdentity
from narwhal.profiling.generation import identity_generation
from narwhal.profiling.model import Profile
from narwhal.profiling.store import ProfileStore
from narwhal.types import Role

MODEL = "simulated"
MAX_CONNECTIONS = 4096


@dataclass(frozen=True)
class Shape:
    engines: int
    prefill_engines: int
    input_tokens: int
    output_tokens: int
    token_interval_s: float
    frames_per_write: int
    prefill_s: float
    ttft_slo_s: float
    tpot_slo_s: float


def engine_roles(engines: list[dict], prefill_engines: int) -> dict[str, Role]:
    """Map each engine ID to its role; the first `prefill_engines` engines prefill."""
    return {
        line["iid"]: Role.PREFILL if index < prefill_engines else Role.DECODE
        for index, line in enumerate(engines)
    }


def write_fleet(out: Path, engines: list[dict], shape: Shape) -> Path:
    """Write the fleet and profiles for simulated engines from their ready lines."""
    path = out / "fleet.json"
    roles = engine_roles(engines, shape.prefill_engines)
    FleetConfig(
        model=MODEL,
        engines=[EngineSpec(line["iid"], line["url"], roles[line["iid"]]) for line in engines],
        slo=SLO(shape.ttft_slo_s, shape.tpot_slo_s),
        advisory=True,
        max_connections=MAX_CONNECTIONS,
        profiles_path=out / "profiles.json",
        state_path=out / "state.json",
    ).save(path)
    store = ProfileStore(out / "profiles.json", load=False)
    kv_tokens = MAX_CONNECTIONS * (shape.input_tokens + shape.output_tokens)
    for line in engines:
        identity = EngineIdentity(line["version"], line["process_start_time_seconds"])
        store.put(
            Profile(
                line["iid"],
                ttft_a=0.0,
                ttft_b=0.0,
                ttft_c=shape.prefill_s,
                tpot_slope=0.0,
                tpot_intercept=shape.token_interval_s,
                tpot_request_slope=0.0,
                decode_min_requests=1,
                decode_max_requests=MAX_CONNECTIONS,
                decode_min_kv_tokens=1,
                decode_max_kv_tokens=kv_tokens,
                kv_capacity_tokens=kv_tokens,
                decode_fit_mape=0.0,
                decode_cv_mape=0.0,
                generation_digest=identity_generation(identity).digest,
            )
        )
    return path
