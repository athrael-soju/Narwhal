"""Export Jobs, which copy a fleet's evidence out of the state volume for `narwhal deploy
--target kubernetes` on a workstation.

The workstation asks for an export by setting the fleet's `EXPORT_REQUEST` annotation to a
`Request`: a nonce, the attempt directories it already holds, and the last file it received
of an attempt it holds only in part. The fleet controller answers by creating the export Job
of that nonce and naming it in `status.exports`. The workstation then reads the log of the
Job's pod, so it needs no verb on Jobs.

The controller builds the Job from its own settings, never from the fleet: its own image,
pinned by the chart, and its own user and group. The Job has no ServiceAccount token, and it
mounts, read-only, only the run record and the finished attempt directories the workstation
lacks, each by its own subPath. The run directory's root is never mounted, so the salt that
keys the run record's digests, the key check's salt and HMACs, and the controller's lock and
stall files stay out of every export pod. A finished attempt never changes again, so the
workstation holds it whole once it has copied it.

The Job prints each file as `kubernetes_gate` returns a gate's files, in path order, until
its log would pass `EXPORT_LOG_BYTES`, below the kubelet's 10 MiB log rotation, then a
summary line and the manifest `kubernetes_target.gate_files` checks. The next request
carries on after the last file it received.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fleet_resource, kubernetes_gate, kubernetes_objects, run_record
from .kubernetes_objects import Owner

EXPORT_REQUEST = fleet_resource.EXPORT_REQUEST
# The gate label value of an export Job.
EXPORT = "export"
MOUNT = "/narwhal-state"
EXPORT_LOG_BYTES = 8 * 1024 * 1024
# What the manifest and summary lines take beside the files.
EXPORT_RESERVE = 64 * 1024
EXPORT_SECONDS = 300
EXPORT_TTL_SECONDS = 600
# Attempt directories one export Job mounts at most; the next request carries on.
MAX_ATTEMPTS = 64
# A request stays well inside the 256 KiB the API server allows a fleet's annotations.
REQUEST_MAX_BYTES = 64 * 1024
# The answers `status.exports` keeps, newest last.
KEPT_ANSWERS = 4
SUMMARY = "narwhal-export "
_ATTEMPTS_ENV = "NARWHAL_EXPORT_ATTEMPTS"
_AFTER_ENV = "NARWHAL_EXPORT_AFTER"
_BYTES_ENV = "NARWHAL_EXPORT_BYTES"
_CAPPED_ENV = "NARWHAL_EXPORT_CAPPED"
NONCE = re.compile(r"[0-9a-f]{32}")
# The directory of one stage instance's attempts: `stages/<stage>[/<instance>]`.
INSTANCE_DIRECTORY = re.compile(
    r"stages/[a-z0-9]([-a-z0-9]*[a-z0-9])?(/[A-Za-z0-9][A-Za-z0-9._-]*)?"
)
# A run-relative file name an export may write: plain parts only.
SAFE_PATH = re.compile(r"(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+")
# A file being written: `stages.write_evidence`'s, the key check's and the mirror's own.
TEMPORARY = r"\..+(-[0-9a-f]{32}|\.[0-9]+|\.[0-9]+\.export)"
# The statuses after which an attempt's directory never changes. An interrupted attempt is
# resumed in its own directory, which the runner empties first.
FINISHED = (
    run_record.PASSED,
    run_record.FAILED,
    run_record.CANCELLED,
    run_record.TIMEOUT,
    run_record.INVALIDATED,
)

# The export Job's program, standard library only. It reads the run record and the attempt
# directories its mounts name, and prints every file after `after`, in path order, until the
# budget.
SCRIPT = f"""import base64, hashlib, json, os, re, sys
root = sys.argv[1]
attempts = json.loads(os.environ[{_ATTEMPTS_ENV!r}])
after = os.environ.get({_AFTER_ENV!r}, "")
capped = os.environ.get({_CAPPED_ENV!r}) == "1"
budget = int(os.environ[{_BYTES_ENV!r}]) - {EXPORT_RESERVE}
safe = re.compile({SAFE_PATH.pattern!r})
temporary = re.compile({TEMPORARY!r})
owner = {{}}
for attempt in attempts:
    for top, dirs, names in os.walk(os.path.join(root, attempt)):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(top, d)))
        for name in names:
            path = os.path.join(top, name)
            if temporary.fullmatch(name) or os.path.islink(path) or not os.path.isfile(path):
                continue
            owner[os.path.relpath(path, root).replace(os.sep, "/")] = attempt
