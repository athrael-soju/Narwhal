"""Rate rows, stop conditions and the benchmark point of one run."""

from __future__ import annotations

import json
import re
from collections import Counter

from .cpus import Allocation

FULL_CPU_SHARE = 0.95
MAX_SCHEDULE_LAG_S = 0.05
FOREIGN_CPU_SHARE = 0.05
REPORT_KIND = "narwhal-router-benchmark"
VERSION = 1
BUSY_METRIC = "narwhal_event_loop_busy_seconds_total"
REASON_END = re.compile(r"[:0-9]")
POINT_FIELDS = (
    "offered_rps",
    "relayed_frames_per_router_cpu_s",
    "relayed_frames_per_s",
    "requests_per_s",
    "router_cpu_s_per_request",
    "event_loop_busy_share",
    "marks",
)


def per(value: float, over: float) -> float | None:
    return value / over if over > 0 else None


def rejection_reason(row: dict) -> str:
    try:
        message = json.loads(row["error_body"])["error"]["message"]
    except (KeyError, TypeError, ValueError):
        return "unparsed"
    if not isinstance(message, str):
        return "unparsed"
    return REASON_END.split(message, maxsplit=1)[0].strip()


def rate_row(
    rate: float,
    offered: int,
    rows: list[dict],
    samples: dict,
    allocation: Allocation,
    roles: dict[str, str],
) -> dict:
    """Compute one report row from client rows and the rate's samples."""
    before, t0, t1, after = (samples[key] for key in ("before", "t0", "t1", "after"))
    completed = [row for row in rows if row["outcome"] == "completed"]
    relayed = sum(row["token_events"] for row in rows)
    starts = [row["scheduled_mono"] + row["schedule_lag_s"] for row in rows]
    ends = [start + row["elapsed_s"] for start, row in zip(starts, rows, strict=True)]
    span = max(ends) - min(starts) if rows else 0.0
    router_cpu = after["router_cpu_s"] - before["router_cpu_s"]
    window = t1["mono"] - t0["mono"]
    router_share = (t1["router_cpu_s"] - t0["router_cpu_s"]) / window
    busy = None
    if BUSY_METRIC in t0["router_metrics"] and BUSY_METRIC in t1["router_metrics"]:
        busy = (t1["router_metrics"][BUSY_METRIC] - t0["router_metrics"][BUSY_METRIC]) / window

    def core(cpu: int) -> dict[str, int]:
        first, last = t0["cores"][str(cpu)], t1["cores"][str(cpu)]
        return {key: last[key] - first[key] for key in ("busy", "irq", "total")}

    own = core(allocation.router)
    busy_share, irq_share = own["busy"] / own["total"], own["irq"] / own["total"]
    siblings = [core(cpu) for cpu in allocation.router_siblings]
    router_core = {
        "busy_share": busy_share,
        "irq_share": irq_share,
        "foreign_share": busy_share + irq_share - router_share,
        "sibling_busy_share": max(
            ((ticks["busy"] + ticks["irq"]) / ticks["total"] for ticks in siblings), default=None
        ),
    }
    clients = []
    for index, cpu in enumerate(allocation.clients):
        cpu_s = t1["clients"][str(index)] - t0["clients"][str(index)]
        clients.append({"index": index, "cpu": cpu, "cpu_s": cpu_s, "cpu_share": cpu_s / window})
    engines = []
    for (iid, role), cpu in zip(roles.items(), allocation.engines, strict=True):
        cpu_s = t1["engines"][iid] - t0["engines"][iid]
        engines.append(
            {
                "iid": iid,
                "role": role,
                "cpu": cpu,
                "cpu_s": cpu_s,
                "cpu_share": cpu_s / window,
                "late_ticks": t1["late_ticks"][iid] - t0["late_ticks"][iid],
            }
        )
    full_cpu = [
        f"client-{client['index']}" for client in clients if client["cpu_share"] >= FULL_CPU_SHARE
    ] + [engine["iid"] for engine in engines if engine["cpu_share"] >= FULL_CPU_SHARE]
    state = after["state"]
    fleet = {
        "ejected": list(state["ejected"]),
        "quarantined": list(state["quarantined"]),
        "probation": list(state["probation"]),
        "degraded": state["monitoring"]["degraded"],
    }
    reasons = Counter(
        (row["status"], rejection_reason(row)) for row in rows if row["outcome"] == "http_error"
    )
    refused = state["admission"]["refused"] - before["state"]["admission"]["refused"]
    max_lag = max((row["schedule_lag_s"] for row in rows), default=0.0)
    drain = samples["drain"]
    conditions = {
        "full_cpu": bool(full_cpu),
        "client_lag": max_lag > MAX_SCHEDULE_LAG_S,
        "engine_late": any(engine["late_ticks"] > 0 for engine in engines),
        "foreign_cpu": router_core["foreign_share"] > FOREIGN_CPU_SHARE,
        "incomplete": len(rows) != offered
        or any(
            row["outcome"] != "completed"
            and not (row["outcome"] == "http_error" and row["status"] == 429)
            for row in rows
        ),
        "refused": refused > 0,
        "ejected": bool(fleet["ejected"] or fleet["quarantined"]),
        "probation": bool(fleet["probation"]),
        "degraded": bool(fleet["degraded"]),
        "drain_timeout": drain != "idle",
    }
    return {
        "offered_rps": rate,
        "offered": offered,
        "completed": len(completed),
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "relayed_frames": relayed,
        "span_s": span,
        "relayed_frames_per_s": per(relayed, span),
        "requests_per_s": per(len(completed), span),
        "router_cpu_s": router_cpu,
        "router_cpu_s_per_request": per(router_cpu, len(completed)),
        "relayed_frames_per_router_cpu_s": per(relayed, router_cpu),
        "window_s": window,
        "router_cpu_share": router_share,
        "event_loop_busy_share": busy,
        "router_core": router_core,
        "saturation_rejections": state["admission"]["rejected"]
        - before["state"]["admission"]["rejected"],
        "refused": refused,
        "rejections_by_reason": [
            {"status": status, "reason": reason, "count": count}
            for (status, reason), count in sorted(reasons.items())
        ],
        "mean_resident_decode": per(
            sum(row["elapsed_s"] - row["ttft_s"] for row in completed), span
        ),
        "max_schedule_lag_s": max_lag,
        "client_cpu_s": sum(client["cpu_s"] for client in clients),
        "engine_cpu_s": sum(engine["cpu_s"] for engine in engines),
        "clients": clients,
        "engines": engines,
        "full_cpu": full_cpu,
        "fleet": fleet,
        "drain": drain,
        "marks": sorted(mark for mark, present in conditions.items() if present),
    }


def stop_reason(row: dict, client_failed: bool) -> str | None:
    if client_failed:
        return "client_failed"
    if row["saturation_rejections"] > 0:
        return "saturation"
    if row["fleet"]["ejected"] or row["fleet"]["quarantined"]:
        return "ejected"
    if row["drain"] != "idle":
        return "drain_timeout"
    return None


def select_point(rows: list[dict]) -> dict | None:
    """Return the highest offered rate without saturation rejections."""
    clean = [row for row in rows if row["saturation_rejections"] == 0]
    if not clean:
        return None
    best = max(clean, key=lambda row: row["offered_rps"])
    return {key: best[key] for key in POINT_FIELDS}
