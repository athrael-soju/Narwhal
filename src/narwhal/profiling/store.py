"""Persist and query validated per-engine cost models."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

from ..contracts import PROFILES, validate_document, versioned
from ..provenance import stamp
from .model import _FIELD_NAMES, _REQUIRED_FIELDS, Profile


def _reject_constant(name: str) -> NoReturn:
    # json's default accepts NaN/Infinity tokens; no profile value may be
    # non-finite because NaN comparisons pass every range check.
    raise ValueError(f"non-finite JSON constant {name}")


class ProfileStore:
    """Persist profiles by engine ID."""

    def __init__(self, path: Path, *, load: bool = True) -> None:
        self.path = path
        self._by_id: dict[str, Profile] = {}
        if load and path.exists():
            document = json.loads(path.read_text(), parse_constant=_reject_constant)
            validate_document(document, PROFILES)
            unknown = sorted(set(document) - {"schema", "schema_version", "meta", "profiles"})
            if unknown:
                raise ValueError(f"unknown profile-store field(s): {', '.join(unknown)}")
            rows = document.get("profiles")
            if not isinstance(rows, list):
                raise ValueError("profile store requires a profiles list")
            for index, raw in enumerate(rows):
                profile = self._load_row(index, raw)
                if profile.iid in self._by_id:
                    raise ValueError(f"{self.path}: duplicate profile id {profile.iid!r}")
                self._by_id[profile.iid] = profile

    def _load_row(self, index: int, raw: Any) -> Profile:
        """Validate one persisted row, naming the file, row, and field on failure."""
        if not isinstance(raw, dict):
            raise ValueError(f"{self.path}: profile row {index} must be an object")
        iid = raw.get("iid")
        if not isinstance(iid, str) or not iid:
            raise ValueError(f"{self.path}: profile row {index}: iid must be a nonempty string")
        unknown = sorted(set(raw) - _FIELD_NAMES)
        if unknown:
            raise ValueError(
                f"{self.path}: profile {iid}: unknown profile field(s): {', '.join(unknown)}"
            )
        missing = sorted(name for name in _REQUIRED_FIELDS if name not in raw)
        if missing:
            raise ValueError(
                f"{self.path}: profile {iid}: missing profile field(s): {', '.join(missing)}"
            )
        try:
            return Profile(**raw)
        except ValueError as exc:
            raise ValueError(f"{self.path}: {exc}") from exc

    def get(self, iid: str) -> Profile | None:
        """Return an engine profile when one has been measured."""
        return self._by_id.get(iid)

    def put(self, profile: Profile) -> None:
        """Replace one engine profile and write the store."""
        try:
            profile.validate()
        except ValueError as exc:
            raise ValueError(f"{self.path}: {exc}") from exc
        self._by_id[profile.iid] = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [asdict(self._by_id[iid]) for iid in sorted(self._by_id)]
        body = {"meta": stamp()["meta"], "profiles": rows}
        self.path.write_text(json.dumps(versioned(PROFILES, body), indent=2) + "\n")

    def engine_set_diff(self, iids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Return (missing, extra) sorted ids between a fleet and the store.

        `iids` is the configured engine set. Missing ids need profiles; extra ids
        belong to engines outside that set.
        """
        fleet = set(iids)
        stored = set(self._by_id)
        return sorted(fleet - stored), sorted(stored - fleet)

    def _rows_for(self, iids: Iterable[str] | None) -> list[Profile]:
        """Return stored profiles for `iids`, or every row when unscoped."""
        if iids is None:
            return list(self._by_id.values())
        return [
            profile for iid in dict.fromkeys(iids) if (profile := self._by_id.get(iid)) is not None
        ]

    def mean_prefill_time(self, input_len: int, iids: Iterable[str] | None = None) -> float | None:
        """Return mean predicted prefill time for the engines selected by `iids`."""
        profiles = self._rows_for(iids)
        if not profiles:
            return None
        times = [p.prefill_time(input_len) for p in profiles]
        return sum(times) / len(times)

    def mean_max_tokens(
        self, tpot_slo_s: float, batch_requests: float = 0.0, iids: Iterable[str] | None = None
    ) -> float | None:
        """Return the mean decode capacity at the TPOT target.

        `iids` scopes the mean to the configured engine set.
        """
        profiles = self._rows_for(iids)
        if not profiles:
            return None
        caps = [p.max_tokens(tpot_slo_s, batch_requests) for p in profiles]
        return sum(caps) / len(caps)

    def mean_token_interval(
        self, batch_tokens: float, batch_requests: float = 0.0, iids: Iterable[str] | None = None
    ) -> float | None:
        """Return the mean predicted decode interval at a batch size.

        `iids` scopes the mean to the configured engine set.
        """
        profiles = self._rows_for(iids)
        if not profiles:
            return None
        intervals = [p.token_interval(batch_tokens, batch_requests) for p in profiles]
        return sum(intervals) / len(intervals)

    def mean_decode_rps(
        self,
        tpot_slo_s: float,
        context_tokens: float,
        output_tokens: float,
        *,
        correction: float = 1.0,
        iids: Iterable[str] | None = None,
    ) -> float | None:
        """Return mean request capacity across the measured decode profiles.

        `iids` scopes the mean to the configured engine set.
        """
        profiles = self._rows_for(iids)
        if not profiles:
            return None
        capacities = [
            profile.decode_rps(
                tpot_slo_s,
                context_tokens,
                output_tokens,
                correction=correction,
            )
            for profile in profiles
        ]
        return sum(capacities) / len(capacities)

    def covers_decode(
        self, batch_requests: float, batch_tokens: float, iids: Iterable[str] | None = None
    ) -> bool:
        """Return whether every engine profile covers a decode point.

        `iids` scopes coverage to the configured engine set.
        """
        profiles = self._rows_for(iids)
        return bool(profiles) and all(
            profile.covers_decode(batch_requests, batch_tokens) for profile in profiles
        )

    def __len__(self) -> int:
        return len(self._by_id)
