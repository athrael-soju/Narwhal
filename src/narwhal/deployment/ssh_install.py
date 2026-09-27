"""Install a verified source bundle under an operation's private directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def install(request_path: Path) -> None:
    """Clone the bound commit and install its wheel in an isolated environment."""
    request = json.loads(request_path.read_bytes())
    root = request_path.parent
    source = root / "source.bundle"
    revision = request["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Installation requires a full source commit")
    if hashlib.sha256(source.read_bytes()).hexdigest() != request["bundle_sha256"]:
        raise ValueError("Source bundle differs from the recorded input")
    os.umask(0o077)
    checkout = root / "checkout"
    if checkout.exists():
        raise ValueError("Installation directory already exists")
    subprocess.run(
        ["git", "clone", "--branch", "deployment", str(source), str(checkout)], check=True
    )
    actual = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != revision:
        raise ValueError("Installed checkout differs from the recorded commit")
    python = checkout / ".venv/bin/python"
    subprocess.run([sys.executable, "-m", "venv", str(checkout / ".venv")], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-input", ".", "-c", "constraints-dev.txt"],
        cwd=checkout,
        check=True,
    )
    subprocess.run([str(checkout / ".venv/bin/narwhal-check"), "--help"], cwd=checkout, check=True)
    descriptor = os.open(
        root / "installed.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {"commit": revision, "bundle_sha256": request["bundle_sha256"], "python": str(python)},
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("One installation request is required")
    install(Path(sys.argv[1]))
