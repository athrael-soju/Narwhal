"""Expose synthetic adapters through the real server for subprocess protocol checks."""

import asyncio
import logging
import os
import subprocess
import sys
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StringConstraints

from narwhal.mcp.adapters import ToolAdapter, ToolInput
from narwhal.mcp.results import result
from narwhal.mcp.server import serve


class EchoInput(ToolInput):
    """Validate a bounded synthetic payload before recording an invocation."""

    target_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    count: Annotated[StrictInt, Field(ge=1, le=10)] = 1
    mode: Literal["quiet", "noisy", "failed_gate"] = "quiet"


def adapters():
    """Create a fresh handler counter for each fixture server process."""
    calls = 0

    async def echo(arguments):
        nonlocal calls
        calls += 1
        if arguments.mode == "noisy":
            print("fixture-python-stdout")
            logging.getLogger("narwhal.fixture").warning("fixture-logging-stderr")
            os.write(1, b"fixture-raw-fd-stdout\n")
            subprocess.run(
                [sys.executable, "-c", "print('fixture-child-stdout', flush=True)"],
                check=True,
                timeout=5,
            )
        failed = arguments.mode == "failed_gate"
        return result(
            "fixture_echo",
            target_id=arguments.target_id,
            outcome="failed_gate" if failed else "success",
            data={"count": arguments.count, "calls": calls},
            errors=(
                [{"code": "gate_failed", "message": "Synthetic gate rejected", "context": {}}]
                if failed
                else []
            ),
        )

    return [
        ToolAdapter(
            name="fixture_echo",
            description="Return a bounded synthetic payload and invocation count.",
            input_model=EchoInput,
            handler=echo,
        )
    ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(adapters()))
