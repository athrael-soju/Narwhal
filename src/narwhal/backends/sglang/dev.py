from __future__ import annotations

from pathlib import Path

from ...deployment.launch_engine.backend import run_import_check

IMPORT_CHECK = "from sglang.srt.server_args import ServerArgs\n"


def runtime(template: dict, model_dir: Path, memory_fraction: float) -> dict:
    return {
        "backend": "sglang",
        "connector": template.get("connector", "mooncake"),
        "expected_packages": template["expected_packages"],
        "model_dtype": template["model_dtype"],
        "kv_cache_dtype": template["kv_cache_dtype"],
        "environment": template["environment"],
        **(
            {"decode_cuda_graph_memory_gb": template["decode_cuda_graph_memory_gb"]}
            if "decode_cuda_graph_memory_gb" in template
            else {}
        ),
        "context_length": template["max_model_len"],
        # Without it the launcher requests 256 running requests, more than SGLang serves from a
        # small memory fraction.
        **({"max_sequences": template["max_num_seqs"]} if "max_num_seqs" in template else {}),
        "tokenizer": str(model_dir),
        "extra_args": [
            "--mem-fraction-static",
            str(memory_fraction),
            *template.get("extra_args", []),
        ],
    }


def check_imports() -> None:
    run_import_check(IMPORT_CHECK, "SGLang")
