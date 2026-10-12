from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path

from ...deployment.launch_engine.backend import run_import_check

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
        "context_length": template["max_model_len"],
        "max_sequences": template["max_num_seqs"],
        "tokenizer": str(model_dir),
        "environment": template["environment"],
        "extra_args": [
            "--hf-config-path",
            str(model_dir),
            "--load-format",
            "gguf",
            "--language-model-only",
            "--gpu-memory-utilization",
            str(memory_fraction),
            "--enforce-eager",
        ],
    }


def check_imports() -> None:
    run_import_check(IMPORT_CHECK, "vLLM/NIXL connector", traceback_fails=True)


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
