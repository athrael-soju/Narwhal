"""Paced neighbour load on engines that share a GPU group."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ...engines.dialect import EngineDialect
from ...types import Role
from ..tasks import cancel_tasks
from .engine import completion_body, engine_context_limit, make_prompt


@dataclass(frozen=True)
class ColocatedWorkload:
    """Explicit offered traffic for each neighbour of a profiled engine."""

    prefill_rps: float
    decode_rps: float
    prefill_tokens: int
    decode_input_tokens: int
    decode_output_tokens: int


class NeighbourLoad:
    """Pace direct completion requests on the other engines in one GPU group."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        peers: list[tuple[str, str, Role]],
        model: str,
        dialect: EngineDialect,
        chars_per_token: float,
        workload: ColocatedWorkload,
        observation_timeout_s: float | None = None,
        *,
        peerless: bool = False,
    ) -> None:
        self.client = client
        self.peers = peers
        self.model = model
        self.dialect = dialect
        self.chars_per_token = chars_per_token
        self.workload = workload
        self.observation_timeout_s = observation_timeout_s
        # Requests carry fields that let each neighbour serve its role without a KV peer.
        self.peerless = peerless
        self.tasks: list[asyncio.Task[None]] = []
        self.roles = {iid: role for iid, _, role in peers}
        self._index = {iid: index for index, (iid, _, _) in enumerate(peers)}
        self.role_counts = {iid: dict.fromkeys(Role, 0) for iid, _, _ in peers}
        self.errors: dict[str, str] = {}
        self.started = 0.0
        self.elapsed: float | None = None
        self.paused_s = 0.0
        self._paused_at: float | None = None
        self._resumes = 0
        self._resumed_at = 0.0
        self._open = False
        self._inflight = 0
        self._state = asyncio.Condition()
        self._prompts: dict[tuple[str, Role], tuple[str, int]] = {}

    def _rate(self, role: Role) -> float:
        return self.workload.prefill_rps if role is Role.PREFILL else self.workload.decode_rps

    def _offset(self, iid: str) -> float:
        # Each neighbour starts at its own point in one period, so they do not send together.
        return self._index[iid] / len(self.peers) / self._rate(self.roles[iid])

    async def _prepare(self, iid: str, url: str, role: Role) -> None:
        if (iid, role) in self._prompts:
            return
        target = (
            self.workload.prefill_tokens
            if role is Role.PREFILL
            else self.workload.decode_input_tokens
        )
        output = 1 if role is Role.PREFILL else self.workload.decode_output_tokens
        prompt, count = await make_prompt(
            self.client,
            url,
            self.model,
            target,
            self.dialect,
            self.chars_per_token,
            timeout_s=self.observation_timeout_s or 30.0,
        )
        limit = await engine_context_limit(
            self.client,
            url,
            self.model,
            self.dialect,
            timeout_s=self.observation_timeout_s or 30.0,
        )
        if count + output > limit:
            raise ValueError(f"neighbour {iid}: {count}+{output} exceeds max_model_len {limit}")
        self._prompts[iid, role] = (prompt, output)

    async def start(self) -> None:
        """Start paced load on every peer and return after one warmup period."""
        for iid, url, role in self.peers:
            await self._prepare(iid, url, role)
        self.started = time.monotonic()
        self._open = True
        self.tasks = [asyncio.create_task(self._serve(iid, url)) for iid, url, _ in self.peers]
        # Warmup lasts one full period; stored rates cover only the latency sweep.
        await asyncio.sleep(max(1.0 / self._rate(role) for _, _, role in self.peers))
        self._collect_task_errors()
        if self.errors:
            await cancel_tasks(self.tasks)
            raise RuntimeError("neighbour load failed during warmup: " + self._error_detail())
        self.role_counts = {iid: dict.fromkeys(Role, 0) for iid in self.role_counts}
        self.started = time.monotonic()

    async def _serve(self, iid: str, url: str) -> None:
        resumes = self._resumes
        next_at = self.started + self._offset(iid)
        while True:
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            async with self._state:
                await self._state.wait_for(lambda: self._open)
                if self._resumes != resumes:
                    # After a pause each neighbour starts again from its own offset.
                    resumes = self._resumes
                    next_at = self._resumed_at + self._offset(iid)
                    if next_at > time.monotonic():
                        continue
                self._inflight += 1
                role = self.roles[iid]
            prompt, output = self._prompts[iid, role]
            extras = self.dialect.cold_probe_extras()
            if self.peerless:
                extras.update(self.dialect.peerless_fields() or {})
            try:
                response = await self.client.post(
                    f"{url}/v1/completions",
                    json=completion_body(self.model, prompt, output, self.dialect, extras),
                )
                response.raise_for_status()
                result = response.json()
                if (
                    result.get("error")
                    or result.get("usage", {}).get("completion_tokens") != output
                ):
                    raise RuntimeError("incomplete neighbour completion")
                self.role_counts[iid][role] += 1
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                self.errors[iid] = str(exc)
                return
            finally:
                self._inflight -= 1
                if not self._inflight and not self._open:
                    async with self._state:
                        self._state.notify_all()
            next_at = max(next_at + 1.0 / self._rate(role), time.monotonic())

    async def pause(self) -> None:
        """Stop issuing requests and return once every request in flight has finished."""
        async with self._state:
            self._open = False
            self._paused_at = time.monotonic()
            bound = self.observation_timeout_s or 300.0
            try:
                async with asyncio.timeout(bound):
                    await self._state.wait_for(lambda: not self._inflight)
            except TimeoutError:
                raise RuntimeError(
                    f"neighbour load kept {self._inflight} request(s) in flight for {bound:g} s "
                    "after it paused"
                ) from None

    async def resume(self, roles: Mapping[str, Role] | None = None) -> None:
        """Issue requests again, with each neighbour in its role from `roles`."""
        roles = {**self.roles, **(roles or {})}
        for iid, url, _ in self.peers:
            await self._prepare(iid, url, roles[iid])
        async with self._state:
            now = time.monotonic()
            if self._paused_at is not None:
                self.paused_s += now - self._paused_at
                self._paused_at = None
            self.roles = roles
            self._resumes += 1
            self._resumed_at = now
            self._open = True
            self._state.notify_all()

    def _collect_task_errors(self) -> None:
        for (iid, _, _), task in zip(self.peers, self.tasks, strict=True):
            if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                self.errors[iid] = f"{type(error).__name__}: {error}"

    def _error_detail(self) -> str:
        return "; ".join(f"{iid}: {error}" for iid, error in self.errors.items())

    def _measured_s(self) -> float:
        now = time.monotonic()
        paused = self.paused_s + (now - self._paused_at if self._paused_at is not None else 0.0)
        return max(now - self.started - paused, 1e-9)

    def evidence(self) -> dict[str, Any]:
        """Return completed traffic, rates and errors for every configured neighbour."""
        elapsed = self.elapsed or self._measured_s()
        counts = {role: sum(served[role] for served in self.role_counts.values()) for role in Role}
        peers: dict[str, dict[str, Any]] = {}
        for iid, _, role in self.peers:
            by_role = self.role_counts[iid]
            completed = sum(by_role.values())
            row: dict[str, Any] = {
                "role": role.value,
                "completed": completed,
                "rps": completed / elapsed,
                "error": self.errors.get(iid),
            }
            if completed != by_role[role]:
                row["completed_by_role"] = {served.value: n for served, n in by_role.items()}
            peers[iid] = row
        evidence: dict[str, Any] = {
            "elapsed_s": elapsed,
            "offered_prefill_rps_per_peer": self.workload.prefill_rps,
            "offered_decode_rps_per_peer": self.workload.decode_rps,
            "completed_prefill": counts[Role.PREFILL],
            "completed_decode": counts[Role.DECODE],
            "prefill_rps": counts[Role.PREFILL] / elapsed,
            "decode_rps": counts[Role.DECODE] / elapsed,
            "peers": peers,
        }
        if self.paused_s:
            evidence["paused_s"] = self.paused_s
        if self.peerless:
            evidence["peerless"] = True
        return evidence

    async def stop(self, *, drain: bool = False) -> dict[str, Any]:
        """Stop the load and return its evidence; raise when a peer failed or completed none.

        With `drain`, requests in flight finish before the load stops.
        """
        if drain:
            await self.pause()
        self.elapsed = self._measured_s()
        await cancel_tasks(self.tasks)
        self._collect_task_errors()
        for iid, by_role in self.role_counts.items():
            if not any(by_role.values()):
                self.errors.setdefault(iid, "completed 0 requests during measurement")
        if self.errors:
            raise RuntimeError("neighbour load failed: " + self._error_detail())
        return self.evidence()
