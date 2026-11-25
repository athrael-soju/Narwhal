"""Check that the image holds each pinned distribution at its release.

    python3 check_packages.py NAME==VERSION ...

A local version label, such as the `+cu130` of a CUDA build of torch, matches its release. The
script exits with status 1 when a distribution is missing or holds another release.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version


def main(pins: list[str]) -> int:
    failed = False
    for pin in pins:
        name, _, release = pin.partition("==")
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = "missing"
        print(f"{name}=={installed}")
        if installed.split("+", 1)[0] != release:
            print(f"{name} must be {release}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
