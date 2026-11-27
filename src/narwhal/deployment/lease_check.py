"""Host commands of the `lease-check` stage, run from the installed wheel on each router host.

Each command works on a probe lease beside the router pair's lease, through the router's own
`FileLease`: the same lock file, the same write to a temporary file renamed over the lease,
and the same read under the lock. The stage runs them on the two router hosts in turn, so a
value written on one host must be the value the other host reads.

- `claim PATH HOLDER TTL [--wait SECONDS]` claims the lease and prints `claimed <epoch>`, or
  `refused` when another holder's epoch is unexpired after the wait.
- `read PATH` prints the holder and epoch as JSON, or `null` for no lease.
- `hold PATH SECONDS MARKER` holds the lock for SECONDS, creating MARKER once it holds it.
- `probe PATH` prints `blocked` when another process holds the lock, else `free`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from pathlib import Path

from ..runtime.lease import FileLease, LeaseError


def claim(path: Path, holder: str, ttl_s: float, wait_s: float) -> str:
    lease = FileLease(path, holder, ttl_s)
    deadline = time.monotonic() + wait_s
    while not lease.claim():
        if time.monotonic() >= deadline:
            return "refused"
        time.sleep(0.25)
    return f"claimed {lease.epoch}"


def read(path: Path) -> str:
    record = FileLease(path, "lease-check", 60).read()
    if record is None:
        return "null"
    return json.dumps({"holder": record.holder, "epoch": record.epoch})


def hold(path: Path, seconds: float, marker: Path) -> str:
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        marker.write_text("held\n")
        time.sleep(seconds)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return "released"


def probe(path: Path) -> str:
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "blocked"
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return "free"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    claiming = commands.add_parser("claim")
    claiming.add_argument("path", type=Path)
    claiming.add_argument("holder")
    claiming.add_argument("ttl", type=float)
    claiming.add_argument("--wait", type=float, default=0.0)
    reading = commands.add_parser("read")
    reading.add_argument("path", type=Path)
    holding = commands.add_parser("hold")
    holding.add_argument("path", type=Path)
    holding.add_argument("seconds", type=float)
    holding.add_argument("marker", type=Path)
    probing = commands.add_parser("probe")
    probing.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "claim":
            print(claim(args.path, args.holder, args.ttl, args.wait))
        elif args.command == "read":
            print(read(args.path))
        elif args.command == "hold":
            print(hold(args.path, args.seconds, args.marker))
        else:
            print(probe(args.path))
    except (OSError, LeaseError, ValueError) as error:
        print(f"narwhal-host: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
