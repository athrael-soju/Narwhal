"""Derive each producer's KV handoff bound from its attested NIXL lease."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter

# vLLM's NIXL connector renews a producer's lease with consumer heartbeats sent at most
# every `kv_lease_duration // 6` seconds, counted from when the consumer accepts the request.
RENEWAL_DIVISOR = 6


def renewal_interval(lease_s: int) -> int:
    """Return the connector's heartbeat interval for a lease of `lease_s` seconds."""
    return lease_s // RENEWAL_DIVISOR


def handoff_bound(router: NarwhalRouter, iid: str) -> float | None:
    """Return the seconds after prefill completion by which decode must reach its engine.

    The producer holds the KV blocks for its lease from prefill completion. The consumer's
    first heartbeat can follow its acceptance by one renewal interval, so the router
    dispatches decode within the lease minus that interval. None means `iid` has no
    attested lease, and the request deadline bounds the handoff.
    """
    lease = router.kv_leases.get(iid)
    return None if lease is None else float(lease - renewal_interval(lease))


def snapshot(router: NarwhalRouter) -> dict[str, Any]:
    """Report each engine's attested lease, renewal interval and handoff bound."""
    return {
        iid: {
            "kv_lease_s": router.kv_leases.get(iid),
            "renewal_s": (
                None if iid not in router.kv_leases else renewal_interval(router.kv_leases[iid])
            ),
            "bound_s": handoff_bound(router, iid),
        }
        for iid in sorted(router.monitor.instances)
    }
