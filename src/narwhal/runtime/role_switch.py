from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from ..types import Role, RoleModel


class RoleSwitcher(ABC):
    model: ClassVar[RoleModel]
    # Whether the engine refuses a switch while it holds requests.
    requires_idle: ClassVar[bool] = False
    # The connectors whose engines switch roles; None means every connector.
    connectors: ClassVar[frozenset[str] | None] = None

    @property
    def engine_held(self) -> bool:
        return self.model is RoleModel.ENGINE

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


class FixedRoles(RoleSwitcher):
    model = RoleModel.FIXED

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None: ...


class RouterRoleSwitch(RoleSwitcher):
    model = RoleModel.ROUTER

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None: ...


class EngineHeldRoles(RoleSwitcher):
    """A decode engine keeps each producer's endpoint until it switches; prefill opens a new one."""

    model = RoleModel.ENGINE

    def __init__(self) -> None:
        # Times each engine entered prefill through this switcher.
        self._generations: dict[str, int] = {}
        # Per decode engine, the producer generations it took handoffs from since its last switch.
        self._received: dict[str, dict[str, int]] = {}

    @abstractmethod
    async def change_role(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None,
    ) -> bool:
        """Switch the engine at `base` to `role`; return False when it already serves it."""

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        launch: Mapping[str, Any] | None = None,
    ) -> None:
        base = base.rstrip("/")
        if not await self.change_role(client, base, role, launch):
            return
        self._received[base] = {}
        if role is Role.PREFILL:
            self._generations[base] = self._generations.get(base, 0) + 1

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
        # An engine this switcher has not switched may hold endpoints from before it.
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
