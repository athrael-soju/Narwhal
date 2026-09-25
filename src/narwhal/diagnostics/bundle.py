"""Collect bounded router snapshots and selected artifacts into a private directory."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..contracts import DIAGNOSTIC_BUNDLE, versioned
from ..provenance import _version, source_digest

SNAPSHOTS = (
    ("health", "/health"),
    ("ready", "/ready"),
    ("state", "/narwhal/state"),
    ("lifecycle", "/narwhal/lifecycle"),
    ("metrics", "/metrics"),
)
_REQUEST_FILES = {"journal.jsonl", "completion.json"}
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key|credential)",
    re.I,
)
_SECRET_FIELD = re.compile(
    r"(?:^|[_-])(?:password|passwd|secret|token|api[_-]?key|authorization|"
    r"private[_-]?key|credential[s]?|access[_-]?key)$",
    re.I,
)
_REQUEST_KEY = re.compile(
    r"^(?:messages|prompt|content|completion|request_body|response_body|text|input|output)$",
    re.I,
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:[\"']?(?:[\w-]*[_-])?(?:password|passwd|secret|token|api[_-]?key|"
    r"authorization|private[_-]?key|credential[s]?|access[_-]?key)[\"']?\s*[:=]\s*)"
)
_REQUEST_TEXT = re.compile(
    r"(?i)(?:\b(?:messages|prompt|content|completion|request_body|response_body|text|input|"
    r"output)[\"']?\s*[:=]\s*)"
)
_REDACTED = "[REDACTED]"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Redactor:
    """Remove credential values while retaining their environment-variable references."""

    def __init__(self, include_request_content: bool) -> None:
        self.include_request_content = include_request_content
        self.secrets = {
            value for name, value in os.environ.items() if value and _SECRET_KEY.search(name)
        }

    def references(self, value: Any) -> None:
        """Resolve credential references solely to remove those values from exports."""
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).endswith("_env") and isinstance(item, str):
                    if secret := os.environ.get(item):
                        self.secrets.add(secret)
                elif _SECRET_FIELD.search(str(key)) and isinstance(item, str) and item:
                    self.secrets.add(item)
                self.references(item)
        elif isinstance(value, list):
            for item in value:
                self.references(item)

    def text(self, value: str) -> str:
        """Scrub known values, authorization headers and URL credentials from text."""
        if match := _SENSITIVE_TEXT.search(value):
            value = value[: match.start()] + _REDACTED
        if not self.include_request_content and (match := _REQUEST_TEXT.search(value)):
            value = value[: match.start()] + _REDACTED
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, _REDACTED)
        value = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[^\s\"']+", r"\1 [REDACTED]", value)
        value = re.sub(
            r"(?i)([?&](?:api[_-]?key|token|password|secret|credential)=)[^&\s\"']+",
            r"\1[REDACTED]",
            value,
        )
        return value

    def value(self, value: Any) -> Any:
        """Scrub structured fields and request bodies under the selected inclusion policy."""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if str(key).endswith("_env") and isinstance(item, str):
                    result[key] = item
                elif _SECRET_FIELD.search(str(key)) or (
                    not self.include_request_content and _REQUEST_KEY.fullmatch(str(key))
                ):
                    result[key] = _REDACTED
                else:
                    result[key] = self.value(item)
            return result
        if isinstance(value, list):
            # Launch records commonly encode credential arguments as adjacent argv entries.
            result_list = []
            hide_next = False
            for item in value:
                if hide_next:
                    result_list.append(_REDACTED)
                    hide_next = False
                else:
                    result_list.append(self.value(item))
                    hide_next = (
                        isinstance(item, str)
                        and item.startswith("--")
                        and bool(_SECRET_FIELD.search(item.removeprefix("--")))
                    )
            return result_list
        return self.text(value) if isinstance(value, str) else value

    def body(self, data: bytes) -> bytes:
        """Redact JSON and JSONL fields, retaining safe prefixes of malformed text."""
        text = data.decode("utf-8", errors="replace")
        try:
            value = json.loads(text)
        except ValueError:
            rows = []
            for line in text.splitlines():
                try:
                    rows.append(json.dumps(self.value(json.loads(line)), ensure_ascii=False))
                except ValueError:
                    # A truncated or multiline value has no reliable closing delimiter.
                    # Once its field begins, omit the remaining text, including continuations.
                    matches = [_SENSITIVE_TEXT.search(line)]
                    if not self.include_request_content:
                        matches.append(_REQUEST_TEXT.search(line))
                    sensitive = [match.start() for match in matches if match is not None]
                    if sensitive:
                        rows.append(self.text(line[: min(sensitive)]) + _REDACTED)
                        break
                    rows.append(self.text(line))
            return ("\n".join(rows) + ("\n" if text.endswith("\n") else "")).encode()
        self.references(value)
        return (json.dumps(self.value(value), indent=2, ensure_ascii=False) + "\n").encode()


def _write(path: Path, data: bytes) -> None:
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def _credential_file(path: Path) -> bool:
    return (
        path.name == ".env"
        or path.name.startswith(".env.")
        or path.suffix.lower() in {".env", ".pem", ".key", ".p12", ".pfx"}
        or path.name
        in {"id_rsa", "id_ed25519", "authorized_keys", "credentials", "credentials.json"}
    )


@dataclass
class _Read:
    data: bytes
    truncated: bool
    mtime_ns: int


def _read(path: Path, maximum: int, deadline: float) -> _Read:
    # Resolve each directory through its open descriptor so selected run symlinks
    # cannot redirect artifact reads outside the selected filesystem tree.
    with contextlib.ExitStack() as stack:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, parent)
        parts = path.absolute().parts[1:]
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
        descriptor = os.open(
            parts[-1] if parts else ".",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact must be a regular file")
            chunks = bytearray()
            while len(chunks) <= maximum:
                if time.monotonic() >= deadline:
                    raise TimeoutError("artifact collection deadline expired")
                chunk = source.read(min(65536, maximum + 1 - len(chunks)))
                if not chunk:
                    return _Read(bytes(chunks), False, metadata.st_mtime_ns)
                chunks.extend(chunk)
            return _Read(bytes(chunks[:maximum]), True, metadata.st_mtime_ns)


def _selected(run: Path, include_request_content: bool) -> list[Path]:
    names = ["fleet.json", "profiles.json", "teardown.json", "router-state.json"]
    files = [run / name for name in names if (run / name).exists()]
    for pattern in (
        "*.log",
        "*.stdout",
        "*.stderr",
        "*.command.json",
        "*.stage.json",
        "engine-*/*.json",
        "engine-*/*.log",
        "engine-*/*.stdout",
        "engine-*/*.stderr",
        "verify-*/*.json",
        "verify-*/*.log",
        "verify-*/*.stdout",
        "verify-*/*.stderr",
    ):
        files.extend(sorted(run.glob(pattern)))
    if include_request_content:
        files.extend(sorted(run.glob("journal.jsonl")))
    return [path for path in files if include_request_content or path.name not in _REQUEST_FILES]


async def collect(
    router: str,
    output: Path,
    *,
    fleet: Path | None = None,
    instance: Path | None = None,
    run: Path | None = None,
    artifacts: tuple[Path, ...] = (),
    source_timeout: float = 5.0,
    timeout: float = 30.0,
    max_source_bytes: int = 8 * 1024 * 1024,
    include_request_content: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Retain every selected source's outcome, including partial HTTP and file failures."""
    parsed = urlsplit(router)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--router requires an absolute HTTP or HTTPS URL")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("--router port must be between 1 and 65535")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("--router accepts a base URL with host, port and optional path")
    for name, value in (("--source-timeout", source_timeout), ("--timeout", timeout)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if max_source_bytes < 1:
        raise ValueError("--max-source-bytes must be positive")
    if instance is not None and run is not None:
        raise ValueError("choose --instance or --run")
    started = time.monotonic()
    deadline = started + timeout
    redactor = Redactor(include_request_content)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest: dict[str, Any] = versioned(
        DIAGNOSTIC_BUNDLE,
        {
            "started_at": _now(),
            "package": {
                "name": "narwhal-inference",
                "version": _version(),
                "source": source_digest(),
            },
            "policy": {
                "include_request_content": include_request_content,
                "credential_files": "excluded",
                "max_source_bytes": max_source_bytes,
                "source_timeout_s": source_timeout,
                "overall_timeout_s": timeout,
            },
            "sources": [],
        },
    )
    sources: list[Path] = [fleet] if fleet is not None else []
    if instance is not None:
        sources.extend(
            instance / name for name in ("instance.json", "lifecycle.json", "fleet.json")
        )
        try:
            state = _read(instance / "lifecycle.json", max_source_bytes, deadline)
            if state.truncated:
                raise ValueError("instance lifecycle state exceeds the source byte limit")
            value = json.loads(state.data)
            if not isinstance(value, dict):
                raise ValueError("instance lifecycle state must be a JSON object")
            selected = value.get("run")
            if selected:
                candidate = Path(selected).absolute()
                if not candidate.resolve().is_relative_to(instance.resolve()):
                    raise ValueError("instance run must belong to the selected instance directory")
                run = candidate
        except (OSError, ValueError, TypeError) as exc:
            manifest["sources"].append(
                {
                    "source": str(instance / "lifecycle.json"),
                    "kind": "selection",
                    "status": "error",
                    "error": redactor.text(str(exc)),
                    "collected_at": _now(),
                }
            )
    if run is not None:
        sources.extend(_selected(run, include_request_content))
        if not run.is_dir():
            sources.append(run)
    sources.extend(artifacts)
    sources = list(dict.fromkeys(sources))
    # References are collected before endpoint bodies or artifact values reach the export.
    for path in sources:
        if path.suffix == ".json" and not path.is_symlink() and not _credential_file(path):
            try:
                result = _read(
                    path, max_source_bytes, min(deadline, time.monotonic() + source_timeout)
                )
                if not result.truncated:
                    redactor.references(json.loads(result.data))
            except (OSError, ValueError):
                pass

    def save(row: dict[str, Any], body: bytes, suffix: str) -> None:
        name = f"{len(manifest['sources']):04d}-{suffix}"
        exported = redactor.body(body)
        try:
            _write(output / name, exported)
        except OSError as exc:
            row.update(status="write_error", error=redactor.text(str(exc)))
        else:
            row.update(file=name, bytes=len(exported), sha256=hashlib.sha256(exported).hexdigest())

    for path in sources:
        row: dict[str, Any] = {
            "source": redactor.text(str(path.absolute())),
            "kind": "artifact",
            "collected_at": _now(),
        }
        if _credential_file(path) or (path.name in _REQUEST_FILES and not include_request_content):
            row.update(status="excluded", error="artifact excluded by the bundle inclusion policy")
        elif time.monotonic() >= deadline:
            row.update(status="timeout", error="overall collection deadline expired")
        else:
            try:
                result = _read(
                    path, max_source_bytes, min(deadline, time.monotonic() + source_timeout)
                )
                row.update(
                    status="truncated" if result.truncated else "ok",
                    source_mtime_ns=result.mtime_ns,
                    source_bytes=len(result.data),
                )
                if not result.truncated:
                    row["source_sha256"] = hashlib.sha256(result.data).hexdigest()
                save(
                    row, result.data, "artifact.json" if path.suffix == ".json" else "artifact.txt"
                )
            except (OSError, ValueError) as exc:
                row.update(
                    status="timeout" if isinstance(exc, TimeoutError) else "error",
                    error=redactor.text(str(exc)),
                )
        manifest["sources"].append(row)

    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False
    ) as client:
        for name, endpoint in SNAPSHOTS:
            url = router.rstrip("/") + endpoint
            row = {"source": redactor.text(url), "kind": "http", "collected_at": _now()}
            body = bytearray()
            remaining = min(source_timeout, deadline - time.monotonic())
            if remaining <= 0:
                row.update(status="timeout", error="overall collection deadline expired")
            else:
                try:
                    async with (
                        asyncio.timeout(remaining),
                        client.stream("GET", url, timeout=remaining) as response,
                    ):
                        row.update(
                            http_status=response.status_code,
                            content_type=redactor.text(response.headers.get("content-type", "")),
                            status="ok" if response.is_success else "http_error",
                        )
                        async for chunk in response.aiter_bytes():
                            available = max_source_bytes - len(body)
                            body.extend(chunk[:available])
                            if len(chunk) > available:
                                row.update(status="truncated", error="source byte limit reached")
                                break
                except (TimeoutError, httpx.HTTPError) as exc:
                    row.update(
                        status="timeout"
                        if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                        else "error",
                        error=redactor.text(str(exc)) or "source collection deadline expired",
                    )
                row["source_bytes"] = len(body)
                if body or "http_status" in row:
                    save(row, bytes(body), f"{name}.txt" if name == "metrics" else f"{name}.json")
            manifest["sources"].append(row)
    manifest.update(
        status="success"
        if all(row["status"] == "ok" for row in manifest["sources"])
        else "partial",
        finished_at=_now(),
        elapsed_s=time.monotonic() - started,
    )
    for row in manifest["sources"]:
        for key in ("source", "error", "content_type"):
            if key in row:
                row[key] = redactor.text(row[key])
    _write(output / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return manifest


def add_commands(commands: Any) -> None:
    """Register diagnostic collection beneath the installed root command."""
    diagnostic = commands.add_parser(
        "diagnostics",
        help="Collect private router and run diagnostics",
        description="Read router endpoints and selected local artifacts into a private bundle.",
    )
    actions = diagnostic.add_subparsers(dest="action", required=True)
    collect_parser = actions.add_parser(
        "collect",
        help="Collect bounded GET snapshots and selected artifacts",
        description=(
            "Fetch bounded router snapshots and copy selected artifacts into a fresh bundle."
        ),
    )
    from ..command_results import add_format

    add_format(collect_parser)
    collect_parser.add_argument("--router", required=True, help="Router HTTP base URL")
    collect_parser.add_argument(
        "--out", type=Path, required=True, help="Fresh bundle directory in an existing parent"
    )
    collect_parser.add_argument("--fleet", type=Path, help="Fleet JSON to include")
    selection = collect_parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--instance", type=Path, help="Select the current run from a dev instance"
    )
    selection.add_argument("--run", type=Path, help="Select an existing run directory")
    collect_parser.add_argument(
        "--artifact",
        type=Path,
        action="append",
        default=[],
        help="Additional regular file; repeatable",
    )
    collect_parser.add_argument(
        "--include-request-content",
        action="store_true",
        help="Include journal/completion artifacts and request fields",
    )
    collect_parser.add_argument(
        "--source-timeout", type=float, default=5.0, help="Seconds allowed for each source"
    )
    collect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Overall collection budget in seconds"
    )
    collect_parser.add_argument(
        "--max-source-bytes",
        type=int,
        default=8 * 1024 * 1024,
        help="Maximum bytes read from each source",
    )


