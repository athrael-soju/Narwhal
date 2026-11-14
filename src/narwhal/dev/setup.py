"""Download a direct template's pinned engine build and model file, then initialize the instance."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from narwhal.deployment.launch_engine.runtime import write_private

from . import lifecycle, template
from .template import starts_directly

AUTO = "auto"
NO_ACCELERATOR = "none"
MODEL_URL = "https://huggingface.co/{repository}/resolve/{revision}/{filename}"
FETCH_TIMEOUT_S = 60
BLOCK = 1024 * 1024
INSTANCE_FILES = ("template.json", "fleet.json", "instance.json")
# Holds the instance's files while new settings replace them.
PREVIOUS = "previous.json"
Fetch = Callable[[str, Path], None]


def templates() -> dict[str, dict]:
    """The installed direct templates by the lowercase name of the model each serves,
    the default one first.
    """
    found = {}
    for entry in resources.files("narwhal.dev").iterdir():
        if entry.name.endswith(".json"):
            document = json.loads(entry.read_text())
            if (
                document.get("schema") == "narwhal.dev-template"
                and starts_directly(document)
                and "engine" in document
            ):
                found[document["model"]["served_name"].lower()] = document
    return dict(sorted(found.items(), key=lambda item: (not item[1].get("setup_default"), item[0])))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, path: Path) -> None:
    """Download `url` to `path`, reporting progress on stderr."""
    if not url.startswith("https://"):
        raise ValueError(f"setup downloads over https only: {url}")
    request = urllib.request.Request(  # noqa: S310 - https only, checked above
        url, headers={"User-Agent": "narwhal-dev-setup"}
    )
    partial = path.with_name(path.name + ".part")
    try:
        with (
            urllib.request.urlopen(  # noqa: S310 - https only, checked above
                request, timeout=FETCH_TIMEOUT_S
            ) as response,
            partial.open("wb") as output,
        ):
            total = int(response.headers.get("Content-Length") or 0)
            done = shown = 0
            for block in iter(lambda: response.read(BLOCK), b""):
                output.write(block)
                done += len(block)
                if total and done * 10 // total > shown:
                    shown = done * 10 // total
                    print(f"  {path.name}: {shown * 10}% of {total >> 20} MiB", file=sys.stderr)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _asset(url: str, path: Path, sha256: str, get: Fetch) -> bool:
    """Leave the pinned file at `path`; return whether it was downloaded."""
    if path.is_file() and _sha256(path) == sha256:
        return False
    print(f"downloading {path.name}", file=sys.stderr)
    try:
        get(url, path)
    except http.client.HTTPException as exc:
        raise OSError(f"download of {path.name} failed: {exc!r}") from exc
    found = _sha256(path)
    if found != sha256:
        path.unlink()
        raise ValueError(f"{path.name} SHA-256 {found} differs from the pinned {sha256}")
    return True


def _build(engine: dict, accelerator: str) -> str:
    known = engine["builds"]
    if accelerator == AUTO:
        detected = [
            name
            for name, build in known.items()
            if any(Path(path).exists() for path in build.get("detect", []))
        ]
        accelerator = detected[0] if detected else NO_ACCELERATOR
    if accelerator not in known:
        raise ValueError(
            f"the template pins no {accelerator} engine build; it pins {', '.join(sorted(known))}"
        )
    return accelerator


def _engine(engine: dict, name: str, assets: Path, get: Fetch) -> tuple[Path, list[str]]:
    machine = platform.machine()
    archives = engine["builds"][name].get(machine)
    if not archives:
        raise ValueError(f"the template pins no {name} engine build for {machine}")
    downloaded = [
        archive["name"]
        for archive in archives
        if _asset(
            engine["url"].format(name=archive["name"]),
            assets / archive["name"],
            archive["sha256"],
            get,
        )
    ]
    target = assets / f"engine-{name}"
    marker = target / ".extracted"
    expected = json.dumps([archive["sha256"] for archive in archives])
    if not marker.is_file() or marker.read_text() != expected:
        # A tree from an interrupted run or from other archives never mixes with this one.
        staging = assets / f"engine-{name}.partial"
        for stale in (target, staging):
            shutil.rmtree(stale, ignore_errors=True)
        for archive in archives:
            try:
                with tarfile.open(assets / archive["name"]) as bundle:
                    # The data filter refuses members that leave the target or carry device
                    # nodes.
                    bundle.extractall(staging, filter="data")
            except tarfile.TarError as exc:
                raise ValueError(f"{archive['name']} cannot be unpacked: {exc}") from exc
        (staging / ".extracted").write_text(expected)
        staging.rename(target)
    binaries = [path for path in target.rglob(engine["binary"]) if path.is_file()]
    if len(binaries) != 1:
        raise ValueError(
            f"the {name} engine build holds {len(binaries)} files named {engine['binary']}"
        )
    binary = binaries[0]
    # A runtime archive unpacks its shared libraries beside the build's directory.
    for library in sorted(target.rglob("*.so*")):
        beside = binary.parent / library.name
        if (
            library.is_file()
            and library.parent != binary.parent
            and not (beside.is_symlink() or beside.exists())
        ):
            beside.symlink_to(library)
    return binary, downloaded


def _restore(root: Path) -> None:
    held = root / PREVIOUS
    for name, content in json.loads(held.read_text()).items():
        (root / name).unlink(missing_ok=True)
        write_private(root / name, content)
    held.unlink()


def run(
    root: Path,
    *,
    model: str | None = None,
    accelerator: str | None = None,
    engine_count: int | None = None,
    port_base: int | None = None,
    get: Fetch = fetch,
) -> dict:
    if not sys.platform.startswith("linux"):
        raise ValueError("narwhal dev setup runs on Linux, and on Windows under WSL2")
    if not hasattr(tarfile, "data_filter"):
        raise ValueError("narwhal dev setup requires Python 3.11.4 or later")
    root = root.expanduser().resolve()
    settings = (model, accelerator, engine_count, port_base, get)
    if (root / "instance.json").is_file() or (root / PREVIOUS).is_file():
        # up reads the instance's files under this lock.
        with lifecycle.locked(root):
            if (root / PREVIOUS).is_file():
                _running(root)
                # An interrupted change left the instance's files here.
                _restore(root)
            return _configure(root, *settings)
    return _configure(root, *settings)


def _running(root: Path) -> None:
    state = root / "lifecycle.json"
    if state.is_file() and json.loads(state.read_text()).get("phase") != "stopped":
        raise ValueError(
            f"run narwhal dev down --instance {root} before changing the instance's settings"
        )


def _configure(
    root: Path,
    model: str | None,
    accelerator: str | None,
    engine_count: int | None,
    port_base: int | None,
    get: Fetch,
) -> dict:
    installed = templates()
    previous: dict[str, str] = {}
    saved = None
    if (root / "instance.json").is_file():
        existing = json.loads((root / "instance.json").read_text())
        if "engine_build" not in existing:
            raise ValueError(f"existing instance {root} was initialized by narwhal dev init")
        saved = json.loads((root / "template.json").read_text())
        served = saved["model"]["served_name"].lower()
        # An option left out keeps the instance's value.
        model = model or served
        if accelerator == AUTO:
            accelerator = _build(saved["engine"], AUTO)
        accelerator = accelerator or existing["engine_build"]
        if engine_count is None:
            engine_count = existing["engine_count"]
        ports = existing["ports"]
        if (
            model == served
            and accelerator == existing["engine_build"]
            and engine_count == existing["engine_count"]
            and (port_base is None or (port_base, port_base + 1) == tuple(ports.values()))
        ):
            return {"status": "reused", "instance": str(root)}
        _running(root)
        previous = {name: (root / name).read_text() for name in INSTANCE_FILES}
        if model != served:
            saved = None
    elif root.exists() and any(entry.name != "assets" for entry in root.iterdir()):
        raise ValueError(f"{root} holds other files; name an empty or new instance directory")
    if not installed:
        raise ValueError("no template that starts its engines directly is installed")
    if saved is None and model is not None and model not in installed:
        raise ValueError(
            f"no installed template serves {model}; choose from {', '.join(installed)}"
        )
    if saved is not None:
        # The instance's own template carries its pins and ports through a change.
        spec = saved
    else:
        spec = copy.deepcopy(installed[model or next(iter(installed))])
        if previous and port_base is None:
            spec["ports"] = dict(ports)
    engine = spec["engine"]
    build = _build(engine, accelerator or AUTO)
    assets = root / "assets"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets.mkdir(exist_ok=True)
    binary, downloaded = _engine(engine, build, assets, get)
    pinned = spec["model"]
    model_file = assets / pinned["filename"]
    if _asset(MODEL_URL.format(**pinned), model_file, pinned["sha256"], get):
        downloaded.append(pinned["filename"])
    # The instance's files give way to the new settings and return if a check refuses them.
    if previous:
        (root / f"{PREVIOUS}.new").unlink(missing_ok=True)
        write_private(root / f"{PREVIOUS}.new", json.dumps(previous))
        os.replace(root / f"{PREVIOUS}.new", root / PREVIOUS)
    for name in previous:
        (root / name).unlink()
    try:
        template.materialize(
            root,
            template=spec,
            engine_binary=binary,
            model_path=model_file,
            engine_count=engine_count,
            port_base=port_base,
            accelerated=build != NO_ACCELERATOR,
            prepared=True,
            recorded={"engine_build": build},
        )
    except BaseException:
        if previous:
            _restore(root)
        raise
    (root / PREVIOUS).unlink(missing_ok=True)
    return {
        "status": "reconfigured" if previous else "initialized",
        "instance": str(root),
        "model": pinned["served_name"],
        "engine_build": build,
        "engines": spec["allocation"]["engine_count"] if engine_count is None else engine_count,
        "downloaded": downloaded,
    }
