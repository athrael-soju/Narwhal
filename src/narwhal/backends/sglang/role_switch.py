from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from ...runtime.role_switch import RoleSwitcher
from ...types import Role
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

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None:
        base = base.rstrip("/")
        info = await client.get(f"{base}/server_info")
        info.raise_for_status()
        state = info.json()
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
