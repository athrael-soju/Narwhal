"""Hash a provisioned checkpoint with Python's standard library on an engine host."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

EXCLUDED_PATHS = ("README.md", ".cache/huggingface/**")


def inspect_checkpoint(root: Path) -> dict:
    """Return a stable content manifest for the files served from a model directory."""
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
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"checkpoint file changed during inspection: {relative}")
        files.append(
            {"path": relative.as_posix(), "size": after.st_size, "sha256": digest.hexdigest()}
        )
    if not files:
        raise ValueError("checkpoint directory contains no files")
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return {
        "manifest_version": 2,
        "excluded_paths": list(EXCLUDED_PATHS),
        "model_tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": files,
        "file_count": len(files),
        "bytes_verified": sum(row["size"] for row in files),
    }


def main() -> int:
    try:
        inputs = json.load(sys.stdin)
        result = inspect_checkpoint(Path(inputs["model_dir"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Checkpoint inspection failed: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
