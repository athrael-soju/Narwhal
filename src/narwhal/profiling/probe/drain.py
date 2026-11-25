"""The router's drain state of the engines a role-switching sweep uses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...config import EngineSpec
from ...contracts import LIFECYCLE, MEMBERSHIP, validate_document
from .pairing import origin


async def _document(client: httpx.AsyncClient, url: str, name: str) -> dict[str, Any]:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"cannot read {url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"cannot read {url}: HTTP {response.status_code}")
    try:
        document = response.json()
        validate_document(document, name)
    except ValueError as exc:
        raise RuntimeError(f"cannot read {url}: {exc}") from exc
    return document


def _held(record: Mapping[str, Any]) -> bool:
    """Whether the router keeps the engine out of placement and sends it nothing."""
    resident = record.get("resident")
    idle = isinstance(resident, Mapping) and not any(resident.values())
    if record.get("state") == "drained":
        # A decode leg that still reads KV from the engine keeps it from being ready to stop.
        return record.get("ready_to_stop") is True
    # A readmission that failed its checks holds the engine until an operator acts.
    return record.get("state") == "blocked" and record.get("accepts_new") is False and idle


async def refuse_undrained(
    router_url: str, engines: list[EngineSpec], targets: set[str], timeout_s: float
) -> None:
    """Raise unless every engine in `engines` is drained or outside the router's fleet.

    An engine counts as a member when a member shares its ID or its URL.
    """
    base = router_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        membership = await _document(client, f"{base}/narwhal/engines", MEMBERSHIP)
        lifecycle = await _document(client, f"{base}/narwhal/lifecycle", LIFECYCLE)
    router = lifecycle.get("router")
    if not isinstance(router, Mapping) or router.get("controls_fleet") is not True:
        raise RuntimeError(
            f"the router at {base} does not control the fleet; pass the active router's URL"
        )
    members = [
        member
        for member in membership.get("engines") or []
        if isinstance(member, Mapping) and isinstance(member.get("iid"), str)
    ]
    records = lifecycle.get("engines")
    records = records if isinstance(records, Mapping) else {}
    serving = []
    for spec in engines:
        for member in members:
            iid = member["iid"]
            url = member.get("url")
            if iid != spec.iid and not (isinstance(url, str) and origin(url) == origin(spec.url)):
                continue
            record = records.get(iid)
            record = record if isinstance(record, Mapping) else {}
            if _held(record):
                continue
            name = spec.iid if spec.iid in targets else f"peer {spec.iid}"
            if iid != spec.iid:
                name += f" (router member {iid})"
            serving.append(f"{name} is {record.get('state', 'unknown')}")
    if serving:
        raise RuntimeError(
            f"refusing to switch engines that the router at {base} has not drained: "
            + ", ".join(serving)
            + ". Drain each one with POST /narwhal/lifecycle/drain and wait until "
            "GET /narwhal/lifecycle reports it ready_to_stop, as in Joining and removing engines"
        )
