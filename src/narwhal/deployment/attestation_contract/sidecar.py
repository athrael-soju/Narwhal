from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

import httpx

from ...engines.attestation import AttestationDocument, fetch_engine_identity, verify_attestation
from ...engines.attestation import main as attest_main
from ..launch_engine.backend import engine_backend
from ..launch_engine.plan import launch_headers
from ..launch_engine.runtime import write_private
from .capture import published_attempt
from .document import attested
from .evidence import checked_plan, live_container, live_native, read_json


def residency_arguments(plan: dict, checked: dict) -> list[str]:
    events = plan.get("kv_events")
    if events is None or not checked.get("prefix_caching") or not checked.get("kv_events"):
        return []
    model = plan["args"][plan["args"].index("--served-model-name") + 1]
    return ["--kv-events", events["socket_dir"], "--model", model]


def attestation_url(plan: dict) -> tuple[SplitResult, str, int]:
    """The sidecar route of the plan's role from NARWHAL_NODE_<n>_ATTESTATION_URL, with its
    host and port."""
    role = plan.get("role", "")
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    name = f"NARWHAL_NODE_{role.split('-')[1]}_ATTESTATION_URL"
    url = urlsplit(os.environ.get(name, ""))
    if url.scheme != "http" or url.path != "/v1/attestation" or not url.hostname or not url.port:
        raise ValueError(f"{name} must name the sidecar route")
    return url, url.hostname, url.port


def require_live_process_start(run: Path, plan: dict, process_start: float) -> None:
    """Require the live engine to report the process start its captures attest; an engine
    process can start again inside a container that keeps running."""
    reader = engine_backend(plan.get("engine")).identity
    live = asyncio.run(
        fetch_engine_identity(plan["endpoint"], headers=launch_headers(run, plan), reader=reader)
    )
    if live.process_start_time_seconds != process_start:
        raise ValueError(
            f"The engine reports process start {live.process_start_time_seconds}, not the "
            f"{process_start} its attestation captured; the engine process started again, "
            "so prepare, check and start a fresh launch"
        )


def serve(run: Path) -> int:
    plan, checked, _ = checked_plan(run)
    backend = engine_backend(plan.get("engine"))
    if plan.get("backend") == "native":
        process = ["--engine-pid", str(live_native(run, plan, checked)["pid"])]
    else:
        process = ["--engine-container", live_container(run, checked)]
    _, host, port = attestation_url(plan)
    attempt = published_attempt(run)
    destination = attempt / "engine-attestation.json"
    AttestationDocument.load(destination)
    document, process_start = attested(run, attempt)
    if read_json(destination) != document:
        raise ValueError("Attestation document differs from current serving evidence")
    if process_start is not None:
        require_live_process_start(run, plan, process_start)
    arguments = [
        "--document",
        str(destination),
        "--engine-base",
        plan["endpoint"],
        "--host",
        host,
        "--port",
        str(port),
        "--backend",
        backend.name,
    ]
    if not backend.identity.reports_process_start:
        arguments += process
    return attest_main(arguments + residency_arguments(plan, checked))


def verify(run: Path) -> Path:
    """Check the running sidecar against the live engine and the document's contract, keeping
    the sidecar's responses and the live identity in a new attestation-check-* directory."""
    plan = read_json(run / "launch.json")
    document = AttestationDocument.load(published_attempt(run) / "engine-attestation.json")
    url, _, _ = attestation_url(plan)
    route = url.geturl()
    base = f"{url.scheme}://{url.netloc}"
    out = Path(tempfile.mkdtemp(prefix="attestation-check-", dir=run))
    responses = {}
    with httpx.Client(timeout=15) as client:
        for name, path in (("health", "/health"), ("attestation", "/v1/attestation")):
            response = client.get(base + path)
            write_private(out / f"{name}.json", response.text)
            write_private(out / f"{name}.status", f"{response.status_code}\n")
            responses[name] = response
    for name, response in responses.items():
        if response.status_code != 200:
            raise ValueError(f"Sidecar {name} returned HTTP {response.status_code}; inspect {out}")
    reader = engine_backend(plan.get("engine")).identity
    headers = launch_headers(run, plan)
    if reader.reports_process_start:
        live = fetch_engine_identity(plan["endpoint"], headers=headers)
    else:
        # The engine publishes no process start; its sidecar reads it on the host.
        live = fetch_engine_identity(
            plan["endpoint"], headers=headers, reader=reader, attestation_url=route
        )
    identity = asyncio.run(live)
    write_private(out / "identity.json", json.dumps(asdict(identity), indent=2) + "\n")
    failures = verify_attestation(responses["attestation"].json(), document.contract, identity)
    if failures:
        raise ValueError(
            "Sidecar attestation fails against the live engine: " + "; ".join(failures)
        )
    return out
