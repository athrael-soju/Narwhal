"""`narwhal-engine probe`: prove that a checked plan's engine serves over HTTP.

The probe reads the engine's health, its version, which must equal the checked version, and its
model list, which must name the served model. Then, as the backend's `LiveProbe` declares, it
reads the process start from /metrics and sends one completion. Each response lands in the
output directory as it arrives, so a failed probe keeps the responses that led to its failure.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from .backend import LiveProbe, engine_backend
from .check import read_checked, require_checked
from .evidence import CheckFailed, evidence_directory, write_bytes, write_json
from .plan import launch_headers
from .runtime import digest

SCHEMA = "narwhal.engine-probe"
RECORD = "probe.json"
# Seconds each request may take, including the completion on an engine that just started.
REQUEST_TIMEOUT_S = 60.0
COMPLETION = {"prompt": "The sea is", "max_tokens": 32, "temperature": 0}


def responses(live: LiveProbe) -> tuple[str, ...]:
    """The response files a probe of an engine with `live` writes, in request order."""
    return (
        "health.txt",
        "version.json",
        "models.json",
        *(("metrics.txt",) if live.process_start is not None else ()),
        *(("completion.json",) if live.completion else ()),
    )


def probe(
    run: Path,
    plan: dict,
    out: Path,
    model: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Probe the engine of the checked plan in `run`, and write the evidence into `out`.

    Returns the probe record that `probe.json` holds once every check passed.
    """
    if plan.get("backend") == "kubernetes":
        raise ValueError("probe does not apply to a Kubernetes plan; the kubelet starts its pod")
    require_checked(run, plan)
    checked = read_checked(run)
    backend = engine_backend(plan.get("engine"))
    live = backend.engine_launcher().live_probe
    names = responses(live)
    out = evidence_directory(out, (*names, RECORD))
    headers = launch_headers(run, plan) or {}
    observed = asyncio.run(
        _requests(plan, checked, backend.identity, live, out, model, headers, transport)
    )
    record = {
        "schema": SCHEMA,
        "schema_version": 1,
        "role": plan["role"],
        "endpoint": plan["endpoint"],
        "plan_sha256": checked["plan_sha256"],
        "engine_version": checked["engine_version"],
        "model": model,
        **observed,
        "responses": {name: digest(out / name) for name in names},
    }
    write_json(out / RECORD, record)
    return record


def _ok(response: httpx.Response) -> httpx.Response:
    if response.is_success:
        return response
    raise CheckFailed(f"{response.request.url.path} returned HTTP {response.status_code}")


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise CheckFailed(f"{response.request.url.path} returned no JSON document") from None


async def _requests(
    plan: dict,
    checked: Mapping[str, Any],
    identity: Any,
    live: LiveProbe,
    out: Path,
    model: str,
    headers: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> dict:
    base = plan["endpoint"].rstrip("/")
    observed: dict = {}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S, headers=dict(headers), transport=transport
    ) as client:
        health = _ok(await client.get(f"{base}/health"))
        write_bytes(out / "health.txt", health.content)
        # The backend's reader takes the version from the route its engine publishes it on,
        # and returns only the version, because a route such as /server_info carries the
        # engine's API keys too.
        try:
            version = await identity.version(client, base)
        except httpx.HTTPStatusError as error:
            raise CheckFailed(
                f"{error.request.url.path} returned HTTP {error.response.status_code}"
            ) from None
        except ValueError as error:
            raise CheckFailed(str(error)) from None
        write_json(out / "version.json", {"version": version})
        if version != checked["engine_version"]:
            raise CheckFailed(
                f"engine reported version {version!r}; the checked image expects "
                f"{checked['engine_version']!r}"
            )
        models = _ok(await client.get(f"{base}/v1/models"))
        write_bytes(out / "models.json", models.content)
        document = _json(models)
        listed = document.get("data") if isinstance(document, dict) else None
        served = {entry.get("id") for entry in listed or [] if isinstance(entry, dict)}
        if model not in served:
            raise CheckFailed(f"/v1/models lists {sorted(map(str, served))}, not {model!r}")
        if live.process_start is not None:
            metrics = _ok(await client.get(f"{base}/metrics"))
            write_bytes(out / "metrics.txt", metrics.content)
            try:
                observed["process_start_time_seconds"] = live.process_start(metrics.text)
            except ValueError as error:
                raise CheckFailed(str(error)) from None
        if live.completion:
            completion = _ok(
                await client.post(f"{base}/v1/completions", json={"model": model, **COMPLETION})
            )
            write_bytes(out / "completion.json", completion.content)
            document = _json(completion)
            choices = document.get("choices") if isinstance(document, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else None
            text = first.get("text") if isinstance(first, dict) else None
            if not isinstance(text, str) or not text:
                raise CheckFailed("/v1/completions returned no completion text")
            observed["completion_sha256"] = hashlib.sha256(completion.content).hexdigest()
    return observed


def summary(record: Mapping[str, Any]) -> str:
    """The pass line that text output prints."""
    parts = ["health", "version", "model"]
    if "process_start_time_seconds" in record:
        parts.append("process start")
    if "completion_sha256" in record:
        parts.append("completion")
    checks = ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{record['role']}: engine {checks} passed."
