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


async def place_pair(
    switcher: RoleSwitcher,
    client: httpx.AsyncClient,
    producer: tuple[str, Mapping[str, Any] | None],
    consumer: tuple[str, Mapping[str, Any] | None],
) -> None:
    await switcher.switch(client, producer[0], Role.PREFILL, producer[1])
    await switcher.switch(client, consumer[0], Role.DECODE, consumer[1])
