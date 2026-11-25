"""Apply the qualified GGUF plugin sources to the installed wheel and check the plugin digests.

The image build runs this script after it installs the plugin wheel:

    python3 install_plugin_sources.py REVISION PYTHON_SHA256 EXTENSION_SHA256

It replaces each Python file of the installed `vllm_gguf_plugin` package with the file at
REVISION of the plugin repository, as the Narwhal dev CUDA runtime does, and exits with status 1
unless the plugin's Python sources and CUDA extension match the two digests. The digests follow
the rule that `narwhal dev` and `narwhal-engine check` apply.
"""

from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
import urllib.request
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath

ARCHIVE = "https://github.com/vllm-project/vllm-gguf-plugin/archive/{revision}.tar.gz"
PACKAGE = "vllm_gguf_plugin"


def gguf_plugin_digests(package: Path) -> dict[str, str]:
    sources = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        sources.update(source.relative_to(package).as_posix().encode() + b"\0")
        sources.update(source.read_bytes() + b"\0")
    extension = hashlib.sha256((package / "_C_gguf.abi3.so").read_bytes()).hexdigest()
    return {
        "gguf_plugin_python_sha256": sources.hexdigest(),
        "gguf_plugin_extension_sha256": extension,
    }


def apply_sources(archive: bytes, package: Path) -> int:
    """Write each Python file under the archive's package directory into `package`."""
    written = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member in bundle.getmembers():
            parts = PurePosixPath(member.name).parts
            # GitHub archives hold one top-level directory, <repository>-<revision>/.
            if len(parts) < 3 or parts[1] != PACKAGE or not member.isfile():
                continue
            relative = PurePosixPath(*parts[2:])
            if relative.suffix != ".py" or ".." in relative.parts:
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            destination = package.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())
            written += 1
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    revision, python_sha256, extension_sha256 = argv
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        print("REVISION must be a full commit SHA", file=sys.stderr)
        return 2
    package = Path(str(distribution("vllm-gguf-plugin").locate_file(PACKAGE)))
    url = ARCHIVE.format(revision=revision)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
        archive = response.read()
    if not apply_sources(archive, package):
        print(f"the plugin archive at {revision} holds no {PACKAGE} sources", file=sys.stderr)
        return 1
    digests = gguf_plugin_digests(package)
    for name, value in digests.items():
        print(f"{name}={value}")
    expected = {
        "gguf_plugin_python_sha256": python_sha256,
        "gguf_plugin_extension_sha256": extension_sha256,
    }
    if digests != expected:
        print("GGUF plugin files differ from the qualified digests", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
