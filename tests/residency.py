import threading
import time
from collections import deque

import zmq

from narwhal.engines.residency_feed import ResidencyFeed


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class FakePublisher:
    def __init__(self, directory, *, buffer=100, replay=True, replay_gap_s=0.0, replay_limit=None):
        self.replay_gap_s = replay_gap_s
        # Emulate a ROUTER that drops a long replay's tail and its end marker.
        self.replay_limit = replay_limit
        self.endpoint = f"ipc://{directory}/events.sock"
        self.replay_endpoint = f"ipc://{directory}/replay.sock" if replay else None
        self.context = zmq.Context()
        self.pub = self.context.socket(zmq.PUB)
        self.pub.bind(self.endpoint)
        self.buffer = deque(maxlen=buffer)
        self.sequence = 0
        self.requests = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        if replay:
            self.router = self.context.socket(zmq.ROUTER)
            self.router.bind(self.replay_endpoint)
            self.thread = threading.Thread(target=self.serve_replay, daemon=True)
            self.thread.start()

    def publish(self, payload, *, deliver=True):
        with self.lock:
            sequence = self.sequence
            self.sequence += 1
            self.buffer.append((sequence, payload))
            if deliver:
                self.pub.send_multipart([b"", sequence.to_bytes(8, "big"), payload])

    def serve_replay(self):
        while not self.stopped.is_set():
            if not self.router.poll(50):
                continue
            client, *request = self.router.recv_multipart()
            with self.lock:
                self.requests.append([frame.hex() for frame in request])
                start = int.from_bytes(request[-1], "big")
                sent = 0
                for sequence, payload in list(self.buffer):
                    if sequence >= start:
                        if self.replay_limit is not None and sent == self.replay_limit:
                            break
                        time.sleep(self.replay_gap_s)
                        self.router.send_multipart(
                            [client, b"", sequence.to_bytes(8, "big"), payload]
                        )
                        sent += 1
                else:
                    self.router.send_multipart(
                        [client, b"", (-1).to_bytes(8, "big", signed=True), b""]
                    )

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
            self.router.close(0)
        self.pub.close(0)
        self.context.term()


def run_feed(case, publisher, index, decoder, *, replay_timeout_s=1):
    feed = ResidencyFeed(
        index,
        publisher.endpoint,
        publisher.replay_endpoint,
        replay_timeout_s=replay_timeout_s,
        poll_s=0.02,
        decoder=decoder,
    )
    feed.start()
    case.addCleanup(feed.stop)
