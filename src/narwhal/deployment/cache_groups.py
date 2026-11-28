"""Cache-equivalence groups: the engine roles that share one fabric budget.

Two roles belong to one group when they run the same image, model configuration, accelerator,
tensor parallel size, GPU visibility variable, runtime settings and transport, and their live
cache geometry matches: the captured cache layout of every TP rank, or the measured KV handoff.
The group's representative is its lowest-numbered role.

Each layout or handoff record names the SHA-256 of the launch record its engine was prepared
from. That is the per-role record `package` wrote, so the grouping reads it from the package
and refuses a record captured from another launch or image.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .fabric_budget import TRANSFER_SCHEMA
from .launch_engine.backend import cache_capture, launcher, transfer_measurement
from .launch_engine.evidence import evidence_directory, write_json
from .launch_engine.runtime import digest

SCHEMA = "narwhal.cache-groups"
EVIDENCE = "cache-groups.json"
# The sizing that `capture-cache` records, and the fabric budget reads.
LAYOUT_SIZING = "runtime_padded_page_upper_bound"
CACHE_LAYOUT = "cache_layout"
TRANSFER_RECORD = "transfer_record"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _node(role: str) -> int:
    return int(role.removeprefix("engine-"))


def static_inputs(role: str, launch: Mapping[str, Any]) -> tuple[dict, Path]:
    """The launch inputs that decide a role's cache, and the discovery record they come from."""
    source = Path(launch["sources"]["runtime"])
    try:
        observed = json.loads(source.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{role}: read sources.runtime {source}: {error}") from None
    inputs = {
        "image_id": observed["image_id"],
        "model_config_sha256": observed["model_sha256"],
        "accelerator": launch["accelerator"],
        "tensor_parallel_size": launch["tensor_parallel_size"],
        "gpu_visibility_env": launch["gpu_visibility_env"],
        "runtime": launch["runtime"],
        "transport": launch["transfer"]["transport"],
    }
    return inputs, source


def geometry_digest(path: Path) -> str:
    """The digest of a `cache-layout.json` that the `cache_geometry` fact records: the whole
    document but its `plan_sha256`, the digest of the launch plan it was captured from, which
    every fresh launch directory changes while the geometry stays."""
    layout = json.loads(path.read_text())
    if not isinstance(layout, dict):
        raise ValueError(f"{path} is not a JSON object")
    return "sha256:" + _hash({key: value for key, value in layout.items() if key != "plan_sha256"})


def signature(inputs: Mapping[str, Any]) -> str:
    """The 16-hex signature of a role's static cache inputs."""
    return _hash(dict(inputs))[:16]


def packaged_launch(role: str, package: Path, launch: Mapping[str, Any]) -> Path:
    """The launch record that `package` wrote for `role`, checked against the launch records."""
    found = sorted(package.glob(f"*/engine-launch.{role}.json"))
    if len(found) != 1:
        raise ValueError(f"{role}: the package {package} holds {len(found)} launch records for it")
    try:
        packaged = json.loads(found[0].read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{role}: read {found[0]}: {error}") from None
    fields = ("accelerator", "tensor_parallel_size", "gpu_visibility_env", "runtime", "gpu_ids")
    if (
        any(packaged.get(name) != launch.get(name) for name in fields)
        or packaged.get("transfer", {}).get("transport") != launch["transfer"]["transport"]
    ):
        raise ValueError(f"{role}: the package's launch record differs from the launch records")
    return found[0]


def layout_geometry(
    role: str, document: Any, inputs: Mapping[str, Any], launch_sha256: str
) -> dict:
    """Every TP rank's cache geometry from a `cache-layout.json`, without its rank numbers."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"{role}: the cache layout is not a version 1 cache-layout.json")
    if document.get("sizing") != LAYOUT_SIZING:
        raise ValueError(f"{role}: the cache layout records no {LAYOUT_SIZING} sizing")
    if document.get("image") != inputs["image_id"]:
        raise ValueError(f"{role}: the cache layout was captured from another image")
    if document.get("model_config_sha256") != inputs["model_config_sha256"]:
        raise ValueError(f"{role}: the cache layout belongs to another model configuration")
    if document.get("launch_config_sha256") != launch_sha256:
        raise ValueError(f"{role}: the cache layout was captured from another launch record")
    ranks = document.get("ranks")
    tp = inputs["tensor_parallel_size"]
    if not isinstance(ranks, list):
        ranks = []
    numbered = all(isinstance(rank, dict) and type(rank.get("rank")) is int for rank in ranks)
    if not numbered or sorted(rank["rank"] for rank in ranks) != list(range(tp)):
        raise ValueError(f"{role}: the cache layout needs every one of its {tp} TP ranks once")
    geometry = []
    for rank in sorted(ranks, key=lambda rank: rank["rank"]):
        layers = rank.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError(f"{role}: TP rank {rank['rank']} records no cache layers")
        record = {key: value for key, value in rank.items() if key != "rank"}
        record["layers"] = sorted(layers, key=lambda layer: str(layer.get("layer")))
        geometry.append(record)
    return {"kind": CACHE_LAYOUT, "ranks": geometry}


def transfer_geometry(role: str, document: Any, launch_sha256: str) -> dict:
    """The measured handoff size from a `transfer.json`."""
    if (
        not isinstance(document, dict)
        or document.get("schema") != TRANSFER_SCHEMA
        or document.get("schema_version") != 1
    ):
        raise ValueError(f"{role}: the transfer record is not a version 1 {TRANSFER_SCHEMA}")
    if document.get("launch_config_sha256") != launch_sha256:
        raise ValueError(f"{role}: the transfer record was measured from another launch record")
    fields = ("series", "prompt_tokens", "payload_bytes_per_handoff")
    if not isinstance(document.get("series"), str) or any(
        type(document.get(name)) is not int or document[name] < 1 for name in fields[1:]
    ):
        raise ValueError(f"{role}: the transfer record lacks its series, prompt or payload size")
    return {"kind": TRANSFER_RECORD, **{name: document[name] for name in fields}}


def derive(
    launch_config: Path,
    layouts: Mapping[str, Path],
    transfers: Mapping[str, Path],
    package: Path,
    out: Path,
) -> dict:
    """Group the roles of `launch_config` and write `cache-groups.json` into `out`.

    Every role whose engine captures its cache layout needs one in `layouts`, and every role
    whose engine measures its handoffs needs one in `transfers`. `package` is the directory
    that `package` wrote from `launch_config`.
    """
    out = evidence_directory(out, (EVIDENCE,))
    document = json.loads(launch_config.read_text())
    if document.get("schema") != "narwhal.engine-launch" or document.get("schema_version") != 1:
        raise ValueError(f"{launch_config}: expected a narwhal.engine-launch version 1 record")
    launches: dict[str, dict] = document["engines"]
    if unknown := sorted((set(layouts) | set(transfers)) - set(launches)):
        raise ValueError(f"{', '.join(unknown)}: not a role of {launch_config}")
    engines: dict[str, dict] = {}
    for role in sorted(launches, key=_node):
        launch = launches[role]
        engine = launcher(launch["runtime"].get("backend"))
        inputs, source = static_inputs(role, launch)
        packaged = packaged_launch(role, package, launch)
        geometry, path = _geometry(role, engine, inputs, layouts, transfers, digest(packaged))
        engines[role] = {
            "signature": signature(inputs),
            "inputs": inputs,
            "runtime_source": str(source),
            "runtime_source_sha256": digest(source),
            "launch": str(packaged),
            "launch_sha256": digest(packaged),
            **(
                {"geometry_source": str(path.resolve()), "geometry_source_sha256": digest(path)}
                if path is not None
                else {}
            ),
            "geometry": geometry,
        }
    grouped: dict[str, list[str]] = {}
    for role, engine_record in engines.items():
        key = _hash({"inputs": engine_record["inputs"], "geometry": engine_record["geometry"]})
        grouped.setdefault(key[:16], []).append(role)
    groups = [
        {
            "id": key,
            "signature": engines[roles[0]]["signature"],
            "representative": roles[0],
            "roles": roles,
            "geometry": engines[roles[0]]["geometry"],
        }
        for key, roles in grouped.items()
    ]
    for group in groups:
        for role in group["roles"]:
            engines[role]["group"] = group["id"]
    result = {
        "schema": SCHEMA,
        "schema_version": 1,
        "launch_config": str(launch_config),
        "launch_config_sha256": digest(launch_config),
        "package": str(package),
        "groups": groups,
        "engines": engines,
    }
    write_json(out / EVIDENCE, result)
    return result


def _geometry(
    role: str,
    engine: Any,
    inputs: Mapping[str, Any],
    layouts: Mapping[str, Path],
    transfers: Mapping[str, Path],
    launch_sha256: str,
) -> tuple[dict | None, Path | None]:
    captures = cache_capture(engine) is not None
    measures = transfer_measurement(engine) is not None
    if captures != (role in layouts):
        need = "needs" if captures else "takes no"
        raise ValueError(f"{role}: its engine {need} --cache-layout")
    if measures != (role in transfers):
        need = "needs" if measures else "takes no"
        raise ValueError(f"{role}: its engine {need} --transfer-record")
    path = layouts.get(role) or transfers.get(role)
    if path is None:
        return None, None
    try:
        evidence = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"{role}: read {path}: {error}") from None
    if role in layouts:
        return layout_geometry(role, evidence, inputs, launch_sha256), path
    return transfer_geometry(role, evidence, launch_sha256), path
