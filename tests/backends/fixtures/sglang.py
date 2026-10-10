import msgpack

from narwhal.engines.kv_events import CacheCleared, RemovedBlocks, StoredBlocks
from tests.backends.contract import ContractFixtures


def _batch(*events: object) -> bytes:
    return msgpack.packb([1.0, list(events), 0])


_TRANSFER = "sglang:per_stage_req_latency_seconds"
_STORED = {
    "type": "BlockStored",
    "block_hashes": [11, 12],
    "parent_block_hash": None,
    "token_ids": [1, 2],
    "block_size": 1,
    "lora_id": None,
    "medium": "GPU",
    "cache_salt": "salt",
}

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
        (_batch(_STORED), [StoredBlocks]),
        (
            _batch({"type": "BlockRemoved", "block_hashes": [11], "medium": "GPU"}),
            [RemovedBlocks],
        ),
        (_batch({"type": "AllBlocksCleared"}), [CacheCleared]),
    ],
    malformed_kv_batch=b"\xc1",
    attestation={"launch": {"args": ["--max-running-requests", "256"]}},
    sequence_limit=256,
    kv_lease=None,
    memory_args=["--mem-fraction-static", "0.4"],
    memory_fraction="0.4",
    kv_event_args=[],
    no_kv_event_args=["--disable-radix-cache"],
)
