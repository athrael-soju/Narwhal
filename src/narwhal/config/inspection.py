"""Inspect validated fleet settings using filesystem and environment reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..contracts import EFFECTIVE_CONFIG, versioned
from .model import FleetConfig
from .serialization import document


def inspect_config(config: FleetConfig, source: str | Path) -> dict[str, Any]:
    """Describe the fleet-file layer and serving defaults from a loaded config."""
    settings = document(config)
    for engine in settings["engines"]:
        engine.setdefault("pin", False)
        engine.setdefault("attestation_url", "")
        engine.setdefault("shared_device", None)
    settings.setdefault("engine_contract", None)
    settings.setdefault("hardware", None)
    control_connections = config.resolved_control_connections()
    return versioned(
        EFFECTIVE_CONFIG,
        {
            "scope": "fleet_file",
            "source": str(Path(source).resolve()),
            "working_directory": str(Path.cwd()),
            "settings": settings,
            "derived": {
                "engine_count": len(config.engines),
                "control_connections": control_connections,
                "data_keepalive_connections": max(1, config.max_connections // 2),
                "control_keepalive_connections": max(1, control_connections // 2),
                "max_concurrent": config.max_connections,
                "http_retained_limit": config.max_connections + config.serving.queue_capacity,
                "uvicorn_graceful_timeout_s": int(config.graceful_timeout_s),
            },
            "artifact_paths": {
                "profiles": str(config.profiles_path.resolve()),
                "state": str(config.state_path.resolve()),
                "journal": str((config.profiles_path.parent / "journal.jsonl").resolve()),
            },
        },
    )
