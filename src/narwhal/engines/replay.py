"""Verify approved replay identities and parse bounded, complete completion events."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import httpx

from ..contracts import (
    CONTRACTS,
    REPLAY_CAPTURE,
    REPLAY_QUALIFICATION,
    ContractVersionError,
    validate_document,
    versioned,
)
from .attestation import EngineIdentity, parse_process_start

REPLAY_PROFILE = "singleton-byte-utf8-v1"
QUALIFICATION_SCHEMA = CONTRACTS[REPLAY_QUALIFICATION].schema
CAPTURE_SCHEMA = CONTRACTS[REPLAY_CAPTURE].schema
_HEX = re.compile(r"[0-9a-f]{64}")
_SOURCE_FIELDS = {"model", "tokenizer", "decoder", "renderer", "generation", "extensions"}
_GENERATION = {
    "temperature": 0,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "repetition_penalty": 1,
    "top_p": 1,
    "top_k": -1,
    "min_p": 0,
    "min_tokens": 0,
    "ignore_eos": False,
    "include_stop_str_in_output": False,
    "echo": False,
    "add_special_tokens": False,
    "skip_special_tokens": True,
    "spaces_between_special_tokens": True,
}


class ReplayError(ValueError):
    """A replay contract failed without including request or response content."""


class ReplayUnavailable(ReplayError):
    """Qualification is absent, stale or unreadable; this is not inference failure."""


class ReplayInterrupted(ReplayError):
    """The transport ended before a complete stream terminator arrived."""


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError("continuation record must be an object")
    if not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ReplayError("continuation record has missing or unknown fields")
    return value


def _positive(value: Any, *, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ReplayError("continuation bound must be a positive integer")
    return value


def _digest(value: Any, *, prefix: bool = False) -> str:
    if not isinstance(value, str):
        raise ReplayError("continuation digest is invalid")
    raw = value.removeprefix("sha256:") if prefix else value
    if not _HEX.fullmatch(raw) or (prefix and not value.startswith("sha256:")):
        raise ReplayError("continuation digest is invalid")
    return value


def _ids(value: Any, *, vocabulary: int) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        type(token) is not int or not 0 <= token < vocabulary for token in value
    ):
        raise ReplayError("continuation token identity is invalid")
    return tuple(value)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ReplayError("continuation JSON has duplicate fields")
        out[key] = value
    return out


def _nonfinite(value: str) -> Any:
    raise ReplayError("continuation JSON has a nonfinite value")


def _json(raw: bytes | str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReplayError("continuation JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ReplayError("continuation JSON must be an object")
    return value


@dataclass(frozen=True, repr=False)
class ReplayContract:
    """Inputs whose semantics were approved by source and live qualification.

    Digests bind that approval to measured artifacts. They do not establish
    decoder semantics without the reviewed qualification trust root.
    """

    model: str
    vocab_size: int
    max_context_tokens: int
    max_output_tokens: int
    eos_token_ids: tuple[int, ...]
    default_stop_token_ids: tuple[int, ...]
    allowed_stop_token_ids: tuple[int, ...]
    source_digests: Mapping[str, str] = field(repr=False)

    @classmethod
    def parse(cls, raw: Any) -> ReplayContract:
        """Read the exact contract approved by the qualification artifact."""
        data = _keys(
            raw,
            {
                "profile",
                "model",
                "vocab_size",
                "max_context_tokens",
                "max_output_tokens",
                "eos_token_ids",
                "default_stop_token_ids",
                "allowed_stop_token_ids",
                "source_digests",
                "generation",
            },
        )
        if data["profile"] != REPLAY_PROFILE:
            raise ReplayError("continuation decoder profile is not supported")
        if not isinstance(data["model"], str) or not data["model"].strip():
            raise ReplayError("continuation model identity is missing")
        generation = _keys(data["generation"], set(_GENERATION))
        for name, expected in _GENERATION.items():
            actual = generation[name]
            if isinstance(expected, bool):
                valid = type(actual) is bool and actual == expected
            else:
                valid = type(actual) in (int, float) and actual == expected
            if not valid:
                raise ReplayError("continuation generation settings are not qualified")
        digests = _keys(data["source_digests"], _SOURCE_FIELDS)
        digests = {name: _digest(value) for name, value in digests.items()}
        vocabulary = _positive(data["vocab_size"], maximum=2**64)
        stops = [
            _ids(data[name], vocabulary=vocabulary)
            for name in ("eos_token_ids", "default_stop_token_ids", "allowed_stop_token_ids")
        ]
        if not stops[0] or any(len(ids) != len(set(ids)) for ids in stops):
            raise ReplayError("continuation stop identity is invalid")
        return cls(
            data["model"],
            vocabulary,
            _positive(data["max_context_tokens"]),
            _positive(data["max_output_tokens"]),
            stops[0],
            stops[1],
            stops[2],
            MappingProxyType(digests),
        )

    def fields(self) -> dict[str, Any]:
        """Return the canonical contract whose digest binds each process capture."""
        return {
            "profile": REPLAY_PROFILE,
            "model": self.model,
            "vocab_size": self.vocab_size,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "eos_token_ids": list(self.eos_token_ids),
            "default_stop_token_ids": list(self.default_stop_token_ids),
            "allowed_stop_token_ids": list(self.allowed_stop_token_ids),
            "source_digests": dict(self.source_digests),
            "generation": dict(_GENERATION),
        }

    def fingerprint(self) -> str:
        """Hash all qualified settings and artifact identities."""
        raw = json.dumps(self.fields(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def validate_request(self, body: dict[str, Any]) -> tuple[int, ...]:
        """Check engine-facing values after serving's HTTP allowlist validation."""
        allowed = set(_GENERATION) | {
            "model",
            "prompt",
            "max_tokens",
            "n",
            "stream",
            "stop",
            "stop_token_ids",
            "return_token_ids",
            "stream_interval",
            "stream_options",
            "request_id",
            "user",
        }
        if body.keys() - allowed:
            raise ReplayError("continuation request has unqualified extension fields")
        prompt = _ids(body.get("prompt"), vocabulary=self.vocab_size)
        maximum = _positive(body.get("max_tokens"))
        if not prompt or len(prompt) + maximum > self.max_context_tokens:
            raise ReplayError("continuation request exceeds its qualified context")
        if maximum > self.max_output_tokens:
            raise ReplayError("continuation request exceeds its qualified output limit")
        if body.get("model") != self.model or body.get("stream") is not True:
            raise ReplayError("continuation request does not match its qualified route")
        if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
            raise ReplayError("continuation requires one choice")
        for name, value in _GENERATION.items():
            actual = body.get(name, value)
            if isinstance(value, bool):
                valid = type(actual) is bool and actual == value
            else:
                valid = type(actual) in (int, float) and actual == value
            if not valid:
                raise ReplayError("continuation request changes qualified generation settings")
        if body.get("stop", []) not in ([], None):
            raise ReplayError("continuation string stops are not qualified")
        requested_stops = _ids(body.get("stop_token_ids", []), vocabulary=self.vocab_size)
        if not set(requested_stops) <= set(self.allowed_stop_token_ids):
            raise ReplayError("continuation token stops are not qualified")
        return prompt

    def request_settings(self) -> dict[str, Any]:
        """Return settings serving must explicitly put on every backend attempt."""
        return dict(_GENERATION)


