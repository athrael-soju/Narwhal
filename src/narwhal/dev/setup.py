"""Download a direct template's pinned engine build and model file, then initialize the instance."""

from __future__ import annotations

import hashlib
import http.client
import json
import platform
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from . import template
from .template import starts_directly

AUTO = "auto"
NO_ACCELERATOR = "none"
MODEL_URL = "https://huggingface.co/{repository}/resolve/{revision}/{filename}"
FETCH_TIMEOUT_S = 60
BLOCK = 1024 * 1024
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


def run(
    root: Path,
    *,
    model: str | None = None,
    accelerator: str = AUTO,
    engine_count: int | None = None,
    port_base: int | None = None,
    get: Fetch = fetch,
) -> dict:
    if not sys.platform.startswith("linux"):
        raise ValueError("narwhal dev setup runs on Linux, and on Windows under WSL2")
    if not hasattr(tarfile, "data_filter"):
        raise ValueError("narwhal dev setup requires Python 3.11.4 or later")
    root = root.expanduser().resolve()
    installed = templates()
    if (root / "instance.json").is_file():
        existing = json.loads((root / "instance.json").read_text())
        if not starts_directly(existing):
            raise ValueError(f"existing instance {root} was initialized by narwhal dev init")
        saved = json.loads((root / "template.json").read_text())
        served = saved["model"]["served_name"].lower()
        conflicts = [
            flag
            for flag, changed in (
                ("--model", model not in (None, served)),
                ("--accelerator", accelerator not in (AUTO, existing.get("engine_build"))),
            )
            if changed
        ]
        if conflicts:
            raise ValueError(
                f"existing instance {root} conflicts with {', '.join(conflicts)}; create a "
                "fresh instance with narwhal dev setup --instance <new-directory>"
            )
        template.materialize(root, engine_count=engine_count, port_base=port_base)
        return {"status": "reused", "instance": str(root)}
    if root.exists() and any(entry.name != "assets" for entry in root.iterdir()):
        raise ValueError(f"{root} holds other files; name an empty or new instance directory")
    if not installed:
        raise ValueError("no template that starts its engines directly is installed")
    if model is not None and model not in installed:
        raise ValueError(
            f"no installed template serves {model}; choose from {', '.join(installed)}"
        )
    spec = installed[model or next(iter(installed))]
    engine = spec["engine"]
    build = _build(engine, accelerator)
    assets = root / "assets"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    assets.mkdir(exist_ok=True)
    binary, downloaded = _engine(engine, build, assets, get)
    pinned = spec["model"]
    model_file = assets / pinned["filename"]
    if _asset(MODEL_URL.format(**pinned), model_file, pinned["sha256"], get):
        downloaded.append(pinned["filename"])
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
    return {
        "status": "initialized",
        "instance": str(root),
        "model": pinned["served_name"],
        "engine_build": build,
        "downloaded": downloaded,
    }
