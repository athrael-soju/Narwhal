import json
from collections.abc import Iterable
from pathlib import Path

import msgpack

from narwhal.backends.sglang.runtime import SETTINGS_TAG
from narwhal.deployment.attestation_contract.evidence import config_model_dimensions
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.engines.kv_events import CacheCleared, RemovedBlocks, StoredBlocks
from tests.backends.contract import ContractFixtures


def page_stored(
    hashes: Iterable[object], tokens: Iterable[object], *, parent: object = None, **fields: object
) -> dict[str, object]:
    """SGLang's one-token-page store event."""
    return {
        "type": "BlockStored",
        "block_hashes": list(hashes),
        "parent_block_hash": parent,
        "token_ids": list(tokens),
        "block_size": 1,
        **fields,
    }


def batch(*events: object) -> bytes:
    return msgpack.packb([1.0, list(events), 0])


def check_output(plan: dict) -> str:
    events = plan.get("kv_events")
    record = {
        "sglang_version": "0.5.21",
        "prefix_caching": True,
        "kv_events": None
        if events is None
        else {key: events[key] for key in ("endpoint", "replay_endpoint")},
    }
    return "loading\n" + SETTINGS_TAG + json.dumps(record) + "\n"


def attestation_evidence(run: Path, plan: dict, container_id: str) -> None:
    config_model_dimensions(run, plan)
    server = {
        "version": "0.5.21",
        "dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "attention_backend": "triton",
        "page_size": 1,
        "disaggregation_transfer_backend": "mooncake",
        "speculative_algorithm": None,
        "max_running_requests": 256,
    }
    checked = json.loads((run / "checked.json").read_text())
    record = {
        "server": server,
        "process_start_time_seconds": 100.0,
        "plan_sha256": digest(run / "launch.json"),
        "image_id": checked["image_id"],
        "container_id": container_id,
    }
    (run / "server-info.json").write_text(json.dumps(record, indent=2) + "\n")


_TRANSFER = "sglang:per_stage_req_latency_seconds"
_STORED = page_stored([11, 12], [1, 2], lora_id=None, medium="GPU", cache_salt="salt")

FIXTURES = ContractFixtures(
    metrics=(
        'sglang:max_total_num_tokens{engine_type="unified",model_name="m"} 4096.0\n'
        'sglang:page_size{engine_type="unified",model_name="m"} 1.0\n'
        'sglang:cached_tokens_total{cache_source="device",model_name="m"} 10.0\n'
        'sglang:cached_tokens_total{cache_source="host",model_name="m"} 2.0\n'
        f'{_TRANSFER}_count{{model_name="m",stage="decode_bootstrap"}} 9.0\n'
        f'{_TRANSFER}_count{{model_name="m",stage="decode_transferred"}} 3.0\n'
        f'{_TRANSFER}_sum{{model_name="m",stage="decode_transferred"}} 0.25\n'
    ),
    kv_capacity=4096,
    cache_block_tokens=1,
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
    attestation={
        "launch": {
            "args": ["--max-running-requests", "256"],
            "bootstrap": {"bootstrap_host": "192.0.2.11", "bootstrap_port": 5600},
            "role_switch": True,
            "decode_cuda_graph_memory_gb": 4.0,
        }
    },
    sequence_limit=256,
    kv_lease=None,
    memory_args=["--mem-fraction-static", "0.4"],
    memory_fraction="0.4",
    image_packages={
        "sglang": "0.5.0",
        "mooncake-transfer-engine": "0.3.0",
        "torch": "2.12.0",
    },
    discovery_settings={"DECODE_CUDA_GRAPH_MEMORY_GB": "4"},
    launch_runtime={
        "backend": "sglang",
        "expected_packages": {"sglang": "0.5.21", "mooncake-transfer-engine": "0.3.9"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "decode_cuda_graph_memory_gb": 4.0,
        "context_length": 4096,
        "environment": {"SGLANG_FIXTURE_FLAG": "1"},
        "extra_args": [],
    },
    check_output=check_output,
    attestation_evidence=attestation_evidence,
    startup_log="max_running_requests=256, context_len=4096\n",
    handoff_random_fields=("bootstrap_room",),
)
