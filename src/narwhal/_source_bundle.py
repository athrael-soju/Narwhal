"""Create and verify the deterministic source artifact carried by built packages."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import stat
import tarfile
from pathlib import Path
from typing import Any

METADATA = "_build_provenance.json"
BUNDLE = "_source_bundle.tar.gz"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def package_files(root: Path, *, allowed: set[str] | None = None) -> dict[str, bytes]:
    """Read package sources and resources without following package symlinks."""
    selected = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.name in {METADATA, BUNDLE} and path.parent == root:
            continue
        if path.is_symlink():
            raise ValueError("Package source must not contain symlinks")
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Package source must contain regular files")
        selected[path.relative_to(root).as_posix()] = path
    if allowed is not None and set(selected) - allowed:
        raise ValueError("Package contains files outside the approved source manifest")
    return {name: path.read_bytes() for name, path in selected.items()}


def create_bundle(
    root: Path,
    *,
    commit: str | None,
    version: str,
    verified: bool,
    allowed: set[str] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Bind a build's package bytes to its full source revision."""
    files = package_files(root, allowed=allowed)
    manifest = {
        "schema": "narwhal.build-provenance",
        "schema_version": 1,
        "commit": commit,
        "distribution_version": version,
        "verified": verified,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in [("manifest.json", encoded), *files.items()]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    bundle = gzip.compress(buffer.getvalue(), mtime=0)
    return {**manifest, "bundle_sha256": hashlib.sha256(bundle).hexdigest()}, bundle


def verify_bundle(root: Path, document: dict[str, Any], bundle: bytes) -> dict[str, Any]:
    """Require the retained artifact and every installed package byte to match."""
    if (
        document.get("schema") != "narwhal.build-provenance"
        or document.get("schema_version") != 1
        or type(document.get("verified")) is not bool
        or not isinstance(document.get("distribution_version"), str)
        or not isinstance(document.get("files"), dict)
        or len(bundle) > MAX_BUNDLE_BYTES
        or hashlib.sha256(bundle).hexdigest() != document.get("bundle_sha256")
    ):
        raise ValueError("Package build provenance is invalid")
    commit = document.get("commit")
    if commit is not None and (
        not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit)
    ):
        raise ValueError("Package source revision is invalid")
    expected = {key: value for key, value in document.items() if key != "bundle_sha256"}
    files = package_files(root, allowed=set(document["files"]))
    if {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} != document[
        "files"
    ]:
        raise ValueError("Installed package differs from its source artifact")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(bundle)) as compressed:
            data = compressed.read(MAX_BUNDLE_BYTES + 1)
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError("Package source artifact exceeds its size limit")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            seen = set()
            for member in archive:
                if not member.isfile() or member.name in seen or member.size > MAX_BUNDLE_BYTES:
                    raise ValueError("Package source artifact contains an invalid entry")
                seen.add(member.name)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Package source artifact entry is missing")
                content = source.read()
                if member.name == "manifest.json":
                    if json.loads(content) != expected:
                        raise ValueError("Package source artifact manifest differs")
                elif member.name not in files or content != files[member.name]:
                    raise ValueError("Package source artifact content differs")
            if seen != {"manifest.json", *files}:
                raise ValueError("Package source artifact is incomplete")
    except (OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Package source artifact cannot be verified") from error
    return document
