"""Refit saved TTFT samples, merge measured profile stores, and copy a member's profile."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import httpx

from ...backends import load as load_backend
from ...config import FleetConfig
from ...contracts import PROFILES, versioned
from ...engines.attestation import launch_digest
from ...provenance import stamp
from ..fitting import fit_prefill_samples
from ..generation import binding_digest, read_generation
from ..model import (
    CACHED_PROFILE_FIELDS,
    PREFILL_SHARE_FIELDS,
    Profile,
    evidence_matches,
    profile_row,
)
from ..store import ProfileStore, measured_evidence
from .engine import kv_capacity
from .instance import prefill_fields
from .warm import apply_cached_fit


def _valid_cached_samples(samples: Any) -> bool:
    """Return whether saved warm samples have the shape the profiler writes."""
    return isinstance(samples, list) and all(
        isinstance(sample, dict)
        and sample.get("state") in ("warm", "cold")
        and all(
            type(sample.get(key)) in (int, float)
            for key in ("prefix_tokens", "suffix_tokens", "seconds")
        )
        for sample in samples
    )


def refit_saved_prefill(samples_path: Path, output_path: Path, engine_ids: set[str]) -> int:
    """Rebuild TTFT coefficients from retained repeats while keeping measured decode fits."""
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    record = json.loads(samples_path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("engines"), dict):
        raise ValueError(f"invalid profile samples: {samples_path}")
    rows = record["engines"]
    if set(rows) != engine_ids:
        raise ValueError("saved profile samples must cover every configured engine")
    profiles = []
    for iid in sorted(engine_ids):
        row = rows[iid]
        if not isinstance(row, dict) or not isinstance(row.get("prefill"), list):
            raise ValueError(f"{iid}: saved prefill samples are missing")
        if any(
            not isinstance(point, list)
            or len(point) != 2
            or any(type(value) not in (int, float) for value in point)
            for point in row["prefill"]
        ):
            raise ValueError(f"{iid}: saved prefill samples are invalid")
        try:
            samples = [tuple(point) for point in row["prefill"]]
            old = Profile(**row["profile"])
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError(f"{iid}: saved profile evidence is invalid") from exc
        if old.iid != iid:
            raise ValueError(f"{iid}: saved profile identity differs from the fleet")
        if old.generation_digest is None or not isinstance(row.get("generation_evidence"), dict):
            raise ValueError(f"{iid}: saved samples lack generation evidence; reprofile the engine")
        block_tokens = row.get("prefill_block_tokens")
        if block_tokens is not None and (type(block_tokens) is not int or block_tokens < 1):
            raise ValueError(f"{iid}: saved prefill block size is invalid")
        prefill_fit, representatives, error = fit_prefill_samples(samples, block_tokens)
        cold = prefill_fields(prefill_fit, block_tokens)
        # The prefill-share fit priced its stalls with the saved prefill coefficients.
        stale = any(getattr(old, name) != value for name, value in cold.items())
        updated = Profile(
            **{
                **asdict(old),
                **cold,
                **dict.fromkeys(CACHED_PROFILE_FIELDS),
                **(dict.fromkeys(PREFILL_SHARE_FIELDS) if stale else {}),
            }
        )
        cached = row.get("cached_prefill") or {}
        if cached.get("reason") is not None and cached.get("cv_mape") is None:
            # A stopped live sweep stays cold.
            row["cached_prefill"] = {
                "samples": cached.get("samples", []),
                "reason": cached["reason"],
            }
        elif cached.get("samples"):
            if not _valid_cached_samples(cached["samples"]):
                raise ValueError(f"{iid}: saved cached prefill samples are invalid")
            try:
                refit, fit = apply_cached_fit(updated, cached["samples"])
            except (KeyError, TypeError) as exc:
                raise ValueError(f"{iid}: saved cached prefill samples are invalid") from exc
            except ValueError as exc:
                row["cached_prefill"] = {"samples": cached["samples"], "reason": str(exc)}
            else:
                updated = refit
                row["cached_prefill"] = {"samples": cached["samples"], **fit}
        row.update(
            prefill_fit_points=representatives,
            prefill_fit_mape=error,
            profile=asdict(updated),
        )
        print(f"  {iid}: prefill median fit MAPE {error:.1%}")
        profiles.append(profile_row(updated))
    record["method_version"] = 2
    record["prefill_refit_source"] = str(samples_path)
    profile_document = versioned(PROFILES, {"meta": stamp()["meta"], "profiles": profiles})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for path, document in ((output_path, profile_document), (sidecar_path, record)):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(document, indent=2) + "\n")
    print(f"refitted {len(profiles)} profile(s) to {output_path}")
    return 0


def merge_profiles(sources: list[Path], output_path: Path, engine_ids: set[str]) -> int:
    """Combine measured profile stores; a later store's variant replaces an earlier one."""
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    store = ProfileStore(output_path, load=False)
    records = []
    profiles: dict[tuple[str, str | None, int | None, int | None, str | None], Profile] = {}
    origin: dict[tuple[str, str | None, int | None, int | None, str | None], Path] = {}
    left_out: set[str] = set()
    for source in sources:
        source_store = ProfileStore(source)
        samples = source.with_suffix(".samples.json")
        evidence, problems = measured_evidence(samples)
        if problems:
            raise problems[0]
        rows = source_store.all_profiles()
        if not rows:
            raise ValueError(f"empty source profiles: {source}")
        for row in rows:
            if row.iid not in engine_ids:
                left_out.add(row.iid)
                continue
            if not any(
                evidence_matches(saved.get("profile"), row) for saved in evidence.get(row.iid, [])
            ):
                raise ValueError(
                    f"{samples}: profile {row.iid} lacks matching measurement evidence"
                )
            key = (
                row.iid,
                row.colocated_group,
                row.colocated_prefill_engines,
                row.colocated_decode_engines,
                row.colocated_target_role,
            )
            if origin.get(key) == source:
                raise ValueError(f"duplicate measured profile variant: {key}")
            if key in origin:
                print(f"{row.iid}: {source} replaces the profile from {origin[key]}")
            profiles[key] = row
            origin[key] = source
        records.append(
            {
                "profiles": os.path.relpath(source.resolve(), output_path.resolve().parent),
                "profiles_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "samples": os.path.relpath(samples.resolve(), output_path.resolve().parent),
                "samples_sha256": hashlib.sha256(samples.read_bytes()).hexdigest(),
            }
        )
    missing = engine_ids - {row.iid for row in profiles.values()}
    if missing:
        raise ValueError(f"merged profiles miss fleet engines: {', '.join(sorted(missing))}")
    for row in profiles.values():
        store.put(row)
    sidecar_path.write_text(json.dumps({"method_version": 3, "sources": records}, indent=2) + "\n")
    if left_out:
        print(f"left out profiles for engines outside the fleet: {', '.join(sorted(left_out))}")
    print(f"merged {len(profiles)} measured profiles to {output_path}")
    return 0


