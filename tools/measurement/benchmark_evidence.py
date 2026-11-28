"""Collect private benchmark telemetry and reconcile point-boundary evidence."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from narwhal.deployment.reconcile import reconcile
from narwhal.fleet_control.rows import client_row


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def private_json(path: Path, value: object) -> None:
    with open(
        path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)
    ) as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def retain_client_file(path: Path, directory: Path, snapshot_name: str) -> tuple[str, dict]:
    """Read one client input and keep a private copy only if it lives outside the point."""
    content = path.read_bytes()
    root = directory.resolve()
    source = path.resolve()
    if not source.is_relative_to(root):
        retained = directory / snapshot_name
        with open(retained, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(content)
    else:
        retained = source
    return content.decode("utf-8"), {
        "path": str(retained.relative_to(root)),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


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
                ("state_after", self.base + "/narwhal/state", True),
                *(
                    (f"engine:{iid}", url, False)
                    for iid, url in self.config["engine_metrics_urls"].items()
                ),
            ]:
                try:
                    response = client.get(
                        url,
                        headers=self.headers
                        if name in ("state", "router_metrics", "state_after")
                        else {},
                    )
                    response.raise_for_status()
                    item[name] = response.json() if json_body else response.text
                except (httpx.HTTPError, ValueError) as error:
                    item["errors"][name] = str(error)
        # The second state read directly follows the metrics it vouches for.
        if "state_after" in item:
            item["journal_run_after"] = item.pop("state_after").get("journal_run")
        elif "state_after" in item["errors"]:
            item["journal_run_after"] = None
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
        client_files = {}
        if client_path.exists():
            client_text, client_files["records"] = retain_client_file(
                client_path, self.directory, "client-records.snapshot.jsonl"
            )
            for number, line in enumerate(client_text.splitlines(), 1):
                try:
                    client_rows.append(client_row(json.loads(line)))
                except ValueError:
                    journal_errors.append(
                        {"kind": "client_parse_error", "point": self.point["id"], "line": number}
                    )
            warmup = client_path.with_name("warmup.json")
            if warmup.exists():
                warmup_text, client_files["warmup"] = retain_client_file(
                    warmup, self.directory, "client-warmup.snapshot.json"
                )
                try:
                    client_rows.append(json.loads(warmup_text) | {"benchmark_warmup": True})
                except ValueError:
                    journal_errors.append({"kind": "warmup_parse_error", "point": self.point["id"]})
        else:
            journal_errors.append({"kind": "client_records_missing", "point": self.point["id"]})
        client_summary = client_path.with_name("summary.json")
        summary_text = None
        if client_summary.exists():
            summary_text, client_files["summary"] = retain_client_file(
                client_summary, self.directory, "client-summary.snapshot.json"
            )
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
        result["file_digests"] = self.start_digests | {
            f"client_{name}": item["sha256"] for name, item in client_files.items()
        }
        result["retained_client_files"] = {
            name: item["path"] for name, item in client_files.items()
        }
        for name, value in end_digests.items():
            if value != self.start_digests[name]:
                result["diagnostics"].append(
                    {"kind": "input_changed", "point": self.point["id"], "input": name}
                )
        result["workload"] = self.point["workload"]
        private_json(self.directory / "evidence.json", result)
        private_json(self.directory / "samples.json", self.samples)
        private_json(self.directory / "journal-rows.json", rows)
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
        performance = {}
        if summary_text is not None:
            source = json.loads(summary_text)
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
