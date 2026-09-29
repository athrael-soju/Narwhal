"""Router view of each engine sidecar's resident prefix blocks.

The router reads a sidecar snapshot, then applies the sidecar's ordered
changes. It resynchronises from a fresh snapshot when it starts, when the
sidecar reports that the requested changes are gone, when the sidecar epoch
or the engine process changes, and after any failed refresh. An engine
without a sidecar, or whose sidecar serves no residency, is priced cold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import EngineSpec
from ..engines.attestation import ATTESTATION_PATH, RESIDENCY_PATH
from ..engines.residency import cached_prefix_blocks


@dataclass
class EngineResidency:
    """What the router knows about one engine's resident prefix blocks."""

    iid: str
    known: bool = False
    reason: str = "not yet synchronised"
    epoch: str | None = None
    process_start_time_seconds: float | None = None
    sequence: int | None = None
    block_size: int | None = None
    groups: dict[str, tuple[str | None, set[bytes]]] = field(default_factory=dict)
    resyncs: int = 0

    def cached_prefix_blocks(self, identities: Sequence[bytes]) -> int:
        """Return how many leading prompt blocks the engine can reuse, or 0 when unknown."""
        if not self.known:
            return 0
        return cached_prefix_blocks(self.groups.values(), identities)

    def forget(self, reason: str) -> None:
        """Drop the view so the engine is priced cold until the next snapshot."""
        self.known = False
        self.reason = reason
        self.epoch = None
        self.sequence = None
        self.groups.clear()


def _sidecar_base(spec: EngineSpec) -> str | None:
    url = spec.attestation_url
    if not url or not url.endswith(ATTESTATION_PATH):
        return None
    return url[: -len(ATTESTATION_PATH)]


class ResidencySubscriptions:
    """Keep a residency view per engine from its attestation sidecar."""

    def __init__(self, engines: Sequence[EngineSpec]) -> None:
        self.views = {spec.iid: EngineResidency(spec.iid) for spec in engines}
        self._specs = {spec.iid: spec for spec in engines}

    def view(self, iid: str) -> EngineResidency:
        """Return the router's residency view of `iid`."""
        return self.views[iid]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return each engine's synchronisation state without block identities."""
        return {
            iid: {
                "known": view.known,
                "reason": view.reason,
                "epoch": view.epoch,
                "sequence": view.sequence,
                "block_size": view.block_size,
                "resident_blocks": {key: len(blocks) for key, (_, blocks) in view.groups.items()},
                "resyncs": view.resyncs,
            }
            for iid, view in self.views.items()
        }

    async def refresh(self, client: httpx.AsyncClient) -> None:
        """Bring every engine's view up to date; failures leave that engine cold."""
        for iid, spec in self._specs.items():
            view = self.views[iid]
            base = _sidecar_base(spec)
            if base is None:
                view.forget("engine has no attestation sidecar")
                continue
            try:
                following = view.known and view.epoch is not None and view.sequence is not None
                if following and await self._follow(client, base, view):
                    continue
                await self._resync(client, base, view)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                view.forget(f"residency refresh failed: {type(exc).__name__}: {exc}")

    async def _follow(self, client: httpx.AsyncClient, base: str, view: EngineResidency) -> bool:
        """Apply ordered changes; return False when a snapshot is required."""
        response = await client.get(
            base + RESIDENCY_PATH + "/events", params={"after": view.sequence}
        )
        if response.status_code == 410:
            return False
        response.raise_for_status()
        body = response.json()
        if body["epoch"] != view.epoch:
            return False
        for change in body["changes"]:
            if view.sequence is None or change["sequence"] != view.sequence + 1:
                return False
            self._apply(view, change)
        return True

    async def _resync(self, client: httpx.AsyncClient, base: str, view: EngineResidency) -> None:
        response = await client.get(base + RESIDENCY_PATH)
        if response.status_code == 404:
            view.forget("engine publishes no cache events")
            return
        response.raise_for_status()
        snapshot: dict[str, Any] = response.json()
        view.resyncs += 1
        view.epoch = snapshot["epoch"]
        view.process_start_time_seconds = snapshot["process_start_time_seconds"]
        view.sequence = snapshot["sequence"]
        view.block_size = snapshot["block_size"]
        view.known = bool(snapshot["known"])
        view.reason = snapshot["reason"]
        view.groups = {
            str(group["group"]): (group["kind"], {bytes.fromhex(i) for i in group["identities"]})
            for group in snapshot["groups"]
        }

    @staticmethod
    def _apply(view: EngineResidency, change: dict[str, Any]) -> None:
        view.sequence = change["sequence"]
        if change["cleared"]:
            view.groups.clear()
        for key, delta in change["groups"].items():
            kind, blocks = view.groups.get(key, (None, set()))
            blocks.difference_update(bytes.fromhex(i) for i in delta["removed"])
            blocks.update(bytes.fromhex(i) for i in delta["stored"])
            view.groups[key] = (delta.get("kind", kind), blocks)