@dataclass(frozen=True, repr=False)
class ReplayCapture:
    """Measured replay identity for one engine process."""

    contract_sha256: str
    attestation_digest: str
    identity: EngineIdentity

    @classmethod
    def parse(cls, raw: Any) -> ReplayCapture:
        """Read a capture bound to an explicit measured engine process."""
        data = _keys(
            raw, {"schema", "schema_version", "contract_sha256", "attestation_digest", "engine"}
        )
        try:
            validate_document(data, REPLAY_CAPTURE)
        except ContractVersionError as exc:
            raise ReplayError("continuation capture schema is not supported") from exc
        engine = _keys(data["engine"], {"vllm_version", "process_start_time_seconds"})
        version, start = engine["vllm_version"], engine["process_start_time_seconds"]
        if not isinstance(version, str) or not version.strip():
            raise ReplayError("continuation engine version is invalid")
        try:
            numeric_start = float(start) if type(start) in (int, float) else math.nan
        except OverflowError as exc:
            raise ReplayError("continuation process identity is invalid") from exc
        if not math.isfinite(numeric_start) or numeric_start <= 0:
            raise ReplayError("continuation process identity is invalid")
        return cls(
            _digest(data["contract_sha256"]),
            _digest(data["attestation_digest"], prefix=True),
            EngineIdentity(version, numeric_start),
        )

    @classmethod
    def load(cls, path: str | Path) -> ReplayCapture:
        """Load a sidecar capture without rebinding it to a replacement process."""
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise ReplayError("continuation capture is unreadable") from exc
        return cls.parse(_json(raw))

    def fields(self) -> dict[str, Any]:
        """Return the process-bound sidecar response."""
        return versioned(
            REPLAY_CAPTURE,
            {
                "contract_sha256": self.contract_sha256,
                "attestation_digest": self.attestation_digest,
                "engine": {
                    "vllm_version": self.identity.vllm_version,
                    "process_start_time_seconds": self.identity.process_start_time_seconds,
                },
            },
        )


