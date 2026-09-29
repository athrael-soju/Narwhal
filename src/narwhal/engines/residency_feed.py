"""Keep a residency index current from one vLLM engine's cache-event sockets.

vLLM publishes numbered event batches on a PUB socket and keeps a bounded
buffer of recent batches behind a ROUTER replay socket. It keeps no full
snapshot. The feed subscribes, then replays from sequence 0 so the index
sees the engine's complete history when that history is still buffered.
Later gaps replay from the missing sequence. History the buffer no longer
holds leaves the index unknown until the engine resets its cache.
"""

from __future__ import annotations

import threading
import time

import zmq

from .kv_events import CacheEvent, decode_batch
from .residency import ResidencyIndex

_END = -1


def _decoded(payload: bytes) -> list[CacheEvent | None] | None:
    try:
        return decode_batch(payload)
    except ValueError:
        return None


class ResidencyFeed:
    """Subscribe to one engine's events and replay missed batches into an index."""

    def __init__(
        self,
        index: ResidencyIndex,
        endpoint: str,
        replay_endpoint: str | None,
        *,
        replay_timeout_s: float = 5.0,
        poll_s: float = 0.2,
    ) -> None:
        self.index = index
        self.endpoint = endpoint
        self.replay_endpoint = replay_endpoint
        self.replay_timeout_s = replay_timeout_s
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="residency-feed", daemon=True)
        self._context = zmq.Context()

    def start(self) -> None:
        """Start the subscription thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop the subscription and release its sockets."""
        self._stop.set()
        self._thread.join(timeout=5)
        self._context.term()

    def _replay(self, start: int) -> list[tuple[int, bytes]] | None:
        """Return buffered batches from `start`, or None when replay is unavailable."""
        if self.replay_endpoint is None:
            return None
        dealer = self._context.socket(zmq.DEALER)
        dealer.setsockopt(zmq.LINGER, 0)
        dealer.connect(self.replay_endpoint)
        try:
            dealer.send_multipart([b"", start.to_bytes(8, "big")])
            batches: list[tuple[int, bytes]] = []
            deadline = time.monotonic() + self.replay_timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not dealer.poll(int(remaining * 1000)):
                    return None
                frames = dealer.recv_multipart()
                if len(frames) < 2:
                    return None
                sequence = int.from_bytes(frames[-2], "big", signed=True)
                if sequence == _END:
                    return batches
                batches.append((sequence, frames[-1]))
        finally:
            dealer.close()

    def _apply(self, batches: list[tuple[int, bytes]]) -> None:
        for sequence, payload in sorted(batches):
            self.index.apply(sequence, _decoded(payload))

    def _run(self) -> None:
        subscriber = self._context.socket(zmq.SUB)
        subscriber.setsockopt(zmq.LINGER, 0)
        subscriber.setsockopt(zmq.SUBSCRIBE, b"")
        subscriber.connect(self.endpoint)
        try:
            # Live batches can arrive during replay; ordering by sequence merges both.
            history = self._replay(0)
            pending: list[tuple[int, bytes]] = []
            while subscriber.poll(0):
                _, raw, payload = subscriber.recv_multipart()
                pending.append((int.from_bytes(raw, "big"), payload))
            if history == [] and not pending:
                # vLLM's replay buffer only drops old batches, so an empty buffer means none.
                self.index.mark_empty()
            elif history is None and not pending:
                self.index.lose("cache-event replay is unavailable")
            self._apply((history or []) + pending)
            while not self._stop.is_set():
                if not subscriber.poll(int(self.poll_s * 1000)):
                    continue
                _, raw, payload = subscriber.recv_multipart()
                sequence = int.from_bytes(raw, "big")
                last = self.index.sequence
                if last is not None and sequence > last + 1:
                    missed = self._replay(last + 1)
                    if missed is not None:
                        self._apply([batch for batch in missed if batch[0] < sequence])
                self.index.apply(sequence, _decoded(payload))
        except zmq.ZMQError as exc:
            if not self._stop.is_set():
                self.index.lose(f"cache-event subscription failed: {exc}")
        finally:
            subscriber.close()
