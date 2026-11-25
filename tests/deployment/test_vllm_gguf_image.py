"""The shipped vLLM GGUF image recipe pins what the vLLM dev templates qualify."""

import importlib.util
import io
import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from narwhal.backends.vllm.dev import gguf_plugin_digests

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "deploy/vllm-gguf"


def installer():
    spec = importlib.util.spec_from_file_location(
        "install_plugin_sources", RECIPE / "install_plugin_sources.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class GgufImageRecipeTests(unittest.TestCase):
    def test_dockerfile_pins_the_dev_template_runtime(self):
        text = (RECIPE / "Dockerfile").read_text()
        arguments = dict(re.findall(r"^ARG ([A-Z0-9_]+)=(\S+)$", text, re.M))
        template = json.loads((ROOT / "src/narwhal/dev/small-cuda-v1.json").read_text())["runtime"]
        packages = dict(re.findall(r"'([a-z0-9-]+)==([^']+)'", text))
        packages["vllm-gguf-plugin"] = re.search(r"/vllm_gguf_plugin-([^-]+)-cp310", text)[1]
        # The plugin's own requirement, installed beside it; the templates do not pin it.
        self.assertEqual(packages.pop("gguf"), "0.19.0")
        self.assertEqual(packages, template["expected_packages"])
        self.assertEqual(arguments["GGUF_PLUGIN_REVISION"], template["gguf_plugin_source_revision"])
        for name in ("gguf_plugin_python_sha256", "gguf_plugin_extension_sha256"):
            self.assertEqual(arguments[name.upper()], template[name])
        self.assertRegex(text, r"(?m)^FROM \S+@sha256:[0-9a-f]{64}$")

    def test_installer_digests_follow_the_runtime_check_rule(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "vllm_gguf_plugin"
            (package / "ops").mkdir(parents=True)
            (package / "__init__.py").write_text("wheel = True\n")
            (package / "ops/kernels.py").write_text("wheel = True\n")
            (package / "_C_gguf.abi3.so").write_bytes(b"extension")
            module = installer()
            written = module.apply_sources(
                archive(
                    {
                        "vllm-gguf-plugin-abc/vllm_gguf_plugin/ops/kernels.py": b"qualified\n",
                        "vllm-gguf-plugin-abc/vllm_gguf_plugin/ops/new.py": b"added\n",
                        "vllm-gguf-plugin-abc/vllm_gguf_plugin/README.md": b"skipped\n",
                        "vllm-gguf-plugin-abc/tests/test_ops.py": b"skipped\n",
                    }
                ),
                package,
            )
            self.assertEqual(written, 2)
            self.assertEqual((package / "ops/kernels.py").read_text(), "qualified\n")
            self.assertEqual((package / "__init__.py").read_text(), "wheel = True\n")
            self.assertFalse((package / "README.md").exists())
            self.assertEqual(module.gguf_plugin_digests(package), gguf_plugin_digests(package))


if __name__ == "__main__":
    unittest.main()
