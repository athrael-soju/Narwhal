"""Start vLLM engines from an unmodified image inside a scheduler allocation."""

from __future__ import annotations

import json

from ...deployment.allocation_start import AllocationEngine, AllocationStart
from .plan import kv_transfer_config


class VllmAllocation(AllocationStart):
    def command(self, python: str, engine: AllocationEngine) -> list[str]:
        command = [
            python,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            engine.model,
            "--served-model-name",
            engine.served_name,
            "--host",
            engine.host,
            "--port",
            str(engine.port),
            "--tensor-parallel-size",
            str(engine.tensor_parallel),
            "--kv-transfer-config",
            json.dumps(kv_transfer_config(), separators=(",", ":")),
        ]
        if engine.max_sequences is not None:
            command += ["--max-num-seqs", str(engine.max_sequences)]
        return command + list(engine.extra_args)

    def environment(self, engine: AllocationEngine) -> dict[str, str]:
        return {
            "NIXL_HOST_IP": engine.host,
            "VLLM_NIXL_SIDE_CHANNEL_HOST": engine.host,
            "VLLM_NIXL_SIDE_CHANNEL_PORT": str(engine.transfer_port),
        }