def run(args: argparse.Namespace) -> int:
    """Collect one bundle and attach its manifest to the command result."""
    from .. import command_results

    command_results.set_operation("diagnostics collect")
    command_results.add_artifact("diagnostic_manifest", args.out / "manifest.json")
    try:
        manifest = asyncio.run(
            collect(
                args.router,
                args.out,
                fleet=args.fleet,
                instance=args.instance,
                run=args.run,
                artifacts=tuple(args.artifact),
                source_timeout=args.source_timeout,
                timeout=args.timeout,
                max_source_bytes=args.max_source_bytes,
                include_request_content=args.include_request_content,
            )
        )
    except (OSError, ValueError) as exc:
        if command_results.json_mode():
            raise
        print(Redactor(False).text(str(exc)), file=sys.stderr)
        return 2 if isinstance(exc, (ValueError, FileExistsError, FileNotFoundError)) else 4
    command_results.set_data(
        {
            "bundle": str(args.out),
            "manifest": str(args.out / "manifest.json"),
            "collection_status": manifest["status"],
            "sources": len(manifest["sources"]),
        }
    )
    if manifest["status"] == "partial":
        command_results.set_status("degraded")
        command_results.record_error(
            "collection_partial",
            "Inspect manifest.json for each source's collection outcome",
            stage="collection",
        )
    if not command_results.json_mode():
        print(json.dumps({"status": manifest["status"], "bundle": str(args.out)}, indent=2))
    return 3 if manifest["status"] == "partial" else 0
