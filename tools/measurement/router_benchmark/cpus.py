"""CPU topology, process pinning and CPU-time accounting."""

from __future__ import annotations

import ctypes
import os
import signal
from dataclasses import dataclass
from pathlib import Path

PR_SET_PDEATHSIG = 1
LIBC = ctypes.CDLL(None, use_errno=True)


@dataclass(frozen=True)
class Allocation:
    router: int
    router_siblings: list[int]
    clients: list[int]
    engines: list[int]
    driver: int


def parse_cpu_list(text: str) -> list[int]:
    """Parse a kernel CPU list such as "200-203,210"."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        low, _, high = part.partition("-")
        cpus.extend(range(int(low), int(high or low) + 1))
    if not cpus or len(set(cpus)) != len(cpus):
        raise ValueError(f"CPU list {text.strip()!r} must name each CPU once")
    return cpus


def thread_siblings(cpu: int) -> list[int]:
    """Return the hardware threads of `cpu`'s core, `cpu` included."""
    return parse_cpu_list(
        Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list").read_text()
    )


def allocate(cpus: list[int], siblings: list[int], clients: int, engines: int) -> Allocation:
    """Assign the router, idle siblings, clients, engines and the driver to CPUs."""
    router = cpus[0]
    router_siblings = [cpu for cpu in siblings if cpu != router]
    idle = set(router_siblings) & set(cpus)
    free = [cpu for cpu in cpus[1:] if cpu not in idle]
    need = 2 + clients + engines + len(idle)
    if len(cpus) < need:
        raise ValueError(
            f"--cpus holds {len(cpus)} CPUs; the run needs {need}: router, "
            f"{len(idle)} idle router siblings, {clients} clients, {engines} engines and the driver"
        )
    return Allocation(
        router=router,
        router_siblings=router_siblings,
        clients=free[:clients],
        engines=free[clients : clients + engines],
        driver=free[-1],
    )


def pinned(cpu: int):
    def setup() -> None:
        if LIBC.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        os.sched_setaffinity(0, {cpu})

    return setup


def proc_cpu_seconds(stat_text: str) -> float:
    """Return utime + stime seconds from one /proc/<pid>/stat text."""
    values = stat_text[stat_text.rindex(")") + 1 :].split()
    return (int(values[11]) + int(values[12])) / os.sysconf("SC_CLK_TCK")


def proc_seconds(pid: int) -> float:
    return proc_cpu_seconds(Path(f"/proc/{pid}/stat").read_text())


def cpu_times(proc_stat_text: str, cpu: int) -> dict[str, int]:
    """Return busy, irq and total ticks of one CPU from /proc/stat text."""
    name = f"cpu{cpu}"
    for line in proc_stat_text.splitlines():
        values = line.split()
        if values and values[0] == name:
            user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in values[1:9])
            return {
                "busy": user + nice + system,
                "irq": irq + softirq,
                "total": user + nice + system + idle + iowait + irq + softirq + steal,
            }
    raise ValueError(f"/proc/stat has no {name} line")
