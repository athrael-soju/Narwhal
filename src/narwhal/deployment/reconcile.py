"""Reconcile a load client's offers with the router journal and the router's counters.

The capacity trial of `narwhal deploy run --trial` and the benchmark evidence collector run
the same join: each sent offer needs one terminal journal row with its `client_rid`, the
completed counts agree, and each router run's counter deltas match its journal's terminal
classes.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from itertools import pairwise
from typing import Any


def metric_values(content: str) -> dict[str, float]:
    names = {
        "narwhal_offered_total",
        "narwhal_served_total",
        "narwhal_failed_total",
        "narwhal_refused_total",
        "narwhal_rejected_total",
        "narwhal_standby_refusals_total",
        "narwhal_expired_total",
        "narwhal_invalid_requests_total",
        "narwhal_cancelled_total",
        "narwhal_flips_total",
        "narwhal_event_loop_busy_seconds_total",
    }
    values: defaultdict[str, float] = defaultdict(float)
    for line in content.splitlines():
        match = re.match(r"^(narwhal_[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", line)
        if match and match[1] in names:
            values[match[1]] += float(match[2])
    return dict(values)


def counter_deltas(samples: list[dict]) -> tuple[dict, list[dict]]:
    by_run = defaultdict(list)
    for sample in samples:
        run = sample.get("state", {}).get("journal_run")
        # A router restart between the state and metrics reads leaves the counters unattributed.
        if (
            run
            and sample.get("router_metrics") is not None
            and sample.get("journal_run_after", run) == run
        ):
            by_run[run].append((sample["at"], metric_values(sample["router_metrics"])))
    diagnostics = []
    result: dict[str, dict[str, float]] = {}
    for run, series in by_run.items():
        first, last = series[0][1], series[-1][1]
        result[run] = {}
        for name in sorted(first.keys() | last.keys()):
            if name not in first or name not in last:
                diagnostics.append({"kind": "counter_missing", "run": run, "metric": name})
                continue
            delta = last[name] - first[name]
            if delta < 0:
                diagnostics.append({"kind": "counter_reset", "run": run, "metric": name})
            else:
                result[run][name] = int(delta) if delta.is_integer() else delta
    return result, diagnostics


def reconcile(
    point_id: str,
    client_rows: list[dict],
    journal_rows: list[dict],
    samples: list[dict],
    sample_interval: float,
) -> dict[str, Any]:
    """Join one point's client rows to its journal rows and counter samples.

    Returns the counts on both sides, the counter deltas of each router run, the role timeline
    and `diagnostics`, one entry for each disagreement; an empty list reconciles.
    """
    diagnostics: list[dict[str, Any]] = []
    ended = [row for row in journal_rows if "terminal" in row]
    # A standby's refusal is an attempt the load balancer retried on its primary, whose row
    # ends the client request.
    terminal = [row for row in ended if not row.get("standby_refusal")]
    client_sent = [row for row in client_rows if row.get("sent", True)]
    client_ids = [row.get("client_rid") for row in client_sent]
    journal_ids = [row.get("client_rid") for row in terminal]
    if len(client_sent) != len(terminal):
        diagnostics.append(
            {
                "kind": "client_journal_count",
                "point": point_id,
                "client_sent": len(client_sent),
                "journal_terminal": len(terminal),
            }
        )
    if client_ids and all(client_ids) and all(journal_ids):
        missing = sorted(str(rid) for rid in set(client_ids) - set(journal_ids))
        extra = sorted(str(rid) for rid in set(journal_ids) - set(client_ids))
        client_duplicates = len(client_ids) - len(set(client_ids))
        journal_duplicates = len(journal_ids) - len(set(journal_ids))
        if missing or extra or client_duplicates or journal_duplicates:
            diagnostics.append(
                {
                    "kind": "client_journal_ids",
                    "point": point_id,
                    "missing_count": len(missing),
                    "extra_count": len(extra),
                    "client_duplicate_count": client_duplicates,
                    "journal_duplicate_count": journal_duplicates,
                }
            )
    elif client_ids and all(client_ids) and journal_ids and not all(journal_ids):
        diagnostics.append({"kind": "journal_client_ids_missing", "point": point_id})
    client_complete = sum(row.get("outcome") == "completed" for row in client_sent)
    journal_complete = sum(row["terminal"] == "completed" for row in terminal)
    if client_complete != journal_complete:
        diagnostics.append(
            {
                "kind": "completed_count",
                "point": point_id,
                "client": client_complete,
                "journal": journal_complete,
            }
        )

    deltas, counter_diagnostics = counter_deltas(samples)
    diagnostics.extend(counter_diagnostics)
    journal_by_run = defaultdict(list)
    for row in ended:
        journal_by_run[row.get("run", "unknown")].append(row)
    metric_for_terminal = {
        "completed": "narwhal_served_total",
        "failed": "narwhal_failed_total",
        "refused": "narwhal_refused_total",
        "rejected": "narwhal_rejected_total",
        "expired": "narwhal_expired_total",
        "invalid": "narwhal_invalid_requests_total",
        "cancelled": "narwhal_cancelled_total",
    }
    for run, rows in journal_by_run.items():
        if run not in deltas:
            diagnostics.append({"kind": "missing_run_metrics", "point": point_id, "run": run})
            continue
        attempts = sum(bool(row.get("standby_refusal")) for row in rows)
        expected = Counter(row["terminal"] for row in rows if not row.get("standby_refusal"))
        expected_metrics = {"narwhal_offered_total": len(rows) - attempts}
        for terminal_class, metric in metric_for_terminal.items():
            expected_metrics[metric] = expected[terminal_class]
        if attempts or "narwhal_standby_refusals_total" in deltas[run]:
            expected_metrics["narwhal_standby_refusals_total"] = attempts
        for metric, count in expected_metrics.items():
            observed = deltas[run].get(metric)
            if observed is None or observed != count:
                diagnostics.append(
                    {
                        "kind": "counter_mismatch",
                        "point": point_id,
                        "run": run,
                        "metric": metric,
                        "journal": count,
                        "counter_delta": observed,
                    }
                )

    timeline = []
    observed_flips = set()
    baseline_flips = {
        (flip.get("at"), flip.get("iid"), flip.get("to"))
        for flip in (samples[0].get("state", {}).get("flips", []) if samples else [])
    }
    previous_pools = None
    for sample in samples:
        state = sample.get("state")
        if state:
            pools = state.get("pools")
            if pools != previous_pools:
                timeline.append({"observed_at": sample["at"], "pools": pools})
                previous_pools = pools
            for flip in state.get("flips", []):
                key = (flip.get("at"), flip.get("iid"), flip.get("to"))
                if key not in observed_flips and key not in baseline_flips:
                    observed_flips.add(key)
                    timeline.append({"observed_at": sample["at"], "flip": flip})
    flip_count = sum(values.get("narwhal_flips_total", 0) for values in deltas.values())
    if flip_count > len(observed_flips):
        diagnostics.append(
            {
                "kind": "role_history_gap",
                "point": point_id,
                "counter_flips": flip_count,
                "observed_flips": len(observed_flips),
            }
        )
    for left, right in pairwise(samples):
        if right["monotonic"] - left["monotonic"] > sample_interval * 2.5:
            diagnostics.append(
                {
                    "kind": "scrape_interval_gap",
                    "point": point_id,
                    "from": left["at"],
                    "to": right["at"],
                }
            )
    diagnostics.extend(
        {
            "kind": "scrape_error",
            "point": point_id,
            "at": sample["at"],
            "targets": sorted(sample["errors"]),
        }
        for sample in samples
        if sample.get("errors")
    )
    return {
        "point": point_id,
        "client": {
            "sent": len(client_sent),
            "measured_sent": sum(not row.get("benchmark_warmup", False) for row in client_sent),
            "warmup_sent": sum(row.get("benchmark_warmup", False) for row in client_sent),
            "outcomes": dict(Counter(row.get("outcome", "unknown") for row in client_rows)),
            "measured_outcomes": dict(
                Counter(
                    row.get("outcome", "unknown")
                    for row in client_rows
                    if not row.get("benchmark_warmup", False)
                )
            ),
        },
        "journal": {
            "terminal": len(terminal),
            "outcomes": dict(Counter(row["terminal"] for row in terminal)),
            "runs": sorted(journal_by_run),
        },
        "counter_deltas_by_run": deltas,
        "role_timeline": timeline,
        "diagnostics": diagnostics,
    }
