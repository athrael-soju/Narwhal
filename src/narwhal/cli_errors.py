"""Render expected command failures while retaining their operation and input."""

from __future__ import annotations

import subprocess
import sys

import httpx


def failure(command: str, operation: str, error: Exception, status: int) -> int:
    """Write an operator diagnostic and return its exit category."""
    detail = str(error)
    if isinstance(error, httpx.HTTPError):
        try:
            request = error.request
        except RuntimeError:
            pass
        else:
            detail = f"{request.method} {request.url}: {detail}"
    elif isinstance(error, subprocess.TimeoutExpired):
        executable = error.cmd[0] if isinstance(error.cmd, (list, tuple)) else error.cmd
        detail = f"{executable} timed out after {error.timeout} seconds"
    print(f"{command}: {operation}: {detail}", file=sys.stderr)
    return status
