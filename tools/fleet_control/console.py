"""Routes for the console page and its settings."""

from __future__ import annotations

import base64
import hashlib
import html as markup
import re
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .app import API
from .config import ConsoleConfig, ControlConfig

PAGE = Path(__file__).with_name("console.html")
CONSOLE_PATH = "/console"
# Paths served without the token. `/` redirects to the console page.
PUBLIC_PATHS = ("/", CONSOLE_PATH)
_INLINE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)
_TOKEN_ANCHOR = '<meta name="referrer" content="no-referrer">\n'
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@cache
def page() -> str:
    """Return the console page."""
    return PAGE.read_text(encoding="utf-8")


def with_token(html: str, token: str) -> str:
    """Return the page with `token` in its `narwhal-control-token` meta element."""
    element = f'<meta name="narwhal-control-token" content="{markup.escape(token)}">\n'
    if html.count(_TOKEN_ANCHOR) != 1:
        raise ValueError("the console page needs exactly one token anchor")
    return html.replace(_TOKEN_ANCHOR, _TOKEN_ANCHOR + element)


def token_host(host: str, trusted: tuple[str, ...]) -> bool:
    """Return whether a Host header names a loopback address or one of the `trusted` names."""
    try:
        name = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    return name in LOOPBACK_HOSTS or name in trusted


def inline_hashes(html: str) -> dict[str, list[str]]:
    """Return the CSP source hash of each inline script and style block, by element name."""
    hashes: dict[str, list[str]] = {"script": [], "style": []}
    for element, body in _INLINE.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes[element].append(f"'sha256-{digest}'")
    return hashes


def origin(url: str) -> str:
    """Return the scheme, host and port of `url`."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def content_security_policy(html: str, console: ConsoleConfig | None) -> str:
    """Return the page's Content-Security-Policy."""
    hashes = inline_hashes(html)
    grafana = None if console is None else origin(console.grafana_url)
    frames = grafana if console is not None and console.panels else "'none'"
    embedded = console is not None and console.embed_in_grafana
    ancestors = f"'self' {grafana}" if embedded else "'none'"
    return "; ".join(
        (
            "default-src 'none'",
            "script-src " + " ".join(hashes["script"]),
            "style-src " + " ".join(hashes["style"]),
            "connect-src 'self'",
            f"frame-src {frames}",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {ancestors}",
        )
    )


def panel_url(console: ConsoleConfig, panel: int) -> str:
    """Return the Grafana URL that renders one dashboard panel on its own."""
    query = urlencode(
        {
            "orgId": 1,
            "panelId": panel,
            "from": console.time_from,
            "to": "now",
            "refresh": console.refresh,
        }
    )
    return f"{console.grafana_url}/d-solo/{console.dashboard_uid}/?{query}"


def console_document(config: ControlConfig) -> dict[str, Any]:
    """Return the console settings."""
    console = config.console
    grafana = None
    if console is not None:
        grafana = {
            "dashboard_uid": console.dashboard_uid,
            "dashboard_url": f"{console.grafana_url}/d/{console.dashboard_uid}/",
            "panels": [{"id": panel, "url": panel_url(console, panel)} for panel in console.panels],
        }
    return {"grafana": grafana, "load": config.load is not None, "hooks": sorted(config.hooks)}


def console_routes(config: ControlConfig, token: str = "") -> APIRouter:
    """Return the console page, its redirect and the console settings routes."""
    routes = APIRouter()
    html = plain = page()
    auto_connect = config.console is not None and config.console.auto_connect
    trusted = config.console.trusted_hosts if config.console is not None else ()
    if auto_connect:
        if not token:
            raise ValueError("console.auto_connect requires the bearer token")
        html = with_token(html, token)
    headers = {
        "Content-Security-Policy": content_security_policy(html, config.console),
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    # `frame-ancestors` names the origins that may frame the page.
    if config.console is None or not config.console.embed_in_grafana:
        headers["X-Frame-Options"] = "DENY"

    @routes.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(CONSOLE_PATH)

    @routes.get(CONSOLE_PATH, include_in_schema=False)
    async def console_page(request: Request) -> Response:
        body = html if token_host(request.headers.get("host", ""), trusted) else plain
        return Response(body, media_type="text/html; charset=utf-8", headers=headers)

    @routes.get(f"{API}/console")
    async def console_settings() -> dict[str, Any]:
        return console_document(config)

    return routes
