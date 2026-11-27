"""Judge a runtime probe's output against its launch plan.

The offline check and a Kubernetes engine pod both run this module. The pod runs it with the
engine image's Python, so it imports only the standard library.
"""

from __future__ import annotations

from types import ModuleType


def verdict(reader: ModuleType, plan: dict, environment: dict[str, str], output: str) -> dict:
    """Return a passing probe's checked evidence and notes; raise ValueError for a failed one.

    `reader` is the engine backend's probe reader module.
    """
    result = reader.probe_result(plan, environment, output)
    version = result["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"runtime check returned an invalid {reader.VERSION_LABEL} version")
    if type(result["prefix_caching"]) is not bool:
        raise ValueError("runtime check requires one resolved cache setting")
    planned = plan.get("kv_events")
    expected = (
        None if planned is None else {key: planned[key] for key in ("endpoint", "replay_endpoint")}
    )
    if result["kv_events"] != expected:
        raise ValueError("runtime cache-event endpoints differ from the launch plan")
    return {
        "evidence": {
            "engine_version": version,
            "prefix_caching": result["prefix_caching"],
            "kv_events": result["kv_events"],
            **result["fields"],
        },
        "notes": list(result["notes"]),
    }
