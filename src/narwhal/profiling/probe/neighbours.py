"""Paced neighbour load on engines that share a GPU group."""

from __future__ import annotations

import asyncio
import time
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
    ) -> None:
        self.client = client
        self.peers = peers
        self.model = model
        self.dialect = dialect
        self.chars_per_token = chars_per_token
        self.workload = workload
        self.observation_timeout_s = observation_timeout_s
        self.tasks: list[asyncio.Task[None]] = []
        self.counts = {iid: 0 for iid, _, _ in peers}
        self.errors: dict[str, str] = {}
        self.started = 0.0
        self.elapsed: float | None = None

    async def start(self) -> None:
        """Start paced load on every peer and return after one warmup period."""
        jobs = []
        for index, (iid, url, role) in enumerate(self.peers):
            rate = self.workload.prefill_rps if role is Role.PREFILL else self.workload.decode_rps
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
            jobs.append((iid, url, role, rate, prompt, output, index / len(self.peers) / rate))
        self.started = time.monotonic()
        self.tasks = [asyncio.create_task(self._serve(*job)) for job in jobs]
        # Warmup lasts one full period; stored rates cover only the latency sweep.
        await asyncio.sleep(max(1.0 / job[3] for job in jobs))
        self._collect_task_errors()
        if self.errors:
            await cancel_tasks(self.tasks)
            raise RuntimeError("neighbour load failed during warmup: " + self._error_detail())
        self.counts = dict.fromkeys(self.counts, 0)
        self.started = time.monotonic()

    async def _serve(
        self, iid: str, url: str, role: Role, rate: float, prompt: str, output: int, phase_s: float
    ) -> None:
        interval = 1.0 / rate
        next_at = self.started + phase_s
        while True:
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            try:
                response = await self.client.post(
                    f"{url}/v1/completions",
                    json=completion_body(
                        self.model, prompt, output, self.dialect, self.dialect.cold_probe_extras()
                    ),
                )
                response.raise_for_status()
                result = response.json()
                if (
                    result.get("error")
                    or result.get("usage", {}).get("completion_tokens") != output
                ):
                    raise RuntimeError("incomplete neighbour completion")
                self.counts[iid] += 1
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                self.errors[iid] = str(exc)
                return
            next_at = max(next_at + interval, time.monotonic())

    def _collect_task_errors(self) -> None:
        for (iid, _, _), task in zip(self.peers, self.tasks, strict=True):
            if task.done() and not task.cancelled() and (error := task.exception()) is not None:
                self.errors[iid] = f"{type(error).__name__}: {error}"

    def _error_detail(self) -> str:
        return "; ".join(f"{iid}: {error}" for iid, error in self.errors.items())

    def evidence(self) -> dict[str, Any]:
        """Return completed traffic, rates and errors for every configured neighbour."""
        elapsed = self.elapsed or max(time.monotonic() - self.started, 1e-9)
        counts = {
            role: sum(self.counts[iid] for iid, _, peer_role in self.peers if peer_role is role)
            for role in Role
        }
        return {
            "elapsed_s": elapsed,
            "offered_prefill_rps_per_peer": self.workload.prefill_rps,
            "offered_decode_rps_per_peer": self.workload.decode_rps,
            "completed_prefill": counts[Role.PREFILL],
            "completed_decode": counts[Role.DECODE],
            "prefill_rps": counts[Role.PREFILL] / elapsed,
            "decode_rps": counts[Role.DECODE] / elapsed,
            "peers": {
                iid: {
                    "role": role.value,
                    "completed": self.counts[iid],
                    "rps": self.counts[iid] / elapsed,
                    "error": self.errors.get(iid),
                }
                for iid, _, role in self.peers
            },
        }

    async def stop(self) -> dict[str, Any]:
        """Stop the load and return its evidence; raise when a peer failed or completed none."""
        self.elapsed = max(time.monotonic() - self.started, 1e-9)
        await cancel_tasks(self.tasks)
        self._collect_task_errors()
        for iid, count in self.counts.items():
            if count == 0:
                self.errors.setdefault(iid, "completed 0 requests during measurement")
        if self.errors:
            raise RuntimeError("neighbour load failed: " + self._error_detail())
        return self.evidence()
