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
