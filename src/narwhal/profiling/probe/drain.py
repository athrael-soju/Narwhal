"""The router's drain state of the engines a role-switching sweep uses."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from typing import Any

import httpx

from ...config import EngineSpec
from ...contracts import LIFECYCLE, MEMBERSHIP, validate_document


async def _endpoints(url: str, timeout_s: float) -> set[tuple[str, int]]:
    """The addresses and port that `url` reaches, plus its host and port.

    Two spellings of one host, such as `localhost` and `127.0.0.1`, share an address. A host
    that does not resolve within `timeout_s` keeps its name and port only.
    """
    parsed = httpx.URL(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    named = {(parsed.host, port)}
    try:
        found = await asyncio.wait_for(_resolve(parsed.host, port), timeout_s)
    except (OSError, UnicodeError, TimeoutError):
        return named
    return {(address, port) for address in found} | named


async def _resolve(host: str, port: int) -> set[str]:
    found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return {str(address[4][0]) for address in found}


async def _document(client: httpx.AsyncClient, url: str, name: str) -> dict[str, Any]:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"cannot read {url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise RuntimeError(f"cannot read {url}: HTTP {response.status_code}{_refusal(response)}")
    try:
        document = response.json()
        validate_document(document, name)
    except ValueError as exc:
        raise RuntimeError(f"cannot read {url}: {exc}") from exc
    return document


def _refusal(response: httpx.Response) -> str:
    """Return `: <message> (<reason>)` for a router's error body, such as a standby's refusal."""
    try:
        error = response.json()["error"]
        message, reason = error["message"], error.get("reason")
    except (ValueError, LookupError, TypeError):
        return ""
    return f": {message}" + (f" ({reason})" if reason else "")


def _held(record: Mapping[str, Any]) -> bool:
    """Whether the router keeps the engine out of placement and sends it nothing."""
    resident = record.get("resident")
    idle = isinstance(resident, Mapping) and not any(resident.values())
    if record.get("state") == "drained":
        # A decode leg that still reads KV from the engine keeps it from being ready to stop.
        return record.get("ready_to_stop") is True
    # A readmission that failed its checks holds the engine until an operator acts. A router
    # that does not report the engine's pending handoffs cannot show that none remain.
    return (
        record.get("state") == "blocked"
        and record.get("accepts_new") is False
        and idle
        and record.get("pending_handoffs") == 0
    )


async def refuse_undrained(
    router_url: str,
    engines: list[EngineSpec],
    targets: set[str],
    timeout_s: float,
    *,
    colocated: bool = False,
) -> None:
    """Raise unless every engine in `engines` is drained or outside the router's fleet.

    An engine counts as a member when a member shares its ID, or its URL's port and an address
    of its host. A `colocated` sweep takes its peers from the target's GPU group, so no engine
    outside the fleet can stand in for them.
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
    listed = [
        (member["iid"], member["url"])
        for member in membership.get("engines") or []
        if isinstance(member, Mapping)
        and isinstance(member.get("iid"), str)
        and isinstance(member.get("url"), str)
    ]
    urls = sorted({url for _, url in listed} | {spec.url for spec in engines})
    resolved = dict(
        zip(urls, await asyncio.gather(*(_endpoints(url, timeout_s) for url in urls)), strict=True)
    )
    members = [(iid, resolved[url]) for iid, url in listed]
    records = lifecycle.get("engines")
    records = records if isinstance(records, Mapping) else {}
    serving = []
    outside = []
    peers = False
    for spec in engines:
        matched = False
        reaches = resolved[spec.url]
        for iid, endpoints in members:
            if iid != spec.iid and not endpoints & reaches:
                continue
            matched = True
            record = records.get(iid)
            record = record if isinstance(record, Mapping) else {}
            if _held(record):
                continue
            name = spec.iid if spec.iid in targets else f"peer {spec.iid}"
            peers = peers or spec.iid not in targets
            if iid != spec.iid:
                name += f" (router member {iid})"
            serving.append(f"{name} is {record.get('state', 'unknown')}")
        if not matched:
            outside.append(spec.iid)
    if serving:
        hint = (
            " To pair a selected engine with an engine outside the router's fleet instead, "
            "sweep with a fleet file that lists that engine right after it"
            if peers and not colocated
            else ""
        )
        raise RuntimeError(
            f"refusing to switch engines that the router at {base} has not drained: "
            + ", ".join(serving)
            + ". Drain each one with POST /narwhal/lifecycle/drain and wait until "
            "GET /narwhal/lifecycle reports it ready_to_stop, as in Joining and removing engines."
            + hint
        )
    if outside:
        # A member listed under a URL that reaches no address of the fleet file's URL shows here.
        print(f"  router {base} has no member with the ID or URL of: {', '.join(outside)}")
