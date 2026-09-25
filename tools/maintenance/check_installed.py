"""Check installed entry points, package data and HTTP contracts outside the checkout."""

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

import narwhal
from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.serving.app import create_app


async def check_http():
    """Build an installed app and inspect its model, state and metrics routes."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        cfg = FleetConfig(
            model="package-smoke",
            engines=[EngineSpec("e", "http://127.0.0.1:1")],
            slo=SLO(1, 1),
            profiles_path=root / "profiles.json",
        )
        app = create_app(cfg, journal_path=root / "journal.jsonl")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://installed"
            ) as client:
                models = await client.get("/v1/models")
                assert models.status_code == 200
                assert models.json()["data"][0]["id"] == "package-smoke"
                state = await client.get("/narwhal/state")
                assert state.status_code == 200
                assert state.json()["schema"] == "narwhal.state"
                metrics = await client.get("/metrics")
                assert metrics.status_code == 200
                assert "narwhal_" in metrics.text
        finally:
            await app.state.router.engines.aclose()


def check_offline_config():
    """Validate and inspect a fleet before profiles and engine credentials exist."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        fleet = root / "fleet.json"
        fleet.write_text(
            json.dumps(
                {
                    "schema": "narwhal.fleet",
                    "schema_version": 1,
                    "model": "installed-offline",
                    "engines": [{"iid": "e", "url": "${NARWHAL_INSTALLED_ENGINE_URL}"}],
                    "slo": {"ttft_s": 1, "tpot_s": 1},
                    "engine": {"engine_api_key_env": "NARWHAL_INSTALLED_CONFIG_KEY"},
                }
            )
        )
        env = os.environ.copy()
        env.pop("NARWHAL_INSTALLED_CONFIG_KEY", None)
        env["NARWHAL_INSTALLED_ENGINE_URL"] = "http://127.0.0.1:1/"
        for action in ("validate", "inspect"):
            subprocess.run(
                ["narwhal", "config", action, "--help"],
                check=True,
                timeout=30,
                capture_output=True,
                cwd=root,
                env=env,
            )
            output = subprocess.run(
                ["narwhal", "config", action, "--fleet", str(fleet), "--format", "json"],
                check=True,
                timeout=30,
                capture_output=True,
                text=True,
                cwd=root,
                env=env,
            )
            result = json.loads(output.stdout)
            assert output.stderr == "", output.stderr
            assert result["schema"] == "narwhal.command-result"
            assert result["operation"] == f"config {action}"
            data = result["data"]
            assert data["schema"] == "narwhal.effective-config"
            assert data["schema_version"] == 1
            assert data["settings"]["engines"][0]["url"] == "http://127.0.0.1:1"
            assert data["settings"]["engine"]["control_connections"] == 4
            assert data["artifact_paths"]["profiles"] == str(root / "runs/profiles.json")
        assert list(root.iterdir()) == [fleet]


def main(argv=None):
    """Check distribution identity, bundled files and every installed console command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    installed = Path(narwhal.__file__).resolve()
    assert not installed.is_relative_to((args.source_root / "src").resolve()), installed
    distribution = importlib.metadata.distribution("narwhal-inference")
    assert distribution.version == args.expected_version
    resources = importlib.resources.files("narwhal")
    for name in ("py.typed", "fleet.example.json"):
        assert resources.joinpath(name).is_file(), name
    assert (
        resources.joinpath("fleet.example.json").read_bytes()
        == (args.source_root / "config/fleet.example.json").read_bytes()
    )
    entries = sorted(
        (entry for entry in distribution.entry_points if entry.group == "console_scripts"),
        key=lambda entry: entry.name,
    )
    expected_entries = {
        "narwhal",
        "narwhal-attest",
        "narwhal-check",
        "narwhal-engine",
        "narwhal-profile",
        "narwhal-serve",
    }
    assert {entry.name for entry in entries} == expected_entries
    for entry in entries:
        subprocess.run([entry.name, "--help"], check=True, timeout=30, capture_output=True)
    reference = importlib.resources.files("narwhal.dev").joinpath("reference-v1.json")
    assert (
        reference.read_bytes()
        == (args.source_root / "src/narwhal/dev/reference-v1.json").read_bytes()
    )
    for command in ("init", "up", "verify", "status", "down"):
        subprocess.run(
            ["narwhal", "dev", command, "--help"], check=True, timeout=30, capture_output=True
        )
    with tempfile.TemporaryDirectory() as folder:
        result = subprocess.run(
            ["narwhal", "dev", "status", "--instance", str(Path(folder) / "missing")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2 and "instance.json" in result.stderr
    for module in ("narwhal.benchmarking", "narwhal.diagnostics.qualification", "narwhal.fleet"):
        assert importlib.util.find_spec(module) is None, module
    check_offline_config()
    asyncio.run(check_http())
    print(f"Installed package passed: {len(entries)} commands, package data and HTTP contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
