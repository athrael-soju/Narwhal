"""Select distinct-engine KV transfers permitted by role pins."""

from __future__ import annotations

from ..config import EngineSpec
from ..types import Role


def can_produce(spec: EngineSpec) -> bool:
    """Return whether role pins permit prefill."""
    return not (spec.pin and spec.role is Role.DECODE)


def can_consume(spec: EngineSpec) -> bool:
    """Return whether role pins permit decode."""
    return not (spec.pin and spec.role is Role.PREFILL)


def pairs_of(
    producers: list[str], consumers: list[str], mesh: bool = False
) -> list[tuple[str, str]]:
    """Cover eligible peers with rotating pairs or every ordered mesh pair."""
    if mesh:
        return [(a, b) for a in producers for b in consumers if a != b]
    pairs: list[tuple[str, str]] = []
    cursor = 0
    for producer in producers:
        for offset in range(len(consumers)):
            index = (cursor + offset) % len(consumers)
            consumer = consumers[index]
            if producer != consumer:
                pairs.append((producer, consumer))
                cursor = index + 1
                break
    covered = {consumer for _, consumer in pairs}
    for consumer in consumers:
        if consumer not in covered:
            peer = next((iid for iid in producers if iid != consumer), None)
            if peer is not None:
                pairs.append((peer, consumer))
    return pairs


def validation_pairs(specs: list[EngineSpec], mesh: bool = False) -> list[tuple[str, str]]:
    """Select transfers covering every engine role with an eligible peer."""
    return pairs_of(
        [spec.iid for spec in specs if can_produce(spec)],
        [spec.iid for spec in specs if can_consume(spec)],
        mesh,
    )


def recovery_pairs(target: EngineSpec, peers: list[EngineSpec]) -> list[tuple[str, str]]:
    """Require a distinct peer for every role the recovery target can serve."""
    peers = [spec for spec in peers if spec.iid != target.iid]
    pairs = []
    if can_produce(target):
        peer = next((spec for spec in peers if can_consume(spec)), None)
        if peer is None:
            raise ValueError(f"{target.iid} has no eligible fabric consumer peer")
        pairs.append((target.iid, peer.iid))
    if can_consume(target):
        peer = next((spec for spec in peers if can_produce(spec)), None)
        if peer is None:
            raise ValueError(f"{target.iid} has no eligible fabric producer peer")
        pairs.append((peer.iid, target.iid))
    return list(dict.fromkeys(pairs))