@dataclass(frozen=True, repr=False)
class ReplayQualification:
    """A private, reviewed qualification pinned by the router configuration."""

    contract: ReplayContract
    engines: Mapping[str, ReplayCapture]
    evidence_sha256: str

    @classmethod
    def load(cls, path: str | Path, sha256: str) -> ReplayQualification:
        """Verify the raw file pin before parsing its approved captures."""
        _digest(sha256)
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise ReplayError("continuation qualification is unreadable") from exc
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise ReplayError("continuation qualification digest does not match")
        data = _keys(
            _json(raw), {"schema", "schema_version", "contract", "engines", "evidence_sha256"}
        )
        try:
            validate_document(data, REPLAY_QUALIFICATION)
        except ContractVersionError as exc:
            raise ReplayError("continuation qualification schema is not supported") from exc
        contract = ReplayContract.parse(data["contract"])
        engines = data["engines"]
        if (
            not isinstance(engines, dict)
            or not engines
            or any(not isinstance(iid, str) or not iid.strip() for iid in engines)
        ):
            raise ReplayError("continuation qualification has no valid engines")
        captures = {iid: ReplayCapture.parse(value) for iid, value in engines.items()}
        if any(c.contract_sha256 != contract.fingerprint() for c in captures.values()):
            raise ReplayError("continuation capture belongs to another contract")
        return cls(contract, MappingProxyType(captures), _digest(data["evidence_sha256"]))

    async def verify(
        self,
        iid: str,
        engine_url: str,
        attestation_url: str,
        *,
        client: httpx.AsyncClient,
        headers: dict[str, str] | None = None,
        timeout_s: float = 5.0,
    ) -> ReplayCapture:
        """Compare a fresh process and sidecar response with approved evidence."""
        expected = self.engines.get(iid)
        if expected is None or not attestation_url:
            raise ReplayUnavailable("engine has no approved continuation capture")
        try:
            version = await client.get(
                engine_url.rstrip("/") + "/version", headers=headers, timeout=timeout_s
            )
            version.raise_for_status()
            version_value = _json(version.content).get("version")
            if not isinstance(version_value, str) or not version_value:
                raise ReplayError("continuation engine version is missing")
            metrics = await client.get(
                engine_url.rstrip("/") + "/metrics", headers=headers, timeout=timeout_s
            )
            metrics.raise_for_status()
            live = EngineIdentity(version_value, parse_process_start(metrics.text))
            base = await client.get(attestation_url, timeout=timeout_s)
            base.raise_for_status()
            standard = _json(base.content)
            response = await client.get(
                attestation_url.rstrip("/") + "/continuation", timeout=timeout_s
            )
            response.raise_for_status()
            capture = ReplayCapture.parse(_json(response.content))
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise ReplayUnavailable("continuation identity verification failed") from exc
        if (
            capture != expected
            or live != expected.identity
            or standard.get("attestation_digest") != expected.attestation_digest
        ):
            raise ReplayUnavailable("continuation identity changed or is unqualified")
        unsigned = {k: v for k, v in standard.items() if k != "attestation_digest"}
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        if digest != expected.attestation_digest:
            raise ReplayUnavailable("continuation base attestation digest does not match")
        return capture


