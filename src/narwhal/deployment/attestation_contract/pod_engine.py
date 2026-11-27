"""The engine container of a Kubernetes pod, as the attestation sidecar in that pod reads it.

The sidecar mounts the launch directory that checked its pod read-only, its pod's directory of
the state volume at the pod steps' STATE, the engine volume at ENGINE and the model at MODEL.
It binds the engine to its container through the pod's shared process namespace and the
Kubernetes API (`narwhal.engines.pod_identity`), and reads what the engine container's serve
step recorded in the engine process's directory of the engine volume.

Each engine container gets its own attestation: its captures and published attempt lie under
`containers/<container ID>/` of the pod's directory, so a container the kubelet restarts is
attested afresh.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...engines.pod_identity import PodIdentity, read_identity, service_account_namespace
from ..launch_engine import pod as pod_steps
from ..launch_engine.checkpoint import checkpoint_status
from ..launch_engine.plan import POD_ADDRESS
from ..launch_engine.runtime import write_private

# The sidecar's paths below lie under POD_ROOT: / in the pod, and a directory of its own when
# a test runs the sidecar.
POD_ROOT = Path("/")
# The pod specification sets it from the downward API.
POD_NAME_ENV = "NARWHAL_POD_NAME"
# The live identity a capture attempt binds, the source of its image digest.
IDENTITY_RECORD = "pod-identity.json"
CONTAINERS = "containers"
# The identity fields that name one engine process in one container of one image.
BINDING_FIELDS = (
    "pod_uid",
    "container_id",
    "image_id",
    "started_at",
    "pid",
    "start_ticks",
)


def pod_path(path: Path) -> Path:
    """Return where the sidecar reads the pod path `path`."""
    return POD_ROOT / path.relative_to("/")


@dataclass(frozen=True)
class PodEngine:
    """The live engine container of the sidecar's pod."""

    identity: PodIdentity
    # The HCA the RDMA device plugin allocated to the engine container, as the check of the
    # pod recorded it. The engine container's serve step sets UCX_NET_DEVICES from it.
    transfer_device: dict

    @property
    def directory(self) -> Path:
        """The engine process's directory in the engine volume, which its serve step wrote."""
        return pod_path(pod_steps.ENGINE) / f"{self.identity.pid}-{self.identity.start_ticks}"

    def binding(self) -> dict:
        return {"image_id": self.identity.image_id, "container_id": self.identity.container_id}

    def environment(self, values: dict[str, str]) -> dict[str, str]:
        """Return launch environment values as the engine received them, with the pod address
        Kubernetes substitutes and the allocated HCA."""
        return {
            **{
                name: value.replace(POD_ADDRESS, self.identity.pod_ip)
                for name, value in values.items()
            },
            "UCX_NET_DEVICES": self.transfer_device["ucx_net_devices"],
        }


def live_pod(checked: dict) -> PodEngine:
    """Return the engine container of this pod, which must be the pod `checked` names."""
    name = os.environ.get(POD_NAME_ENV, "")
    if not name:
        raise ValueError(
            f"{POD_NAME_ENV} is unset; a Kubernetes engine's attestation binds in its pod, so "
            "run this in the pod's attestation container"
        )
    device = checked.get("transfer_device")
    if not isinstance(device, dict) or not isinstance(device.get("ucx_net_devices"), str):
        raise ValueError(
            "the check of the pod records no allocated HCA; check the pod's records again"
        )
    identity = read_identity(
        service_account_namespace(), name, checked["pod_uid"], checked["planned_image"]
    )
    return PodEngine(identity, device)


def engine_home(pod_evidence: Path, container_id: str) -> Path:
    """The directory of one engine container's attestation in the pod's directory."""
    return pod_evidence / CONTAINERS / container_id


def write_identity(engine: PodEngine, attempt: Path) -> Path:
    destination = attempt / IDENTITY_RECORD
    record = {**engine.identity.record(), "transfer_device": engine.transfer_device}
    write_private(destination, json.dumps(record, indent=2) + "\n")
    return destination


def require_identity(engine: PodEngine, attempt: Path) -> Path:
    """Require the attempt's identity record to name the live engine process."""
    path = attempt / IDENTITY_RECORD
    recorded = json.loads(path.read_text())
    live = engine.identity.record()
    for name in BINDING_FIELDS:
        if recorded.get(name) != live[name]:
            raise ValueError(
                f"the engine's {name} is {live[name]}, not the {recorded.get(name)} the "
                f"attempt's {IDENTITY_RECORD} records; the engine container started again"
            )
    return path


def engine_record(engine: PodEngine, name: str) -> Path:
    path = engine.directory / name
    if not path.is_file():
        raise ValueError(
            f"{path} is missing; the engine container's serve step writes it for process "
            f"{engine.identity.pid}"
        )
    return path


def recorded_inspection(engine: PodEngine, name: str, argv: list[str], log: Path) -> str:
    """Return the output of an inspection the engine container ran before the engine started,
    kept in `log`. Raise when it ran other arguments or failed."""
    records = json.loads(engine_record(engine, pod_steps.INSPECTIONS_RECORD).read_text())
    record = records.get(name)
    if not isinstance(record, dict):
        raise ValueError(f"the engine container recorded no {name} inspection")
    if record.get("argv") != argv:
        raise ValueError(f"the engine container's {name} inspection ran other arguments")
    write_private(log, f"{record.get('stdout', '')}\nSTDERR\n{record.get('stderr', '')}")
    if "error" in record:
        raise ValueError(f"the {name} inspection failed: {record['error']}; inspect {log}")
    if record.get("returncode") != 0:
        raise ValueError(f"the {name} inspection exited {record.get('returncode')}; inspect {log}")
    return str(record["stdout"])


def require_model_files(checked: dict) -> None:
    """Refuse the pod's model files when they changed after its checkpoint init container
    hashed them. The check compares file status, so it reads no model file.

    The kubelet restarts the engine container without its init containers, so each
    attestation compares the files again.
    """
    recorded = checked.get("model_status_sha256")
    if not isinstance(recorded, str):
        raise ValueError(
            "the check of the pod records no model file status; check the pod's records again"
        )
    model = pod_path(pod_steps.MODEL)
    try:
        status = checkpoint_status(model, contained=True)
    except (OSError, ValueError) as error:
        raise ValueError(f"the model files under {model} are unreadable: {error}") from error
    if status != recorded:
        raise ValueError(
            f"the model files under {model} changed after the pod's checkpoint init container "
            "hashed them; replace the pod, whose new init containers hash them again"
        )


def model_config() -> Path:
    """The model configuration the pod serves."""
    return pod_path(pod_steps.MODEL) / "config.json"
