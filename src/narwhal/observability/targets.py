from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ..config import FleetConfig


def scrape_targets(cfg: FleetConfig) -> list[dict[str, Any]]:
    groups = []
    for spec in cfg.engines:
        url = urlsplit(spec.url)
        groups.append(
            {
                "targets": [url.netloc.rpartition("@")[2]],
                "labels": {
                    "iid": spec.iid,
                    "backend": cfg.backend,
                    "__scheme__": url.scheme,
                    "__metrics_path__": url.path.rstrip("/") + "/metrics",
                },
            }
        )
    return groups
