"""Metrics a Narwhal tool serves while its operation sends requests to engines."""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .metrics.exposition import metric_lines

METRICS_URL_ENV = "NARWHAL_TOOL_METRICS_URL"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class ToolActivity:
    """One operation's engines and progress, read by the metrics endpoint from its own thread."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self._lock = threading.Lock()
        self._engines: frozenset[str] = frozenset()
        self._total = 0
        self._completed = 0
        self._count: Callable[[], int] | None = None

    def use(self, iids: Iterable[str]) -> None:
        """Report `iids` as the engines the operation sends requests to."""
        with self._lock:
            self._engines = frozenset(iids)

    def plan(self, total: int, count: Callable[[], int] | None = None) -> None:
        """Set the operation's step total; `count`, when given, reads the completed steps."""
        with self._lock:
            self._total = total
            self._completed = 0
            self._count = count

    def advance(self, steps: int = 1) -> None:
        """Count `steps` more completed steps."""
        with self._lock:
            self._completed += steps

    def freeze(self) -> None:
        """Keep the steps the shared count reports now, and stop reading it."""
        with self._lock:
            count, completed = self._count, self._completed
        if count is not None:
            with contextlib.suppress(OSError, EOFError):
                completed = count()
        with self._lock:
            self._count = None
            self._completed = completed

    def render(self) -> str:
        with self._lock:
            engines = sorted(self._engines)
            total = self._total
            count = self._count
            completed = self._completed
        if count is not None:
            # A count shared with worker processes fails once their manager stops.
            with contextlib.suppress(OSError, EOFError):
                completed = count()
        operation = {"operation": self.operation}
        lines = [
            *metric_lines(
                "narwhal_tool_operation",
                "Operation this Narwhal tool is running",
                "gauge",
                [(operation, 1)],
            ),
            *metric_lines(
                "narwhal_tool_engine_in_use",
                "Engine the operation sends requests to",
                "gauge",
                [({**operation, "iid": iid}, 1) for iid in engines],
            ),
            *metric_lines(
                "narwhal_tool_steps_completed",
                "Steps of the operation completed",
                "gauge",
                [(operation, min(completed, total))],
            ),
            *metric_lines(
                "narwhal_tool_steps_total",
                "Steps the operation plans",
                "gauge",
                [(operation, total)],
            ),
        ]
        return "\n".join(lines) + "\n"


def listen_address(url: str, source: str = METRICS_URL_ENV) -> tuple[str, int]:
    """Return the host and port in an `http://host:port` metrics URL that `source` names."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{source}: invalid URL {url!r}: {exc}") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{source} must be http://host:port, got {url!r}")
    return parsed.hostname, port


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class _Server6(_Server):
    address_family = socket.AF_INET6


@contextlib.contextmanager
def serve_tool_metrics(activity: ToolActivity, url: str | None) -> Iterator[None]:
    """Serve `activity` at `url`/metrics until the block exits; without `url`, serve nothing."""
    if not url:
        yield
        return
    host, port = listen_address(url)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = activity.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = (_Server6 if ":" in host else _Server)((host, port), Handler)
    except OSError as exc:
        # The operation still runs; the dashboard then shows its engines as probing.
        print(f"tool metrics: cannot listen on {url}: {exc}", file=sys.stderr)
        yield
        return
    thread = threading.Thread(target=server.serve_forever, name="tool-metrics", daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
