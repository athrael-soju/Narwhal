from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException

from ..cli_support import add_version_argument
from ..config import EngineContract
from ..config.model import LEGACY_CONTRACT_FIELDS, current_contract_names
from ..contracts import (
    ATTESTATION,
    ContractVersionError,
    canonical_digest,
    validate_document,
    versioned,
)
from .residency import ResidencyIndex
from .residency_feed import ResidencyFeed

ATTESTATION_PATH = "/v1/attestation"
RESIDENCY_PATH = "/v1/residency"
# Socket names inside the launch plan's cache-event directory; the launcher uses the same.
EVENTS_SOCKET = "events.sock"
REPLAY_SOCKET = "replay.sock"


@dataclass(frozen=True)
class EngineIdentity:
    version: str
    process_start_time_seconds: float


class EngineIdentityReader(ABC):
    @abstractmethod
    async def read(
        self, client: httpx.AsyncClient, base: str, headers: Mapping[str, str] | None = None
    ) -> EngineIdentity: ...

    @abstractmethod
    def sequence_limit(self, attestation: Any) -> int | None: ...

    @abstractmethod
    def kv_lease(self, attestation: Any) -> int | None: ...


@dataclass(frozen=True)
class AttestationDocument:
    contract: EngineContract
    sources: dict[str, str]
    launch: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> AttestationDocument:
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
        if not isinstance(raw.get("sources"), dict):
            raise ValueError("attestation sources must be an object")
        sources = _read_sources(raw["sources"], contract)
        launch = raw.get("launch")
        if launch is not None and not isinstance(launch, dict):
            raise ValueError("attestation launch must be an object")
        return cls(contract=contract, sources=sources, launch=launch)


def _read_contract(raw: Any) -> EngineContract:
    if not isinstance(raw, dict):
        raise ValueError("attestation contract must be an object")
    raw = current_contract_names(raw)
    defaults = EngineContract().fields()
    unknown = sorted(set(raw) - set(defaults))
    if unknown:
        raise ValueError(f"unknown attestation contract field(s): {', '.join(unknown)}")

    bool_fields = {
        "cross_layers_blocks",
        "hybrid_kv_cache_manager",
        "enforce_handshake_compat",
    }
    int_fields = {"connector_version", "kv_heads", "head_size", "hidden_layers"}
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
        engine_version=cast(str, values["engine_version"]),
        image_digest=cast(str, values["image_digest"]),
        transfer_version=cast(str, values["transfer_version"]),
        connector_version=cast(int, values["connector_version"]),
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
    if not contract.engine_version:
        raise ValueError("contract.engine_version is required")
    if not contract.connector:
        raise ValueError("contract.connector is required")
    if not contract.enforce_handshake_compat:
        raise ValueError("contract.enforce_handshake_compat must stay true")
    if contract.image_digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", contract.image_digest):
        raise ValueError("contract.image_digest must be an immutable sha256 digest")
    return contract


def _read_sources(raw: Any, contract: EngineContract) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in raw.items()
    ):
        raise ValueError("attestation sources must map field names to nonempty strings")
    raw = {LEGACY_CONTRACT_FIELDS.get(k, k): v for k, v in raw.items()}
    unknown_sources = sorted(set(raw) - set(contract.fields()))
    if unknown_sources:
        raise ValueError(f"sources name unknown contract field(s): {', '.join(unknown_sources)}")
    missing_sources = sorted(_populated_fields(contract) - set(raw))
    if missing_sources:
        raise ValueError(f"sources missing contract field(s): {', '.join(missing_sources)}")
    return dict(raw)


def _populated_fields(contract: EngineContract) -> set[str]:
    return {
        name
        for name, value in contract.fields().items()
        if isinstance(value, bool) or (value is not None and value != "" and value != 0)
    }


async def fetch_engine_identity(
    engine_base: str,
    *,
    timeout_s: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
    reader: EngineIdentityReader | None = None,
) -> EngineIdentity:
    if reader is None:
        from ..backends import DEFAULT_BACKEND, load

        reader = load(DEFAULT_BACKEND).identity
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport, headers=headers) as client:
        return await reader.read(client, engine_base)


def make_attestation(
    document: AttestationDocument,
    identity: EngineIdentity,
) -> dict[str, Any]:
    payload: dict[str, Any] = versioned(
        ATTESTATION,
        {
            "contract": document.contract.fields(),
            "sources": dict(sorted(document.sources.items())),
            "engine": {
                "version": identity.version,
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
    return canonical_digest({"contract": contract, "launch": launch})


def verify_attestation(
    payload: Any,
    declared: EngineContract,
    live: EngineIdentity,
) -> list[str]:
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
    version = engine.get("version", engine.get("vllm_version"))
    if version != live.version:
        failures.append(
            f"engine vLLM version is {version!r}, live /version returned {live.version!r}"
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
    contract = _read_contract(payload.get("contract"))
    return AttestationDocument(contract, _read_sources(payload.get("sources"), contract))


def _payload_digest(payload: dict[str, Any]) -> str:
    return canonical_digest({k: v for k, v in payload.items() if k != "attestation_digest"})


def build_app(
    document: AttestationDocument,
    engine_base: str,
    bound_identity: EngineIdentity,
    *,
    timeout_s: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
    residency: ResidencyIndex | None = None,
) -> FastAPI:
    app = FastAPI(title="narwhal-engine-attestation")
    # Each sidecar process serves its own epoch.
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
        return {
            "epoch": epoch,
            "sequence": result.sequence,
            "block_size": result.block_size,
            "reason": result.reason,
            "changes": result.changes,
        }

    return app


def main(argv: list[str] | None = None) -> int:
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
    from ..backends import load as load_backend
    from ..cli_errors import failure

    try:
        document = AttestationDocument.load(args.document)
    except (OSError, ValueError) as exc:
        return failure("narwhal-attest", f"load document {args.document}", exc, 2)
    try:
        identity = asyncio.run(fetch_engine_identity(args.engine_base, timeout_s=args.timeout_s))
        if identity.version != document.contract.engine_version:
            raise ValueError(
                f"engine runs vLLM {identity.version}, "
                f"document expects {document.contract.engine_version}"
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
            decoder=load_backend("vllm").kv_events,
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
