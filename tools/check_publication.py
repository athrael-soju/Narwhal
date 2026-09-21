"""Check indexed blobs without printing private content or following worktree symlinks."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        (path.name.startswith(".env") and path.name != ".env.example")
        or path.name == "engine.env"
        or name == "config/fleet.json"
        or name.startswith(("runs/", "profiles/"))
        or (
            name.startswith("config/engine-launch.")
            and name.endswith(".json")
            and name != "config/engine-launch.example.json"
        )
        or (
            name.startswith("config/hosts.")
            and name.endswith(".json")
            and name != "config/hosts.example.json"
        )
        or (
            name.startswith("config/fleet.")
            and name.endswith(".json")
            and name not in {"config/fleet.example.json", "config/fleet.stub.json"}
        )
    )


def main() -> int:
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    failed = False
    for entry in git("ls-files", "--stage", "-z", "--full-name").split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        name = raw_name.decode(errors="surrogateescape")
        reasons = []
        if stage != b"0":
            reasons.append("unmerged index entry")
        if private_path(name):
            reasons.append("private file path")
        if mode != b"160000":
            content = git("cat-file", "blob", oid.decode())
            if PRIVATE_KEY.search(content):
                reasons.append("private key material")
        if reasons:
            # repr escapes control characters in filenames before writing to CI logs.
            print(f"{name!r}: {', '.join(reasons)}", file=sys.stderr)
            failed = True
    if failed:
        print("publication check failed; remove private content from the index", file=sys.stderr)
    else:
        print(f"publication check passed for {root.name}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
