from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..backends import load as load_backend

if TYPE_CHECKING:
    from ..config import EngineSpec, FleetConfig


def scrape_targets(cfg: FleetConfig, engines: Iterable[EngineSpec]) -> list[dict[str, Any]]:
    metrics = load_backend(cfg.backend).metrics
    groups = []
    for spec in engines:
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
