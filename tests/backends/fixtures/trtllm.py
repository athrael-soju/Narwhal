import json

from tests.backends.contract import ContractFixtures


def _iteration(used: int) -> dict:
    return {
        "iter": used,
        "numActiveRequests": 1,
        "kvCacheStats": {
            "maxNumBlocks": 48574,
            "freeNumBlocks": 48574 - used,
            "usedNumBlocks": used,
            "tokensPerBlock": 32,
            "reusedBlocks": 0,
        },
    }


CONFIG = {
    "disable_overlap_scheduler": True,
    "cache_transceiver_config": {
        "backend": "UCX",
        "transceiver_runtime": "CPP",
        "kv_transfer_timeout_ms": 30000,
    },
}

FIXTURES = ContractFixtures(
    metrics=json.dumps([_iteration(2), _iteration(3)]),
    kv_capacity=48574 * 32,
    cache_block_tokens=32,
    # The iteration statistics carry no hit count that a reader can difference.
    prefix_cache_hits=None,  # type: ignore[arg-type]
    transfer_totals=None,
    kv_batches=[],
    malformed_kv_batch=b"[",
    attestation={
        "launch": {
            "args": [
                "trtllm-serve",
                "serve",
                "/model",
                "--max_batch_size",
                "64",
                "--max_seq_len",
                "32768",
                "--config",
                "/narwhal-hooks/engine.yaml",
            ],
            "config_files": {"engine.yaml": json.dumps(CONFIG)},
        }
    },
    sequence_limit=64,
    kv_lease=30,
    handoff_random_fields=(
        "disaggregated_params.disagg_request_id",
        "disaggregated_params.ctx_request_id",
    ),
)
