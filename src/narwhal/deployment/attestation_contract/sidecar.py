from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from ...engines.attestation import AttestationDocument
from ...engines.attestation import main as attest_main
from ..launch_engine.backend import engine_backend, plan_launcher
from .evidence import checked_plan, live_container, live_native, read_json


def residency_arguments(plan: dict, checked: dict) -> list[str]:
    events = plan.get("kv_events")
    if events is None or not checked.get("prefix_caching") or not checked.get("kv_events"):
        return []
    model = plan["args"][plan["args"].index("--served-model-name") + 1]
    return ["--kv-events", events["socket_dir"], "--model", model]


def serve(run: Path) -> int:
    plan, checked, _ = checked_plan(run)
    backend = engine_backend(plan.get("engine"))
    if plan.get("backend") == "native":
        process = ["--engine-pid", str(live_native(run, plan, checked)["pid"])]
    else:
        process = ["--engine-container", live_container(run, checked)]
    role = plan.get("role", "")
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    node = role.split("-")[1]
    destination = run / "engine-attestation.json"
    AttestationDocument.load(destination)
    startup_log = run / (
        "startup-attestation.log" if plan.get("backend") == "native" else "startup.log"
    )
    if read_json(destination) != plan_launcher(plan).engine_document(run, startup_log):
        raise ValueError("Attestation document differs from current serving evidence")
    expected = os.environ.get(f"NARWHAL_NODE_{node}_ATTESTATION_URL", "")
    url = urlsplit(expected)
    if url.scheme != "http" or url.path != "/v1/attestation" or not url.hostname or not url.port:
        raise ValueError(f"NARWHAL_NODE_{node}_ATTESTATION_URL must name the sidecar route")
    arguments = [
        "--document",
        str(destination),
        "--engine-base",
        plan["endpoint"],
        "--host",
        url.hostname,
        "--port",
        str(url.port),
        "--backend",
        backend.name,
    ]
    if not backend.identity.reports_process_start:
        arguments += process
    return attest_main(arguments + residency_arguments(plan, checked))
