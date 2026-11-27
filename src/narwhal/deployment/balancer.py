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


def shipped() -> str:
    return resources.files(__package__).joinpath(CONFIG).read_text()


def render(text: str, bind: str, routers: tuple[str, str]) -> str:
    """The configuration listening on `bind`, with router-a and router-b at `routers`.

    `bind` and each router are `host:port`.
    """
    for name, address in zip(("A", "B"), routers, strict=True):
        placeholder = f"ROUTER_{name}_ADDRESS:8000"
        if text.count(placeholder) != 1:
            raise ValueError(f"{CONFIG} needs one {placeholder}")
        text = text.replace(placeholder, address)
    text, binds = re.subn(r"(?m)^(\s*)bind :8000$", rf"\g<1>bind {bind}", text)
    if binds != 1:
        raise ValueError(f"{CONFIG} needs one `bind :8000`")
    return text
