"""Every relative Markdown link in the tracked tree must resolve.

Four references dangled after one file move (pre-release audit); this is the
guard. Offline by design: external URLs are somebody's uptime, not this
repository's integrity, so only repository-relative targets are checked.
Anchors are stripped; a `#fragment`-only link is skipped.

Exit 0 clean, 1 with one line per dangling link.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", "*.md"], capture_output=True, text=True, check=True
    )
    return [Path(line) for line in out.stdout.splitlines()]


def dangling(files: list[Path]) -> list[str]:
    bad = []
    for md in files:
        for target in LINK.findall(md.read_text()):
            if "://" in target or target.startswith(("mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            if not (md.parent / path).exists():
                bad.append(f"{md}: {target}")
    return bad


def main() -> int:
    bad = dangling(tracked_markdown())
    for line in bad:
        print(line)
    if bad:
        print(f"{len(bad)} dangling link(s)", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
