"""Serve and verify engine contract evidence for one vLLM process."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException

from ..cli_support import add_version_argument
from ..config import EngineContract
from ..contracts import ATTESTATION, ContractVersionError, validate_document, versioned
from .residency import ResidencyIndex
from .residency_feed import ResidencyFeed

ATTESTATION_PATH = "/v1/attestation"
RESIDENCY_PATH = "/v1/residency"
# Socket names inside the launch plan's cache-event directory; the launcher uses the same.
EVENTS_SOCKET = "events.sock"
REPLAY_SOCKET = "replay.sock"
_PROCESS_START = re.compile(
    r"^process_start_time_seconds(?:\{[^}]*\})?\s+([0-9.eE+-]+)(?:\s|$)", re.MULTILINE
)


@dataclass(frozen=True)
class EngineIdentity:
    """Values read directly from one running vLLM process."""

    vllm_version: str
    process_start_time_seconds: float


@dataclass(frozen=True)
class AttestationDocument:
    """Contract values and where the engine launcher obtained each value."""

    contract: EngineContract
    sources: dict[str, str]
    launch: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> AttestationDocument:
        """Read a strict attestation document."""
        raw = json.loads(Path(path).read_text())
        if not isinstance(raw, dict):
            raise ValueError("attestation document must be an object")
        try:
            validate_document(raw, ATTESTATION)
        except ContractVersionError as exc:
            raise ValueError(str(exc)) from exc
        unknown = sorted(set(raw) - {"schema", "schema_version", "contract", "sources", "launch"})
        if unknown:
            raise ValueError(f"unknown attestation field(s): {', '.join(unknown)}")
        contract = _read_contract(raw.get("contract"))
        sources = raw.get("sources")
        if not isinstance(sources, dict):
            raise ValueError("attestation sources must be an object")
        if not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in sources.items()
        ):
            raise ValueError("attestation sources must map field names to nonempty strings")
        unknown_sources = sorted(set(sources) - set(contract.fields()))
        if unknown_sources:
            raise ValueError(
                f"sources name unknown contract field(s): {', '.join(unknown_sources)}"
            )
        missing_sources = sorted(_populated_fields(contract) - set(sources))
        if missing_sources:
            raise ValueError(f"sources missing contract field(s): {', '.join(missing_sources)}")
        launch = raw.get("launch")
        if launch is not None and not isinstance(launch, dict):
            raise ValueError("attestation launch must be an object")
        return cls(contract=contract, sources=dict(sources), launch=launch)


def _read_contract(raw: Any) -> EngineContract:
    """Build an EngineContract without coercing ambiguous JSON values."""
    if not isinstance(raw, dict):
        raise ValueError("attestation contract must be an object")
    defaults = EngineContract().fields()
    unknown = sorted(set(raw) - set(defaults))
    if unknown:
        raise ValueError(f"unknown attestation contract field(s): {', '.join(unknown)}")

    bool_fields = {
        "cross_layers_blocks",
        "hybrid_kv_cache_manager",
        "enforce_handshake_compat",
    }
    int_fields = {"nixl_connector_version", "kv_heads", "head_size", "hidden_layers"}
    for name in bool_fields:
        value = raw.get(name, defaults[name])
        if name == "enforce_handshake_compat":
            if not isinstance(value, bool):
                raise ValueError(f"contract.{name} must be a boolean")
        elif value is not None and not isinstance(value, bool):
            raise ValueError(f"contract.{name} must be a boolean or null")
    for name in int_fields:
        value = raw.get(name, defaults[name])
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"contract.{name} must be a nonnegative integer")
    for name in set(defaults) - bool_fields - int_fields:
        value = raw.get(name, defaults[name])
        if not isinstance(value, str):
            raise ValueError(f"contract.{name} must be a string")

    values = defaults | raw
    contract = EngineContract(
        vllm_version=cast(str, values["vllm_version"]),
        image_digest=cast(str, values["image_digest"]),
        nixl_version=cast(str, values["nixl_version"]),
        nixl_connector_version=cast(int, values["nixl_connector_version"]),
        model_architecture=cast(str, values["model_architecture"]),
        model_dtype=cast(str, values["model_dtype"]),
        kv_heads=cast(int, values["kv_heads"]),
        head_size=cast(int, values["head_size"]),
        hidden_layers=cast(int, values["hidden_layers"]),
        attention_backend=cast(str, values["attention_backend"]),
        kv_cache_dtype=cast(str, values["kv_cache_dtype"]),
        cross_layers_blocks=cast(bool | None, values["cross_layers_blocks"]),
        hybrid_kv_cache_manager=cast(bool | None, values["hybrid_kv_cache_manager"]),
        connector=cast(str, values["connector"]),
        kv_role=cast(str, values["kv_role"]),
        transfer_mode=cast(str, values["transfer_mode"]),
        speculative_config=cast(str, values["speculative_config"]),
        enforce_handshake_compat=cast(bool, values["enforce_handshake_compat"]),
    )
    if not contract.vllm_version:
        raise ValueError("contract.vllm_version is required")
    if not contract.connector:
        raise ValueError("contract.connector is required")
    if not contract.enforce_handshake_compat:
        raise ValueError("contract.enforce_handshake_compat must stay true")
    if contract.image_digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", contract.image_digest):
        raise ValueError("contract.image_digest must be an immutable sha256 digest")
    return contract


def _populated_fields(contract: EngineContract) -> set[str]:
    """Return fields for which the sidecar must name an evidence source."""
    return {
        name
        for name, value in contract.fields().items()
        if isinstance(value, bool) or (value is not None and value != "" and value != 0)
    }


def parse_process_start(metrics: str) -> float:
    """Read the process start marker exported by the engine's metrics route."""
    match = _PROCESS_START.search(metrics)
    if match is None:
        raise ValueError("/metrics has no process_start_time_seconds")
    value = float(match.group(1))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("process_start_time_seconds must be positive and finite")
    return value


