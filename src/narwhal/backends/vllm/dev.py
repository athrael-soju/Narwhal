from __future__ import annotations

import hashlib
import subprocess
import sys
from importlib import metadata
from pathlib import Path

# The same connector API check as narwhal-engine.
IMPORT_CHECK = """from vllm.config import KVTransferConfig
from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
config = KVTransferConfig(
    kv_connector='NixlConnector', kv_role='kv_both',
    kv_connector_extra_config={'backends': ['UCX'], 'enforce_handshake_compat': True},
)
KVConnectorFactory.get_connector_class(config)
"""


def runtime(template: dict, model_dir: Path, memory_fraction: float) -> dict:
    return {
        "expected_packages": template["expected_packages"],
        "model_dtype": template["model_dtype"],
        "kv_cache_dtype": template["kv_cache_dtype"],
        "block_size": template["block_size"],
        "environment": template["environment"],
        "extra_args": [
            "--tokenizer",
            str(model_dir),
            "--hf-config-path",
            str(model_dir),
            "--load-format",
            "gguf",
            "--language-model-only",
            "--max-model-len",
            str(template["max_model_len"]),
            "--gpu-memory-utilization",
            str(memory_fraction),
            "--max-num-seqs",
            str(template["max_num_seqs"]),
            "--enforce-eager",
        ],
    }


def check_imports() -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", IMPORT_CHECK],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr[-800:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ValueError(f"native vLLM/NIXL connector import failed: {detail}") from exc
    if result.stderr and "Traceback" in result.stderr:
        raise ValueError(f"native vLLM/NIXL connector import reported: {result.stderr[-800:]}")


def check_plugin(runtime: dict) -> None:
    if "gguf_plugin_python_sha256" not in runtime:
        return
    package = Path(str(metadata.distribution("vllm-gguf-plugin").locate_file("vllm_gguf_plugin")))
    digest = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        digest.update(source.relative_to(package).as_posix().encode() + b"\0")
        digest.update(source.read_bytes() + b"\0")
    if digest.hexdigest() != runtime["gguf_plugin_python_sha256"]:
        raise ValueError("GGUF plugin Python sources differ from the qualified revision")
    extension = hashlib.sha256((package / "_C_gguf.abi3.so").read_bytes()).hexdigest()
    if extension != runtime["gguf_plugin_extension_sha256"]:
        raise ValueError("GGUF plugin CUDA extension differs from the qualified wheel")
