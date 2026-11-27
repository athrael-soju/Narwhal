"""Client result rows shared by load jobs and the measurement tools."""

from __future__ import annotations


def client_row(row: dict) -> dict:
    """Return a load trial row, or the same fields for an AIPerf `profile_export.jsonl` record."""
    metadata = row.get("metadata")
    if not isinstance(metadata, dict) or "metrics" not in row:
        return row
    error = row.get("error")
    if metadata.get("was_cancelled"):
        outcome = "cancelled"
    elif error:
        outcome = f"http_{error['code']}" if error.get("code") else error.get("type") or "error"
    else:
        outcome = "completed"
    return {
        "client_rid": metadata.get("x_request_id"),
        "sent": True,
        "outcome": outcome,
        "benchmark_warmup": metadata.get("benchmark_phase") == "warmup",
    }
