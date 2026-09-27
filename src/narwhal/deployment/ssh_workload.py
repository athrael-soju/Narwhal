"""Measure fixed workstation trials against owned SSH services and reconcile remote evidence."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from narwhal.diagnostics.bundle import Redactor
from narwhal.diagnostics.management_artifacts import ArtifactStore
from narwhal.observability.management_status import observe_monitoring
from narwhal.observability.management_targets import TargetContract
from narwhal.observability.management_types import MonitoringBinding

from .management_access import directory, read_input
from .management_exports import public_value
from .management_records import OperationError, encode_record
from .ssh_prepare import input_document
from .ssh_settings import LoadRecipe
from .ssh_tunnel import forward
from .ssh_worker import _F_ADD_SEALS, _SEALS

if TYPE_CHECKING:
    from .ssh_adapter import Session

MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
EXPORT_CHUNK_BYTES = 8 * 1024 * 1024
SAMPLE_INTERVAL_S = 5
MAX_SAMPLE_SOURCE_BYTES = 8_388_608


def _write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def point_plan(recipe: LoadRecipe, index: int, source: Path, workload: Path) -> dict[str, Any]:
    """Translate a registered recipe into one fixed benchmark client command."""
    rate = recipe.rates[index]
    timeout = recipe.request_timeout_ms / 1000
    arguments = [
        sys.executable,
        str(source / "tools/measurement/load_trial.py"),
        "run",
        "--base",
        "{base}",
        "--expected-model",
        "{model}",
        "--out",
        "{point_dir}/client",
        "--workload",
        str(workload),
        "--rate",
        str(rate),
        "--requests",
        str(recipe.requests),
        "--ttft",
        str(recipe.ttft_ms / 1000),
        "--tpot",
        str(recipe.tpot_us / 1_000_000),
        "--attainment",
        str(recipe.attainment),
        "--timeout",
        str(timeout),
        "--max-inflight",
        str(recipe.max_inflight),
        "--max-lag",
        str(recipe.max_lag_ms / 1000),
    ]
    return {
        "id": f"rate-{index + 1:02d}",
        "workload": {
            "kind": "synthetic-token-length",
            "rate_rps": rate,
            "requests": recipe.requests,
            "input_tokens": recipe.input_tokens,
            "output_tokens": recipe.output_tokens,
            "seed": recipe.seed,
        },
        "client_argv": arguments,
        "client_timeout_s": recipe.requests / rate + 5 * timeout + 15,
        "drain_timeout_s": timeout,
    }


def _local(session: Session, request: dict[str, Any]) -> int:
    host = next(host for host in session.hosts if host.id == session.router_host)
    environment = session.role_environment("router")
    request.update(
        source_root=session.settings.source_root,
        source_revision=session.settings.source_commit,
        destination=session.environment[host.ssh_env],
        password=session.environment.get(host.password_env) if host.password_env else None,
        known_hosts=session.settings.known_hosts_path,
        connect_timeout_s=session.settings.connect_timeout_s,
        deadline=min(session.context.deadline, session.context.operation_deadline),
        recipe=session.recipe.load.model_dump(mode="json"),
        router_port=session.state["router"]["port"],
        model=session.fleet["model"],
    )
    descriptor = os.memfd_create("narwhal-workload", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        raw = encode_record(request)
        if len(raw) > 1024 * 1024:
            raise OperationError("invalid_input", "Workload helper request exceeds its bound")
        os.write(descriptor, raw)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _SEALS)
        if value := os.environ.get("NARWHAL_MANAGEMENT_REGISTRY"):
            environment["NARWHAL_MANAGEMENT_REGISTRY"] = value
        completed = session.context.run_command(
            [
                sys.executable,
                "-m",
                "narwhal.deployment.ssh_workload",
                "--request-fd",
                str(descriptor),
            ],
            cwd=Path(session.settings.source_root),
            env=environment,
            pass_fds=(descriptor,),
        )
        return completed.returncode
    finally:
        os.close(descriptor)


def _identity(session: Session) -> dict[str, Any]:
    models = input_document(session.context, session.plan, "model_identity")
    hashes = {row["model_tree_sha256"] for row in models.values()}
    if len(hashes) != 1:
        raise OperationError("stale_plan", "Workload checkpoint identities disagree")
    engines = session.state["engines"]
    return {
        "narwhal_revision": session.settings.source_commit,
        "model_id": session.fleet["model"],
        "benchmark_client_version": "sha256:"
        + hashlib.sha256(
            read_input(Path(session.settings.source_root) / "tools/measurement/load_trial.py")
        ).hexdigest(),
        "engine_image": json.dumps(
            {name: row["image"] for name, row in engines.items()}, sort_keys=True
        ),
        "engine_version": json.dumps(
            {name: row["generation"]["vllm_version"] for name, row in engines.items()},
            sort_keys=True,
        ),
        "checkpoint_revision": "sha256:" + hashes.pop(),
        "gpu_shape": json.dumps(session.fleet["hardware"], sort_keys=True),
        "gpu_allocation": input_document(session.context, session.plan, "network"),
        "initial_role_split": "captured by the first router observation",
    }


def _await(session: Session, effect: dict[str, Any], *, ready: str | None = None) -> dict[str, Any]:
    while True:
        session.context.assert_current()
        row = session.transport.status(effect["host_id"], effect["owner"]["launch_token"])
        if row["owner"] != effect["owner"]:
            raise OperationError("ownership_conflict", "Workload collector owner changed")
        if row["state"] in {"succeeded", "failed", "cancelled", "timed_out", "recovery_required"}:
            effect["identity"] = {"job_id": row["job_id"], "receipt": row}
            if (
                row["state"] != "recovery_required"
                and not row.get("cleanup", {}).get("error")
                and not row.get("observed_processes")
                and not row.get("containers")
                and not row.get("container_error")
            ):
                effect["effect"] = "absent"
                session.context.record_effect(effect)
            if row["state"] != "succeeded" or ready is not None:
                raise OperationError(
                    "command_failed", "Workload evidence collection did not finish"
                )
            return row
        if ready is not None:
            try:
                captured = json.loads(session.files.read(effect["host_id"], ready, max_bytes=4096))
            except OperationError as error:
                if error.code != "input_missing":
                    raise
            else:
                if captured.get("owner") != effect["owner"] or captured.get("ready") is not True:
                    raise OperationError("ownership_conflict", "Workload ready receipt differs")
                return row
        time.sleep(min(0.2, max(0, session.context.deadline - time.monotonic())))


def _exports(session: Session, paths: list[Path], kind: str) -> list[dict[str, Any]]:
    store = ArtifactStore(str(session.context.registry.registry_id), session.context.target)
    references = []
    for path in paths:
        session.context.assert_current()
        raw = session.context.redactor.body(read_input(path, maximum=MAX_EVIDENCE_BYTES))
        try:
            value = json.loads(raw)
        except ValueError:
            content = b"\n".join(
                encode_record(
                    public_value(session.context.access, session.context.target, json.loads(line))
                )
                for line in raw.splitlines()
                if line.strip()
            )
        else:
            content = encode_record(
                public_value(session.context.access, session.context.target, value)
            )
        offset = 0
        while offset < len(content) or (not content and offset == 0):
            end = min(offset + EXPORT_CHUNK_BYTES, len(content))
            if end < len(content):
                while end > offset and content[end] & 0xC0 == 0x80:
                    end -= 1
            reference = store.export(content[offset:end], f"{kind}/{path.name}/{offset}")
            references.append(reference)

            def retain(record: dict[str, Any], reference: dict[str, Any] = reference) -> None:
                record["artifacts"].append(reference)
                for stage in record.get("stages", []):
                    if stage["stage_id"] == session.context.stage_id:
                        stage["artifacts"].append(reference)

            session.context.update(retain)
            if hasattr(session, "artifacts"):
                session.artifacts.append(reference)
            if end == len(content):
                break
            offset = end
    return references


def run(session: Session) -> dict[str, Any]:
    """Execute each recipe rate on the workstation with owned remote journal sampling."""
    recipe = session.recipe.load
    root = session.context.output_dir / "workload"
    with directory(session.context.output_dir, private=True, create=True):
        pass
    with directory(root, private=True, create=True):
        pass
    source = Path(session.settings.source_root)
    seed = root / "seed"
    points = [
        point_plan(recipe, index, source, seed / "workload.json")
        for index in range(len(recipe.rates))
    ]
    required = sum(
        point["client_timeout_s"] + 2 * point["drain_timeout_s"] + 15 for point in points
    )
    required += recipe.request_timeout_ms / 1000 + 30
    if required > session.context.deadline - time.monotonic():
        raise OperationError("prerequisite_failed", "Workload recipe exceeds its stage budget")
    if _local(session, {"mode": "prepare", "out": str(seed)}):
        raise OperationError("command_failed", "Workload token preparation failed")
    identity = _identity(session)
    router = session.state["router"]
    rows = []
    for point in points:
        session.context.assert_current()
        point_id = point["id"]
        local = root / point_id
        remote_name = session.relative + "/workload/" + point_id
        remote = Path(session.settings.remote_root) / remote_name
        effect, _ = session.gate(
            session.router_host,
            "evidence-" + point_id,
            {
                "operation": "workload_evidence",
                "directory": str(remote),
                "base": router["url"],
                "fleet": router["fleet_path"],
                "profiles": router["profiles_path"],
                "journal": router["journal_path"],
                "point": point,
                "identity": identity,
                "seconds": max(1, session.context.deadline - time.monotonic() - 5),
            },
            background=True,
        )
        _await(session, effect, ready=remote_name + "/ready.json")
        code = _local(session, {"mode": "point", "out": str(local), "point": point})
        with directory(local, private=True, create=True):
            pass
        point_root = local / point_id
        with directory(point_root, private=True, create=True):
            pass
        for name in ("requests.jsonl", "warmup.json", "summary.json"):
            path = point_root / "client" / name
            if path.exists():
                session.files.put(
                    session.router_host,
                    remote_name + "/client/" + name,
                    read_input(path, maximum=MAX_EVIDENCE_BYTES),
                )
        session.files.put(
            session.router_host,
            remote_name + "/finish.json",
            encode_record({"owner": effect["owner"]}),
        )
        collection_error = None
        try:
            _await(session, effect)
        except OperationError as error:
            collection_error = error
        retained = []
        for name in (
            "evidence.json",
            "samples.json",
            "journal-rows.json",
            "summary.shareable.json",
            "collection.json",
        ):
            try:
                content = session.files.read(
                    session.router_host, remote_name + "/" + name, max_bytes=MAX_EVIDENCE_BYTES
                )
            except OperationError as error:
                if error.code != "input_missing":
                    raise
                continue
            destination = point_root / name
            _write(destination, content)
            retained.append(destination)
        evidence_path = point_root / "evidence.json"
        evidence = (
            json.loads(read_input(evidence_path, maximum=MAX_EVIDENCE_BYTES))
            if evidence_path.exists()
            else {"diagnostics": [{"kind": "collection_incomplete"}]}
        )
        summary_path = point_root / "client/summary.json"
        summary = json.loads(read_input(summary_path)) if summary_path.exists() else {}
        references = _exports(
            session,
            [
                path
                for path in [
                    *retained,
                    point_root / "result.json",
                    summary_path,
                    point_root / "client/network-before.json",
                    point_root / "client/network-after.json",
                    point_root / "client/requests.jsonl",
                    point_root / "client/warmup.json",
                    local / "management-path.json",
                ]
                if path.exists()
            ],
            "workload-evidence",
        )
        row = {
            "point": point_id,
            "rate_rps": point["workload"]["rate_rps"],
            "summary": summary,
            "evidence": evidence,
            "artifacts": references,
        }
        rows.append(row)
        session.state["workload"] = {
            "operation_id": session.context.operation_id,
            "points": rows,
            "complete": False,
            "artifacts": [reference for row in rows for reference in row["artifacts"]],
        }
        session.save()
        if collection_error is not None:
            raise collection_error
        if (
            code
            or not summary.get("candidate_pass")
            or not summary.get("client_schedule_valid")
            or evidence["diagnostics"]
        ):
            raise OperationError(
                "prerequisite_failed",
                "Workload SLO, client schedule or journal reconciliation failed",
            )
    session.state["workload"]["complete"] = True
    session.save()
    return session.state["workload"]


async def _sample(
    base: str, engines: dict[str, str], engine_headers: dict[str, str], deadline: float
) -> dict[str, Any]:
    """Capture one concurrent, bounded observation in the existing collector's format."""
    from datetime import UTC, datetime

    sample: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "monotonic": time.monotonic(),
        "errors": {},
    }
    captured_bytes = 0

    async def source(client: httpx.AsyncClient, name: str, url: str) -> None:
        nonlocal captured_bytes
        remaining = min(5, deadline - time.monotonic())
        try:
            if remaining <= 0:
                raise TimeoutError
            async with (
                asyncio.timeout(remaining),
                client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept-Encoding": "identity",
                        **(engine_headers if name.startswith("engine:") else {}),
                    },
                    timeout=remaining,
                ) as response,
            ):
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                    raise ValueError
                body = bytearray()
                async for block in response.aiter_bytes(chunk_size=65_536):
                    if (
                        len(body) + len(block) > MAX_SAMPLE_SOURCE_BYTES
                        or captured_bytes + len(block) > MAX_EVIDENCE_BYTES // 2
                    ):
                        raise ValueError
                    body.extend(block)
                    captured_bytes += len(block)
            sample[name] = json.loads(body) if name == "state" else body.decode("utf-8")
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError):
            sample["errors"][name] = "source_unavailable_or_invalid"

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        await asyncio.gather(
            source(client, "state", base + "/narwhal/state"),
            source(client, "router_metrics", base + "/metrics"),
            *(source(client, "engine:" + name, url) for name, url in engines.items()),
        )
    return sample


