"""Child processes of the benchmark driver."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from .cpus import pinned
from .files import write_private

# Repository root holding `tools` and `src`.
ROOT = Path(__file__).resolve().parents[3]


def child_env(pythonpath: str | None) -> dict[str, str]:
    """Return the explicit environment of one child process."""
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env["LANG"] = "C.UTF-8"
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


async def ready_line(process: subprocess.Popen, deadline: float) -> dict:
    """Read one JSON line from a child's stdout before `deadline`."""
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = b""
    while b"\n" not in buffer:
        if process.poll() is not None:
            raise RuntimeError(f"child {process.pid} exited {process.returncode} before ready")
        if time.monotonic() > deadline:
            raise RuntimeError(f"child {process.pid} printed no ready line in time")
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            await asyncio.sleep(0.05)
            continue
        if not chunk:
            raise RuntimeError(f"child {process.pid} closed stdout before ready")
        buffer += chunk
    return json.loads(buffer.split(b"\n", 1)[0])


class Children:
    """Child processes started by the driver and recorded in pids.json."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.router: subprocess.Popen | None = None
        self.engines: dict[str, subprocess.Popen] = {}
        self.clients: dict[str, subprocess.Popen] = {}

    def record(self) -> None:
        def live(group: dict[str, subprocess.Popen]) -> dict[str, int]:
            return {key: proc.pid for key, proc in group.items() if proc.poll() is None}

        router = self.router if self.router is not None and self.router.poll() is None else None
        write_private(
            self.path,
            {
                "driver": os.getpid(),
                "router": router and router.pid,
                "engines": live(self.engines),
                "clients": live(self.clients),
            },
        )

    def spawn(self, argv: list[str], cpu: int, **options) -> subprocess.Popen:
        return subprocess.Popen(argv, preexec_fn=pinned(cpu), **options)

    def stop(self, processes: list[subprocess.Popen]) -> None:
        live = [proc for proc in processes if proc.poll() is None]
        for proc in live:
            proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 10.0
        while any(proc.poll() is None for proc in live) and time.monotonic() < deadline:
            time.sleep(0.1)
        for proc in live:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.record()

    def stop_all(self) -> None:
        self.stop([*self.clients.values(), *([self.router] if self.router else [])])
        self.stop(list(self.engines.values()))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