async def fetch_engine_identity(
    engine_base: str,
    *,
    timeout_s: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> EngineIdentity:
    """Read the version and process start directly from a running engine."""
    base = engine_base.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, headers=headers) as client:
        version_response = await client.get(f"{base}/version")
        version_response.raise_for_status()
        version = version_response.json().get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("/version returned no version")
        metrics_response = await client.get(f"{base}/metrics")
        metrics_response.raise_for_status()
    return EngineIdentity(version.strip(), parse_process_start(metrics_response.text))


def make_attestation(
    document: AttestationDocument,
    identity: EngineIdentity,
) -> dict[str, Any]:
    """Build the response with a checksum over every returned field."""
    payload: dict[str, Any] = versioned(
        ATTESTATION,
        {
            "contract": document.contract.fields(),
            "sources": dict(sorted(document.sources.items())),
            "engine": {
                "vllm_version": identity.vllm_version,
                "process_start_time_seconds": identity.process_start_time_seconds,
            },
        },
    )
    if document.launch is not None:
        payload["launch"] = document.launch
        payload["launch_digest"] = launch_digest(document.contract.fields(), document.launch)
    payload["attestation_digest"] = _payload_digest(payload)
    return payload


def launch_digest(contract: dict[str, Any], launch: dict[str, Any]) -> str:
    """Digest the contract and launch evidence that fix an engine's timing."""
    raw = json.dumps(
        {"contract": contract, "launch": launch}, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + sha256(raw).hexdigest()


def verify_attestation(
    payload: Any,
    declared: EngineContract,
    live: EngineIdentity,
) -> list[str]:
    """List missing or mismatched attestation evidence for the live engine."""
    if not isinstance(payload, dict):
        return ["response is not an object"]
    failures: list[str] = []
    expected_keys = {
        "schema",
        "schema_version",
        "contract",
        "sources",
        "engine",
        "attestation_digest",
    }
    unknown = sorted(set(payload) - expected_keys - {"launch", "launch_digest"})
    missing = sorted(expected_keys - set(payload))
    if unknown:
        failures.append(f"unknown response field(s): {', '.join(unknown)}")
    if missing:
        failures.append(f"missing response field(s): {', '.join(missing)}")
    if ("launch" in payload) != ("launch_digest" in payload):
        failures.append("launch and launch_digest must appear together")
    if failures:
        return failures
    if "launch" in payload and (
        not isinstance(payload["launch"], dict)
        or not isinstance(payload["contract"], dict)
        or payload["launch_digest"] != launch_digest(payload["contract"], payload["launch"])
    ):
        failures.append("launch_digest does not match the launch evidence")
    try:
        validate_document(payload, ATTESTATION)
    except ContractVersionError as exc:
        failures.append(str(exc))
    digest = payload.get("attestation_digest")
    if not isinstance(digest, str) or digest != _payload_digest(payload):
        failures.append("attestation_digest does not match the response")
    try:
        document = _document_from_response(payload)
    except ValueError as exc:
        failures.append(str(exc))
        return failures
    for name, expected in declared.fields().items():
        observed = document.contract.fields()[name]
        if observed != expected:
            failures.append(f"contract.{name} is {observed!r}, expected {expected!r}")

    engine = payload.get("engine")
    if not isinstance(engine, dict):
        failures.append("engine identity is not an object")
        return failures
    if engine.get("vllm_version") != live.vllm_version:
        failures.append(
            f"engine vLLM version is {engine.get('vllm_version')!r}, "
            f"live /version returned {live.vllm_version!r}"
        )
    process_start = engine.get("process_start_time_seconds")
    if not isinstance(process_start, int | float) or isinstance(process_start, bool):
        failures.append("engine process_start_time_seconds is not a number")
    elif not math.isclose(
        float(process_start), live.process_start_time_seconds, rel_tol=0.0, abs_tol=1e-6
    ):
        failures.append(
            f"engine process started at {process_start}, live /metrics reports "
            f"{live.process_start_time_seconds}"
        )
    return failures


def _document_from_response(payload: dict[str, Any]) -> AttestationDocument:
    raw = {
        **({"schema": payload["schema"]} if "schema" in payload else {}),
        "schema_version": payload.get("schema_version"),
        "contract": payload.get("contract"),
        "sources": payload.get("sources"),
    }
    contract = _read_contract(raw["contract"])
    sources = raw["sources"]
    if not isinstance(sources, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in sources.items()
    ):
        raise ValueError("attestation sources must map field names to nonempty strings")
    unknown_sources = sorted(set(sources) - set(contract.fields()))
    if unknown_sources:
        raise ValueError(f"sources name unknown contract field(s): {', '.join(unknown_sources)}")
    missing_sources = sorted(_populated_fields(contract) - set(sources))
    if missing_sources:
        raise ValueError(f"sources missing contract field(s): {', '.join(missing_sources)}")
    launch = payload.get("launch")
    return AttestationDocument(
        contract, dict(sources), launch=launch if isinstance(launch, dict) else None
    )


def _payload_digest(payload: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "attestation_digest"}
    raw = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(raw).hexdigest()


def build_app(
    document: AttestationDocument,
    engine_base: str,
    bound_identity: EngineIdentity,
    *,
    timeout_s: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
    residency: ResidencyIndex | None = None,
) -> FastAPI:
    """Build a sidecar that stops attesting after its engine process changes.

    With `residency`, the sidecar also serves the engine's resident prefix
    blocks. Without it, the residency routes answer 404 and callers price the
    engine cold.
    """
    app = FastAPI(title="narwhal-engine-attestation")
    # A restarted sidecar serves a new epoch, so subscribers resynchronise.
    epoch = uuid4().hex

    async def current_identity() -> EngineIdentity:
        try:
            current = await fetch_engine_identity(
                engine_base,
                timeout_s=timeout_s,
                transport=transport,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail=f"engine identity unreadable: {exc}"
            ) from exc
        if current != bound_identity:
            raise HTTPException(
                status_code=503,
                detail="engine process changed; restart the attestation sidecar",
            )
        return current

    @app.get("/health")
    async def health() -> dict[str, str]:
        await current_identity()
        return {"status": "ok"}

    @app.get(ATTESTATION_PATH)
    async def attestation() -> dict[str, Any]:
        identity = await current_identity()
        return make_attestation(document, identity)

    def require_residency() -> ResidencyIndex:
        if residency is None:
            raise HTTPException(status_code=404, detail="engine publishes no cache events")
        return residency

    @app.get(RESIDENCY_PATH)
    async def residency_snapshot() -> dict[str, Any]:
        identity = await current_identity()
        index = require_residency()
        return {
            **index.snapshot(),
            "epoch": epoch,
            "process_start_time_seconds": identity.process_start_time_seconds,
        }

    @app.get(RESIDENCY_PATH + "/events")
    async def residency_events(after: int) -> dict[str, Any]:
        await current_identity()
        index = require_residency()
        result = index.changes_after(after)
        if result is None:
            raise HTTPException(status_code=410, detail="resynchronise from the residency snapshot")
        sequence, changes = result
        return {
            "epoch": epoch,
            "sequence": sequence,
            "block_size": index.block_size,
            "reason": index.reason,
            "changes": changes,
        }

    return app


def main(argv: list[str] | None = None) -> int:
    """Start one engine attestation sidecar."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_version_argument(parser)
    parser.add_argument("--document", required=True, help="attestation document JSON")
    parser.add_argument("--engine-base", required=True, help="vLLM HTTP base URL")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8010, help="TCP port (default: %(default)s)")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=5.0,
        help="engine identity HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--kv-events",
        type=Path,
        help="directory holding the engine's cache-event sockets; enables residency routes",
    )
    parser.add_argument("--model", help="served model name; required with --kv-events")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error(f"--port must be between 0 and 65535, got {args.port}")
    if args.kv_events is not None and not args.model:
        parser.error("--kv-events requires --model")
    from ..cli_errors import failure

    try:
        document = AttestationDocument.load(args.document)
    except (OSError, ValueError) as exc:
        return failure("narwhal-attest", f"load document {args.document}", exc, 2)
    try:
        identity = asyncio.run(fetch_engine_identity(args.engine_base, timeout_s=args.timeout_s))
        if identity.vllm_version != document.contract.vllm_version:
            raise ValueError(
                f"engine runs vLLM {identity.vllm_version}, "
                f"document expects {document.contract.vllm_version}"
            )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        return failure("narwhal-attest", f"attest engine {args.engine_base}", exc, 1)
    residency = feed = None
    if args.kv_events is not None:
        residency = ResidencyIndex(args.model, document.contract.fingerprint())
        feed = ResidencyFeed(
            residency,
            f"ipc://{args.kv_events / EVENTS_SOCKET}",
            f"ipc://{args.kv_events / REPLAY_SOCKET}",
        )
        feed.start()
    try:
        uvicorn.run(
            build_app(
                document, args.engine_base, identity, timeout_s=args.timeout_s, residency=residency
            ),
            host=args.host,
            port=args.port,
        )
    finally:
        if feed is not None:
            feed.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