def collect_remote(request: dict[str, Any]) -> dict[str, Any]:
    """Sample next to the live journal, then reconcile uploaded workstation client records."""
    from narwhal.config import FleetConfig

    from .ssh_gates import write_result

    module = importlib.import_module("tools.measurement.benchmark_evidence")
    root = Path(request["directory"])
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    (root / "client").mkdir(mode=0o700)
    fleet = FleetConfig.load(request["fleet"])
    configuration = {
        "journal_path": request["journal"],
        "fleet_path": request["fleet"],
        "profiles_path": request["profiles"],
        "sample_interval_s": SAMPLE_INTERVAL_S,
        "engine_metrics_urls": {
            engine.iid: engine.url.rstrip("/") + "/metrics" for engine in fleet.engines
        },
        "identity": request["identity"],
    }
    collector = module.EvidenceCollector(configuration, request["base"], request["point"], root, {})
    size = 0
    truncated = False
    deadline = time.monotonic() + request["seconds"]
    complete = False
    engine_headers = fleet.engine_headers()

    def bounded_sample() -> None:
        nonlocal size, truncated
        if truncated:
            return
        sample = asyncio.run(
            _sample(request["base"], configuration["engine_metrics_urls"], engine_headers, deadline)
        )
        collector.samples.append(sample)
        encoded_size = len(encode_record(sample))
        if size + encoded_size > MAX_EVIDENCE_BYTES // 2:
            truncated = True
            sample.clear()
            sample.update(
                at=module.stamp(), monotonic=time.monotonic(), errors={"capture": "evidence_limit"}
            )
            collector.stop_event.set()
        else:
            size += encoded_size

    collector.sample = bounded_sample
    try:
        collector.start()
        if not collector.samples or collector.samples[-1]["errors"]:
            raise ValueError("Initial workload telemetry is unavailable")
        pools = collector.samples[0].get("state", {}).get("pools")
        if not pools:
            raise ValueError("Initial role allocation is unavailable")
        configuration["identity"]["initial_role_split"] = json.dumps(pools, sort_keys=True)
        write_result(root / "ready.json", {"owner": request["owner"], "ready": True})
        while not (root / "finish.json").exists():
            if truncated:
                raise ValueError("Workload telemetry exceeds its evidence limit")
            if time.monotonic() >= deadline:
                raise ValueError("Workload evidence deadline expired")
            time.sleep(0.1)
        finished = json.loads(read_input(root / "finish.json", maximum=4096))
        if finished.get("owner") != request["owner"]:
            raise ValueError("Workload finish receipt belongs to another owner")
        if (
            Path(request["journal"]).stat().st_size - collector.start_cursor["offset"]
            > MAX_EVIDENCE_BYTES
        ):
            raise ValueError("Workload journal exceeds its evidence limit")
        result = collector.finish()
        if truncated:
            raise ValueError("Workload telemetry exceeds its evidence limit")
        complete = not result["diagnostics"]
        return {"complete": complete, "directory": str(root)}
    finally:
        collector.stop_event.set()
        if collector.thread:
            collector.thread.join(timeout=max(0, deadline - time.monotonic()))
        if not (root / "samples.json").exists():
            write_result(root / "samples.json", collector.samples)
        write_result(
            root / "collection.json",
            {"complete": complete, "truncated": truncated, "samples": len(collector.samples)},
        )


