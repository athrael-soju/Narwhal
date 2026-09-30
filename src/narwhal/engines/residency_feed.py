"""Keep a residency index current from one vLLM engine's cache-event sockets.

vLLM publishes numbered event batches on a PUB socket and keeps a bounded
buffer of recent batches behind a ROUTER replay socket. It keeps no full
snapshot. The feed subscribes, then replays from sequence 0 so the index
sees the engine's complete history when that history is still buffered.
Later gaps replay from the missing sequence. History the buffer no longer
holds leaves the index unknown until the engine resets its cache.
"""

from __future__ import annotations

import logging
import threading

import zmq

from .kv_events import CacheEvent, decode_batch
from .residency import ResidencyIndex

_END = -1
log = logging.getLogger("narwhal.residency")


def _frames(frames: list[bytes]) -> tuple[bytes, bytes, bytes]:
    """Split one published message into topic, sequence and payload frames."""
    if len(frames) != 3 or len(frames[1]) != 8:
        raise ValueError(f"cache-event message has {len(frames)} frames; expected 3")
    return frames[0], frames[1], frames[2]


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
        max_replay_rounds: int = 100,
    ) -> None:
        self.index = index
        self.endpoint = endpoint
        self.replay_endpoint = replay_endpoint
        self.replay_timeout_s = replay_timeout_s
        self.poll_s = poll_s
        self.max_replay_rounds = max_replay_rounds
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

    def _replay(self, start: int) -> tuple[list[tuple[int, bytes]], bool] | None:
        """Return batches replayed from `start` and whether the end marker arrived.

        None means no replay answered. vLLM's ROUTER socket drops messages past
        its high-water mark, so a long replay can lose batches or its end marker.
        """
        if self.replay_endpoint is None:
            return None
        dealer = self._context.socket(zmq.DEALER)
        dealer.setsockopt(zmq.LINGER, 0)
        dealer.setsockopt(zmq.RCVHWM, 0)
        dealer.connect(self.replay_endpoint)
        try:
            dealer.send_multipart([b"", start.to_bytes(8, "big")])
            batches: list[tuple[int, bytes]] = []
            while True:
                # A long history replays for as long as batches keep arriving.
                if not dealer.poll(int(self.replay_timeout_s * 1000)):
                    return (batches, False) if batches else None
                frames = dealer.recv_multipart()
                if len(frames) < 2:
                    return (batches, False) if batches else None
                sequence = int.from_bytes(frames[-2], "big", signed=True)
                if sequence == _END:
                    return batches, True
                batches.append((sequence, frames[-1]))
        finally:
            dealer.close()

    def _catch_up(self, start: int, until: int | None = None) -> bool | None:
        """Apply buffered batches from `start` in replay rounds.

        Each round applies the contiguous run it received and asks again from
        the first missing batch. Returns False when the buffer was empty, None
        when no replay answered, and True otherwise.
        """
        expected = start
        applied = False
        for _ in range(self.max_replay_rounds):
            result = self._replay(expected)
            if result is None:
                return True if applied else None
            batches, ended = result
            batches = sorted(b for b in batches if until is None or b[0] < until)
            batches = [b for b in batches if b[0] >= expected]
            if not batches:
                if applied:
                    return True
                return False if ended else None
            if batches[0][0] != expected:
                # The buffer no longer holds `expected`; the index records the loss.
                for sequence, payload in batches:
                    self.index.apply(sequence, _decoded(payload))
                return True
            for sequence, payload in batches:
                if sequence != expected:
                    break
                self.index.apply(sequence, _decoded(payload))
                expected += 1
                applied = True
            if ended and expected > batches[-1][0]:
                return True
        return True

    def _drain(self, subscriber: zmq.Socket) -> None:
        """Apply batches that queued on the live socket during replay."""
        while subscriber.poll(0):
            _, raw, payload = _frames(subscriber.recv_multipart())
            self._receive(int.from_bytes(raw, "big"), payload)

    def _receive(self, sequence: int, payload: bytes) -> None:
        last = self.index.sequence
        start = 0 if last is None else last + 1
        if sequence > start:
            # The pre-gap state is stale until the missed batches are applied.
            current = self.index.current
            self.index.set_current(False)
            try:
                self._catch_up(start, until=sequence)
            finally:
                self.index.set_current(current)
        self.index.apply(sequence, _decoded(payload))

    def _run(self) -> None:
        subscriber = self._context.socket(zmq.SUB)
        subscriber.setsockopt(zmq.LINGER, 0)
        subscriber.setsockopt(zmq.RCVHWM, 0)
        subscriber.setsockopt(zmq.SUBSCRIBE, b"")
        try:
            subscriber.connect(self.endpoint)
            # Live batches can arrive during replay; the index ignores repeats.
            self.index.set_current(False)
            history = self._catch_up(0)
            if history is False and not subscriber.poll(0):
                # vLLM's replay buffer only drops old batches, so an empty buffer means none.
                self.index.mark_empty()
            elif history is None:
                self.index.lose("cache-event replay is unavailable")
            self._drain(subscriber)
            self.index.set_current(True)
            while not self._stop.is_set():
                if not subscriber.poll(int(self.poll_s * 1000)):
                    continue
                _, raw, payload = _frames(subscriber.recv_multipart())
                self._receive(int.from_bytes(raw, "big"), payload)
        except Exception as exc:
            # A dead feed must not leave a stale known state behind it.
            if not self._stop.is_set():
                log.exception("cache-event subscription failed")
                self.index.lose(f"cache-event subscription failed: {type(exc).__name__}")
        finally:
            subscriber.close()
