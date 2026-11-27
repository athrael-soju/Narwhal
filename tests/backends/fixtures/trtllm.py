import json
from pathlib import Path

from narwhal.backends.trtllm.runtime import SETTINGS_TAG
from narwhal.deployment.attestation_contract.evidence import config_model_dimensions
from narwhal.deployment.launch_engine.runtime import digest
from tests.backends.contract import CONTAINER_STARTED_S, ContractFixtures, EngineAnswers


def check_output(plan: dict) -> str:
    record = {"tensorrt_llm_version": "1.4.0rc3", "prefix_caching": True, "kv_events": None}
    return "loading\n" + SETTINGS_TAG + json.dumps(record) + "\n"


def attestation_evidence(run: Path, attempt: Path, plan: dict, container_id: str) -> None:
    config_model_dimensions(run, plan, attempt)
    checked = json.loads((run / "checked.json").read_text())
    record = {
        "server": {
            "version": "1.4.0rc3",
            "tokens_per_block": 32,
            "max_batch_size": 64,
            "kv_cache_hash_algo": "sha256",
        },
        "process_start_time_seconds": CONTAINER_STARTED_S,
        "plan_sha256": digest(run / "launch.json"),
        "image_id": checked["image_id"],
        "container_id": container_id,
    }
    (attempt / "server-info.json").write_text(json.dumps(record, indent=2) + "\n")


def engine_answers(reference: Path) -> EngineAnswers:
    server = dict(json.loads((reference / "server-info.json").read_text())["server"])
    version = server.pop("version")
    return EngineAnswers(routes={"/version": {"version": version}, "/server_info": server})


_LABELS = 'engine_type="pytorch",model_name="m",pid="1"'
# The Prometheus route as one worker serves it, with two ranks' cache sizes.
METRICS = (
    "# TYPE trtllm_kv_cache_max_blocks gauge\n"
    f'trtllm_kv_cache_max_blocks{{dp_rank="0",{_LABELS}}} 48574.0\n'
    f'trtllm_kv_cache_max_blocks{{dp_rank="1",{_LABELS}}} 48600.0\n'
    f'trtllm_kv_cache_tokens_per_block{{dp_rank="0",{_LABELS}}} 32.0\n'
    f'trtllm_kv_cache_tokens_per_block{{dp_rank="1",{_LABELS}}} 32.0\n'
    f"trtllm_prompt_cached_tokens_total{{{_LABELS}}} 96.0\n"
    f"trtllm_prompt_tokens_total{{{_LABELS}}} 6404.0\n"
)


CONFIG = {
    "disable_overlap_scheduler": True,
    "cache_transceiver_config": {
        "backend": "UCX",
        "transceiver_runtime": "CPP",
        "kv_transfer_timeout_ms": 30000,
    },
}

FIXTURES = ContractFixtures(
    metrics=METRICS,
    kv_capacity=48574 * 32,
    cache_block_tokens=32,
    prefix_cache_hits=96,
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
    memory_args=["--free_gpu_memory_fraction", "0.4"],
    memory_fraction="0.4",
    image_packages={"tensorrt_llm": "1.4.0rc3", "torch": "2.12.0"},
    discovery_settings={},
    launch_runtime={
        "backend": "trtllm",
        "expected_packages": {"tensorrt_llm": "1.4.0rc3"},
        "model_dtype": "bfloat16",
        "kv_cache_dtype": "auto",
        "context_length": 4096,
        "environment": {"TRTLLM_FIXTURE_FLAG": "1"},
        "extra_args": [],
    },
    check_output=check_output,
    attestation_evidence=attestation_evidence,
    startup_log="TensorRT-LLM version: 1.4.0rc3\n",
    engine_answers=engine_answers,
    handoff_random_fields=(
        "disaggregated_params.disagg_request_id",
        "disaggregated_params.ctx_request_id",
    ),
)
