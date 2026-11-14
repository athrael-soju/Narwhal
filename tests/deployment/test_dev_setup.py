import copy
import hashlib
import http.client
import io
import shutil
import tarfile
from pathlib import Path
from unittest.mock import patch

from narwhal.dev import cli, lifecycle, setup
from tests.deployment.test_dev_direct import MODEL_BYTES, DirectInstance, free_port_base

MACHINE = "x86_64"


def archive(path: Path, members: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o755
            bundle.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SetupTests(DirectInstance):
    def setUp(self):
        super().setUp()
        self.remote = self.folder / "remote"
        self.remote.mkdir()
        self.fetched: list[str] = []
        build = archive(
            self.remote / "build.tar.gz",
            {"engine-b1/server": b"#!/bin/sh\n", "engine-b1/a.so": b"a"},
        )
        runtime = archive(self.remote / "runtime.tar.gz", {"runtime/libb.so.1": b"b"})
        (self.remote / "model.gguf").write_bytes(MODEL_BYTES)
        self.spec["model"]["filename"] = "model.gguf"
        self.spec["engine"] = {
            "url": "https://engine.invalid/{name}",
            "binary": "server",
            "builds": {
                "none": {MACHINE: [{"name": "build.tar.gz", "sha256": build}]},
                "fast": {
                    "detect": [str(self.folder / "device")],
                    MACHINE: [
                        {"name": "build.tar.gz", "sha256": build},
                        {"name": "runtime.tar.gz", "sha256": runtime},
                    ],
                },
            },
        }
        for patched in (
            patch.object(setup, "templates", return_value={"model": self.spec}),
            patch.object(setup.platform, "machine", return_value=MACHINE),
        ):
            patched.start()
            self.addCleanup(patched.stop)

    def fetch(self, url: str, path: Path) -> None:
        self.fetched.append(url)
        shutil.copyfile(self.remote / url.rsplit("/", 1)[1], path)

    def run_setup(self, **settings) -> dict:
        settings.setdefault("engine_count", 2)
        settings.setdefault("port_base", free_port_base(2))
        return setup.run(self.root, get=self.fetch, **settings)

    def test_setup_downloads_verifies_and_initializes_an_instance(self):
        result = self.run_setup()
        self.assertEqual(
            (result["status"], result["engine_build"], result["downloaded"]),
            ("initialized", "none", ["build.tar.gz", "model.gguf"]),
        )
        self.assertEqual(
            self.fetched,
            [
                "https://engine.invalid/build.tar.gz",
                "https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/resolve/"
                f"{self.spec['model']['revision']}/model.gguf",
            ],
        )
        instance = lifecycle.instance(self.root)
        assets = self.root / "assets"
        self.assertEqual(instance["engine_binary"], str(assets / "engine-none/engine-b1/server"))
        self.assertEqual(instance["model_path"], str(assets / "model.gguf"))
        self.assertIs(instance["accelerated"], False)
        self.assertEqual(setup.run(self.root, get=self.fetch)["status"], "reused")
        self.assertEqual(len(self.fetched), 2)
        self.assertEqual(lifecycle.instance(self.root)["engine_build"], "none")
        with self.assertRaisesRegex(ValueError, "conflicts with --model, --accelerator; create"):
            setup.run(self.root, get=self.fetch, model="another", accelerator="fast")
        self.assertEqual(
            setup.run(self.root, get=self.fetch, model="qwen3.5-0.8b", accelerator="none")[
                "status"
            ],
            "reused",
        )

    def test_a_detected_device_selects_its_build_and_links_the_runtime_libraries(self):
        self.assertEqual(setup._build(self.spec["engine"], setup.AUTO), "none")
        (self.folder / "device").touch()
        result = self.run_setup()
        self.assertEqual(result["engine_build"], "fast")
        binary = Path(lifecycle.instance(self.root)["engine_binary"])
        self.assertEqual((binary.parent / "libb.so.1").read_bytes(), b"b")
        self.assertIs(lifecycle.instance(self.root)["accelerated"], True)

    def test_a_wrong_checksum_stops_setup_and_a_verified_file_is_kept(self):
        (self.remote / "model.gguf").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "model.gguf SHA-256 .* differs from the pinned"):
            self.run_setup()
        assets = self.root / "assets"
        self.assertFalse((assets / "model.gguf").exists())
        self.assertFalse((self.root / "instance.json").exists())
        (self.remote / "model.gguf").write_bytes(MODEL_BYTES)
        self.fetched.clear()
        self.assertEqual(self.run_setup()["downloaded"], ["model.gguf"])
        self.assertEqual(len(self.fetched), 1)

    def test_setup_refuses_a_build_the_template_does_not_pin(self):
        with self.assertRaisesRegex(ValueError, "pins no other engine build; it pins fast, none"):
            self.run_setup(accelerator="other")
        with self.assertRaisesRegex(ValueError, "no installed template serves other; choose from"):
            self.run_setup(model="other")
        with (
            patch.object(setup.platform, "machine", return_value="riscv64"),
            self.assertRaisesRegex(ValueError, "pins no none engine build for riscv64"),
        ):
            self.run_setup()

    def test_an_archive_member_outside_the_target_is_refused(self):
        spec = copy.deepcopy(self.spec)
        escaped = archive(self.remote / "bad.tar.gz", {"../outside": b"x"})
        spec["engine"]["builds"]["none"][MACHINE] = [{"name": "bad.tar.gz", "sha256": escaped}]
        with (
            patch.object(setup, "templates", return_value={"model": spec}),
            self.assertRaisesRegex(ValueError, "bad.tar.gz cannot be unpacked"),
        ):
            self.run_setup()
        self.assertEqual(
            sorted(path.name for path in (self.root / "assets").iterdir()), ["bad.tar.gz"]
        )

    def test_a_tree_from_an_interrupted_run_is_replaced(self):
        stale = self.root / "assets/engine-none/old-release"
        stale.mkdir(parents=True)
        (stale / "server").write_bytes(b"stale")
        self.run_setup()
        binary = Path(lifecycle.instance(self.root)["engine_binary"])
        self.assertEqual(binary.parent.name, "engine-b1")
        self.assertFalse(stale.exists())

    def test_setup_leaves_a_directory_that_holds_other_files(self):
        self.root.mkdir()
        (self.root / "fleet.json").write_text("mine")
        with self.assertRaisesRegex(ValueError, "holds other files"):
            self.run_setup()
        self.assertEqual((self.root / "fleet.json").read_text(), "mine")
        self.assertEqual(self.fetched, [])

    def test_downloads_use_https_and_report_a_broken_transfer(self):
        with self.assertRaisesRegex(ValueError, "https only"):
            setup.fetch("http://engine.invalid/build.tar.gz", self.folder / "build.tar.gz")

        def broken(url: str, path: Path) -> None:
            raise http.client.IncompleteRead(b"")

        with self.assertRaisesRegex(OSError, "download of build.tar.gz failed"):
            setup.run(self.root, get=broken, engine_count=2, port_base=free_port_base(2))

    def test_the_installed_templates_pin_every_build_and_the_cli_passes_its_options(self):
        patch.stopall()
        installed = setup.templates()
        self.assertEqual(list(installed), ["qwen3.5-0.8b", "ernie-4.5-0.3b"])
        for spec in installed.values():
            self.assertEqual(sorted(spec["engine"]["builds"]), ["cuda", "cuda12", "none"])
            for name, build in spec["engine"]["builds"].items():
                for entry in build[MACHINE]:
                    self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$", name)
                    self.assertTrue(entry["name"].endswith(".tar.gz"))
        with patch.object(setup, "run", return_value={"status": "initialized"}) as run:
            arguments = ["--accelerator", "none", "--model-name", "ERNIE-4.5-0.3B"]
            self.assertEqual(
                cli._main(["dev", "setup", "--instance", str(self.root), *arguments]), 0
            )
        self.assertEqual(
            (run.call_args.kwargs["accelerator"], run.call_args.kwargs["model"]),
            ("none", "ernie-4.5-0.3b"),
        )
