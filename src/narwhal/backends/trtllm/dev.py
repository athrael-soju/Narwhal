from __future__ import annotations

from pathlib import Path

from ...deployment.launch_engine.backend import run_import_check

IMPORT_CHECK = "import tensorrt_llm\n"


def runtime(template: dict, model_dir: Path, memory_fraction: float) -> dict:
    return {
        "backend": "trtllm",
        "connector": template.get("connector", "ucx"),
        "expected_packages": template["expected_packages"],
        "model_dtype": template["model_dtype"],
        "kv_cache_dtype": template["kv_cache_dtype"],
        "environment": template["environment"],
        "context_length": template["max_model_len"],
        **({"max_sequences": template["max_num_seqs"]} if "max_num_seqs" in template else {}),
        "tokenizer": str(model_dir),
        "extra_args": [
            "--free_gpu_memory_fraction",
            str(memory_fraction),
            *template.get("extra_args", []),
        ],
    }


def check_imports() -> None:
    run_import_check(IMPORT_CHECK, "TensorRT-LLM")