# `after` names the last file sent of one attempt; the files of that attempt up to it were sent.
resumed = next((a for a in attempts if after.startswith(a + "/")), None)
manifest, used, large, unnamed, last = {{}}, 0, [], 0, ""
def read(rel):
    try:
        with open(os.path.join(root, rel), "rb") as source:
            return source.read()
    except OSError:
        return None
def cost(rel, data):
    return 2 * len(rel) + 4 * ((len(data) + 2) // 3) + 160
def send(rel, data):
    print({kubernetes_gate.FILE!r} + rel + " " + base64.b64encode(data).decode(), flush=True)
    manifest[rel] = {{"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
record = read({run_record.RECORD!r})
if record is not None:
    if cost({run_record.RECORD!r}, record) > budget:
        large.append({run_record.RECORD!r})
    else:
        used = cost({run_record.RECORD!r}, record)
        send({run_record.RECORD!r}, record)
paths = sorted(owner)
unsent = set()
for index, rel in enumerate(paths):
    if owner[rel] == resumed and rel <= after:
        continue
    if not safe.fullmatch(rel) or ".." in rel.split("/"):
        unnamed += 1
        continue
    data = read(rel)
    if data is None:
        continue
    if cost(rel, data) > budget:
        large.append(rel)
        continue
    if used + cost(rel, data) > budget:
        unsent = {{owner[r] for r in paths[index:]}}
        break
    used += cost(rel, data)
    send(rel, data)
    last = rel
deferred = bool(unsent)
complete = [a for a in attempts if a not in unsent]
summary = {{"more": deferred or capped, "deferred": deferred, "large": large[:64],
            "unnamed": unnamed, "complete": complete, "last": last if deferred else ""}}
print({SUMMARY!r} + json.dumps(summary), flush=True)
print({kubernetes_gate.MANIFEST!r} + json.dumps(manifest, sort_keys=True), flush=True)
"""


@dataclass(frozen=True)
class Request:
    """What the workstation asks of one export: `held` maps each stage instance's directory
    to the attempt numbers it holds whole, and `after` names the last file it received of the
    attempt it holds in part, or is empty."""

    nonce: str
    held: Mapping[str, Sequence[int]] = field(default_factory=dict)
    after: str = ""

    def value(self) -> str:
        """The annotation's value."""
        held = {name: sorted(set(numbers)) for name, numbers in sorted(self.held.items())}
        text = json.dumps(
            {"nonce": self.nonce, "held": held, "after": self.after},
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(text.encode()) > REQUEST_MAX_BYTES:
            raise ValueError(
                f"the export request holds more than {REQUEST_MAX_BYTES} bytes of attempts; "
                "start a new run directory"
            )
        return text

    @classmethod
    def parse(cls, text: str) -> Request:
        """The request an annotation holds; raises ValueError for one no export runs."""
        if not isinstance(text, str) or len(text.encode()) > REQUEST_MAX_BYTES:
            raise ValueError(f"{EXPORT_REQUEST} must be JSON of at most {REQUEST_MAX_BYTES} bytes")
        try:
            found = json.loads(text)
        except ValueError:
            raise ValueError(f"{EXPORT_REQUEST} is not JSON") from None
        if not isinstance(found, dict) or set(found) - {"nonce", "held", "after"}:
            raise ValueError(f"{EXPORT_REQUEST} holds only nonce, held and after")
        nonce = found.get("nonce")
        if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
            raise ValueError(f"{EXPORT_REQUEST}: nonce must be 32 lowercase hex digits")
        held = found.get("held", {})
        if not isinstance(held, dict) or not all(
            isinstance(name, str)
            and INSTANCE_DIRECTORY.fullmatch(name)
            and isinstance(numbers, list)
            and all(type(n) is int and 0 < n <= 1_000_000 for n in numbers)
            for name, numbers in held.items()
        ):
            raise ValueError(
                f"{EXPORT_REQUEST}: held maps stages/<stage>[/<instance>] to attempt numbers"
            )
        after = found.get("after", "")
        if not isinstance(after, str) or (after and not safe_path(after)):
            raise ValueError(f"{EXPORT_REQUEST}: after must be a run-relative file name")
        return cls(nonce, {name: tuple(numbers) for name, numbers in held.items()}, after)


def safe_path(name: str) -> bool:
    """Whether `name` is a run-relative file name of plain parts, none `.`, `..` or empty."""
    parts = name.split("/")
    return (
        len(name) <= 1024
        and bool(SAFE_PATH.fullmatch(name))
        and all(part not in ("", ".", "..") for part in parts)
    )


def finished_attempts(record: Mapping[str, Any]) -> list[tuple[str, int]]:
    """Every attempt directory of the record that will not change again, as (the stage
    instance's directory, attempt number): each earlier attempt of an instance, and its last
    one once it ended."""
    found = []
    for entry in record.get("stages", {}).values():
        directory = str(entry["directory"])
        parent, _, last = directory.rpartition("/attempt-")
        if not INSTANCE_DIRECTORY.fullmatch(parent) or not last.isdigit():
            continue
        latest = int(last)
        ended = entry["status"] in FINISHED
        found += [(parent, n) for n in range(1, latest + (1 if ended else 0))]
    return sorted(found)


def wanted(record: Mapping[str, Any], request: Request, directory: Path) -> list[str]:
    """The attempt directories the workstation lacks for `request`: the finished ones the run
    directory holds as real directories, in path order. An export Job mounts the first
    `MAX_ATTEMPTS`."""
    attempts = []
    for parent, number in finished_attempts(record):
        if number in request.held.get(parent, ()):
            continue
        relative = f"{parent}/attempt-{number}"
        path = directory / relative
        # The controller made every part itself; a link would point an export elsewhere.
        parts = [
            directory / Path(*relative.split("/")[:n]) for n in range(1, relative.count("/") + 2)
        ]
        if any(part.is_symlink() for part in parts) or not path.is_dir():
            continue
        attempts.append(relative)
    return sorted(attempts)


def job_name(uid: str, nonce: str) -> str:
    return f"{EXPORT}-{hashlib.sha256(f'{uid}:{nonce}'.encode()).hexdigest()[:12]}"


def job(
    owner: Owner,
    request: Request,
    attempts: Sequence[str],
    *,
    image: str,
    user: int,
    group: int,
    claim: str,
    runs: str,
) -> dict[str, Any]:
    """The export Job of `request`, which mounts the run record and the first `MAX_ATTEMPTS`
    of `attempts` read-only, and reports more to come when it leaves some out.

    `runs` is the run directory's path on the state volume. The fleet owns the Job, so it
    goes with the fleet, but does not control it: stopping the fleet leaves an export that
    copies the evidence of the stop to finish.
    """
    name = job_name(owner.uid, request.nonce)
    capped = len(attempts) > MAX_ATTEMPTS
    attempts = list(attempts)[:MAX_ATTEMPTS]
    mounts = [
        {
            "name": "state",
            "mountPath": f"{MOUNT}/{run_record.RECORD}",
            "subPath": f"{runs}/{run_record.RECORD}",
            "readOnly": True,
        }
    ]
    mounts += [
        {
            "name": "state",
            "mountPath": f"{MOUNT}/{attempt}",
            "subPath": f"{runs}/{attempt}",
            "readOnly": True,
        }
        for attempt in attempts
    ]
    container = {
        "name": EXPORT,
        "image": image,
        "command": ["python3", "-I", "-B", "-c", SCRIPT, MOUNT],
        "env": [
            {"name": _ATTEMPTS_ENV, "value": json.dumps(list(attempts))},
            {"name": _AFTER_ENV, "value": request.after},
            {"name": _CAPPED_ENV, "value": "1" if capped else "0"},
            {"name": _BYTES_ENV, "value": str(EXPORT_LOG_BYTES)},
        ],
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": mounts,
    }
    extra = {"gate": EXPORT}
    metadata = kubernetes_objects.metadata(owner, name, kubernetes_objects.GATE, extra=extra)
    metadata["ownerReferences"] = [{**owner.reference(), "controller": False}]
    document = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": EXPORT_SECONDS,
            "ttlSecondsAfterFinished": EXPORT_TTL_SECONDS,
            "template": {
                "metadata": {"labels": kubernetes_objects.labels(kubernetes_objects.GATE, **extra)},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": user,
                        "runAsGroup": group,
                        "seccompProfile": {"type": kubernetes_objects.RUNTIME_DEFAULT},
                    },
                    "containers": [container],
                    "volumes": [
                        {
                            "name": "state",
                            "persistentVolumeClaim": {"claimName": claim, "readOnly": True},
                        }
                    ],
                },
            },
        },
    }
    kubernetes_objects.check_metadata(document)
    return document
