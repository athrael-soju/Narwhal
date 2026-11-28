"""Pause, resume, stop or start the engine container that narwhal-engine launched."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from urllib.request import urlopen

from narwhal.deployment.launch_engine.backend import engine_backend

RUNTIME_COMMANDS = {"pause": "pause", "resume": "unpause", "stop": "stop", "start": "start"}
DEFAULT_HEALTH_TIMEOUT_S = 600.0
HEALTH_POLL_S = 2.0
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    pass


class Failed(Exception):
    pass


def launch_dir(dirs_path: Path, engine: str) -> Path:
    try:
        dirs = json.loads(dirs_path.read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read the launch-directory map {dirs_path}: {exc}") from exc
    if not isinstance(dirs, dict) or not all(isinstance(v, str) for v in dirs.values()):
        raise Refused(f"{dirs_path} must map engine IDs to launch directory paths")
    if engine not in dirs:
        raise Refused(f"{dirs_path} has no launch directory for engine {engine!r}")
    run = Path(dirs[engine])
    if not run.is_absolute():
        raise Refused(f"launch directory {run} for engine {engine!r} must be an absolute path")
    return run


def recorded_container(run: Path) -> tuple[str, str]:
    try:
        plan = json.loads((run / "launch.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"cannot read the launch plan in {run}: {exc}") from exc
    if plan.get("backend") == "native":
        raise Refused(f"{run} is a native launch; this hook controls container launches")
    try:
        cid = (run / "container.id").read_text().strip()
    except OSError as exc:
        raise Refused(f"{run} records no container: {exc}") from exc
    if not CONTAINER_ID.fullmatch(cid):
        raise Refused(f"{run / 'container.id'} does not hold a container ID")
    try:
        health = engine_backend(plan.get("engine")).dialect.health_path
        url = plan["endpoint"].rstrip("/") + health
    except (KeyError, AttributeError, ValueError) as exc:
        raise Refused(f"the launch plan in {run} names no usable engine endpoint: {exc}") from exc
    return cid, url


def runtime(*args: str) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
    except OSError as exc:
        raise Failed(f"docker {args[0]} could not run: {exc}") from exc
    if result.returncode:
        raise Failed(f"docker {args[0]} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=HEALTH_POLL_S) as response:
            return bool(response.status == 200)
    except (OSError, ValueError):
        return False


def wait_healthy(cid: str, url: str, timeout_s: float) -> float:
    started = time.monotonic()
    while True:
        state = runtime("inspect", "--format", "{{json .State}}", cid)
        try:
            running = json.loads(state)["Running"]
        except (ValueError, KeyError, TypeError) as exc:
            raise Failed(f"docker inspect returned no container state: {state[:200]}") from exc
        if not running:
            raise Failed(f"container {cid[:12]} stopped running while starting")
        if healthy(url):
            return time.monotonic() - started
        if time.monotonic() - started >= timeout_s:
            raise Failed(f"{url} did not answer within {timeout_s:g}s")
        time.sleep(HEALTH_POLL_S)


def act(
    action: str, engine: str, run: Path, health_timeout_s: float, after_start: Sequence[str]
) -> None:
    cid, url = recorded_container(run)
    runtime(RUNTIME_COMMANDS[action], cid)
    print(f"{action} {engine}: docker {RUNTIME_COMMANDS[action]} {cid[:12]}", flush=True)
    if action != "start":
        return
    waited = wait_healthy(cid, url, health_timeout_s)
    print(f"start {engine}: {url} answered after {waited:.1f}s", flush=True)
    if after_start:
        env = {**os.environ, "NARWHAL_CONTROL_LAUNCH_DIR": str(run)}
        try:
            code = subprocess.run(list(after_start), env=env).returncode
        except OSError as exc:
            raise Failed(f"post-start command could not run: {exc}") from exc
        if code:
            raise Failed(f"post-start command exited {code}")
        print(f"start {engine}: post-start command exited 0", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launch-dirs",
        type=Path,
        required=True,
        help="JSON file mapping each engine ID to its narwhal-engine launch directory",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=DEFAULT_HEALTH_TIMEOUT_S,
        help="seconds a start waits for the engine's health route (default: %(default)g)",
    )
    parser.add_argument("action", choices=sorted(RUNTIME_COMMANDS))
    parser.add_argument(
        "after_start",
        nargs="*",
        metavar="-- COMMAND",
        help="command to run after a start, once the engine answers its health route",
    )
    args = parser.parse_args(argv)
    try:
        engine = os.environ.get("NARWHAL_CONTROL_ENGINE", "")
        if not engine:
            raise Refused("NARWHAL_CONTROL_ENGINE is not set")
        requested = os.environ.get("NARWHAL_CONTROL_ACTION", args.action)
        if requested != args.action:
            raise Refused(
                f"the service requested {requested!r}, but this hook runs {args.action!r}"
            )
        if args.after_start and args.action != "start":
            raise Refused("a post-start command applies only to start")
        run = launch_dir(args.launch_dirs, engine)
        act(args.action, engine, run, args.health_timeout, args.after_start)
    except Refused as exc:
        print(f"engine hook refused: {exc}", file=sys.stderr)
        return 2
    except Failed as exc:
        print(f"engine hook failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
