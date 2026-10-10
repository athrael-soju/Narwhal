from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .router.routing import NarwhalRouter


def handoff_bound(router: NarwhalRouter, iid: str) -> float | None:
    # Without an attested lease the request deadline bounds the handoff.
    lease = router.kv_leases.get(iid)
    return None if lease is None else router.engines.kv.handoff_bound(lease)


def snapshot(router: NarwhalRouter) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for iid in sorted(router.monitor.instances):
        lease = router.kv_leases.get(iid)
        bound = handoff_bound(router, iid)
        rows[iid] = {
            "kv_lease_s": lease,
            "renewal_s": None if lease is None or bound is None else int(lease - bound),
            "bound_s": bound,
        }
    return rows
