from tests.backends.contract import ContractFixtures

FIXTURES = ContractFixtures(
    metrics=(
        "llamacpp:prompt_tokens_total 18\n"
        "llamacpp:prompt_tokens_cached_total 9\n"
        "llamacpp:tokens_predicted_total 2\n"
        "llamacpp:requests_processing 0\n"
    ),
    kv_capacity=None,
    cache_block_tokens=None,
    prefix_cache_hits=9,
    transfer_totals=None,
    kv_batches=[],
    malformed_kv_batch=b"\xc1",
    attestation={"launch": {}},
    sequence_limit=None,
    kv_lease=None,
    handoff_random_fields=("filename",),
)
