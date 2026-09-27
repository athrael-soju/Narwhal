"""Launch and host detached management workers from the installed package."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from pathlib import Path


def launch_worker(
    registry_path: Path, target_id: str, operation_id: str, *, reconcile: bool = False
) -> None:
    """Start fixed package code without inheriting any client transport handles."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "narwhal.deployment.management_worker",
            "--registry",
            str(registry_path),
            "--target",
            target_id,
            "--operation",
            operation_id,
            *(["--reconcile"] if reconcile else []),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    threading.Thread(target=child.wait, daemon=True, name="management-worker-reaper").start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    args.registry = args.registry.absolute()
    os.environ["NARWHAL_MANAGEMENT_REGISTRY"] = str(args.registry)
    from .management_coordinator import OperationCoordinator, run_worker
    from .management_registry import load_registry

    try:
        if args.reconcile:
            coordinator = OperationCoordinator(load_registry(args.registry), args.registry)
            coordinator.reconcile_worker(args.target, args.operation)
        else:
            run_worker(args.registry, args.target, args.operation)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
