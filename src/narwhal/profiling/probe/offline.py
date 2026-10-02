"""Offline prefill refit and profile merge."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from ...contracts import PROFILES, versioned
from ...provenance import stamp
from ..fitting import fit_prefill_samples
from ..model import CACHED_PROFILE_FIELDS, Profile
from ..store import ProfileStore
from .prefill import prefill_fields
from .warm import _valid_cached_samples, apply_cached_fit


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
        updated = Profile(
            **{
                **asdict(old),
                **prefill_fields(prefill_fit, block_tokens),
                **dict.fromkeys(CACHED_PROFILE_FIELDS),
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
        profiles.append(asdict(updated))
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


def _evidence_matches(saved: object, row: Profile) -> bool:
    """Return whether saved profile evidence describes `row`; absent optional fields are unset."""
    if not isinstance(saved, dict):
        return False
    try:
        return Profile(**saved) == row
    except (TypeError, ValueError):
        return False


def merge_profiles(sources: list[Path], output_path: Path, engine_ids: set[str]) -> int:
    """Combine separately measured role mixes, retaining source sidecars."""
    sidecar_path = output_path.with_suffix(".samples.json")
    for path in (output_path, sidecar_path):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"output exists: {path}; choose a fresh profile path")
    store = ProfileStore(output_path, load=False)
    records = []
    profiles: list[Profile] = []
    seen: set[tuple[str, str | None, int | None, int | None, str | None]] = set()
    for source in sources:
        source_store = ProfileStore(source)
        samples = source.with_suffix(".samples.json")
        if not samples.is_file():
            raise FileNotFoundError(f"missing source measurement sidecar: {samples}")
        record = json.loads(samples.read_text())
        if not isinstance(record, dict) or not isinstance(record.get("engines"), dict):
            raise ValueError(f"invalid source measurement sidecar: {samples}")
        rows = source_store.all_profiles()
        if not rows:
            raise ValueError(f"empty source profiles: {source}")
        for row in rows:
            if row.iid not in engine_ids:
                raise ValueError(f"{source}: profile {row.iid} is outside the fleet")
            evidence = record["engines"].get(row.iid)
            if not isinstance(evidence, dict) or not _evidence_matches(
                evidence.get("profile"), row
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
            if key in seen:
                raise ValueError(f"duplicate measured profile variant: {key}")
            seen.add(key)
            profiles.append(row)
        records.append(
            {
                "profiles": str(source),
                "profiles_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "samples": str(samples),
                "samples_sha256": hashlib.sha256(samples.read_bytes()).hexdigest(),
            }
        )
    missing = engine_ids - {row.iid for row in profiles}
    if missing:
        raise ValueError(f"merged profiles miss fleet engines: {', '.join(sorted(missing))}")
    for row in profiles:
        store.put(row)
    sidecar_path.write_text(json.dumps({"method_version": 3, "sources": records}, indent=2) + "\n")
    print(f"merged {len(seen)} measured profiles to {output_path}")
    return 0
