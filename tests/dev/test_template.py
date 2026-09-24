"""Installed template uses the existing fleet, launch and target contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.config.loading import load as load_fleet
from narwhal.deployment.engine_launch import selected_launch
from narwhal.dev import template


class TemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
        self.model_path = self.model_dir / "Qwen3.5-0.8B-Q4_K_M.gguf"
        self.model_path.write_bytes(b"synthetic model fixture")
        (self.model_dir / "config.json").write_text("{}")
        self.spec = copy.deepcopy(template.reference())
        self.spec["model"]["sha256"] = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        self.output = self.root / "instance"
        patches = [
            patch.object(
                template,
                "_gpu_rows",
                return_value=[{"name": "NVIDIA GeForce RTX 5090", "uuid": "GPU-test"}],
            ),
            patch.object(
                template, "gpu_memory", return_value={"used_mib": 1000, "total_mib": 32607}
            ),
            patch.object(template, "_check_runtime"),
            patch.object(template, "_address", return_value="192.0.2.10"),
            patch.object(template, "_check_free_ports"),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def materialize(self) -> Path:
        return template.materialize(
            self.output,
            model_dir=self.model_dir,
            model_path=self.model_path,
            fabric_interface="eth0",
            template=self.spec,
        )

    def test_generated_contracts_and_no_overwrite(self) -> None:
        self.materialize()
        fleet = load_fleet(self.output / "fleet.json")
        document = json.loads((self.output / "engine-launch.json").read_text())
        assert len(fleet.engines) == 3
        assert all(engine.shared_device is not None for engine in fleet.engines)
        for index in range(1, 4):
            record = selected_launch(document, f"engine-{index}", {})
            assert record["runtime"]["environment"]["VLLM_SSM_CONV_STATE_LAYOUT"] == "DS"
        targets = json.loads((self.output / "targets" / "engines.json").read_text())
        assert [row["labels"]["iid"] for row in targets] == ["n1", "n2", "n3"]
        with self.assertRaises(FileExistsError):
            self.materialize()
        assert len(load_fleet(self.output / "fleet.json").engines) == 3

    def test_rejects_wrong_model_before_writing(self) -> None:
        self.model_path.write_bytes(b"other model")
        with self.assertRaisesRegex(ValueError, "GGUF file SHA-256"):
            self.materialize()
        assert not self.output.exists()

    def test_rejects_excess_budget_before_writing(self) -> None:
        self.spec["allocation"]["device_allowance"] = 0.2
        with self.assertRaisesRegex(ValueError, "budgets exceed"):
            self.materialize()
        assert not self.output.exists()

    def test_rejects_other_gpu_before_writing(self) -> None:
        self.spec["gpu"]["product"] = "unsupported GPU"
        with self.assertRaisesRegex(ValueError, "template requires"):
            self.materialize()
        assert not self.output.exists()
