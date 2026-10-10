from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
        "extra_args": [
            "--tokenizer-path",
            str(model_dir),
            "--context-length",
            str(template["max_model_len"]),
            "--mem-fraction-static",
            str(memory_fraction),
        ],
    }


def check_imports() -> None:
    try:
        subprocess.run(
            [sys.executable, "-c", IMPORT_CHECK],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr[-800:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ValueError(f"native SGLang import failed: {detail}") from exc
