"""Persist and query validated per-engine cost models."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

from ..contracts import PROFILES, validate_document, versioned
from ..provenance import stamp
from ..types import Role
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
        self._by_mix: dict[tuple[str, str, int, int, str], Profile] = {}
        self._group_by_id: dict[str, str] = {}
        self._mix_for_group: Callable[[str], tuple[int, int]] | None = None
        self._role_for_id: Callable[[str], Role] | None = None
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
                self._insert(profile, duplicate_error=True)

    @staticmethod
    def _mix_key(profile: Profile) -> tuple[str, str, int, int, str] | None:
        if profile.colocated_group is None:
            return None
        if (
            profile.colocated_prefill_engines is None
            or profile.colocated_decode_engines is None
            or profile.colocated_target_role is None
        ):
            raise ValueError(f"{profile.iid}: colocated role mix is incomplete")
        return (
            profile.iid,
            profile.colocated_group,
            profile.colocated_prefill_engines,
            profile.colocated_decode_engines,
            profile.colocated_target_role,
        )

    def _insert(self, profile: Profile, *, duplicate_error: bool = False) -> None:
        key = self._mix_key(profile)
        if key is None:
            if duplicate_error and profile.iid in self._by_id:
                raise ValueError(f"{self.path}: duplicate profile id {profile.iid!r}")
            self._by_id[profile.iid] = profile
            return
        prior_group = self._group_by_id.get(profile.iid)
        if prior_group is not None and prior_group != key[1]:
            raise ValueError(
                f"{self.path}: profile {profile.iid} has conflicting shared-device groups"
            )
        if duplicate_error and key in self._by_mix:
            raise ValueError(f"{self.path}: duplicate profile role mix {key!r}")
        self._group_by_id[profile.iid] = key[1]
        self._by_mix[key] = profile

    def bind_role_mix(
        self,
        group_by_id: dict[str, str],
        mix_for_group: Callable[[str], tuple[int, int]],
        role_for_id: Callable[[str], Role] | None = None,
    ) -> None:
        """Select measured shared-device variants from live engine roles."""
        self._group_by_id.update(group_by_id)
        self._mix_for_group = mix_for_group
        self._role_for_id = role_for_id

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

    def get(
        self,
        iid: str,
        *,
        role_mix: tuple[int, int] | None = None,
        role: Role | None = None,
    ) -> Profile | None:
        """Return the profile measured for this engine's shared-device mix."""
        group = self._group_by_id.get(iid)
        if group is not None and any(key[0] == iid for key in self._by_mix):
            mix = role_mix
            if mix is None and self._mix_for_group is not None:
                mix = self._mix_for_group(group)
            if mix is None:
                return None
            target_role = role
            if target_role is None and self._role_for_id is not None:
                target_role = self._role_for_id(iid)
            if target_role is None:
                return None
            return self._by_mix.get((iid, group, *mix, target_role.value))
        return self._by_id.get(iid)

    def profiles_for_split(
        self,
        iids: Iterable[str],
        prefill: int,
        decode: int,
        roles: dict[str, Role] | None = None,
    ) -> tuple[Profile, ...]:
        """Resolve a candidate shared-device mix without extrapolating variants.

        A global split identifies a device mix only when the selected fleet has
        one shared-device group. Legacy profiles remain available to fleets
        without measured colocated variants.
        """
        selected = tuple(dict.fromkeys(iids))
        groups = {self._group_by_id.get(iid) for iid in selected if iid in self._group_by_id}
        if len(groups) > 1 and any(key[0] in selected for key in self._by_mix):
            return ()
        rows = tuple(
            self.get(iid, role_mix=(prefill, decode), role=roles.get(iid) if roles else None)
            for iid in selected
        )
        return tuple(row for row in rows if row is not None) if all(rows) else ()

    def put(self, profile: Profile) -> None:
        """Replace one engine profile and write the store."""
        try:
            profile.validate()
        except ValueError as exc:
            raise ValueError(f"{self.path}: {exc}") from exc
        self._insert(profile)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        profiles = [*self._by_id.values(), *self._by_mix.values()]
        rows = [
            asdict(profile)
            for profile in sorted(
                profiles,
                key=lambda p: (
                    p.iid,
                    p.colocated_group or "",
                    p.colocated_prefill_engines or 0,
                    p.colocated_decode_engines or 0,
                    p.colocated_target_role or "",
                ),
            )
        ]
        body = {"meta": stamp()["meta"], "profiles": rows}
        self.path.write_text(json.dumps(versioned(PROFILES, body), indent=2) + "\n")

    def all_profiles(self) -> tuple[Profile, ...]:
        """Return every standalone and role-mix variant in the store."""
        return (*self._by_id.values(), *self._by_mix.values())

    def profiles_for_engine(self, iid: str) -> tuple[Profile, ...]:
        """Return every measured variant that could be selected for an engine."""
        return tuple(profile for profile in self.all_profiles() if profile.iid == iid)

    def engine_set_diff(self, iids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Return (missing, extra) sorted ids between a fleet and the store.

        `iids` is the configured engine set. Missing ids need profiles; extra ids
        belong to engines outside that set.
        """
        fleet = set(iids)
        stored = set(self._by_id) | {key[0] for key in self._by_mix}
        return sorted(fleet - stored), sorted(stored - fleet)

    def _rows_for(self, iids: Iterable[str] | None) -> list[Profile]:
        """Return stored profiles for `iids`, or every row when unscoped."""
        if iids is None:
            iids = set(self._by_id) | {key[0] for key in self._by_mix}
        return [profile for iid in dict.fromkeys(iids) if (profile := self.get(iid)) is not None]

    def mean_prefill_time(self, input_len: int, iids: Iterable[str] | None = None) -> float | None:
        """Return mean predicted prefill time for the engines selected by `iids`."""
        profiles = self._rows_for(iids)
        if not profiles or not all(p.covers_prefill(input_len) for p in profiles):
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
        return len(set(self._by_id) | {key[0] for key in self._by_mix})
