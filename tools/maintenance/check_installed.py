"""Check installed entry points, package data and HTTP contracts outside the checkout."""

import argparse
import asyncio
import importlib.metadata
import importlib.resources
import importlib.util
import socket
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


def check_cli_failures():
    """Exercise operator errors through installed commands and their real streams."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        missing = root / "missing.json"
        malformed = root / "malformed.json"
        malformed.write_text("{")
        array = root / "array.json"
        array.write_text("[]")
        cases = []
        for command in ("narwhal-serve", "narwhal-check", "narwhal-profile"):
            extra = ["--port", "0"] if command == "narwhal-serve" else []
            cases.extend(
                ([command, "--fleet", str(path), *extra], 2, "", [str(path)])
                for path in (missing, malformed, array)
            )
        cases.extend(
            (
                ["narwhal-serve", "--fleet", str(missing), "--port", str(port)],
                2,
                "",
                ["--port", str(port)],
            )
            for port in (-1, 65536)
        )
        cases.extend(
            [
                (
                    [
                        "narwhal-attest",
                        "--document",
                        str(missing),
                        "--engine-base",
                        "http://127.0.0.1:1",
                    ],
                    2,
                    "",
                    [str(missing)],
                ),
                (
                    ["narwhal-engine", "check", "--run", str(root / "missing")],
                    2,
                    "",
                    [str(root / "missing" / "launch.json"), "check"],
                ),
                (
                    [
                        "narwhal-engine",
                        "start-shared",
                        "--run",
                        str(root / "missing"),
                        "--ready-seconds",
                        "-1",
                    ],
                    2,
                    "",
                    ["--ready-seconds", "-1"],
                ),
            ]
        )
        # Keeping a bound, non-listening socket reserves a refused loopback endpoint.
        with socket.socket() as endpoint:
            endpoint.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{endpoint.getsockname()[1]}"
            fleet = root / "fleet.json"
            FleetConfig(
                model="cli-failure-smoke",
                engines=[EngineSpec("e", url)],
                slo=SLO(1, 1),
                profiles_path=root / "profiles.json",
            ).save(fleet)
            cases.append(
                (
                    ["narwhal-profile", "--fleet", str(fleet)],
                    1,
                    "profiling 1 instance(s) against model cli-failure-smoke\n",
                    [str(fleet), url, "profile fleet"],
                )
            )
            for command, status, stdout, details in cases:
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                assert result.returncode == status, (command, result)
                assert result.stdout == stdout, (command, result.stdout)
                assert command[0] in result.stderr, (command, result.stderr)
                assert "Traceback" not in result.stderr, (command, result.stderr)
                for detail in details:
                    assert detail in result.stderr, (command, detail, result.stderr)


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
        help_result = subprocess.run(
            [entry.name, "--help"], check=True, timeout=30, capture_output=True, text=True
        )
        assert "--version" in help_result.stdout, entry.name
        if entry.name == "narwhal":
            inventory = help_result.stdout.split("Installed commands:\n", 1)[1]
            purposes = dict(line.strip().split(maxsplit=1) for line in inventory.splitlines())
            assert set(purposes) == expected_entries, purposes
            assert all(purposes.values()), purposes
        version_result = subprocess.run(
            [entry.name, "--version"], check=True, timeout=30, capture_output=True, text=True
        )
        assert version_result.stdout == f"narwhal-inference {distribution.version}\n", entry.name
        assert version_result.stderr == "", (entry.name, version_result.stderr)
    for name in ("small-cuda-v1.json", "reference-v1.json"):
        packaged = importlib.resources.files("narwhal.dev").joinpath(name)
        assert packaged.read_bytes() == (args.source_root / "src/narwhal/dev" / name).read_bytes()
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
    asyncio.run(check_http())
    check_cli_failures()
    print(f"Installed package passed: {len(entries)} commands, package data and HTTP contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
