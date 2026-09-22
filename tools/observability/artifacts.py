"""Stage readable monitoring mounts inside a private deployment directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from tools.observability.make_targets import TargetContract, write_contract

BASE = Path(__file__).resolve().parent
MOUNTS = BASE.parents[1] / "runs" / "observability" / "mounts"
FILES = {
    "prometheus.yml": "prometheus/prometheus.yml",
    "../prometheus-alerts.yml": "prometheus/prometheus-alerts.yml",
    "grafana/provisioning/dashboards/narwhal.yml": "grafana-provisioning/dashboards/narwhal.yml",
    "grafana/provisioning/datasources/prometheus.yml": (
        "grafana-provisioning/datasources/prometheus.yml"
    ),
    "../grafana-narwhal.json": "grafana-dashboards/narwhal.json",
}


def _directory(path: Path, mode: int) -> None:
    if path.is_symlink():
        raise ValueError(f"Monitoring mount directory must be a real directory: {path}")
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    path.chmod(mode)


def stage_artifacts(contract: TargetContract, root: Path = MOUNTS, source: Path = BASE) -> None:
    """Copy the named configs and discovery targets with explicit container permissions."""
    _directory(root, 0o700)
    for relative in (
        "prometheus",
        "prometheus/targets",
        "grafana-provisioning",
        "grafana-provisioning/dashboards",
        "grafana-provisioning/datasources",
        "grafana-dashboards",
    ):
        _directory(root / relative, 0o755)
    for origin, destination in FILES.items():
        target = root / destination
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write((source / origin).read_bytes())
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o644)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    write_contract(contract, root / "prometheus" / "targets")
