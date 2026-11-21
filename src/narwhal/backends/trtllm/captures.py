from __future__ import annotations

import json
import re
from pathlib import Path

from ...deployment.launch_engine.check import require_checked
from ...deployment.launch_engine.docker import docker
from ...deployment.launch_engine.runtime import digest, write_private
from . import plan as policy

# Each rank of the serving container writes its own layout record here.
RANK_RECORD = "/tmp/narwhal-cache-layout.rank{rank}.json"


def tensor_parallel_size(plan: dict) -> int:
    args = plan["args"]
    return int(args[args.index(policy.SETTINGS.tensor_parallel_flag) + 1])


def capture_cache(run: Path, plan: dict) -> None:
    """Merge each rank's live cache layout from the serving container or launch directory."""
    require_checked(run, plan)
    destination = run / "cache-layout.json"
    if destination.exists():
        raise ValueError("cache layout capture exists; retain it and use a fresh plan")
    native = plan.get("backend") == "native"
    if not native:
        cid = (run / "container.id").read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", cid):
            raise ValueError("container.id must contain the recorded serving container")
        state = json.loads(
            docker(["inspect", "--format", "{{json .State}}", cid], run, "cache-capture.log")
        )
        if not state["Running"]:
            raise ValueError(
                "serving container exited; inspect its launch log before cache capture"
            )
    expected = {
        "image": plan["image"],
        "expected_packages": plan["expected_packages"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
        "launch_config_sha256": plan["launch_sha256"],
        "plan_sha256": digest(run / "launch.json"),
        "launcher_sha256": plan["launcher_sha256"],
        "cache_capture_sha256": plan["cache_capture_sha256"],
    }
    merged: dict | None = None
    for rank in range(tensor_parallel_size(plan)):
        if native:
            # A native engine's ranks write next to the launch's cache-layout.json.
            source = run / f"cache-layout.rank{rank}.json"
            if not source.is_file():
                raise ValueError(f"rank {rank} has not recorded its cache layout; start the engine")
            record = json.loads(source.read_text())
        else:
            pending = run / f"cache-layout.rank{rank}.pending.json"
            pending.unlink(missing_ok=True)
            docker(
                ["cp", f"{cid}:{RANK_RECORD.format(rank=rank)}", str(pending)],
                run,
                "cache-capture.log",
            )
            pending.chmod(0o600)
            record = json.loads(pending.read_text())
            pending.unlink()
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError("live cache capture differs from the checked serving plan")
        if "unsupported" in record:
            raise ValueError(f"rank {rank} could not size its cache: {record['unsupported']}")
        ranks = record["ranks"]
        if len(ranks) != 1 or ranks[0]["rank"] != rank:
            raise ValueError(f"rank {rank} cache record must describe that rank alone")
        if merged is None:
            merged = record
        else:
            if {k: v for k, v in record.items() if k != "ranks"} != {
                k: v for k, v in merged.items() if k != "ranks"
            }:
                raise ValueError("ranks of one engine recorded different cache sizing")
            merged["ranks"].extend(ranks)
    write_private(destination, json.dumps(merged, indent=2) + "\n")
    print("Live serving cache pages captured in cache-layout.json; container remains running.")
