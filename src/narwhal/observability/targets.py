from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..backends import load as load_backend

if TYPE_CHECKING:
    from ..config import FleetConfig


def scrape_targets(cfg: FleetConfig) -> list[dict[str, Any]]:
    metrics = load_backend(cfg.backend).metrics
    # A scrape of a draining engine would take the counts that profiling and checks read.
    if metrics.drains:
        return []
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
                    "__metrics_path__": url.path.rstrip("/") + metrics.path,
                },
            }
        )
    return groups
