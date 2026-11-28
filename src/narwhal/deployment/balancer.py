"""The load balancer of an SSH router pair: `deploy/ha/haproxy.cfg` bound to the pair's origins.

The wheel carries the file as `narwhal/deployment/haproxy.cfg`, so `narwhal deploy run`
renders the same configuration as the chart and `tools/measurement/takeover_check.py`.
"""

from __future__ import annotations

import re
from importlib import resources

CONFIG = "haproxy.cfg"
# The container's configuration path in the official HAProxy image.
CONTAINER_CONFIG = "/usr/local/etc/haproxy/haproxy.cfg"
# The routes HAProxy forwards to the active router, on SSH hosts and in the chart: the
# completion routes, what a client or an external load balancer reads about the fleet, and the
# engine targets Prometheus reads. Every other route, such as /narwhal/state, /metrics or a
# membership or lifecycle action, answers 404 there; the stages, the other router and
# monitoring reach it at a router's own address.
SERVING_ROUTES = (
    ("POST", "/v1/completions"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/v1/models"),
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/narwhal/targets"),
)


def shipped() -> str:
    return resources.files(__package__).joinpath(CONFIG).read_text()


def route_rules() -> str:
    """The frontend lines that refuse every request but those `SERVING_ROUTES` names, matched
    by method and by the exact path, so no encoding or extra slash reaches another route."""
    lines = []
    methods = sorted({method for method, _ in SERVING_ROUTES})
    for method in methods:
        paths = " ".join(path for verb, path in SERVING_ROUTES if verb == method)
        lines.append(f"    acl narwhal_{method.lower()} method {method}")
        lines.append(f"    acl narwhal_{method.lower()}_routes path {paths}")
    allowed = " || ".join(f"narwhal_{m.lower()} narwhal_{m.lower()}_routes" for m in methods)
    lines.append(f"    http-request deny deny_status 404 unless {allowed}")
    return "\n".join(lines) + "\n"


def render(text: str, bind: str, routers: tuple[str, str]) -> str:
    """The configuration listening on `bind`, with router-a and router-b at `routers`, that
    forwards only `SERVING_ROUTES`.

    `bind` and each router are `host:port`.
    """
    for name, address in zip(("A", "B"), routers, strict=True):
        placeholder = f"ROUTER_{name}_ADDRESS:8000"
        if text.count(placeholder) != 1:
            raise ValueError(f"{CONFIG} needs one {placeholder}")
        text = text.replace(placeholder, address)
    text, binds = re.subn(
        r"(?m)^(\s*)bind :8000\n", lambda m: f"{m[1]}bind {bind}\n{route_rules()}", text
    )
    if binds != 1:
        raise ValueError(f"{CONFIG} needs one `bind :8000`")
    return text
