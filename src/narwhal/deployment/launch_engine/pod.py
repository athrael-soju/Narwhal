"""Run the steps of a Kubernetes engine pod with the Python of the unmodified engine image.

The launch delivers this module and checkpoint.py to the pod in a ConfigMap, so both import only
the standard library. Before it runs this module in isolated mode, the pod's bootstrap checks
every delivered file against its SHA-256 in the launch's pod specification.

Each step takes its arguments after its name:

- `checkpoint` hashes the model files the pod serves and records the tree digest.
- `runtime-check <seconds> <probe arguments>` runs the runtime probe within that budget and
  records its output, with UCX_NET_DEVICES set to the HCA the device plugin allocated.
- `serve <engine arguments>` replaces itself with the engine once both records pass for this
  launch plan, this pod and this HCA.

The records land in the pod's own directory of the state volume, where `narwhal-engine check`
reads them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

# The pod's paths below lie under this root, which a test moves to a directory of its own.
ROOT = Path("/")
HOOKS = Path("/narwhal-hooks")
TOOLS = Path("/narwhal-pod")
STATE = Path("/narwhal-state")
MODEL = Path("/model")
VERBS_DEVICES = Path("/dev/infiniband")
VERBS_CLASS = Path("/sys/class/infiniband_verbs")
# The engine sends KV through this port of its HCA.
HCA_PORT = 1
CHECKPOINT_RECORD = "checkpoint.json"
RUNTIME_RECORD = "runtime-check.json"
# The pod specification sets both from the downward API.
POD_UID_ENV = "NARWHAL_POD_UID"
NODE_NAME_ENV = "NARWHAL_NODE_NAME"


class StepFailure(Exception):
    """A pod step that cannot pass; the message names the cause."""


def local(path: Path) -> Path:
    """Return where this process reads the pod path `path`; records keep the pod path."""
    return ROOT / path.relative_to("/")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def delivered_files() -> dict[str, dict[str, str]]:
    """Return the SHA-256 of each delivered file, by mount path and file name.

    The kubelet's own entries in a ConfigMap volume start with two dots.
    """
    return {
        str(root): {
            name: file_digest(local(root) / name)
            for name in sorted(os.listdir(local(root)))
            if not name.startswith("..")
        }
        for root in (HOOKS, TOOLS)
    }


def launch_plan() -> tuple[dict, str]:
    path = local(HOOKS) / "launch.json"
    return json.loads(path.read_text()), file_digest(path)


def record_header(plan_sha256: str) -> dict:
    return {
        "plan_sha256": plan_sha256,
        "pod_uid": os.environ[POD_UID_ENV],
        "node": os.environ[NODE_NAME_ENV],
        "files": delivered_files(),
    }


def write_record(name: str, record: dict) -> None:
    # A restarted pod sandbox runs its init containers again; the latest record replaces the
    # earlier one, and `narwhal-engine check` keeps each copy it reads.
    pending = local(STATE) / f".{name}.{os.getpid()}"
    pending.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(pending, local(STATE) / name)


def transfer_device() -> dict:
    """Return the one HCA the RDMA device plugin allocated to this container."""
    names = sorted(path.name for path in local(VERBS_DEVICES).glob("uverbs*"))
    if len(names) != 1:
        raise StepFailure(
            f"the container holds {len(names)} RDMA verbs devices under {VERBS_DEVICES}; "
            "the engine needs exactly one"
        )
    try:
        hca = (local(VERBS_CLASS) / names[0] / "ibdev").read_text().strip()
    except OSError as error:
        raise StepFailure(f"the HCA of {names[0]} is unreadable: {error}") from error
    if not hca:
        raise StepFailure(f"{VERBS_CLASS / names[0] / 'ibdev'} names no HCA")
    return {
        "verbs_device": names[0],
        "hca": hca,
        "port": HCA_PORT,
        "ucx_net_devices": f"{hca}:{HCA_PORT}",
    }


def load_checkpoint_module() -> ModuleType:
    path = Path(__file__).with_name("checkpoint.py")
    spec = importlib.util.spec_from_file_location("narwhal_pod_checkpoint", path)
    if spec is None or spec.loader is None:
        raise StepFailure(f"{path} is not a Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fail(message: str) -> int:
    print(f"narwhal: {message}", file=sys.stderr)
    return 1


def checkpoint() -> int:
    plan, plan_sha256 = launch_plan()
    header = record_header(plan_sha256)
    try:
        manifest = load_checkpoint_module().inspect_checkpoint(local(MODEL), contained=True)
    except (OSError, ValueError, StepFailure) as error:
        message = f"checkpoint inspection failed: {error}"
        write_record(CHECKPOINT_RECORD, {**header, "error": message})
        return fail(message)
    write_record(
        CHECKPOINT_RECORD,
        {
            **header,
            "model_tree_sha256": manifest["model_tree_sha256"],
            "file_count": manifest["file_count"],
            "bytes_verified": manifest["bytes_verified"],
        },
    )
    if manifest["model_tree_sha256"] != plan["model_tree_sha256"]:
        return fail(f"the model files under {MODEL} differ from the launch plan's tree digest")
    return 0


def runtime_check(seconds: float, probe: list[str]) -> int:
    _, plan_sha256 = launch_plan()
    header = record_header(plan_sha256)
    try:
        device = transfer_device()
    except StepFailure as error:
        write_record(RUNTIME_RECORD, {**header, "error": str(error)})
        return fail(str(error))
    environment = {**os.environ, "UCX_NET_DEVICES": device["ucx_net_devices"]}
    try:
        result = subprocess.run(
            [sys.executable, *probe],
            env=environment,
            capture_output=True,
            text=True,
            timeout=seconds,
        )
    except subprocess.TimeoutExpired as error:
        message = f"the runtime check exceeded {seconds:g} seconds"
        write_record(
            RUNTIME_RECORD,
            {
                **header,
                "transfer_device": device,
                "error": message,
                "stdout": _text(error.stdout),
                "stderr": _text(error.stderr),
            },
        )
        return fail(message)
    write_record(
        RUNTIME_RECORD,
        {
            **header,
            "transfer_device": device,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return fail(f"the runtime check exited {result.returncode}") if result.returncode else 0


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def passed_records(plan: dict, plan_sha256: str) -> dict:
    """Return the runtime record once both records pass for this plan and this pod."""
    records = {}
    for name in (CHECKPOINT_RECORD, RUNTIME_RECORD):
        try:
            record = json.loads((local(STATE) / name).read_text())
        except (OSError, ValueError) as error:
            raise StepFailure(f"{STATE / name} is unreadable: {error}") from error
        if record.get("plan_sha256") != plan_sha256 or record.get("pod_uid") != os.environ.get(
            POD_UID_ENV
        ):
            raise StepFailure(f"{STATE / name} belongs to another launch plan or pod")
        if "error" in record:
            raise StepFailure(f"{STATE / name} records a failure: {record['error']}")
        records[name] = record
    if records[CHECKPOINT_RECORD].get("model_tree_sha256") != plan["model_tree_sha256"]:
        raise StepFailure("the checked model files differ from the launch plan's tree digest")
    if records[RUNTIME_RECORD].get("returncode") != 0:
        raise StepFailure("the runtime check did not pass")
    return records[RUNTIME_RECORD]


def serve(arguments: list[str]) -> int:
    plan, plan_sha256 = launch_plan()
    if arguments != plan["args"]:
        return fail("the engine arguments differ from the launch plan")
    try:
        checked = passed_records(plan, plan_sha256)
        device = transfer_device()
    except StepFailure as error:
        return fail(str(error))
    if device != checked.get("transfer_device"):
        return fail("the allocated HCA differs from the HCA the runtime check recorded")
    os.environ["UCX_NET_DEVICES"] = device["ucx_net_devices"]
    # The engine keeps this process, so its process ID is the one the container started.
    os.execv(sys.executable, ["python3", *arguments])


def main(argv: list[str]) -> int:
    if argv[:1] == ["checkpoint"] and len(argv) == 1:
        return checkpoint()
    if argv[:1] == ["runtime-check"] and len(argv) > 2:
        return runtime_check(float(argv[1]), argv[2:])
    if argv[:1] == ["serve"] and len(argv) > 1:
        return serve(argv[1:])
    return fail("usage: pod.py checkpoint | runtime-check <seconds> <probe> | serve <engine>")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
