"""Apply a scheduled role change to an engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from ..types import Role


class RoleSwitcher(ABC):
    """Change an engine's role with its weights resident."""

    requires_idle: ClassVar[bool]

    @abstractmethod
    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        peer: Mapping[str, Any] | None = None,
    ) -> None:
        """Switch the engine to `role`; `peer` is the state of an engine already serving it."""


class RouterRoleSwitch(RoleSwitcher):
    """For engines that serve both roles, placement alone changes the role."""

    requires_idle = False

    async def switch(
        self,
        client: httpx.AsyncClient,
        base: str,
        role: Role,
        peer: Mapping[str, Any] | None = None,
    ) -> None:
        """Leave the engine unchanged."""
