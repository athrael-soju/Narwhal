"""Read this node's place in a Slurm allocation from the environment Slurm sets for each task."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_RANGE = re.compile(r"^(\d+)(?:-(\d+))?$")


@dataclass(frozen=True)
class Allocation:
    job: str
    # This node's index in `nodes`.
    rank: int
    # Node names in allocation order.
    nodes: tuple[str, ...]
    # Device indices visible to this node's task, or empty when the scheduler sets none.
    devices: tuple[str, ...]

    @property
    def head(self) -> bool:
        return self.rank == 0


def expand_hostlist(text: str) -> list[str]:
    """Expand a Slurm host list such as `gpu[01-03,07],login1` into node names, in order."""
    names: list[str] = []
    for item in _split(text.strip()):
        names.extend(_expand(item))
    if not names:
        raise ValueError("the node list is empty")
    return names


def _split(text: str) -> list[str]:
    items, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced ']' in node list {text!r}")
        elif char == "," and depth == 0:
            items.append(text[start:index])
            start = index + 1
    if depth:
        raise ValueError(f"unbalanced '[' in node list {text!r}")
    items.append(text[start:])
    if any(not item for item in items):
        raise ValueError(f"empty name in node list {text!r}")
    return items


def _expand(item: str) -> list[str]:
    open_at = item.find("[")
    if open_at < 0:
        return [item]
    close_at = item.index("]", open_at)
    prefix, body, rest = item[:open_at], item[open_at + 1 : close_at], item[close_at + 1 :]
    tails = _expand(rest) if rest else [""]
    names: list[str] = []
    for part in body.split(","):
        match = _RANGE.match(part)
        if not match:
            raise ValueError(f"bad range {part!r} in node list item {item!r}")
        low, high = match.group(1), match.group(2) or match.group(1)
        if int(high) < int(low):
            raise ValueError(f"descending range {part!r} in node list item {item!r}")
        width = len(low) if low.startswith("0") else 0
        names.extend(
            f"{prefix}{number:0{width}d}{tail}"
            for number in range(int(low), int(high) + 1)
            for tail in tails
        )
    return names


def _integer(env: Mapping[str, str], name: str) -> int:
    value = env.get(name)
    if value is None:
        raise ValueError(f"{name} is not set: run the launch under srun inside an allocation")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, not {value!r}") from None


def from_environment(env: Mapping[str, str], device_env: str) -> Allocation:
    """Return the allocation of the task this environment belongs to.

    The launch runs one task per node of its job step, so the node's rank is `SLURM_NODEID`, the
    nodes are the step's, and the task count equals the node count.
    """
    job = env.get("SLURM_JOB_ID")
    if not job:
        raise ValueError("SLURM_JOB_ID is not set: run the launch under srun inside an allocation")
    listed = env.get("SLURM_STEP_NODELIST") or env.get("SLURM_JOB_NODELIST")
    if not listed:
        raise ValueError("SLURM_STEP_NODELIST and SLURM_JOB_NODELIST are not set")
    nodes = tuple(expand_hostlist(listed))
    if len(set(nodes)) != len(nodes):
        raise ValueError(f"the node list {listed!r} names a node twice")
    count = env.get("SLURM_STEP_NUM_NODES") or env.get("SLURM_JOB_NUM_NODES")
    if count is not None and int(count) != len(nodes):
        raise ValueError(f"the node list has {len(nodes)} nodes, but the allocation has {count}")
    tasks = env.get("SLURM_NTASKS")
    if tasks is not None and int(tasks) != len(nodes):
        raise ValueError(
            f"the step runs {tasks} tasks on {len(nodes)} nodes: start one task per node "
            "with srun --ntasks-per-node=1"
        )
    rank = _integer(env, "SLURM_NODEID")
    if not 0 <= rank < len(nodes):
        raise ValueError(f"SLURM_NODEID {rank} is outside the {len(nodes)} allocated nodes")
    visible = env.get(device_env, "").strip()
    devices = tuple(device.strip() for device in visible.split(",") if device.strip())
    return Allocation(job=job, rank=rank, nodes=nodes, devices=devices)


def resolve(name: str, lookup: Callable[..., list] = socket.getaddrinfo) -> str:
    """Return the IPv4 address of a node name."""
    try:
        found = lookup(name, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError(f"node {name!r} does not resolve: {error}") from error
    if not found:
        raise ValueError(f"node {name!r} has no IPv4 address")
    return str(found[0][4][0])
