"""Text renderings of report and comparison documents."""

from __future__ import annotations


def number(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def mark_text(marks: list[str], full_cpu: list[str]) -> str:
    named = [f"full_cpu({','.join(full_cpu)})" if mark == "full_cpu" else mark for mark in marks]
    return ",".join(named) or "-"


def report_text(doc: dict) -> str:
    """Render a report document as text lines."""
    lines = [
        f"{doc['label']} router_source {doc['router_source']}",
        "rate_rps frames_per_s requests_per_s router_cpu_s frames_per_router_cpu_s "
        "rejections busy_share client_cpu_s engine_cpu_s marks",
    ]
    lines.extend(
        " ".join(
            (
                f"{row['offered_rps']:g}",
                number(row["relayed_frames_per_s"], 1),
                number(row["requests_per_s"], 2),
                number(row["router_cpu_s"], 3),
                number(row["relayed_frames_per_router_cpu_s"], 1),
                str(row["saturation_rejections"]),
                number(row["event_loop_busy_share"], 3),
                number(row["client_cpu_s"], 3),
                number(row["engine_cpu_s"], 3),
                mark_text(row["marks"], row["full_cpu"]),
            )
        )
        for row in doc["rates"]
    )
    point = doc["point"]
    if point is None:
        lines.append("point: none")
    else:
        lines.append(
            f"point: {point['offered_rps']:g} rps, "
            f"{number(point['relayed_frames_per_router_cpu_s'], 1)} frames/router CPU-s, "
            f"{number(point['relayed_frames_per_s'], 1)} frames/s, "
            f"{number(point['requests_per_s'], 2)} requests/s, "
            f"{number(point['router_cpu_s_per_request'], 5)} router CPU-s/request, "
            f"busy {number(point['event_loop_busy_share'], 3)}, "
            f"marks {','.join(point['marks']) or '-'}"
        )
    lines.append(f"stopped_by: {doc['stopped_by'] or '-'}")
    return "\n".join(lines) + "\n"


def comparison_text(doc: dict) -> str:
    """Render a comparison document as text lines."""
    lines = ["run label point_rps frames_per_router_cpu_s router_source"]
    for index, run in enumerate(doc["runs"], 1):
        point = run["point"] or {}
        lines.append(
            f"{index} {run['label']} {number(point.get('offered_rps'), 1)} "
            f"{number(point.get('relayed_frames_per_router_cpu_s'), 1)} {run['router_source']}"
        )
    for label, version in doc["versions"].items():
        lines.append(
            f"{label}: median point {number(version['median_point_rps'], 1)} rps, "
            f"median {number(version['median_relayed_frames_per_router_cpu_s'], 1)} "
            "frames/router CPU-s"
        )
    lines.append(f"ratio branch/base frames per router CPU-s: {number(doc['ratio'], 3)}")
    return "\n".join(lines) + "\n"
