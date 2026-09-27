"""Setuptools hooks that preserve source provenance through source archives."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
import tomllib
import zipfile
from pathlib import Path

from setuptools import build_meta

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "src/narwhal"
RESOURCE_LINKS = {"fleet.example.json": "config/fleet.example.json"}


def _git(*arguments):
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("Package build requires Git")
    # Callers supply fixed Git inspection arguments; no shell evaluates them.
    return subprocess.check_output(  # noqa: S603
        [executable, "-C", str(ROOT), *arguments], text=True
    )


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "narwhal_source_bundle", PACKAGE / "_source_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tracked_sources():
    """Approve package paths and the repository's declared fixture resource link."""
    tracked = _git("ls-files", "--stage", "-z", "--", "src/narwhal", *RESOURCE_LINKS.values())
    entries = {}
    for entry in tracked.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, _object_id, stage = metadata.split(" ")
        if stage != "0":
            raise ValueError("Package build requires resolved source entries")
        entries[name] = mode
    allowed = {
        name.removeprefix("src/narwhal/") for name in entries if name.startswith("src/narwhal/")
    }
    if not allowed:
        raise ValueError("Package build requires a tracked source manifest")
    resource_links = {}
    for name, destination in RESOURCE_LINKS.items():
        if entries.get("src/narwhal/" + name) != "120000":
            continue
        target = ROOT / destination
        if (
            entries.get(destination) not in {"100644", "100755"}
            or target.resolve(strict=True) != target
            or not stat.S_ISREG(target.lstat().st_mode)
        ):
            raise ValueError("Package resource link requires an approved tracked regular target")
        resource_links[name] = target
    return allowed, resource_links


@contextlib.contextmanager
def _provenance():
    helper = _helpers()
    metadata_path = PACKAGE / helper.METADATA
    bundle_path = PACKAGE / helper.BUNDLE
    if metadata_path.exists() or bundle_path.exists():
        # An sdist owns its original build identity, including when extracted into
        # a new Git repository by package tests.
        document = json.loads(metadata_path.read_bytes())
        helper.verify_bundle(PACKAGE, document, bundle_path.read_bytes())
        yield
        return
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    try:
        allowed, resource_links = _tracked_sources()
        # Check names before reading package payloads, including ignored files.
        helper.package_files(PACKAGE, allowed=allowed, resource_links=resource_links)
        commit = _git("rev-parse", "HEAD").strip()
        dirty = _git("status", "--porcelain", "--untracked-files=normal")
        verified = not dirty.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "Package build requires Git metadata or a retained source bundle"
        ) from error
    document, bundle = helper.create_bundle(
        PACKAGE,
        commit=commit,
        version=version,
        verified=verified,
        allowed=allowed,
        resource_links=resource_links,
    )
    try:
        metadata_path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
        bundle_path.write_bytes(bundle)
        yield
    finally:
        metadata_path.unlink(missing_ok=True)
        bundle_path.unlink(missing_ok=True)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with _provenance():
        filename = build_meta.build_wheel(wheel_directory, config_settings, metadata_directory)
        wheel = Path(wheel_directory) / filename
        try:
            helper = _helpers()
            document = json.loads((PACKAGE / helper.METADATA).read_bytes())
            with zipfile.ZipFile(wheel) as archive:
                names = {
                    name.removeprefix("narwhal/")
                    for name in archive.namelist()
                    if name.startswith("narwhal/") and not name.endswith("/")
                }
                if names != {*document["files"], helper.METADATA, helper.BUNDLE}:
                    raise ValueError("Wheel contains files outside its source artifact")
                for name in (helper.METADATA, helper.BUNDLE):
                    if archive.read("narwhal/" + name) != (PACKAGE / name).read_bytes():
                        raise ValueError("Wheel provenance differs from its source artifact")
                for name, expected in document["files"].items():
                    if hashlib.sha256(archive.read("narwhal/" + name)).hexdigest() != expected:
                        raise ValueError("Wheel package bytes differ from its source artifact")
        except BaseException:
            wheel.unlink(missing_ok=True)
            raise
        return filename


def build_sdist(sdist_directory, config_settings=None):
    with _provenance():
        return build_meta.build_sdist(sdist_directory, config_settings)


# Editable installs remain useful for development. They intentionally have no
# verified source artifact and cannot start managed mutations.
build_editable = build_meta.build_editable
get_requires_for_build_editable = build_meta.get_requires_for_build_editable
get_requires_for_build_sdist = build_meta.get_requires_for_build_sdist
get_requires_for_build_wheel = build_meta.get_requires_for_build_wheel
prepare_metadata_for_build_wheel = build_meta.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = build_meta.prepare_metadata_for_build_editable
