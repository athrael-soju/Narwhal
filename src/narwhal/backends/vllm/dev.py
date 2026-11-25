from __future__ import annotations

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


def gguf_plugin_digests(package: Path) -> dict[str, str]:
    """Digest the GGUF plugin's Python sources and CUDA extension.

    The runtime check runs this function's source in the engine image, so it stays
    self-contained.
    """
    import hashlib

    sources = hashlib.sha256()
    for source in sorted(package.rglob("*.py")):
        sources.update(source.relative_to(package).as_posix().encode() + b"\0")
        sources.update(source.read_bytes() + b"\0")
    extension = hashlib.sha256((package / "_C_gguf.abi3.so").read_bytes()).hexdigest()
    return {
        "gguf_plugin_python_sha256": sources.hexdigest(),
        "gguf_plugin_extension_sha256": extension,
    }


def check_plugin(runtime: dict) -> None:
    if "gguf_plugin_python_sha256" not in runtime:
        return
    package = Path(str(metadata.distribution("vllm-gguf-plugin").locate_file("vllm_gguf_plugin")))
    digests = gguf_plugin_digests(package)
    if digests["gguf_plugin_python_sha256"] != runtime["gguf_plugin_python_sha256"]:
        raise ValueError("GGUF plugin Python sources differ from the qualified revision")
    if digests["gguf_plugin_extension_sha256"] != runtime["gguf_plugin_extension_sha256"]:
        raise ValueError("GGUF plugin CUDA extension differs from the qualified wheel")
