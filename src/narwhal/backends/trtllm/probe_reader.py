"""Read the TensorRT-LLM runtime probe's output.

A Kubernetes engine pod runs this module with the engine image's Python, so it imports only the
standard library.
"""

from __future__ import annotations

import json

VERSION_LABEL = "TensorRT-LLM"
# The tag the launch wrapper's `_check` prints before its record.
SETTINGS_TAG = "NARWHAL_IMAGE_RUNTIME="


def probe_result(plan: dict, environment: dict[str, str], output: str) -> dict:
    """Return the probe's version and cache settings; raise for a failed probe."""
    records = [
        json.loads(line.removeprefix(SETTINGS_TAG))
        for line in output.splitlines()
        if line.startswith(SETTINGS_TAG)
    ]
    if len(records) != 1:
        raise ValueError("runtime check requires one runtime record")
    return {
        "version": records[0].get("tensorrt_llm_version"),
        "prefix_caching": records[0].get("prefix_caching"),
        "kv_events": records[0].get("kv_events"),
        "fields": {},
        "notes": [],
    }
