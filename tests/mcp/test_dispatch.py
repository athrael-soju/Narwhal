"""Validate adapter boundaries and lossless command-result mapping."""

import asyncio
import json
import unittest
from copy import deepcopy

from narwhal.command_results import EXIT_CODES
from narwhal.contracts import COMMAND_RESULT, ContractVersionError, versioned
from narwhal.mcp.adapters import MAX_RESULT_BYTES, ToolAdapter, ToolInput, ToolRegistry
from narwhal.mcp.results import from_command_result, result


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_outcomes_errors_and_evidence_survive_mapping(self):
        for status, code in EXIT_CODES.items():
            with self.subTest(status=status):
                document = versioned(
                    COMMAND_RESULT,
                    {
                        "command": "narwhal-check",
                        "operation": "preflight",
                        "status": status,
                        "exit_code": code,
                        "data": {"complete": False},
                        "errors": [
                            {
                                "code": "future_compatible_code",
                                "message": "[REDACTED]",
                                "command": "narwhal-check",
                                "stage": "preflight",
                                "context": {"engine": "engine-a"},
                            }
                        ],
                        "artifacts": [
                            {
                                "kind": "preflight",
                                "path": "/private/preflight.json",
                                "state": "missing",
                            }
                        ],
                    },
                )
                before = deepcopy(document)
                mapped = from_command_result("fixture", "fleet-a", document)
                self.assertEqual(mapped["outcome"], status)
                self.assertEqual(mapped["command_result"], before)
                self.assertEqual(mapped["errors"][0]["code"], "future_compatible_code")
                self.assertEqual(
                    mapped["errors"][0]["context"],
                    {"command": "narwhal-check", "stage": "preflight", "engine": "engine-a"},
                )
                self.assertEqual(mapped["artifacts"], [])
                mapped["data"]["complete"] = True
                self.assertEqual(document, before)

    async def test_unsupported_document_version_and_adapter_exceptions_are_safe(self):
        async def unsupported(inputs):
            return from_command_result(
                "fixture", None, {"schema": "narwhal.command-result", "schema_version": 99}
            )

        registry = ToolRegistry([ToolAdapter("fixture", "Fixture", ToolInput, unsupported)])
        mapped = await registry.dispatch("fixture", {})
        self.assertEqual(mapped["outcome"], "invalid_input")
        self.assertEqual(mapped["errors"][0]["code"], "unsupported_contract")
        with self.assertRaises(ContractVersionError):
            from_command_result(
                "fixture", None, {"schema": "narwhal.command-result", "schema_version": True}
            )

        async def broken(inputs):
            raise RuntimeError("secret-token-with-site-path")

        registry = ToolRegistry([ToolAdapter("fixture", "Fixture", ToolInput, broken)])
        mapped = await registry.dispatch("fixture", {})
        self.assertEqual(mapped["outcome"], "error")
        self.assertNotIn("secret-token", json.dumps(mapped))

    async def test_exchange_timeout_cancels_only_the_request_handler(self):
        cancelled = []

        async def slow(inputs):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

        registry = ToolRegistry([ToolAdapter("fixture", "Fixture", ToolInput, slow)])
        mapped = await registry.dispatch("fixture", {"timeout_s": 1})
        self.assertEqual(mapped["errors"][0]["code"], "stage_timeout")
        self.assertEqual(cancelled, [True])

    async def test_invalid_output_and_oversized_output_never_reach_the_wire(self):
        for payload in (
            result("different"),
            result("fixture", data={"size": "x" * MAX_RESULT_BYTES}),
            result("fixture", data={"nonfinite": float("nan")}),
            {**result("fixture"), "schema_version": True},
            {**result("fixture"), "extra": True},
        ):

            async def handler(inputs, response=payload):
                return response

            registry = ToolRegistry([ToolAdapter("fixture", "Fixture", ToolInput, handler)])
            mapped = await registry.dispatch("fixture", {})
            self.assertEqual(mapped["errors"][0]["code"], "adapter_failed")
            self.assertLess(len(json.dumps(mapped).encode()), MAX_RESULT_BYTES)

    async def test_strict_inputs_and_duplicate_names(self):
        invoked = []

        async def handler(inputs):
            invoked.append(inputs)
            return result("fixture")

        adapter = ToolAdapter("fixture", "Fixture", ToolInput, handler)
        with self.assertRaises(ValueError):
            ToolRegistry([adapter, adapter])
        registry = ToolRegistry([adapter])
        for arguments in (
            {"timeout_s": True},
            {"timeout_s": 1.0},
            {"timeout_s": "1"},
            {"timeout_s": 0},
            {"timeout_s": 31},
            {"extra": 1},
            {"timeout_s": float("inf")},
        ):
            self.assertEqual(
                (await registry.dispatch("fixture", arguments))["outcome"], "invalid_input"
            )
        self.assertEqual(invoked, [])
        await registry.dispatch("fixture", {})
        self.assertEqual(invoked[0].timeout_s, 30)
