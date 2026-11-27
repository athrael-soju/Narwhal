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
- `witness PATH [--check]` leaves the lease witness at PATH, or with `--check` prints `present`
  or `missing`.
- `state PATH` prints the lease's holder, epoch and expiry with this host's clock as JSON.
- `clock` prints this host's wall clock for each line it reads, until its input ends.
- `mount PATH` prints the file system type, mount point and options of the mount that holds
  PATH as JSON, from `/proc/self/mountinfo`, or the type `missing` when PATH is no directory.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

from ..runtime.lease import DEFAULT_SAFETY_MARGIN_S, FileLease, LeaseError

MOUNTINFO = Path("/proc/self/mountinfo")
# The Linux NFS client's attribute cache bounds, in seconds, while a mount sets none of them.
NFS_ATTRIBUTE_CACHE = {"acregmin": 3.0, "acregmax": 60.0, "acdirmin": 30.0, "acdirmax": 60.0}

WITNESS_TEXT = "narwhal lease directory\n"


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


def witness(path: Path, check: bool) -> str:
    if check:
        return "present" if path.is_file() and path.read_text() == WITNESS_TEXT else "missing"
    if not path.parent.is_dir():
        raise ValueError(f"{path.parent} is not a directory")
    if not path.is_file():
        staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        staged.write_text(WITNESS_TEXT)
        os.replace(staged, path)
    return "witness"


def state(path: Path) -> str:
    record = FileLease(path, "lease-check", 60).read()
    now = time.time()
    if record is None:
        return json.dumps({"holder": None, "now": now})
    return json.dumps(
        {
            "holder": record.holder,
            "epoch": record.epoch,
            "expires_at": record.expires_at,
            "now": now,
        }
    )


def clock() -> None:
    for _ in sys.stdin:
        print(repr(time.time()), flush=True)


def _unescape(field: str) -> str:
    """A mountinfo field with its octal escapes, such as `\\040` for a space, decoded."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), field)


def mount(path: Path, mountinfo: Path = MOUNTINFO) -> str:
    """The type, mount point and options of the mount that holds `path`, as JSON."""
    if not path.is_dir():
        return json.dumps({"type": "missing", "target": None, "options": []})
    device = os.stat(path).st_dev
    real = os.path.realpath(path)
    found = None
    for line in mountinfo.read_text().splitlines():
        fields = line.split(" ")
        separator = fields.index("-")
        major, minor = (int(part) for part in fields[2].split(":"))
        target = _unescape(fields[4])
        inside = real == target or real.startswith(target.rstrip("/") + "/")
        if os.makedev(major, minor) == device and inside:
            # A later entry mounts over an earlier one.
            options = fields[5].split(",") + fields[separator + 3].split(",")
            found = {
                "type": fields[separator + 1],
                "target": target,
                "options": sorted(set(options)),
            }
    if found is None:
        raise ValueError(f"no mount in {mountinfo} holds {path}")
    return json.dumps(found)


def mount_problems(found: dict, margin_s: float = DEFAULT_SAFETY_MARGIN_S) -> list[str]:
    """What in an NFS lease mount breaks the lease, each with its fix.

    The lease needs each open of the lease file to read what another host last wrote
    (close-to-open consistency), `flock` to reach the server, and attribute caching no longer
    than the `margin_s` that the lease reserves for the hosts' views to differ.
    """
    if not str(found.get("type", "")).startswith("nfs"):
        return []
    options = {}
    for option in found.get("options", []):
        name, _, value = option.partition("=")
        options[name] = value
    problems = []
    if "nocto" in options:
        problems.append(
            "nocto lets an open of the lease read what this host cached rather than what the "
            "other host wrote; remount without nocto"
        )
    if options.get("local_lock", "none") != "none":
        problems.append(
            f"local_lock={options['local_lock']} keeps flock on this host, so the hosts' locks "
            "do not exclude each other; remount with local_lock=none"
        )
    if "nolock" in options:
        problems.append(
            "nolock keeps locks on this host, so the hosts' locks do not exclude each other; "
            "remount without nolock"
        )
    cache = dict(NFS_ATTRIBUTE_CACHE)
    if "noac" in options:
        cache = dict.fromkeys(cache, 0.0)
    if "actimeo" in options:
        cache = dict.fromkeys(cache, float(options["actimeo"]))
    for name in cache:
        if name in options:
            cache[name] = float(options[name])
    over = [f"{name}={value:g}" for name, value in cache.items() if value > margin_s]
    if over:
        problems.append(
            f"the attribute cache holds lease attributes for up to "
            f"{max(cache.values()):g}s ({', '.join(over)}), beyond the {margin_s:g}s the lease "
            f"reserves; remount with actimeo={margin_s:g} or noac"
        )
    return problems


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
    witnessing = commands.add_parser("witness")
    witnessing.add_argument("path", type=Path)
    witnessing.add_argument("--check", action="store_true")
    stating = commands.add_parser("state")
    stating.add_argument("path", type=Path)
    commands.add_parser("clock")
    mounting = commands.add_parser("mount")
    mounting.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "claim":
            print(claim(args.path, args.holder, args.ttl, args.wait))
        elif args.command == "read":
            print(read(args.path))
        elif args.command == "hold":
            print(hold(args.path, args.seconds, args.marker))
        elif args.command == "witness":
            print(witness(args.path, args.check))
        elif args.command == "state":
            print(state(args.path))
        elif args.command == "clock":
            clock()
        elif args.command == "mount":
            print(mount(args.path))
        else:
            print(probe(args.path))
    except (OSError, LeaseError, ValueError) as error:
        print(f"narwhal-host: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
