"""Collect private benchmark telemetry and reconcile point-boundary evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import httpx


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def private_json(path: Path, value: object) -> None:
    with open(
        path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)
    ) as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def cursor(path: Path) -> dict:
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "offset": stat.st_size}


def read_journal(path: Path, start: dict, end: dict) -> tuple[list[dict], list[dict]]:
    diagnostics = []
    if (start["device"], start["inode"]) != (end["device"], end["inode"]):
        return [], [{"kind": "journal_replaced", "from": start, "to": end}]
    if end["offset"] < start["offset"]:
        return [], [{"kind": "journal_truncated", "from": start, "to": end}]
    with path.open("rb") as stream:
        stream.seek(start["offset"])
        content = stream.read(end["offset"] - start["offset"])
    rows = []
    for number, line in enumerate(content.splitlines(), 1):
        try:
            rows.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            diagnostics.append({"kind": "journal_parse_error", "line": number})
    return rows, diagnostics


def metric_values(content: str) -> dict[str, float]:
    names = {
        "narwhal_offered_total",
        "narwhal_served_total",
        "narwhal_failed_total",
        "narwhal_refused_total",
        "narwhal_rejected_total",
        "narwhal_expired_total",
        "narwhal_invalid_requests_total",
        "narwhal_cancelled_total",
        "narwhal_flips_total",
    }
    values = defaultdict(float)
    for line in content.splitlines():
        match = re.match(r"^(narwhal_[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", line)
        if match and match[1] in names:
            values[match[1]] += float(match[2])
    return dict(values)


def counter_deltas(samples: list[dict]) -> tuple[dict, list[dict]]:
    by_run = defaultdict(list)
    for sample in samples:
        run = sample.get("state", {}).get("journal_run")
        if run and sample.get("router_metrics") is not None:
            by_run[run].append((sample["at"], metric_values(sample["router_metrics"])))
    diagnostics = []
    result = {}
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


def anonymize_timeline(timeline: list[dict]) -> list[dict]:
    identities = {}

    def label(iid):
        if iid not in identities:
            identities[iid] = f"engine-{len(identities) + 1}"
        return identities[iid]

    safe = []
    for item in timeline:
        if item.get("pools"):
            safe.append(
                {
                    "observed_at": item["observed_at"],
                    "pools": {
                        role: [label(iid) for iid in iids] for role, iids in item["pools"].items()
                    },
                }
            )
        if "flip" in item:
            safe.append(
                {
                    "observed_at": item["observed_at"],
                    "flip": {
                        "iid": label(item["flip"]["iid"]),
                        "to": item["flip"].get("to"),
                        "by": item["flip"].get("by"),
                    },
                }
            )
    return safe


def reconcile(
    point_id: str,
    client_rows: list[dict],
    journal_rows: list[dict],
    samples: list[dict],
    sample_interval: float,
) -> dict:
    diagnostics = []
    terminal = [row for row in journal_rows if "terminal" in row]
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
        missing = sorted(set(client_ids) - set(journal_ids))
        extra = sorted(set(journal_ids) - set(client_ids))
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
    for row in terminal:
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
        expected = Counter(row["terminal"] for row in rows)
        expected_metrics = {"narwhal_offered_total": len(rows)}
        for terminal_class, metric in metric_for_terminal.items():
            expected_metrics[metric] = expected[terminal_class]
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
            "outcomes": dict(Counter(row.get("outcome", "unknown") for row in client_rows)),
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


class EvidenceCollector:
    """Sample router and engine telemetry throughout one benchmark point."""

    def __init__(self, config: dict, base: str, point: dict, directory: Path, headers: dict):
        self.config = config
        self.base = base
        self.point = point
        self.directory = directory
        self.headers = headers
        self.journal = Path(config["journal_path"])
        self.interval = config["sample_interval_s"]
        self.samples: list[dict] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def sample(self) -> None:
        item = {"at": stamp(), "monotonic": time.monotonic(), "errors": {}}
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, url, json_body in [
                ("state", self.base + "/narwhal/state", True),
                ("router_metrics", self.base + "/metrics", False),
                *(
                    (f"engine:{iid}", url, False)
                    for iid, url in self.config["engine_metrics_urls"].items()
                ),
            ]:
                try:
                    response = client.get(
                        url, headers=self.headers if name in ("state", "router_metrics") else {}
                    )
                    response.raise_for_status()
                    item[name] = response.json() if json_body else response.text
                except (httpx.HTTPError, ValueError) as error:
                    item["errors"][name] = str(error)
        self.samples.append(item)

    def start(self) -> None:
        self.start_cursor = cursor(self.journal)
        self.start_digests = {
            name: hashlib.sha256(Path(self.config[field]).read_bytes()).hexdigest()
            for name, field in (("fleet", "fleet_path"), ("profiles", "profiles_path"))
        }
        self.sample()

        def loop():
            while not self.stop_event.wait(self.interval):
                self.sample()

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self) -> dict:
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        self.sample()
        end_cursor = cursor(self.journal)
        rows, journal_errors = read_journal(self.journal, self.start_cursor, end_cursor)
        client_path = Path(
            self.config.get("client_records", "{point_dir}/client/requests.jsonl").format_map(
                {"point_dir": str(self.directory), "point_id": self.point["id"]}
            )
        )
        client_rows = []
        if client_path.exists():
            for number, line in enumerate(client_path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    client_rows.append(json.loads(line))
                except ValueError:
                    journal_errors.append(
                        {"kind": "client_parse_error", "point": self.point["id"], "line": number}
                    )
            warmup = client_path.with_name("warmup.json")
            if warmup.exists():
                try:
                    client_rows.append(json.loads(warmup.read_text(encoding="utf-8")))
                except ValueError:
                    journal_errors.append({"kind": "warmup_parse_error", "point": self.point["id"]})
        else:
            journal_errors.append({"kind": "client_records_missing", "point": self.point["id"]})
        result = reconcile(self.point["id"], client_rows, rows, self.samples, self.interval)
        result["diagnostics"].extend(journal_errors)
        result["cursors"] = {"start": self.start_cursor, "end": end_cursor}
        result["sample_interval_s"] = self.interval
        result["point_window"] = {
            "first_sample": self.samples[0]["at"],
            "last_sample": self.samples[-1]["at"],
        }
        result["identity"] = self.config["identity"]
        end_digests = {
            name: hashlib.sha256(Path(self.config[field]).read_bytes()).hexdigest()
            for name, field in (("fleet", "fleet_path"), ("profiles", "profiles_path"))
        }
        result["file_digests"] = self.start_digests
        for name, value in end_digests.items():
            if value != self.start_digests[name]:
                result["diagnostics"].append(
                    {"kind": "input_changed", "point": self.point["id"], "input": name}
                )
        result["workload"] = self.point["workload"]
        private_json(self.directory / "evidence.json", result)
        private_json(self.directory / "samples.json", self.samples)
        private_json(self.directory / "journal-rows.json", rows)
        private_json(self.directory / "client-rows.json", client_rows)
        safe_identity = {
            key: self.config["identity"].get(key)
            for key in (
                "narwhal_revision",
                "model_id",
                "benchmark_client_version",
                "engine_version",
                "checkpoint_revision",
                "gpu_shape",
            )
        }
        safe_identity["engine_image_sha256"] = hashlib.sha256(
            self.config["identity"]["engine_image"].encode()
        ).hexdigest()
        safe_identity["gpu_allocation_sha256"] = hashlib.sha256(
            json.dumps(self.config["identity"]["gpu_allocation"], sort_keys=True).encode()
        ).hexdigest()
        client_summary = client_path.with_name("summary.json")
        performance = {}
        if client_summary.exists():
            source = json.loads(client_summary.read_text(encoding="utf-8"))
            performance = {
                key: source.get(key)
                for key in (
                    "completed_rps_including_drain",
                    "completed_output_tokens_per_s_including_drain",
                    "ttft_s",
                    "tpot_s",
                    "attainment",
                )
            }
        shareable = {
            "point": self.point["id"],
            "identity": safe_identity,
            "file_digests": result["file_digests"],
            "workload": {
                key: self.point["workload"].get(key)
                for key in ("rate_rps", "requests", "input_tokens", "output_tokens")
            },
            "client": result["client"],
            "performance": performance,
            "journal": result["journal"],
            "counter_deltas_by_run": result["counter_deltas_by_run"],
            "role_timeline": anonymize_timeline(result["role_timeline"]),
            "diagnostics": [
                item
                | {
                    "targets": [
                        "engine-metrics" if target.startswith("engine:") else target
                        for target in item["targets"]
                    ]
                }
                if "targets" in item
                else item
                for item in result["diagnostics"]
            ],
        }
        private_json(self.directory / "summary.shareable.json", shareable)
        return result
