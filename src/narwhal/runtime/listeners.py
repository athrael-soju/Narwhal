from __future__ import annotations

import asyncio
import ipaddress
import socket

import uvicorn


def check_http_bind(host: str, port: int) -> None:

    async def check() -> None:
        server = await asyncio.get_running_loop().create_server(asyncio.Protocol, host, port)
        server.close()
        await server.wait_closed()

    factory = uvicorn.Config(app="").get_loop_factory()
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(check())


def check_engine_bind(host: str, port: int, *, dual_stack: bool = False) -> None:
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        family = socket.AF_INET
    else:
        family = socket.AF_INET6
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if dual_stack and family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind((host, port))
        # SO_REUSEADDR can share a bound socket until one of the owners listens.
        listener.listen(1)
