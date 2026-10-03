"""Write the router engine contract after verifying every live sidecar."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import httpx

from ...config import EngineContract, FleetConfig
from ...engines.attestation import fetch_engine_identity, verify_attestation
from .evidence import read_json, write_private_json


def finalize_fleet(path: Path) -> EngineContract:
    if path.is_symlink():
        raise ValueError("Fleet configuration must be a regular private file")
    fleet = FleetConfig.load(path)
    contracts = []
    headers = fleet.engine_headers()
    with httpx.Client(timeout=fleet.health_timeout_s) as client:
        for engine in fleet.engines:
            identity = asyncio.run(
                fetch_engine_identity(engine.url, timeout_s=fleet.health_timeout_s, headers=headers)
            )
            response = client.get(engine.attestation_url)
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("contract")
            if not isinstance(raw, dict):
                raise ValueError(f"{engine.iid}: sidecar returned no contract")
            contract = EngineContract(**raw)
            failures = verify_attestation(payload, contract, identity)
            if failures:
                raise ValueError(f"{engine.iid}: " + "; ".join(failures))
            if contract.missing():
                raise ValueError(
                    f"{engine.iid}: incomplete contract: " + ", ".join(contract.missing())
                )
            contracts.append(contract)
    if not contracts or any(
        contract.fields() != contracts[0].fields() for contract in contracts[1:]
    ):
        raise ValueError("Engine sidecars report different contracts")
    raw_fleet = read_json(path)
    if "engine_contract" in raw_fleet:
        if raw_fleet["engine_contract"] != contracts[0].fields():
            raise ValueError("Existing router contract differs from the live sidecars")
        return contracts[0]
    raw_fleet["engine_contract"] = contracts[0].fields()
    backup = Path("runs") / f"fleet.before-attestation-{uuid.uuid4().hex}.json"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private_json(backup, read_json(path))
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, raw_fleet)
        parsed = FleetConfig.load(temporary)
        if parsed.engine_contract != contracts[0]:
            raise ValueError("Generated router contract failed fleet validation")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return contracts[0]
