"""Preserve unchanged measurements when a managed fleet profiles selected engines."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from narwhal.contracts import PROFILES, versioned
from narwhal.provenance import stamp

from .store import ProfileStore


def _measured(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = ProfileStore(path).all_profiles()
    samples = path.with_suffix(".samples.json")
    data = samples.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Profile measurement sidecar exceeds its byte limit")
    document = json.loads(data)
    evidence = document.get("engines")
    if not isinstance(evidence, dict):
        raise ValueError("Managed profile updates require per-engine sample evidence")
    rows = {}
    for profile in profiles:
        if profile.iid in rows or profile.colocated_group is not None:
            raise ValueError("Managed SSH profiles require one disjoint-device variant per engine")
        row = evidence.get(profile.iid)
        if not isinstance(row, dict) or row.get("profile") != asdict(profile):
            raise ValueError("Profile does not match its retained measurement evidence")
        if profile.generation_digest is None or not isinstance(
            row.get("generation_evidence"), dict
        ):
            raise ValueError("Profile samples lack generation evidence")
        rows[profile.iid] = row
    return rows, {
        "profiles": str(path),
        "profiles_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "samples": str(samples),
        "samples_sha256": hashlib.sha256(data).hexdigest(),
    }


def combine_selected(previous: Path, selected: Path, output: Path, engine_ids: set[str]) -> None:
    """Publish fresh selected profiles with unchanged rows and their original samples."""
    old, old_source = _measured(previous)
    new, new_source = _measured(selected)
    if set(old) != engine_ids or not new or not set(new) <= engine_ids:
        raise ValueError("Managed profile inputs do not cover the registered fleet")
    rows = {**old, **new}
    documents = (
        (
            output.with_suffix(".samples.json"),
            {"method_version": 2, "engines": rows, "sources": [old_source, new_source]},
        ),
        (
            output,
            versioned(
                PROFILES,
                {
                    "meta": stamp()["meta"],
                    "profiles": [rows[iid]["profile"] for iid in sorted(rows)],
                },
            ),
        ),
    )
    if any(path.exists() or path.is_symlink() for path, _ in documents):
        raise FileExistsError("Managed profile output already exists")
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    for path, document in documents:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
