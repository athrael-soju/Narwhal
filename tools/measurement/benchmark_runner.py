"""Run ordered external benchmark clients against a qualified Narwhal router."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.measurement.benchmark_evidence import EvidenceCollector
from tools.measurement.load_trial import poll_drain


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: object) -> None:
    with open(
        path, "x", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)
    ) as file:
        json.dump(value, file, indent=2, allow_nan=False)
        file.write("\n")


def positive(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("schema") != 1:
        raise ValueError("benchmark plan requires schema 1")
    points = plan.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("benchmark plan requires ordered points")
    seen = set()
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("each point must be an object")
        name = point.get("id")
        if not isinstance(name, str) or not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ValueError("point id must contain only letters, digits, hyphens, or underscores")
        if name in seen:
            raise ValueError(f"duplicate point id: {name}")
        seen.add(name)
        if not isinstance(point.get("workload"), dict) or not point["workload"]:
            raise ValueError(f"{name}: workload must be a nonempty object")
        command = point.get("client_argv")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
        ):
            raise ValueError(f"{name}: client_argv must be a nonempty string array")
        for field in ("client_timeout_s", "drain_timeout_s"):
            positive(point.get(field), f"{name}.{field}")
    evidence = plan.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be an object")
        for field in ("journal_path", "fleet_path", "profiles_path"):
            if not isinstance(evidence.get(field), str) or not evidence[field]:
                raise ValueError(f"evidence.{field} must name a local file")
        positive(evidence.get("sample_interval_s"), "evidence.sample_interval_s")
        urls = evidence.get("engine_metrics_urls")
        if (
            not isinstance(urls, dict)
            or not urls
            or any(
                not isinstance(key, str)
                or not isinstance(url, str)
                or not url.startswith(("http://", "https://"))
                for key, url in urls.items()
            )
        ):
            raise ValueError(
                "evidence.engine_metrics_urls requires engine names and HTTP metrics URLs"
            )
        identity = evidence.get("identity")
        required = (
            "narwhal_revision",
            "model_id",
            "benchmark_client_version",
            "engine_image",
            "engine_version",
            "checkpoint_revision",
            "gpu_shape",
            "gpu_allocation",
            "initial_role_split",
        )
        if not isinstance(identity, dict) or any(not identity.get(key) for key in required):
            raise ValueError(
                "evidence.identity requires revision, engine, checkpoint, GPU, and role fields"
            )
        if any(not isinstance(identity[key], str) for key in required if key != "gpu_allocation"):
            raise ValueError("evidence.identity labels must be strings")
    return plan


def command_for(point: dict, base: str, model: str, directory: Path) -> list[str]:
    substitutions = {
        "base": base,
        "model": model,
        "point_id": point["id"],
        "point_dir": str(directory),
    }
    command = []
    for arg in point["client_argv"]:
        for key, value in substitutions.items():
            arg = arg.replace("{" + key + "}", value)
        command.append(arg)
    if not any("{base}" in arg for arg in point["client_argv"]):
        raise ValueError(f"{point['id']}: client_argv must pass {{base}} to the client")
    if not any("{model}" in arg for arg in point["client_argv"]):
        raise ValueError(f"{point['id']}: client_argv must pass {{model}} to the client")
    return command


def probe(client: httpx.Client, base: str, model: str) -> dict:
    result = {"at": now()}
    try:
        ready = client.get(base + "/ready")
        result["ready"] = {"status": ready.status_code, "body": ready.json()}
        if ready.status_code != 200 or result["ready"]["body"].get("status") != "ready":
            result["condition"] = "readiness_refused"
            return result
        models = client.get(base + "/v1/models")
        result["models"] = {"status": models.status_code, "body": models.json()}
        if models.status_code != 200:
            result["condition"] = "models_unavailable"
        elif [item.get("id") for item in result["models"]["body"]["data"]] != [model]:
            result["condition"] = "model_mismatch"
        else:
            result["condition"] = "ready"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        result["condition"] = "probe_error"
        result["error"] = str(error)
    return result


def wait_for_drain(client: httpx.Client, base: str, timeout: float, poll_s: float = 0.25) -> dict:
    start = time.monotonic()
    started_at = now()

    async def observe() -> dict:
        async with httpx.AsyncClient(
            headers=client.headers, timeout=10, trust_env=False
        ) as observer:
            return await poll_drain(observer, base, timeout, poll_s)

    result = asyncio.run(observe())
    result["started_at"] = started_at
    result["finished_at"] = now()
    result["elapsed_s"] = time.monotonic() - start
    return result


def run_client(command: list[str], directory: Path, timeout: float) -> dict:
    result = {"started_at": now(), "argv": command, "exit_status": None}
    with (
        open(
            directory / "client.stdout", "x", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as stdout,
        open(
            directory / "client.stderr", "x", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as stderr,
    ):
        try:
            process = subprocess.Popen(
                command, stdout=stdout, stderr=stderr, start_new_session=True
            )
            try:
                result["exit_status"] = process.wait(timeout=timeout)
                result["condition"] = "exited"
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                result["condition"] = "timeout"
        except OSError as error:
            result["condition"] = "start_error"
            result["error"] = str(error)
    result["finished_at"] = now()
    return result


def run(plan: dict, base: str, model: str, out: Path, client: httpx.Client) -> int:
    for point in plan["points"]:
        directory = out / point["id"]
        directory.mkdir(mode=0o700)
        record = {"id": point["id"], "workload": point["workload"], "started_at": now()}
        record["readiness"] = probe(client, base, model)
        if record["readiness"]["condition"] != "ready":
            record["condition"] = record["readiness"]["condition"]
        else:
            record["initial_drain"] = wait_for_drain(client, base, point["drain_timeout_s"])
            if record["initial_drain"]["condition"] != "idle":
                record["condition"] = "initial_drain_" + record["initial_drain"]["condition"]
            else:
                command = command_for(point, base, model, directory)
                collector = None
                try:
                    if plan.get("evidence"):
                        collector = EvidenceCollector(
                            plan["evidence"], base, point, directory, dict(client.headers)
                        )
                        collector.start()
                    record["client"] = run_client(command, directory, point["client_timeout_s"])
                    record["drain"] = wait_for_drain(client, base, point["drain_timeout_s"])
                    if collector:
                        evidence = collector.finish()
                        record["evidence"] = {
                            "file": "evidence.json",
                            "diagnostics": len(evidence["diagnostics"]),
                        }
                    if record["drain"]["condition"] != "idle":
                        record["condition"] = "drain_" + record["drain"]["condition"]
                    elif (
                        record["client"]["condition"] != "exited"
                        or record["client"]["exit_status"] != 0
                    ):
                        record["condition"] = "client_failure"
                    else:
                        record["condition"] = "completed"
                except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
                    record["condition"] = "evidence_error"
                    record["evidence_error"] = str(error)
        record["finished_at"] = now()
        write_json(directory / "result.json", record)
        print(f"{point['id']}: {record['condition']}")
        if record["condition"] != "completed":
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="private router URL")
    parser.add_argument("--model", required=True, help="expected served model ID")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="fresh private run directory")
    parser.add_argument(
        "--api-key-env", help="environment variable containing ingress bearer token"
    )
    args = parser.parse_args(argv)
    url = urlsplit(args.base)
    if (
        url.scheme not in ("http", "https")
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("--base must be an HTTP URL without embedded credentials, query, or fragment")
    if not args.model:
        parser.error("--model must be nonempty")
    try:
        plan = load_plan(args.plan)
        base = args.base.rstrip("/")
        out = args.out.resolve()
        for point in plan["points"]:
            command_for(point, base, args.model, out / point["id"])
        headers = {}
        if args.api_key_env:
            if not os.environ.get(args.api_key_env):
                parser.error("named API key environment variable is empty")
            headers["authorization"] = "Bearer " + os.environ[args.api_key_env]
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
        write_json(
            out / "manifest.json",
            {
                "schema": 1,
                "started_at": now(),
                "router": base,
                "expected_model": args.model,
                "api_key_env": args.api_key_env,
                "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "command": sys.argv if argv is None else [sys.argv[0], *argv],
                "plan": plan,
            },
        )
        with httpx.Client(headers=headers, timeout=10, trust_env=False) as client:
            return run(plan, base, args.model, out, client)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Benchmark blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
