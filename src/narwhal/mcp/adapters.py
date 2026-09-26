"""Explicit typed dispatch for implemented Narwhal operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import Field, ValidationError

from narwhal.contracts import ContractVersionError

from .results import ManagementResult, WireModel, result

MAX_RESULT_BYTES = 262_144


class ToolInput(WireModel):
    """Common synchronous exchange deadline, independent of durable work."""

    timeout_s: int = Field(default=30, ge=1, le=30)


Input = TypeVar("Input", bound=ToolInput)


@dataclass(frozen=True)
class ToolAdapter(Generic[Input]):
    """Bind a fixed tool name and input type to its owning operation adapter.

    Handlers validate target permissions before side effects and return redacted,
    bounded results. They must use asynchronous I/O for the exchange deadline.
    Durable work must be submitted to an independent executor.
    """

    name: str
    description: str
    input_model: type[Input]
    handler: Callable[[Input], Awaitable[dict[str, Any]]]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False


class ToolRegistry:
    """Freeze explicitly supplied adapters; no dynamic imports or shell dispatch."""

    def __init__(self, adapters: Sequence[ToolAdapter[Any]] = ()) -> None:
        self._adapters: dict[str, ToolAdapter[Any]] = {}
        for adapter in adapters:
            # Validate names with the actual output contract, independent of the SDK.
            ManagementResult.model_validate(result(adapter.name))
            if adapter.name in self._adapters:
                raise ValueError("duplicate tool name")
            self._adapters[adapter.name] = adapter

    def adapters(self) -> tuple[ToolAdapter[Any], ...]:
        """Return the fixed catalogue in registration order."""
        return tuple(self._adapters.values())

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate before invoking a handler, bounding and validating its result."""
        adapter = self._adapters[name]

        def failure(outcome: str, code: str, message: str) -> dict[str, Any]:
            payload = result(name, errors=[{"code": code, "message": message, "context": {}}])
            payload["outcome"] = outcome
            return payload

        try:
            # Reject non-JSON/nonfinite values even inside otherwise untyped objects.
            json.dumps(arguments, allow_nan=False)
            inputs = adapter.input_model.model_validate(arguments)
        except (ValidationError, ValueError, TypeError):
            return failure("invalid_input", "invalid_arguments", "Tool arguments failed validation")
        try:
            async with asyncio.timeout(inputs.timeout_s):
                payload = await adapter.handler(inputs)
            validate = ManagementResult.model_validate(payload)
            if validate.tool != name or type(payload.get("schema_version")) is not int:
                raise ValueError("adapter returned a different tool or invalid version")
            encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                raise ValueError("adapter did not export its oversized result")
            return payload
        except ContractVersionError:
            return failure(
                "invalid_input", "unsupported_contract", "Document version is unsupported"
            )
        except TimeoutError:
            return failure("error", "stage_timeout", "Tool exchange exceeded its deadline")
        except Exception:
            # Exception prose can contain paths, endpoints or credential values.
            return failure(
                "error", "adapter_failed", "Tool adapter failed to return a valid result"
            )
