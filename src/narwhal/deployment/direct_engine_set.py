"""Turn a backend's direct start into the Helm chart's `directEngines` values.

`narwhal dev` starts these engines as plain host processes. The chart runs them as its `engine`
StatefulSet, one pod per engine named for its engine role, from the command the backend's
direct start renders here. Each pod binds its own IP on one port, mounts the model read-only and
shares one directory with every other engine for the backend's KV handoff files.

The engines take no launch plan and no attestation, so the fleet that routes over them has no
`engine_contract`. The router admits them as it admits the engines of `narwhal dev`. Every
production fleet runs attested engines from `chart_values`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from narwhal.backends import load as load_backend

from .direct_start import DirectEngine
from .launch_engine.engine_set import STATEFULSET, escape

SCHEMA = "narwhal.direct-engine-set"
SCHEMA_VERSION = 1
PORT = 8080
MODEL_MOUNT = "/model"
SHARED_MOUNT = "/narwhal-shared"
# The pod's IP, which the chart sets in each engine container.
POD_IP = "$(NARWHAL_POD_IP)"
_CPUS = re.compile(r"([1-9][0-9]*)")


def _cpus(limit: Any) -> int:
    """Whole CPUs of a limit such as 2 or "4"; the engine runs one thread on each."""
    found = _CPUS.fullmatch(str(limit))
    if found is None:
        raise ValueError(f"give the CPU limit as whole CPUs, not {limit!r}")
    return int(found.group(1))


def direct_values(
    *,
    backend: str,
    runtime: Mapping[str, Any],
    binary: str,
    model_file: str,
    served_name: str,
    count: int,
    image: str,
    resources: Mapping[str, Mapping[str, Any]],
    model_volume: Mapping[str, Any],
    shared: Mapping[str, str],
    namespace: str,
    service: str = "engines",
    state_claim: str = "narwhal-state",
) -> dict[str, Any]:
    """The `directEngines` values of `count` engines of `backend`.

    `binary` is the engine server's path in `image`, `model_file` the model's path inside the
    model volume, and `runtime` the backend's runtime section, as a `narwhal dev` template
    holds it. `resources` gives each engine's CPU and memory requests and limits. The
    environment the direct start adds is the engine's, in place of the image's variables of the
    same name.
    """
    if count < 2:
        raise ValueError("a disaggregated fleet needs at least two engines")
    start = load_backend(backend).direct_start()
    engine = DirectEngine(
        model=Path(MODEL_MOUNT) / model_file,
        served_name=served_name,
        host="<pod-ip>",
        port=PORT,
        runtime=dict(runtime),
        shared_dir=Path(SHARED_MOUNT),
        accelerated=False,
        threads=_cpus(resources["limits"]["cpu"]),
    )
    command = [
        POD_IP if part == engine.host else escape(part)
        for part in start.command(Path(binary), engine)
    ]
    # The pod's variables replace the image's variables of the same name.
    environment = start.environment(Path(binary), {})
    return {
        "schema": SCHEMA,
        "schemaVersion": SCHEMA_VERSION,
        "backend": backend,
        "count": count,
        "image": image,
        "command": command,
        "env": [{"name": name, "value": escape(value)} for name, value in environment.items()],
        "port": PORT,
        "resources": {kind: dict(values) for kind, values in resources.items()},
        "modelVolume": dict(model_volume),
        "modelMount": MODEL_MOUNT,
        "sharedMount": SHARED_MOUNT,
        "shared": dict(shared),
        "stateClaim": state_claim,
        "serviceName": service,
        "namespace": namespace,
    }


def fleet_engines(values: Mapping[str, Any], cluster_domain: str = "cluster.local") -> list[dict]:
    """The fleet file's engine entries for direct engines: the first half prefill, as dev does."""
    count = values["count"]
    domain = f"{values['serviceName']}.{values['namespace']}.svc.{cluster_domain}"
    return [
        {
            "iid": f"n{number}",
            "url": f"http://{STATEFULSET}-{number}.{domain}:{values['port']}",
            "role": "prefill" if number <= count // 2 else "decode",
        }
        for number in range(1, count + 1)
    ]
