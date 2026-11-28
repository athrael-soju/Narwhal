"""Shipped images, the Narwhal image and the distributions hold only pinned, tracked inputs."""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from importlib import metadata
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from narwhal.observability.stack.start import services
from tools.maintenance import chart_e2e
from tools.measurement import takeover_check

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "src/narwhal/observability/stack/compose.yml"
PUBLICATION = ROOT / "tools/maintenance/check_publication.py"
# <repository>:<tag>@sha256:<digest>: the tag for readers, the digest for the pull.
TAGGED_DIGEST = re.compile(r"^[^\s@]+:(?P<tag>[^\s@:/]+)@sha256:[0-9a-f]{64}$")
DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [name for name in listed.decode().split("\0") if name]


def constraint_pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "constraints-dev.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==")
            pins[canonicalize_name(name)] = version
    return pins


class ImagePinTests(unittest.TestCase):
    def test_compose_names_the_service_images_pinned_by_tag_and_digest(self) -> None:
        compose = yaml.safe_load(COMPOSE.read_text())
        images = {name: service["image"] for name, service in compose["services"].items()}
        self.assertEqual(
            images,
            {
                "prometheus": services.PROMETHEUS_IMAGE,
                "grafana": services.GRAFANA_IMAGE,
                "renderer": services.RENDERER_IMAGE,
            },
        )
        tags = {}
        for name, image in images.items():
            pinned = TAGGED_DIGEST.match(image)
            self.assertIsNotNone(pinned, f"{name} image {image!r} has no tag and digest")
            assert pinned is not None
            tags[name] = pinned["tag"].removeprefix("v")
        self.assertEqual(tags["prometheus"], services.PROMETHEUS_VERSION)
        self.assertEqual(tags["grafana"], services.GRAFANA_VERSION)

    def test_ci_pulls_the_prometheus_image_compose_pins(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        pulls = re.findall(r"docker pull (.+)", workflow)
        self.assertTrue(pulls)
        for pull in pulls:
            self.assertIn("src/narwhal/observability/stack/compose.yml", pull)
        command = re.search(r"sed -n '([^']+)'", pulls[0])
        assert command is not None
        pulled = subprocess.run(
            ["sed", "-n", command[1], str(COMPOSE)], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(pulled, services.PROMETHEUS_IMAGE)

    def test_every_other_shipped_image_reference_is_pinned_by_digest(self) -> None:
        references = {
            "takeover_check.DEFAULT_IMAGE": takeover_check.DEFAULT_IMAGE,
            "chart_e2e.NODE_IMAGE": chart_e2e.NODE_IMAGE,
            "chart_e2e.ENGINE_IMAGE": chart_e2e.ENGINE_IMAGE,
            "chart_e2e.HAPROXY_IMAGE": chart_e2e.HAPROXY_IMAGE,
            "devcontainer": json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())[
                "image"
            ],
        }
        for dockerfile in ("Dockerfile", "deploy/vllm-gguf/Dockerfile"):
            text = (ROOT / dockerfile).read_text()
            for number, image in enumerate(re.findall(r"(?m)^FROM (\S+)", text)):
                references[f"{dockerfile} FROM {number}"] = image
        for name, image in references.items():
            self.assertRegex(image, DIGEST, name)

    def test_chart_requires_monitoring_images_by_digest(self) -> None:
        schema = json.loads((ROOT / "deploy/helm/narwhal/values.schema.json").read_text())
        monitoring = schema["properties"]["monitoring"]["properties"]
        for service, image in (
            ("prometheus", services.PROMETHEUS_IMAGE),
            ("grafana", services.GRAFANA_IMAGE),
        ):
            pattern = monitoring[service]["properties"]["image"]["pattern"]
            self.assertIsNone(re.match(pattern, image.split("@")[0]))
            self.assertIsNotNone(re.match(pattern, image))


class NarwhalImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / "Dockerfile").read_text()

    def test_both_stages_start_from_one_base_digest(self) -> None:
        bases = re.findall(r"(?m)^FROM (\S+)", self.text)
        self.assertEqual(len(bases), 2)
        self.assertEqual(bases[0], bases[1])
        self.assertRegex(bases[0], r"^python:3\.12-slim@sha256:[0-9a-f]{64}$")

    def test_every_pip_install_uses_the_constraints(self) -> None:
        installs = re.findall(r"pip install ([^\n\\&]+)", self.text)
        self.assertEqual(len(installs), 2)
        self.assertIn("-c constraints-dev.txt", installs[0])
        self.assertIn("-c /tmp/constraints-dev.txt", installs[1])
        self.assertIn("python -m build --wheel --no-isolation", self.text)
        pins = constraint_pins()
        for package in installs[0].split():
            if not package.startswith("-") and package != "constraints-dev.txt":
                self.assertIn(canonicalize_name(package), pins, package)

    def test_constraints_pin_the_build_backend_and_every_runtime_dependency(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        pins = constraint_pins()
        for requirement in project["build-system"]["requires"]:
            parsed = Requirement(requirement)
            self.assertIn(pins.get(canonicalize_name(parsed.name)), parsed.specifier, requirement)
        pending = [Requirement(item) for item in project["project"]["dependencies"]]
        seen = set()
        while pending:
            requirement = pending.pop()
            name = canonicalize_name(requirement.name)
            if name in seen or (requirement.marker and not requirement.marker.evaluate()):
                continue
            seen.add(name)
            self.assertIn(name, pins, f"constraints-dev.txt does not pin {name}")
            try:
                requires = metadata.requires(requirement.name) or []
            except metadata.PackageNotFoundError:
                continue
            for item in requires:
                child = Requirement(item)
                if not child.marker or child.marker.evaluate({"extra": ""}):
                    pending.append(child)
        self.assertIn("pydantic-core", seen)

    def test_runtime_runs_as_uid_10001(self) -> None:
        self.assertIn("useradd --system --uid 10001 --gid 10001", self.text)
        users = re.findall(r"(?m)^USER (\S+)", self.text)
        self.assertEqual(users, ["10001:10001"])
        self.assertLess(self.text.index("RUN pip install"), self.text.rindex("USER 10001"))


class BuildContextTests(unittest.TestCase):
    def test_dockerignore_keeps_out_what_gitignore_keeps_private(self) -> None:
        def rules(path: str) -> set[str]:
            return {
                line.rstrip("/")
                for line in (ROOT / path).read_text().splitlines()
                if line and not line.startswith("#")
            }

        docker = rules(".dockerignore")
        private = {rule for rule in rules(".gitignore") if rule.lstrip("!").startswith("config/")}
        private |= {".claude", "local", "runs", "profiles", ".env", ".env.*", "!.env.example"}
        self.assertEqual(private - docker, set())


def copy_tracked(destination: Path) -> None:
    """Copy the working tree's tracked files, keeping symlinks, to `destination`."""
    for name in tracked():
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)


