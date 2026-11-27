from __future__ import annotations

import json

from ...deployment.launch_engine.check import CheckContext


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
