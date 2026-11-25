"""Hash a checkpoint's files, and fingerprint their file status, with the standard library.

Discovery runs this module's source on each engine host, so it imports only the standard
library.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

EXCLUDED_PATHS = ("README.md", ".cache/huggingface/**")


def json_digest(value: object) -> str:
    """Return the SHA-256 of the compact, key-sorted JSON encoding of `value`."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_status(relative: str, info: os.stat_result) -> list:
    """Return the status fields that change when a file's content is rewritten or replaced.

    Writing a file changes its modification and change times, replacing it changes its inode,
    and only a change of the system clock sets a change time back.
    """
    return [relative, info.st_size, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]


def _files(root: Path, *, contained: bool) -> list[tuple[str, Path]]:
    """Return the checkpoint files under `root` in path order, as relative names and paths.

    With `contained`, every entry must resolve inside `root` through relative links only, so
    a symbolic link cannot name a file that a container mount of `root` would not serve.
    """
    root = root.resolve(strict=True)
    if not root.is_dir() or not (root / "config.json").is_file():
        raise ValueError("checkpoint directory needs config.json")
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative == Path("README.md") or relative.parts[:2] == (".cache", "huggingface"):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        if not path.is_file():
            raise ValueError(f"checkpoint entry is not a regular file: {relative}")
        if contained and not path.resolve().is_relative_to(root):
            raise ValueError(f"checkpoint entry resolves outside the model directory: {relative}")
        # A container resolves an absolute target under its mount point, not the host path.
        if contained and path.is_symlink() and os.path.isabs(os.readlink(path)):
            raise ValueError(f"checkpoint entry links to an absolute path: {relative}")
        files.append((relative.as_posix(), path))
    if not files:
        raise ValueError("checkpoint directory contains no files")
    return files


def hash_regular_file(path: Path, relative: str) -> tuple[str, list]:
    """Hash the regular file at `path`, returning its SHA-256 and its file status.

    The file is opened without blocking and its type read from the open descriptor, so a
    FIFO or device swapped in after a directory scan fails instead of blocking the read.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"model entry is not a regular file: {relative}")
        digest = hashlib.sha256()
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    status = file_status(relative, after)
    if file_status(relative, before) != status or file_status(relative, path.stat()) != status:
        raise ValueError(f"model file changed during inspection: {relative}")
    return digest.hexdigest(), status


def inspect_checkpoint(root: Path, *, contained: bool = True) -> dict:
    """Return a stable content manifest for the files served from a model directory.

    `contained` refuses an entry that resolves outside `root`; a native launch, which reads
    Hugging Face snapshot links into the cache, passes False.
    """
    files = []
    statuses = []
    for relative, path in _files(root, contained=contained):
        sha256, status = hash_regular_file(path, relative)
        files.append({"path": relative, "size": status[1], "sha256": sha256})
        statuses.append(status)
    return {
        "manifest_version": 2,
        "excluded_paths": list(EXCLUDED_PATHS),
        "model_tree_sha256": json_digest(files),
        "status_sha256": json_digest(statuses),
        "files": files,
        "file_count": len(files),
        "bytes_verified": sum(row["size"] for row in files),
    }


def checkpoint_status(root: Path, *, contained: bool = True) -> str:
    """Return the status fingerprint of the files `inspect_checkpoint` hashes, reading none."""
    return json_digest(
        [file_status(relative, path.stat()) for relative, path in _files(root, contained=contained)]
    )


def main() -> int:
    try:
        inputs = json.load(sys.stdin)
        # Discovery inspects the directory that a container engine mounts.
        result = inspect_checkpoint(
            Path(inputs["model_dir"]), contained=not inputs.get("follow_links", False)
        )
        # File status holds host-local inode numbers and times.
        del result["status_sha256"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Checkpoint inspection failed: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
