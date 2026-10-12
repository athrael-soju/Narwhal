from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from ..types import Role


class RoleSwitcher(ABC):
    requires_idle: ClassVar[bool]
    # The connectors whose engines switch roles; None means every connector.
    connectors: ClassVar[frozenset[str] | None] = None

    @abstractmethod
    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def place(
        self,
        client: httpx.AsyncClient,
        producer: tuple[str, Mapping[str, Any] | None],
        consumer: tuple[str, Mapping[str, Any] | None],
    ) -> None:
        await self.switch(client, producer[0], Role.PREFILL, producer[1])
        await self.switch(client, consumer[0], Role.DECODE, consumer[1])

    def stale_handoff(self, producer: str, consumer: str) -> bool:
        """Whether `consumer` must refresh before it takes a handoff from `producer`."""
        return False

    def record_handoff(self, producer: str, consumer: str) -> None:
        return None

    async def refresh(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None:
        """Clear the transfer state `base` holds from earlier handoffs; it keeps `role`."""
        return None

    async def role(self, client: httpx.AsyncClient, base: str) -> Role | None:
        """The role `base` serves now, or None when the engine does not report it."""
        return None


class RouterRoleSwitch(RoleSwitcher):
    requires_idle = False

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None: ...


def engine_side(switcher: RoleSwitcher | None) -> RoleSwitcher | None:
    # A switcher that needs an idle engine changes the engine itself, not only the router.
    return switcher if switcher is not None and switcher.requires_idle else None


async def current_roles(
    switcher: RoleSwitcher,
    client: httpx.AsyncClient,
    launches: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, tuple[Role, Mapping[str, Any] | None]]:
    """The role each engine base in `launches` serves now; an unreadable role is left out."""
    roles: dict[str, tuple[Role, Mapping[str, Any] | None]] = {}
    for base, launch in launches.items():
        try:
            role = await switcher.role(client, base)
        except (httpx.HTTPError, ValueError):
            continue
        if role is not None:
            roles[base] = (role, launch)
    return roles


async def restore_roles(
    switcher: RoleSwitcher,
    client: httpx.AsyncClient,
    roles: Mapping[str, tuple[Role, Mapping[str, Any] | None]],
) -> None:
    """Serve each engine in `roles` as its role again, with no transfer state from before."""
    ordered = sorted(roles.items(), key=lambda item: item[1][0] is not Role.PREFILL)
    for base, (role, launch) in ordered:
        await switcher.switch(client, base, role, launch)
    producers = [base for base, (role, _) in ordered if role is Role.PREFILL]
    for base, (role, launch) in ordered:
        if role is Role.DECODE and any(switcher.stale_handoff(p, base) for p in producers):
            await switcher.refresh(client, base, role, launch)
