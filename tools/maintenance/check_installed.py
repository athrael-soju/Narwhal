"""Check installed entry points, package data and HTTP contracts outside the checkout."""

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def check_command_results():
    """Exercise versioned stdout through installed commands outside the checkout."""
    from narwhal.contracts import COMMAND_RESULT, validate_document

    cases = [
        (["narwhal-check", "--print-contract-versions"], 0, "success"),
        (["narwhal-check", "--unknown-option"], 2, "invalid_input"),
        (["narwhal-profile"], 2, "invalid_input"),
        (["narwhal-engine", "check", "--run", "missing-instance"], 2, "invalid_input"),
        (["narwhal", "dev", "status", "--instance", "missing-instance"], 2, "invalid_input"),
    ]
    for argv, code, status in cases:
        completed = subprocess.run(
            [*argv, "--format", "json"], capture_output=True, text=True, timeout=30
        )
        document = json.loads(completed.stdout)
        validate_document(document, COMMAND_RESULT)
        assert document["status"] == status, document
        assert document["exit_code"] == completed.returncode == code, document
        assert "Traceback" not in completed.stderr, completed.stderr


def check_diagnostics():
    """Collect local HTTP fixtures through the installed command and inspect its artifacts."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status = 503 if self.path == "/partial/ready" else 200
            body = json.dumps(
                {
                    "phase": "degraded" if status == 503 else "ready",
                    "api_key": "fixture-secret",
                    "prompt": "fixture-request-content",
                }
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for case, exit_code, status in (
                    ("healthy", 0, "success"),
                    ("partial", 3, "degraded"),
                ):
                    output = root / case
                    result = subprocess.run(
                        [
                            "narwhal",
                            "diagnostics",
                            "collect",
                            "--router",
                            f"http://127.0.0.1:{server.server_port}/{case}",
                            "--out",
                            str(output),
                            "--format",
                            "json",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=root,
                    )
                    document = json.loads(result.stdout)
                    assert result.returncode == document["exit_code"] == exit_code, result
                    assert document["status"] == status, document
                    assert document["operation"] == "diagnostics collect", document
                    assert document["artifacts"][0]["state"] == "created", document
                    assert result.stderr == "", result.stderr
                    manifest = json.loads((output / "manifest.json").read_text())
                    assert manifest["schema"] == "narwhal.diagnostic-bundle", manifest
                    assert len(manifest["sources"]) == 5, manifest
                    if case == "partial":
                        assert manifest["sources"][1]["http_status"] == 503, manifest
                    for path in output.iterdir():
                        assert "fixture-secret" not in path.read_text(), path
                        assert "fixture-request-content" not in path.read_text(), path
                existing = subprocess.run(
                    [
                        "narwhal",
                        "diagnostics",
                        "collect",
                        "--router",
                        f"http://127.0.0.1:{server.server_port}",
                        "--out",
                        str(root / "healthy"),
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=root,
                )
                assert existing.returncode == 2, existing
                assert json.loads(existing.stdout)["errors"][0]["code"] == "output_exists"
        finally:
            server.shutdown()
            worker.join(timeout=5)


def check_stage_supervisor():
    """Execute the installed helper supervisor through completion and deadline expiry."""
    from narwhal.deployment import stages

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        completed = stages.run(
            [sys.executable, "-c", "print('installed helper')"],
            stage="installed-completion",
            log=root / "completion.log",
            timeout=5,
        )
        assert completed.returncode == 0, completed
        assert completed.stdout.strip() == "installed helper", completed
        try:
            stages.run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                stage="installed-timeout",
                log=root / "timeout.log",
                timeout=0.2,
            )
        except stages.StageTimeout as error:
            record = json.loads(Path(error.context["evidence"]).read_text())
            assert record["supervisor"] is True, record
            assert record["status"] == "timeout", record
            assert record["cleanup"]["surviving_processes"] == {}, record
        else:
            raise AssertionError("installed helper must exhaust its execution budget")


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
    check_command_results()
    check_diagnostics()
    check_stage_supervisor()
    asyncio.run(check_http())
    print(f"Installed package passed: {len(entries)} commands, package data and HTTP contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
