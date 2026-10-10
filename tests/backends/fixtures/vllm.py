import json

import msgpack

from narwhal.engines.kv_events import CacheCleared, RemovedBlocks, StoredBlocks
from tests.backends.contract import ContractFixtures


def _batch(*events: object) -> bytes:
    return msgpack.packb([1.0, list(events), None])


_STORED = {
    "type": "BlockStored",
    "block_hashes": [11, 12],
    "parent_block_hash": None,
    "token_ids": [1, 2, 3, 4, 5, 6, 7, 8],
    "block_size": 4,
    "lora_id": None,
    "medium": "GPU",
    "lora_name": None,
    "group_idx": 0,
    "kv_cache_spec_kind": "full_attention",
}
_CONNECTOR = {
    "kv_connector": "NixlConnector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"kv_lease_duration": 30},
}

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
        (_batch(_STORED), [StoredBlocks]),
        (
            _batch({"type": "BlockRemoved", "block_hashes": [11], "medium": "GPU"}),
            [RemovedBlocks],
        ),
        (_batch({"type": "AllBlocksCleared"}), [CacheCleared]),
    ],
    malformed_kv_batch=b"\xc1",
    attestation={
        "launch": {"args": ["--max-num-seqs", "64", "--kv-transfer-config", json.dumps(_CONNECTOR)]}
    },
    sequence_limit=64,
    kv_lease=30,
    memory_args=["--gpu-memory-utilization", "0.4"],
    memory_fraction="0.4",
    no_kv_event_args=["--no-enable-prefix-caching"],
    image_packages={"vllm": "0.29.0", "nixl": "1.0.0", "torch": "2.12.0"},
)