class DistributionTests(unittest.TestCase):
    # Untracked files an operator could leave in a checkout, in each directory a build reads.
    PLANTED = (
        "deploy/helm/narwhal/values.prod.yaml",
        "deploy/helm/narwhal/templates/values.local.yaml",
        "config/helm/prod.yaml",
        "src/narwhal/dev/private.json",
        "src/narwhal/observability/stack/values.yml",
        "tools/maintenance/hosts.json",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        source = Path(cls.scratch.name) / "source"
        dist = Path(cls.scratch.name) / "dist"
        copy_tracked(source)
        for name in cls.PLANTED:
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text("private: planted\n")
        build = [sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel", "--outdir"]
        subprocess.run([*build, str(dist), str(source)], check=True, capture_output=True)
        wheel = next(dist.glob("*.whl"))
        sdist = next(dist.glob("*.tar.gz"))
        cls.wheel = set(zipfile.ZipFile(wheel).namelist())
        with tarfile.open(sdist) as archive:
            cls.sdist = {
                member.name.split("/", 1)[1] for member in archive.getmembers() if member.isfile()
            }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def test_planted_untracked_files_reach_neither_distribution(self) -> None:
        for name in self.PLANTED:
            self.assertNotIn(name, self.sdist)
            self.assertNotIn(name.removeprefix("src/"), self.wheel)

    def test_wheel_ships_the_tracked_package_files(self) -> None:
        package = {
            name.removeprefix("src/")
            for name in tracked()
            if name.startswith("src/narwhal/") and not name.endswith("README.md")
        }
        shipped = {name for name in self.wheel if not name.split("/")[0].endswith(".dist-info")}
        self.assertEqual(shipped, package)

    def test_sdist_holds_tracked_files_and_every_deploy_file(self) -> None:
        files = set(tracked())
        generated = {name for name in self.sdist if ".egg-info/" in name}
        self.assertEqual(self.sdist - files - generated, {"PKG-INFO", "setup.cfg"})
        self.assertEqual({name for name in files if name.startswith("deploy/")} - self.sdist, set())


class PublicationTests(unittest.TestCase):
    def check(self, files: dict[str, bool]) -> subprocess.CompletedProcess[str]:
        """Run the publication check on a repository that stages `files`, forced when True."""
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            subprocess.run(["git", "init", "-q", directory], check=True, env=env)
            shutil.copy(ROOT / ".gitignore", repository / ".gitignore")
            for name, forced in {".gitignore": False, **files}.items():
                path = repository / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("{}\n")
                add = ["git", "add", "--force", name] if forced else ["git", "add", name]
                subprocess.run(add, cwd=repository, check=True, env=env)
            return subprocess.run(
                [sys.executable, str(PUBLICATION)],
                cwd=repository,
                capture_output=True,
                text=True,
                env=env,
            )

    def test_force_added_ignored_files_fail_the_check(self) -> None:
        result = self.check(
            {
                "config/hosts.example.json": False,
                "config/helm/prod.yaml": True,
                "local/notes/hosts.md": True,
            }
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("'config/helm/prod.yaml': path .gitignore ignores", result.stderr)
        self.assertIn("'local/notes/hosts.md': path .gitignore ignores", result.stderr)
        self.assertNotIn("hosts.example.json", result.stderr)

    def test_gitignore_exceptions_pass_the_check(self) -> None:
        result = self.check(
            {"config/hosts.example.json": False, "config/fleet-control.example.json": False}
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