@dataclass(frozen=True, repr=False)
class ReplayEvent:
    """One validated, complete backend event; content never appears in repr."""

    kind: Literal["completion", "usage", "done"]
    envelope: dict[str, Any]
    generated_ids: tuple[int, ...] = ()
    text: str = ""
    finish_reason: str | None = None


class ReplayEventReader:
    """Bound framing before decoding JSON and verify each attempt's token identity."""

    def __init__(
        self,
        contract: ReplayContract,
        prompt_ids: Sequence[int],
        max_tokens: int,
        max_event_bytes: int,
        *,
        stop_token_ids: Sequence[int] = (),
    ) -> None:
        self.contract = contract
        self.prompt_ids = tuple(prompt_ids)
        self.max_tokens = _positive(max_tokens)
        self.max_event_bytes = _positive(max_event_bytes)
        self._line = bytearray()
        self._data: list[bytes] = []
        self._size = 0
        self._first = True
        self._finished = False
        self._done = False
        self._generated = 0
        self._identity: tuple[str, str, int] | None = None
        self._usage_seen = False
        self._stop_ids = (
            set(contract.eos_token_ids) | set(contract.default_stop_token_ids) | set(stop_token_ids)
        )
        self._last_token: int | None = None

    def feed(self, chunk: bytes) -> Iterator[ReplayEvent]:
        """Yield complete LF/CRLF data events while bounding each unfinished event."""
        offset = 0
        while offset < len(chunk):
            end = chunk.find(b"\n", offset)
            stop = len(chunk) if end < 0 else end + 1
            size = stop - offset
            if self._size + size > self.max_event_bytes:
                raise ReplayError("continuation event exceeds its byte limit")
            self._size += size
            self._line.extend(chunk[offset:stop])
            offset = stop
            if end < 0:
                return
            line = bytes(self._line[:-1]).removesuffix(b"\r")
            self._line.clear()
            if b"\r" in line:
                raise ReplayError("continuation SSE framing is invalid")
            if not line:
                payload = b"\n".join(self._data)
                self._data.clear()
                self._size = 0
                if payload:
                    yield self._event(payload)
            elif line.startswith(b":"):
                continue
            elif line.startswith(b"data:"):
                self._data.append(line[5:].removeprefix(b" "))
            else:
                raise ReplayError("continuation SSE field is not supported")

    def finish(self) -> None:
        """Require a complete terminator when the HTTP response reaches EOF."""
        if not self._done:
            raise ReplayInterrupted("continuation stream ended before a complete terminator")
        if self._line or self._data:
            raise ReplayError("continuation stream contains data after its terminator")

    def _event(self, payload: bytes) -> ReplayEvent:
        if self._done:
            raise ReplayError("continuation event arrived after the terminator")
        if payload.strip() == b"[DONE]":
            if not self._finished or not self._generated:
                raise ReplayError("continuation terminator arrived before completion")
            self._done = True
            return ReplayEvent("done", {})
        obj = _keys(
            _json(payload),
            {"id", "object", "created", "model", "choices"},
            {"usage", "system_fingerprint"},
        )
        if (
            obj["object"] != "text_completion"
            or not isinstance(obj["id"], str)
            or not obj["id"]
            or obj["model"] != self.contract.model
            or type(obj["created"]) is not int
            or obj["created"] < 0
        ):
            raise ReplayError("continuation response identity is invalid")
        identity = (obj["id"], obj["model"], obj["created"])
        if self._identity is not None and self._identity != identity:
            raise ReplayError("continuation response identity changed within an attempt")
        self._identity = identity
        if obj.get("system_fingerprint") is not None and not isinstance(
            obj["system_fingerprint"], str
        ):
            raise ReplayError("continuation response fingerprint is invalid")
        usage = obj.get("usage")
        if usage is not None:
            self._usage(usage)
        choices = obj["choices"]
        if choices == []:
            if not self._finished or usage is None or self._usage_seen:
                raise ReplayError("continuation usage arrived outside its terminal boundary")
            self._usage_seen = True
            return ReplayEvent("usage", obj)
        if not isinstance(choices, list) or len(choices) != 1 or self._finished:
            raise ReplayError("continuation choice count or terminal order is invalid")
        choice = _keys(
            choices[0],
            {"index", "text", "token_ids"},
            {"finish_reason", "stop_reason", "prompt_token_ids", "logprobs"},
        )
        if (
            type(choice["index"]) is not int
            or choice["index"] != 0
            or not isinstance(choice["text"], str)
            or choice.get("logprobs") is not None
        ):
            raise ReplayError("continuation completion fields are invalid")
        ids = _ids(choice["token_ids"], vocabulary=self.contract.vocab_size)
        try:
            choice["text"].encode("utf-8")
        except UnicodeError as exc:
            raise ReplayError("continuation text is not valid Unicode") from exc
        if choice["text"] and not ids:
            raise ReplayError("continuation text has no generated token identity")
        prompt = choice.get("prompt_token_ids")
        if self._first:
            if _ids(prompt, vocabulary=self.contract.vocab_size) != self.prompt_ids:
                raise ReplayError("continuation prompt identity does not match the request")
            self._first = False
        elif prompt is not None:
            raise ReplayError("continuation prompt identity was repeated")
        finish = choice.get("finish_reason")
        if finish not in (None, "stop", "length"):
            raise ReplayError("continuation finish reason is not supported")
        if usage is not None and finish is None:
            raise ReplayError("continuation usage arrived before terminal metadata")
        stop_reason = choice.get("stop_reason")
        if stop_reason is not None and (
            finish != "stop"
            or type(stop_reason) is not int
            or not 0 <= stop_reason < self.contract.vocab_size
        ):
            raise ReplayError("continuation stop reason is invalid")
        self._generated += len(ids)
        if self._generated > self.max_tokens:
            raise ReplayError("continuation output exceeds the requested token limit")
        if ids:
            self._last_token = ids[-1]
        if stop_reason is not None and (
            stop_reason not in self._stop_ids or stop_reason != self._last_token
        ):
            raise ReplayError("continuation stop reason does not match the qualified stop set")
        if finish == "length" and self._generated != self.max_tokens:
            raise ReplayError("continuation length termination changed the requested token limit")
        self._finished = finish is not None
        self._usage_seen = usage is not None
        return ReplayEvent("completion", obj, ids, choice["text"], finish)

    @staticmethod
    def _usage(value: Any) -> None:
        usage = _keys(
            value, {"prompt_tokens", "completion_tokens", "total_tokens"}, {"prompt_tokens_details"}
        )
        if any(
            type(usage[name]) is not int or usage[name] < 0
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            raise ReplayError("continuation usage counts are invalid")
        if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
            raise ReplayError("continuation usage totals do not agree")
        details = usage.get("prompt_tokens_details")
        if details is not None:
            details = _keys(details, {"cached_tokens"})
            if (
                type(details["cached_tokens"]) is not int
                or not 0 <= details["cached_tokens"] <= usage["prompt_tokens"]
            ):
                raise ReplayError("continuation cached-token usage is invalid")
