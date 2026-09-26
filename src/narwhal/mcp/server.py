"""MCP SDK transport and wire mapping, imported only by the optional server."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from .adapters import ToolAdapter, ToolRegistry
from .results import ManagementResult

logger = logging.getLogger(__name__)


def build_server(adapters: Sequence[ToolAdapter[Any]] = ()) -> Server:
    """Advertise only supplied adapters and map domain failures to tool results."""
    registry = ToolRegistry(adapters)
    output_schema = ManagementResult.model_json_schema(by_alias=True)

    async def list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=adapter.name,
                    description=adapter.description,
                    input_schema=adapter.input_model.model_json_schema(),
                    output_schema=output_schema,
                    annotations=types.ToolAnnotations(
                        read_only_hint=adapter.read_only,
                        destructive_hint=adapter.destructive,
                        idempotent_hint=adapter.idempotent,
                        open_world_hint=adapter.open_world,
                    ),
                )
                for adapter in registry.adapters()
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name not in {adapter.name for adapter in registry.adapters()}:
            raise MCPError(code=types.INVALID_PARAMS, message="Unknown tool")
        payload = await registry.dispatch(params.name, params.arguments or {})
        return types.CallToolResult(
            content=[
                types.TextContent(text=json.dumps(payload, allow_nan=False, ensure_ascii=False))
            ],
            structured_content=payload,
            is_error=payload["outcome"] not in {"success", "accepted", "degraded"},
        )

    try:
        installed_version = version("narwhal-inference")
    except PackageNotFoundError:
        installed_version = "unknown"
    return Server(
        "narwhal", version=installed_version, on_list_tools=list_tools, on_call_tool=call_tool
    )


async def serve(adapters: Sequence[ToolAdapter[Any]] = ()) -> None:
    """Serve one stdio connection; the SDK isolates inherited stdout descriptors."""
    async with stdio_server() as (read_stream, write_stream):
        server = build_server(adapters)
        logger.info("Narwhal MCP server started")
        try:
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            logger.info("Narwhal MCP server stopped")
            # Flush Python's diagnostic buffer before the SDK restores fd 1 to the wire.
            sys.stdout.flush()
