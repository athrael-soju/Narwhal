"""Read the vLLM runtime probe's output.

A Kubernetes engine pod runs this module with the engine image's Python, so it imports only the
standard library.
"""

from __future__ import annotations

import json

VERSION_LABEL = "API"
# First UCX release that unmaps CUDA IPC rkeys when the transfer library removes a remote agent.
UCX_PEER_RELEASE = (1, 22)


def releases_peers(ucx_version: str | None, evicts: bool) -> bool:
    """Return whether peers unmap a stopped engine's KV memory once the engine evicts them."""
    try:
        version = tuple(int(part) for part in (ucx_version or "").split(".")[:2])
    except ValueError:
        return False
    return version >= UCX_PEER_RELEASE and evicts


def tagged(output: str, prefix: str) -> list:
    return [
        json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)
    ]


def probe_result(plan: dict, environment: dict[str, str], output: str) -> dict:
    """Return the probe's version, cache settings, fields and notes; raise for a failed probe."""
    records = tagged(output, "NARWHAL_IMAGE_RUNTIME=")
    if len(records) != 1:
        raise ValueError("runtime check requires one runtime version record")
    if output.splitlines().count("NARWHAL_TOKENIZER_READY=1") != 1:
        raise ValueError("runtime check requires one tokenizer confirmation")
    if plan.get("runtime_pins") and tagged(output, "NARWHAL_GGUF_PLUGIN=") != [
        plan["runtime_pins"]
    ]:
        raise ValueError("GGUF plugin files differ from the pinned digests")
    ucx = records[0].get("ucx_version")
    if ucx is not None and not isinstance(ucx, str):
        raise ValueError("runtime check returned an invalid UCX version")
    settings = tagged(output, "NARWHAL_CACHE_SETTINGS=")
    if len(settings) != 1:
        raise ValueError("runtime check requires one resolved cache setting")
    ipc_cache = environment.get("UCX_CUDA_IPC_CACHE")
    # The plan gives the connector an engine TTL when peers evict a stopped engine, which it does
    # for CUDA IPC peers with UCX_CUDA_IPC_CACHE off.
    evicts = "engine_ttl" in plan["connector"]["kv_connector_extra_config"]
    ipc_peers = plan.get("cuda_ipc_peers", evicts)
    peer_release = not ipc_peers or releases_peers(ucx, evicts)
    notes = []
    if ipc_peers and peer_release:
        notes.append(
            "UCX_CUDA_IPC_CACHE is off: peers release a stopped engine's GPU memory, "
            "transfers from a restarted or idle-evicted producer map its KV memory per transfer, "
            "and transfers and decoding slow as the engine serves until it restarts."
        )
    if not peer_release:
        notes.append(
            f"UCX {ucx or 'version unknown'} with UCX_CUDA_IPC_CACHE={ipc_cache or 'y'} keeps "
            "a stopped CUDA IPC peer's GPU memory mapped; recover a crashed engine with a "
            "whole-wave restart."
        )
    return {
        "version": records[0].get("vllm_api_version"),
        "prefix_caching": settings[0].get("prefix_caching"),
        "kv_events": settings[0].get("kv_events"),
        "fields": {"ucx_version": ucx, "peer_release": peer_release},
        "notes": notes,
    }