def _fresh_outputs(output_path: Path) -> Path:
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    return sidecar_path


async def copy_profile(
    cfg: FleetConfig,
    source: str,
    target: str,
    output_path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Write `source`'s measured profile under `target` after checking the live target engine.

    The profile at `profiles.path` and its sidecar evidence must describe `source`. The target
    engine must attest the fleet's engine contract with the same launch, model files and
    generation digest as the profile, and report the same KV capacity. The output store holds
    only `target`'s rows; merge it into the fleet's store as after a sweep.
    """
    sidecar_path = _fresh_outputs(output_path)
    specs = {spec.iid: spec for spec in cfg.engines}
    if target not in specs:
        raise ValueError(f"{target} is not an engine in the fleet file")
    if source == target:
        raise ValueError("--copy-from names the engine that --only selects")
    if cfg.engine_contract is None or cfg.contract_missing():
        raise ValueError(
            "copying a profile requires a complete engine_contract, so that the generation "
            "digest names the launch and model files"
        )
    store = ProfileStore(cfg.profiles_path)
    rows = store.profiles_for_engine(source)
    if not rows:
        raise ValueError(f"{cfg.profiles_path} holds no profile for {source}")
    if store.profiles_for_engine(target):
        raise ValueError(f"{cfg.profiles_path} already holds a profile for {target}")
    for row in rows:
        if row.nominal or row.generation_digest is None:
            raise ValueError(f"{source} has a nominal profile; copy only a measured one")
        if row.colocated_group is not None:
            raise ValueError(
                f"{source} was profiled in shared-device group {row.colocated_group}; "
                "profile the new engine in its own group"
            )
    samples = cfg.profiles_path.with_suffix(".samples.json")
    evidence, problems = measured_evidence(samples)
    if problems:
        raise problems[0]
    saved = []
    for row in rows:
        match = next(
            (
                item
                for item in evidence.get(source, [])
                if evidence_matches(item.get("profile"), row)
            ),
            None,
        )
        if match is None:
            raise ValueError(f"{samples}: profile {source} lacks matching measurement evidence")
        saved.append(match)
    spec = specs[target]
    generation = await read_generation(cfg, spec, transport=transport)
    for row, item in zip(rows, saved, strict=True):
        member = item.get("generation_evidence")
        if not isinstance(member, dict) or binding_digest(member) != row.generation_digest:
            raise ValueError(
                f"{samples}: the {source} generation evidence does not match its profile"
            )
        measured, live = shared_generation(member), shared_generation(generation.document)
        if measured is None or live is None or measured != live:
            raise ValueError(
                f"{target} runs generation {generation.digest}, and the {source} profile was "
                f"measured on {row.generation_digest}: the engine contract, launch or model "
                "files differ; profile the new engine"
            )
    metrics = load_backend(cfg.backend).metrics
    async with httpx.AsyncClient(
        timeout=cfg.health_timeout_s, headers=cfg.engine_headers(), transport=transport
    ) as client:
        capacity = await kv_capacity(client, spec.url, metrics, cfg.health_timeout_s)
    for row in rows:
        if row.kv_capacity_tokens is not None and capacity != row.kv_capacity_tokens:
            live = "unreadable" if capacity is None else f"{capacity} tokens"
            raise ValueError(
                f"{target} KV capacity is {live}, and the {source} profile was measured with "
                f"{row.kv_capacity_tokens} tokens; profile the new engine"
            )
    # Each copied row binds to the new engine's own generation, as a sweep's row would.
    copied = [replace(row, iid=target, generation_digest=generation.digest) for row in rows]
    engines = {
        target: {
            **saved[0],
            "profile": asdict(copied[0]),
            "generation_evidence": generation.document,
            "copied_from": {
                "iid": source,
                "profiles": os.path.relpath(
                    cfg.profiles_path.resolve(), output_path.resolve().parent
                ),
                "profiles_sha256": hashlib.sha256(cfg.profiles_path.read_bytes()).hexdigest(),
                "kv_capacity_tokens": capacity,
            },
        }
    }
    out = ProfileStore(output_path, load=False)
    for row in copied:
        out.put(row)
    record = {"method_version": 2, "engines": engines}
    descriptor = os.open(sidecar_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(json.dumps(record, indent=2) + "\n")
    print(f"copied the {source} profile to {target} in {output_path}")
    return 0


def shared_generation(payload: dict[str, Any]) -> str | None:
    """Digest an attestation's contract and launch evidence without its engine's own launch file.

    `launch_sha256` covers the whole launch file, which differs between engines in their ports
    and devices, so two engines with the same launch never share a generation digest. The rest
    of the launch evidence names the launch arguments other than per-engine flags, the image,
    the packages and the model files. Without launch evidence, return None.
    """
    contract, launch = payload.get("contract"), payload.get("launch")
    if not isinstance(contract, dict) or not isinstance(launch, dict):
        return None
    if payload.get("launch_digest") != launch_digest(contract, launch):
        return None
    return launch_digest(contract, {k: v for k, v in launch.items() if k != "launch_sha256"})