def _monitoring(request: dict[str, Any], recipe: LoadRecipe) -> dict[str, Any]:
    binding = request["binding"]
    targets = binding["targets"]
    selected = MonitoringBinding(
        TargetContract(targets["router"], tuple(tuple(row) for row in targets["engines"])),
        binding["datasource_url"],
        binding["host_id"],
        binding["prometheus_version"],
        binding["grafana_version"],
    )
    return asyncio.run(
        observe_monitoring(
            selected,
            prometheus_url=f"http://127.0.0.1:{recipe.local_prometheus_port}",
            grafana_url=f"http://127.0.0.1:{recipe.local_grafana_port}",
            router_url=f"http://127.0.0.1:{recipe.local_router_port}",
            deadline=request["deadline"],
            freshness_s=request["freshness_s"],
            redactor=Redactor(False),
        )
    )


def local_helper(request: dict[str, Any]) -> int:
    """Run fixed verified-source clients while the same helper owns their SSH path."""
    source = Path(request["source_root"])
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    recipe = LoadRecipe.model_validate_json(encode_record(request["recipe"]))
    mode = request["mode"]
    if mode not in {"prepare", "point", "monitoring"}:
        raise ValueError("Unknown fixed workload operation")
    selected = None
    if mode == "point":
        index = int(request["point"]["id"].removeprefix("rate-")) - 1
        if not 0 <= index < len(recipe.rates):
            raise ValueError("Workload point is outside the fixed recipe")
        root = Path(request["out"])
        selected = point_plan(recipe, index, source, root.parent / "seed/workload.json")
        if selected != request["point"]:
            raise ValueError("Workload point differs from its fixed recipe")
    ports = [(recipe.local_router_port, request["router_port"])]
    if mode == "monitoring":
        ports.extend(
            [
                (recipe.local_prometheus_port, request["prometheus_port"]),
                (recipe.local_grafana_port, request["grafana_port"]),
            ]
        )
    with forward(
        destination=request["destination"],
        password=request["password"],
        known_hosts=Path(request["known_hosts"]),
        ports=tuple(ports),
        connect_timeout_s=request["connect_timeout_s"],
        deadline=request["deadline"],
    ) as tunnel:
        base = f"http://127.0.0.1:{recipe.local_router_port}"
        if mode == "monitoring":
            observed = _monitoring(request, recipe)
            for index, row in enumerate(observed["sources"]):
                if raw := row.pop("raw_body", None):
                    retained = Path(request["out"]).with_name(f"monitoring-source-{index}.txt")
                    _write(retained, raw)
                    row["retained_prefix_sha256"] = hashlib.sha256(raw).hexdigest()
                    row["retained_prefix"] = raw.decode("utf-8", errors="replace")
            _write(Path(request["out"]), encode_record({"monitoring": observed, "tunnel": tunnel}))
            return 0 if observed["readiness"] == "pass" else 1
        runner = importlib.import_module("tools.measurement.benchmark_runner")
        trial = importlib.import_module("tools.measurement.load_trial")
        if mode == "prepare":
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                if runner.probe(client, base, request["model"])["condition"] != "ready":
                    raise ValueError("Workload router or served model is not ready")
            return int(
                trial.main(
                    [
                        "prepare",
                        "--base",
                        base,
                        "--out",
                        request["out"],
                        "--input-tokens",
                        str(recipe.input_tokens),
                        "--output-tokens",
                        str(recipe.output_tokens),
                        "--seed",
                        str(recipe.seed),
                        "--timeout",
                        str(recipe.request_timeout_ms / 1000),
                    ]
                )
            )
        # The command was regenerated and checked before opening the SSH connection.
        root = Path(request["out"])
        plan = root.with_name(root.name + ".plan.json")
        _write(plan, encode_record({"schema": 1, "points": [selected]}))
        result = int(
            runner.main(
                [
                    "--base",
                    base,
                    "--model",
                    request["model"],
                    "--plan",
                    str(plan),
                    "--out",
                    str(root),
                ]
            )
        )
        _write(
            root / "management-path.json",
            encode_record({"tunnel": tunnel, "source_revision": request["source_revision"]}),
        )
        return result


