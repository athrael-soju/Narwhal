import json
from collections.abc import Iterable
from pathlib import Path

import msgpack

from narwhal.deployment.launch_engine.plan import option
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.engines.kv_events import CacheCleared, RemovedBlocks, StoredBlocks
from tests.backends.contract import ContractFixtures
from tests.deployment.fixtures import runtime, vllm_check_output


def stored(
    hashes: Iterable[object],
    tokens: Iterable[object],
    *,
    parent: object = None,
    group: int = 0,
    kind: str = "full_attention",
    size: int = 4,
    **fields: object,
) -> dict[str, object]:
    return {
        "type": "BlockStored",
        "block_hashes": list(hashes),
        "parent_block_hash": parent,
        "token_ids": list(tokens),
        "block_size": size,
        "lora_id": None,
        "medium": "GPU",
        "lora_name": None,
        "group_idx": group,
        "kv_cache_spec_kind": kind,
        **fields,
    }


def batch(*events: object) -> bytes:
    return msgpack.packb([1.0, list(events), None])


def nixl_config(lease: object, *, ucx: bool = False) -> dict[str, object]:
    extra: dict[str, object] = {"backends": ["UCX"]} if ucx else {}
    if lease is not None:
        extra["kv_lease_duration"] = lease
    return {
        "kv_connector": "NixlConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": extra,
    }


def launch(*args: object) -> dict[str, object]:
    return {"launch": {"args": list(args)}}


def check_output(plan: dict) -> str:
    return vllm_check_output(plan, packages=plan["expected_packages"], ucx="1.22.0")


def attestation_evidence(run: Path, plan: dict, container_id: str) -> None:
    plan_hash = digest(run / "launch.json")
    image = plan["image"]
    ranks = range(int(option(plan["args"], "--tensor-parallel-size") or 1))
    captures = {
        "model-dimensions.json": {
            "plan_sha256": plan_hash,
            "image": image,
            "model_config_sha256": plan["model_config_sha256"],
            "revision": plan["revision"],
            "contract": {"kv_heads": 8, "head_size": 64, "hidden_layers": 4},
            "model_architecture": "FixtureForCausalLM",
        },
        "cache-registration.json": {
            "plan_sha256": plan_hash,
            "image": image,
            "kv_cache_layout": "NHD",
            "cross_layers_blocks": False,
        },
        "cache-layout.json": {
            "plan_sha256": plan_hash,
            "image": image,
            "model_config_sha256": plan["model_config_sha256"],
            "launch_config_sha256": plan["launch_sha256"],
            "ranks": [
                {"rank": rank, "kv_cache_layout": "NHD", "layers": [{"kind": "FullAttentionSpec"}]}
                for rank in ranks
            ],
        },
        "nixl-connector-version.json": {
            "plan_sha256": plan_hash,
            "image_id": image,
            "container_id": container_id,
            "nixl_connector_version": 9,
        },
        "transfer-mode.json": {
            "plan_sha256": plan_hash,
            "image_id": image,
            "transfer_mode": "pull",
        },
        "handshake-policy.json": {
            "plan_sha256": plan_hash,
            "image": image,
            "connector_config": plan["connector"],
            "enforce_handshake_compat": True,
        },
        "version.json": {"version": "0.29.0"},
    }
    for name, value in captures.items():
        (run / name).write_text(json.dumps(value) + "\n")
    (run / "metrics.txt").write_text("process_start_time_seconds 100\n")


_STORED = stored([11, 12], range(1, 9))

FIXTURES = ContractFixtures(
    metrics=(
        'vllm:kv_cache_info{engine="0",kv_cache_size_tokens="4096"} 1\n'
        'vllm:cache_config_info{block_size="16",engine="0"} 1\n'
        'vllm:prefix_cache_hits_total{engine="0",model_name="m"} 12.0\n'
        'vllm:nixl_xfer_time_seconds_count{engine="0"} 3.0\n'
        'vllm:nixl_xfer_time_seconds_sum{engine="0"} 0.25\n'
    ),
    kv_capacity=4096,
    cache_block_tokens=16,
    prefix_cache_hits=12,
    transfer_totals=(3.0, 0.25),
    kv_batches=[
        (batch(_STORED), [StoredBlocks]),
        (
            batch({"type": "BlockRemoved", "block_hashes": [11], "medium": "GPU"}),
            [RemovedBlocks],
        ),
        (batch({"type": "AllBlocksCleared"}), [CacheCleared]),
    ],
    malformed_kv_batch=b"\xc1",
    attestation=launch("--max-num-seqs", "64", "--kv-transfer-config", json.dumps(nixl_config(30))),
    sequence_limit=64,
    kv_lease=30,
    memory_args=["--gpu-memory-utilization", "0.4"],
    memory_fraction="0.4",
    no_kv_event_args=["--no-enable-prefix-caching"],
    image_packages={"vllm": "0.29.0", "nixl": "1.0.0", "torch": "2.12.0"},
    launch_runtime=runtime(),
    check_output=check_output,
    attestation_evidence=attestation_evidence,
    startup_log="Using FLASH_ATTN attention backend.\n",
)
