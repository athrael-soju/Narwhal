"""Steps that `narwhal deploy run` runs on a host, in the deployment directory `install` made.

A step runs one command in a role's installed environment, with the role file loaded and the
deployment directory's virtual environment active. Its output goes to the host log, and the
files it names come back to the stage's attempt directory on the workstation, the evidence of
a failed step included. `SSHRunner` lets a supervisor run its commands on a host the same way.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import shlex
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .hosts import HOST_CAUSE, SSH, Host

FILE = "narwhal-file "
STATUS = "narwhal-status "
LAST = "narwhal-last "
STREAM = "narwhal-stream "
# A returned file's path, relative to the attempt directory.
_RELATIVE = re.compile(r"(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+")


class StepFailed(RuntimeError):
    """A command of a step exited non-zero; the files it left are in the attempt directory."""

    def __init__(self, host: str, operation: str, status: int, cause: str, log: Path) -> None:
        self.host = host
        self.context = {"host": host, "exit_status": status, "log": str(log)}
        detail = f": {cause}" if cause else ""
        super().__init__(f"{host}: {operation} exited {status}{detail}; inspect {log}")


def prelude(remote_dir: str, role: str | None = None) -> str:
    """Start a host script in the deployment directory, with `role`'s environment loaded."""
    script = f"""set -eu
umask 077
fail() {{ printf '{HOST_CAUSE}%s\\n' "$1" >&2; exit 4; }}
emit() {{
  test -f "$2" && test ! -L "$2" || fail "$2 is missing or not a regular file"
  printf '{FILE}%s %s\\n' "$1" "$(base64 -w 0 < "$2")"
}}
emit_if() {{ if test -e "$2"; then emit "$1" "$2"; fi; }}
emit_tree() {{
  if test -d "$2"; then
    (cd "$2" && find . -type f | sort) | while IFS= read -r path; do
      emit "$1${{path#./}}" "$2/${{path#./}}"
    done
  fi
}}
cd {shlex.quote(remote_dir)}
"""
    if role is not None:
        script += f"""set +x
. ./runs/deployment/.env.{role}
. ./.venv/bin/activate
"""
    return script


def files(output: str) -> dict[str, bytes]:
    """The files a step returned, by their path relative to the attempt directory."""
    found = {}
    for line in output.splitlines():
        if not line.startswith(FILE):
            continue
        name, _, data = line[len(FILE) :].partition(" ")
        if not _RELATIVE.fullmatch(name) or ".." in PurePosixPath(name).parts:
            raise ValueError(f"the host returned a file with the unsafe name {name!r}")
        try:
            found[name] = base64.b64decode(data, validate=True)
        except binascii.Error:
            raise ValueError(f"the host returned {name} in an unreadable form") from None
    return found


def write(directory: Path, returned: Mapping[str, bytes]) -> None:
    """Write returned files under `directory`, each new and private."""
    for name, data in sorted(returned.items()):
        path = directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o600)


@dataclass(frozen=True)
class Step:
    """One command on one host, in a fresh working directory of the deployment directory.

    `workdir` is created first, and must not exist yet. `command` is shell text; quote its
    arguments with `shlex`. `returns` maps each name in the attempt directory to a path on
    the host, a directory's files included, returned whether the command passed or not.
    """

    host: Host
    stage: str
    operation: str
    remote_dir: str
    role: str | None
    command: str
    workdir: str | None = None
    returns: Mapping[str, str] | None = None
    # Shell text run after a failed command, before the files return, such as a cleanup.
    on_failure: str = ""