def accept(session: Session) -> dict[str, Any]:
    """Require reconciled rate points, the post-load ring and fresh monitoring together."""
    workload = session.state.get("workload", {})
    if (
        workload.get("operation_id") != session.context.operation_id
        or workload.get("complete") is not True
        or [point["rate_rps"] for point in workload.get("points", [])] != session.recipe.load.rates
        or any(
            point["evidence"]["diagnostics"] or not point["summary"].get("candidate_pass")
            for point in workload["points"]
        )
    ):
        raise OperationError(
            "prerequisite_failed", "Workload acceptance lacks current reconciled points"
        )
    record = session.context.read()
    if not any(
        row["stage_id"] == "fleet-postload" and row["state"] == "succeeded"
        for row in record["stages"]
    ):
        raise OperationError("prerequisite_failed", "Post-load KV ring has not passed")
    monitoring = session.state["monitoring"]
    destination = session.context.output_dir / "monitoring-final.json"
    code = _local(
        session,
        {
            "mode": "monitoring",
            "out": str(destination),
            "binding": monitoring["binding"],
            "freshness_s": session.context.target.freshness_s,
            "prometheus_port": urlsplit(monitoring["prometheus_url"]).port,
            "grafana_port": urlsplit(monitoring["grafana_url"]).port,
        },
    )
    references = (
        _exports(session, [destination], "monitoring-final") if destination.exists() else []
    )
    if code or not destination.exists():
        raise OperationError("prerequisite_failed", "Final monitoring readiness did not pass")
    observed = json.loads(read_input(destination))
    if observed["monitoring"]["readiness"] != "pass":
        raise OperationError("prerequisite_failed", "Final monitoring evidence is incomplete")
    return {
        "qualified": True,
        "workload_operation_id": session.context.operation_id,
        "points": [row["point"] for row in workload["points"]],
        "monitoring": observed,
        "artifacts": references,
    }


def main(argv: list[str] | None = None) -> int:
    """Read an inherited sealed request; credentials never enter command arguments or files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-fd", type=int, required=True)
    arguments = parser.parse_args(argv)
    os.umask(0o077)
    try:
        seals = fcntl.fcntl(arguments.request_fd, getattr(fcntl, "F_GET_SEALS", 1034))
        if seals & _SEALS != _SEALS:
            raise ValueError("Workload request is not sealed")
        raw = os.pread(arguments.request_fd, 1024 * 1024 + 1, 0)
        if len(raw) > 1024 * 1024:
            raise ValueError("Workload request is too large")
        request = json.loads(raw)
        if not math.isfinite(request["deadline"]) or time.monotonic() >= request["deadline"]:
            raise ValueError("Workload deadline expired")
        return local_helper(request)
    except (OSError, ValueError, KeyError, TypeError, OperationError):
        print("Fixed workload helper failed; inspect its retained evidence", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
