"""Check an engine set against the profile store and the first-token calibration."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

import httpx

from ..config import EngineSpec, FleetConfig
from .calibration import CalibrationCheck, verify_calibration
from .generation import bound_profiles, generation_problem, nominal_problem, read_generation
from .store import ProfileStore


def engine_set_problem(
    store: ProfileStore, engines: Collection[str], noun: str, *, exact: bool = True
) -> str | None:
    """Return why the store does not hold a profile for exactly `engines`, or None.

    `noun` names the engine set in the message, such as "configured". With `exact` false, the
    store must hold a profile for every engine in `engines`, and a profile for an engine outside
    the set is allowed.
    """
    missing, extra = store.engine_set_diff(engines)
    if not exact:
        extra = []
    if not missing and not extra:
        return None
    configured = noun == "configured"
    outside = "unconfigured engines" if configured else f"engines outside the {noun} set"
    fleet = "the fleet" if configured else f"the {noun} fleet"
    detail = []
    if missing:
        detail.append(f"no profile for {', '.join(missing)}")
    if extra:
        detail.append(f"stale profiles for {outside}: {', '.join(extra)}")
    required = f"exactly the {noun} engine set" if exact else f"every engine in the {noun} set"
    return (
        "; ".join(detail) + f": run `narwhal-profile` against {fleet} first. Placement requires "
        f"a profile for {required}."
    )


async def generation_problems(
    cfg: FleetConfig,
    store: ProfileStore,
    engines: Sequence[EngineSpec],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Return why a profile in `store` does not fit the live engine it describes."""
    problems: list[str] = []
    for spec in engines:
        if cfg.engine_contract is not None and (problem := nominal_problem(store, spec.iid)):
            problems.append(problem)
            continue
        profiles = bound_profiles(store, spec.iid)
        if not profiles:
            continue
        try:
            generation = await read_generation(cfg, spec, transport=transport)
        # A malformed answer names the engine instead of raising.
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(
                f"{spec.iid} profile generation unreadable: {exc}; "
                "restart its attestation sidecar if it answers HTTP 503"
            )
            continue
        for profile in profiles:
            problem = generation_problem(spec.iid, profile.generation_digest, generation.digest)
            if problem and problem not in problems:
                problems.append(problem)
    return problems


@dataclass(frozen=True)
class EngineSetEvidence:
    """The profile store and calibration read for a proposed engine set, and its gaps."""

    profiles: ProfileStore | None
    calibration: CalibrationCheck
    problems: tuple[str, ...]


async def check_engine_set(
    cfg: FleetConfig,
    engines: Sequence[EngineSpec],
    *,
    live: Collection[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EngineSetEvidence:
    """Re-read `profiles.path` and the calibration file and apply the startup rules to `engines`.

    The store must hold a profile for every engine in `engines`. A row for an engine outside
    `engines`, such as an engine that left the membership, is not checked. Each engine in `live`,
    every engine by default, must run the profile's generation. When
    `engine.first_token_calibration_path` is set, the calibration must cover every directed pair
    of `engines` and match the process generation of each live engine.
    """
    ids = [spec.iid for spec in engines]
    try:
        store = ProfileStore(cfg.profiles_path)
    except (OSError, ValueError) as exc:
        return EngineSetEvidence(
            None,
            CalibrationCheck("uncalibrated"),
            (f"profile store {cfg.profiles_path} is unreadable: {exc}",),
        )
    problems: list[str] = []
    # A leave keeps the departed engine's rows, as a resumed engine set does.
    if problem := engine_set_problem(store, ids, "proposed", exact=False):
        problems.append(problem)
    checked = [spec for spec in engines if live is None or spec.iid in live]
    problems.extend(await generation_problems(cfg, store, checked, transport=transport))
    try:
        calibration = await verify_calibration(cfg, engines=engines, live=live, transport=transport)
    except Exception as exc:
        # A malformed answer from a joining engine refuses the join instead of raising.
        calibration = CalibrationCheck(
            "rejected", problems=(f"calibration check failed: {type(exc).__name__}: {exc}",)
        )
    if calibration.status == "rejected":
        problems.extend(calibration.problems)
    return EngineSetEvidence(store, calibration, tuple(problems))