def run(ssh: SSH, step: Step, directory: Path) -> str:
    """Run `step`, write the files it returns to `directory`, and return its output.

    Raises `StepFailed` with the command's last output line after writing the files.
    """
    script = prelude(step.remote_dir, step.role)
    if step.workdir is not None:
        workdir = PurePosixPath(step.workdir)
        script += (
            f"mkdir -p {shlex.quote(str(workdir.parent))}\nmkdir {shlex.quote(str(workdir))}\n"
        )
    script += f"""set +e
output=$({step.command} 2>&1)
status=$?
set -e
printf '%s\\n' "$output" >&2
printf '{STREAM}%s\\n' "$(printf '%s' "$output" | base64 -w 0)"
printf '{STATUS}%s\\n' "$status"
printf '{LAST}%s\\n' "$(printf '%s' "$output" | tail -n 1)"
"""
    if step.on_failure:
        script += f'if test "$status" != 0; then {step.on_failure}; fi\n'
    for name, path in (step.returns or {}).items():
        if name.endswith("/"):
            script += f"emit_tree {shlex.quote(name)} {shlex.quote(path)}\n"
        else:
            script += f"emit_if {shlex.quote(name)} {shlex.quote(path)}\n"
    output = ssh.run(step.host, step.stage, step.operation, script)
    returned = files(output)
    write(directory, returned)
    status, last, stream = 0, "", ""
    for line in output.splitlines():
        if line.startswith(STATUS):
            status = int(line[len(STATUS) :])
        elif line.startswith(LAST):
            last = line[len(LAST) :]
        elif line.startswith(STREAM):
            stream = base64.b64decode(line[len(STREAM) :]).decode(errors="replace")
    if status:
        raise StepFailed(step.host.id, step.operation, status, last, ssh.log(step.host))
    return stream


def upload(ssh: SSH, host: Host, stage: str, source: Path, target: str) -> None:
    """Copy `source` to `target` on `host`, a new file checked against the source's SHA-256."""
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    path = shlex.quote(target)
    temporary = shlex.quote(f"{target}.part-{expected[:16]}")
    script = f"""set -eu
umask 077
fail() {{ printf '{HOST_CAUSE}%s\\n' "$1" >&2; exit 4; }}
mkdir -p "$(dirname {path})"
temporary={temporary}
trap 'rm -f "$temporary"' EXIT
(set -C; cat > "$temporary")
test "$(sha256sum "$temporary" | cut -d' ' -f1)" = {expected} ||
  fail "{target} arrived with another SHA-256"
ln "$temporary" {path} || fail "{target} already exists"
"""
    ssh.run(host, stage, f"copy {source.name}", script, payload=source)


class SSHRunner:
    """A supervision `Runner` that runs each command on `host` over `ssh`.

    The command's exit status, output and error output come back as they would locally, and
    a missing executable exits 127, as it does under a shell.
    """

    def __init__(self, ssh: SSH, host: Host, stage: str) -> None:
        self.ssh = ssh
        self.host = host
        self.stage = stage

    def __call__(
        self, argv: Sequence[str], *, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        command = shlex.join(argv)
        # The input, such as a unit's environment with its credentials, reaches the command on
        # standard input through ssh, so neither the host log nor an argument list holds it.
        script = f"""set -u
out=$(mktemp) && err=$(mktemp) || exit 4
trap 'rm -f "$out" "$err"' EXIT
{command} {"" if input is not None else "< /dev/null "}> "$out" 2> "$err"
status=$?
printf '{STATUS}%s\\n' "$status"
printf '{FILE}stdout %s\\n' "$(base64 -w 0 < "$out")"
printf '{FILE}stderr %s\\n' "$(base64 -w 0 < "$err")"
"""
        with tempfile.TemporaryDirectory(prefix="narwhal-input-") as folder:
            payload = None
            if input is not None:
                payload = Path(folder) / "input"
                payload.touch(mode=0o600)
                payload.write_text(input)
            output = self.ssh.run(self.host, self.stage, f"run {argv[0]}", script, payload=payload)
        streams = files(output)
        status = next(
            (int(line[len(STATUS) :]) for line in output.splitlines() if line.startswith(STATUS)),
            1,
        )
        return subprocess.CompletedProcess(
            list(argv),
            status,
            streams.get("stdout", b"").decode(errors="replace"),
            streams.get("stderr", b"").decode(errors="replace"),
        )
