from __future__ import annotations

import json

from ...deployment.launch_engine.check import CheckContext, ProbeResult
from .runtime import SETTINGS_TAG


def engine_arguments(plan: dict) -> list[str]:
    # The wrapper's own "-m launch_engine serve" precedes the server arguments.
    return list(plan["args"][3:])


def probe(context: CheckContext) -> list[str]:
    plan = context.plan
    return [
        context.launcher_path,
        "_check",
        json.dumps(plan["expected_packages"]),
        json.dumps(engine_arguments(plan)),
    ]


def result(context: CheckContext, output: str) -> ProbeResult:
    records = [
        json.loads(line.removeprefix(SETTINGS_TAG))
        for line in output.splitlines()
        if line.startswith(SETTINGS_TAG)
    ]
    if len(records) != 1:
        raise context.failure("runtime check requires one runtime record")
    record = records[0]
    return ProbeResult(
        version=record.get("sglang_version"),
        prefix_caching=record.get("prefix_caching"),
        kv_events=record.get("kv_events"),
    )
