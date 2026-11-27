"""What a gate Job of `narwhal deploy --target kubernetes` runs around its command.

`run` unpacks the inputs the runner sent in the Job's ConfigMap, runs one command, and prints
the files it names as `narwhal-file <name> <base64>` lines, then one `narwhal-manifest` line
with each file's SHA-256 and size. The runner reads them from the Job's log and refuses a log
whose files differ from the manifest, so a truncated log fails loudly. The command's own
output goes to standard error, so only this module writes those lines.

With `NARWHAL_GATE_KEEP` set, `run` also writes the returned files beneath that directory of the
state volume, under `files/`.

The module uses the standard library only: a discovery Job runs it in the engine image, from
its ConfigMap, where Narwhal is not installed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

FILE = "narwhal-file "
MANIFEST = "narwhal-manifest "
CAUSE = "narwhal-gate: "
INPUTS = "inputs.tar.gz"
# The state volume directory where the runner has a Job keep its output.
KEEP_ENV = "NARWHAL_GATE_KEEP"


def unpack(archive: Path, into: Path) -> None:
    """Unpack the runner's inputs into a fresh directory, refusing links and unsafe names."""
    into.mkdir(mode=0o700, parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts
            if (
                member.name.startswith("/")
                or ".." in parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"the inputs hold the unsafe entry {member.name!r}")
            target = into.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = tar.extractfile(member)
            assert source is not None
            target.write_bytes(source.read())
            target.chmod(member.mode & 0o755)


def pack(files: dict[str, bytes], modes: dict[str, int] | None = None) -> bytes:
    """A gzip tar of `files`, by their relative paths, for a gate's inputs ConfigMap."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = (modes or {}).get(name, 0o600)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def returned(names: list[str]) -> dict[str, Path]:
    """The files each `NAME=PATH` names; a NAME ending in / returns PATH's files beneath it."""
    found: dict[str, Path] = {}
    for item in names:
        name, _, path = item.partition("=")
        source = Path(path)
        if name.endswith("/"):
            if source.is_dir():
                for file in sorted(p for p in source.rglob("*") if p.is_file()):
                    found[name + file.relative_to(source).as_posix()] = file
        elif source.is_file():
            found[name] = source
    return found


def emit(files: dict[str, bytes]) -> None:
    """Print each file, then the manifest the runner checks them against."""
    manifest = {}
    for name, data in sorted(files.items()):
        print(FILE + name + " " + base64.b64encode(data).decode(), flush=True)
        manifest[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    print(MANIFEST + json.dumps(manifest, sort_keys=True), flush=True)


def run(inputs: Path | None, into: Path | None, names: list[str], command: list[str]) -> int:
    if inputs is not None and into is not None:
        unpack(inputs, into)
    status = subprocess.run(command, stdout=sys.stderr, check=False).returncode
    # The evidence of a failed command returns too, and stays on the state volume.
    files = {name: path.read_bytes() for name, path in returned(names).items()}
    if keep := os.environ.get(KEEP_ENV):
        for name, data in files.items():
            target = Path(keep, "files", *PurePosixPath(name).parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(data)
    emit(files)
    if status:
        print(f"{CAUSE}{command[0]} exited with status {status}", file=sys.stderr, flush=True)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m narwhal.deployment.kubernetes_gate")
    actions = parser.add_subparsers(dest="action", required=True)
    action = actions.add_parser("run", help="run one gate command and return its files")
    action.add_argument("--inputs", type=Path, help="the inputs archive the runner sent")
    action.add_argument("--into", type=Path, help="fresh directory to unpack the inputs into")
    action.add_argument("--return", dest="names", action="append", default=[], metavar="NAME=PATH")
    action.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("name the command after --")
    os.umask(0o077)
    return run(args.inputs, args.into, args.names, command)


if __name__ == "__main__":
    sys.exit(main())
