from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...runtime.role_switch import RoleSwitcher
from ...types import Role
from .identity import server_info
from .plan import ROLE_SWITCH_CONNECTORS

SWITCH_PATH = "/pd_role_switch"


def _scheduler(info: Any) -> dict[str, Any]:
    states = info.get("internal_states") if isinstance(info, dict) else None
    state = states[0] if isinstance(states, list) and states else {}
    return state if isinstance(state, dict) else {}


def current_role(info: Any) -> str | None:
    # The scheduler reports its live role; the server arguments keep the launch role.
    mode = _scheduler(info).get("disaggregation_mode")
    return mode if mode in (Role.PREFILL.value, Role.DECODE.value) else None


def graph_memory(info: Any, launch: Mapping[str, Any] | None) -> float | None:
    # A decode switch without resident decode graphs needs their memory budget.
    value = _scheduler(info).get("decode_cuda_graph_memory_gb")
    if not (isinstance(value, int | float) and value > 0) and launch is not None:
        value = launch.get("decode_cuda_graph_memory_gb")
    return float(value) if isinstance(value, int | float) and value > 0 else None


class SglangRoleSwitch(RoleSwitcher):
    # SGLang refuses a switch while the instance holds requests.
    requires_idle = True
    connectors = frozenset(ROLE_SWITCH_CONNECTORS)

    def __init__(self) -> None:
        # Times each engine entered prefill through this switcher.
        self._generations: dict[str, int] = {}
        # Per decode engine, the generation of each producer it took a handoff from since its
        # own last switch.
        self._received: dict[str, dict[str, int]] = {}

    async def place(
        self,
        client: httpx.AsyncClient,
        producer: tuple[str, Mapping[str, Any] | None],
        consumer: tuple[str, Mapping[str, Any] | None],
    ) -> None:
        await super().place(client, producer, consumer)
        if self.stale_handoff(producer[0], consumer[0]):
            await self.refresh(client, consumer[0], Role.DECODE, consumer[1])
        self.record_handoff(producer[0], consumer[0])

    def stale_handoff(self, producer: str, consumer: str) -> bool:
        # A decode engine keeps each producer's transfer endpoint until it switches itself, and
        # a producer that enters prefill again listens on a new one. An engine this switcher has
        # not switched may hold endpoints from before it.
        source, target = producer.rstrip("/"), consumer.rstrip("/")
        received = self._received.get(target)
        generation = self._generations.get(source, 0)
        return received is None or received.get(source, generation) != generation

    def record_handoff(self, producer: str, consumer: str) -> None:
        if not self.stale_handoff(producer, consumer):
            source, target = producer.rstrip("/"), consumer.rstrip("/")
            self._received[target][source] = self._generations.get(source, 0)

    async def refresh(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None:
        other = Role.DECODE if role is Role.PREFILL else Role.PREFILL
        await self.switch(client, base, other, launch)
        await self.switch(client, base, role, launch)

    async def role(self, client: httpx.AsyncClient, base: str) -> Role | None:
        mode = current_role(await server_info(client, base))
        return None if mode is None else Role(mode)

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None:
        base = base.rstrip("/")
        state = await server_info(client, base)
        if current_role(state) == role.value:
            return
        body: dict[str, Any] = {"new_role": role.value}
        if role is Role.DECODE and (memory := graph_memory(state, launch)) is not None:
            body["decode_cuda_graph_memory_gb"] = memory
        response = await client.post(f"{base}{SWITCH_PATH}", json=body)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("success") is not True:
            message = result.get("message") if isinstance(result, dict) else result
            raise ValueError(f"engine refused the switch to {role.value}: {message}")
        self._received[base] = {}
        if role is Role.PREFILL:
            self._generations[base] = self._generations.get(base, 0) + 1
