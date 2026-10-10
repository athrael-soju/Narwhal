import tempfile
import threading
import time
import unittest
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path

import httpx
import msgpack
import zmq

from narwhal.backends import load
from narwhal.config import EngineSpec, FleetConfig
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, build_app
from narwhal.engines.prefix import CacheNamespace, block_identities
from narwhal.engines.residency import ResidencyIndex, cached_prefix_blocks
from narwhal.engines.residency_feed import ResidencyFeed
from narwhal.runtime.residency import ResidencySubscriptions
from narwhal.serving.router.sizing import _hash_prompt
from tests.characterization.golden import assert_golden
from tests.fixtures import ROOT

VLLM_EVENTS = load("vllm").kv_events
decode_batch = VLLM_EVENTS.decode_batch

MODEL, TOKENIZER = "model", "contract"
SIDECAR = "http://sidecar:8010"
ENGINE = "http://engine:8000"
EPOCH = "<epoch>"


def stored(hashes, tokens, *, parent=None, group=0, kind="full_attention", size=4, **extra):
    return {
        "type": "BlockStored",
        "block_hashes": list(hashes),
        "parent_block_hash": parent,
        "token_ids": list(tokens),
        "block_size": size,
        "lora_id": None,
        "medium": "GPU",
        "lora_name": None,
        "group_idx": group,
        "kv_cache_spec_kind": kind,
        **extra,
    }


def removed(hashes, *, group=0, medium="GPU"):
    return {
        "type": "BlockRemoved",
        "block_hashes": list(hashes),
        "group_idx": group,
        "medium": medium,
    }


def batch(*events):
    return msgpack.packb([1.0, list(events), None])


def names(tokens, size=4, **namespace):
    identities = block_identities(CacheNamespace(MODEL, TOKENIZER, **namespace), tokens, size)
    return [i.hex() for i in identities]


def plain(value):
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return "bytes:" + value.hex()
    if isinstance(value, tuple | list):
        return [plain(item) for item in value]
    if is_dataclass(value):
        return {
            "event": type(value).__name__,
            **{f.name: plain(getattr(value, f.name)) for f in fields(value)},
        }
    raise TypeError(type(value).__name__)


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class Publisher:
    def __init__(self, directory, *, replay=True):
        self.endpoint = f"ipc://{directory}/events.sock"
        self.replay_endpoint = f"ipc://{directory}/replay.sock" if replay else None
        self.context = zmq.Context()
        self.pub = self.context.socket(zmq.PUB)
        self.pub.bind(self.endpoint)
        self.buffer = []
        self.requests = []
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        if replay:
            self.router = self.context.socket(zmq.ROUTER)
            self.router.bind(self.replay_endpoint)
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()

    def publish(self, payload, *, deliver=True):
        with self.lock:
            sequence = len(self.buffer)
            self.buffer.append(payload)
            if deliver:
                self.pub.send_multipart([b"", sequence.to_bytes(8, "big"), payload])

    def serve(self):
        while not self.stopped.is_set():
            if not self.router.poll(50):
                continue
            client, *request = self.router.recv_multipart()
            with self.lock:
                self.requests.append([frame.hex() for frame in request])
                start = int.from_bytes(request[-1], "big")
                for sequence, payload in enumerate(self.buffer):
                    if sequence >= start:
                        self.router.send_multipart(
                            [client, b"", sequence.to_bytes(8, "big"), payload]
                        )
                self.router.send_multipart([client, b"", (-1).to_bytes(8, "big", signed=True), b""])

    def close(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
            self.router.close(0)
        self.pub.close(0)
        self.context.term()


class CacheEventDecodingTests(unittest.TestCase):
    def test_event_fields_decode_from_vllm_batches(self):
        events = decode_batch(
            batch(
                stored([b"h1", b"h2"], range(8), extra_keys=[["salt"], None]),
                stored([7], range(8, 12), parent=b"h2", group=1, kind="mla_attention"),
                stored(
                    [8],
                    range(4),
                    group=2,
                    kind="sliding_window",
                    lora_name="adapter",
                    extra_keys=[["adapter"]],
                    kv_cache_spec_sliding_window=8,
                ),
                stored([9], range(4), group=3, kind="mamba", medium="CPU"),
                removed([b"h1"]),
                {"type": "BlockRemoved", "block_hashes": [7]},
                {"type": "AllBlocksCleared"},
                {"type": "FutureEvent"},
            )
        )
        assert_golden(self, "residency_decoded_events", plain(events))

    def test_malformed_batches_are_rejected(self):
        cases = {
            "not msgpack": b"\xc1",
            "map batch": msgpack.packb({"events": []}),
            "short batch": msgpack.packb([1.0]),
            "event not a map": batch([1, 2]),
            "stored without fields": batch({"type": "BlockStored"}),
            "string hashes": batch(stored(["h"], range(4))),
            "float tokens": batch(stored([1], [1.0, 2, 3, 4])),
            "boolean tokens": batch(stored([1], [True, 2, 3, 4])),
            "map parent": batch(stored([1], range(4), parent={"h": 1})),
            "map extra keys": batch(stored([1], range(4), extra_keys={"k": 1})),
            "list LoRA name": batch(stored([1], range(4), lora_name=["adapter"])),
            "removed without hashes": batch({"type": "BlockRemoved"}),
        }
        messages = {}
        for name, payload in cases.items():
            with self.assertRaises(ValueError) as caught:
                decode_batch(payload)
            message = str(caught.exception)
            # msgpack's own wording follows its version; Narwhal's prefix is the contract.
            messages[name] = message.split(":")[0] if name == "not msgpack" else message
        assert_golden(self, "residency_malformed_batches", messages)


class ResidencyIndexTests(unittest.TestCase):
    def apply(self, index, sequence, *events):
        index.apply(sequence, decode_batch(batch(*events)))

    def test_hybrid_groups_salt_adapter_and_media(self):
        index = ResidencyIndex(MODEL, TOKENIZER)
        prompt = tuple(range(12))
        steps = {}
        # vLLM lists the Mamba group before the attention group that names its block.
        self.apply(
            index,
            0,
            stored([3], prompt, group=2, kind="mamba"),
            stored([1, 2, 3], prompt, group=0),
            stored(
                [1, 2, 3], prompt, group=1, kind="sliding_window", kv_cache_spec_sliding_window=8
            ),
            stored([4], range(4), group=0, medium="CPU"),
        )
        steps["hybrid"] = index.snapshot()
        self.apply(
            index,
            1,
            stored([11], range(100, 104), extra_keys=[["salt"]]),
            stored([21], range(200, 204), lora_name="adapter", extra_keys=[["adapter"]]),
            stored([31], range(300, 304), extra_keys=[["image-key"]]),
            stored([41], range(400, 404), parent=40),
        )
        steps["salt_adapter_unnamed"] = index.snapshot()
        self.apply(index, 2, removed([2], group=None), removed([3], medium="CPU"))
        steps["removed"] = index.snapshot()
        steps["changes_after_0"] = asdict(index.changes_after(0))
        self.apply(index, 3, {"type": "AllBlocksCleared"})
        steps["cleared"] = index.snapshot()
        steps["changes_after_2"] = asdict(index.changes_after(2))
        self.apply(index, 4, stored([5], prompt[:4], group=4, size=8))
        steps["mixed_block_size"] = index.snapshot()
        steps["names"] = {
            "prompt": names(prompt),
            "salt": names(range(100, 104), cache_salt="salt"),
            "adapter": names(range(200, 204), adapter="adapter"),
        }
        assert_golden(self, "residency_index_snapshots", steps)

    def test_history_states(self):
        prompt = tuple(range(4))
        cases = {
            "empty": [],
            "late start": [(3, batch(stored([1], prompt)))],
            "gap": [(0, batch(stored([1], prompt))), (2, batch())],
            "unreadable": [(0, b"\xc1")],
            "unknown type": [(0, batch({"type": "FutureEvent"}))],
            "reset after loss": [(3, batch()), (4, batch({"type": "AllBlocksCleared"}))],
            "repeat": [(0, batch(stored([1], prompt))), (0, batch(removed([1])))],
        }
        states = {}
        for name, batches in cases.items():
            index = ResidencyIndex(MODEL, TOKENIZER)
            if not batches:
                index.mark_empty()
            for sequence, payload in batches:
                try:
                    events = decode_batch(payload)
                except ValueError:
                    events = None
                index.apply(sequence, events)
            states[name] = index.snapshot()
        replaying = ResidencyIndex(MODEL, TOKENIZER)
        replaying.mark_empty()
        replaying.set_current(False)
        states["replaying"] = replaying.snapshot()
        assert_golden(self, "residency_history_states", states)


class PrefixMatchTests(unittest.TestCase):
    def test_group_kind_rules(self):
        prompt = [bytes([i]) for i in range(6)]
        full = set(prompt)
        cases = {
            "full": [("full_attention", None, full)],
            "mla": [("mla_attention", None, set(prompt[:4]))],
            "sink": [("sink_full_attention", None, full)],
            "untyped": [(None, None, full)],
            "window 8 trailing": [("sliding_window", 8, set(prompt[3:]))],
            "window 9 trailing": [("sliding_window", 9, set(prompt[3:]))],
            "window mla": [("sliding_window_mla", 4, {prompt[5]})],
            "window without size": [("sliding_window", None, full)],
            "mamba boundary": [("full_attention", None, full), ("mamba", None, {prompt[2]})],
            "mamba only": [("mamba", None, {prompt[4]})],
            "unknown kind": [("future_kind", None, full)],
            "no groups": [],
        }
        assert_golden(
            self,
            "residency_prefix_rules",
            {name: cached_prefix_blocks(groups, prompt, 4) for name, groups in cases.items()},
        )


class SidecarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        contract = FleetConfig.load(ROOT / "tests/data/fleet.json").engine_contract
        self.document = AttestationDocument(contract, dict.fromkeys(contract.fields(), "test"))
        self.identity = EngineIdentity(contract.engine_version, 100.0)
        self.index = ResidencyIndex(MODEL, TOKENIZER)
        self.requests = []

    def engine(self, request):
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": self.identity.version})
        return httpx.Response(200, text="process_start_time_seconds 100.0\n")

    def sidecar(self, index):
        app = build_app(
            self.document,
            ENGINE,
            self.identity,
            transport=httpx.MockTransport(self.engine),
            residency=index,
        )
        transport = httpx.ASGITransport(app=app)

        async def recorded(request):
            self.requests.append(str(request.url))
            return await transport.handle_async_request(request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(recorded), base_url=SIDECAR)
        self.addAsyncCleanup(client.aclose)
        return client

    @staticmethod
    def body(response):
        body = response.json()
        if isinstance(body, dict) and "epoch" in body:
            body["epoch"] = EPOCH
        return {"status": response.status_code, "body": body}

    async def test_residency_routes(self):
        client = self.sidecar(self.index)
        documents = {}
        self.index.mark_empty()
        documents["empty"] = self.body(await client.get("/v1/residency"))
        self.index.apply(0, decode_batch(batch(stored([1, 2], range(8)))))
        self.index.apply(1, decode_batch(batch(removed([2]))))
        documents["snapshot"] = self.body(await client.get("/v1/residency"))
        for after in (-1, 0, 1, 5):
            documents[f"events after {after}"] = self.body(
                await client.get("/v1/residency/events", params={"after": after})
            )
        unread = self.sidecar(None)
        documents["no feed snapshot"] = self.body(await unread.get("/v1/residency"))
        documents["no feed events"] = self.body(
            await unread.get("/v1/residency/events", params={"after": 0})
        )
        assert_golden(self, "residency_sidecar_routes", documents)

    async def test_router_view_and_final_token(self):
        client = self.sidecar(self.index)
        prompt = tuple(range(16))
        self.index.apply(0, decode_batch(batch(stored([1, 2, 3, 4], prompt))))
        specs = [
            EngineSpec("e1", "http://engine", attestation_url=SIDECAR + "/v1/attestation"),
            EngineSpec("e2", "http://engine", attestation_url=SIDECAR + "/attest"),
            EngineSpec("e3", "http://engine"),
        ]
        subscriptions = ResidencySubscriptions(specs)
        await subscriptions.refresh(client)
        await subscriptions.refresh(client)
        namespace = CacheNamespace(MODEL, TOKENIZER)
        matches = {}
        for length in (4, 5, 8, 9, 16, 17):
            identities = _hash_prompt(namespace, list(range(length)), {4})
            cached, sequences, matched = subscriptions.match(identities)
            matches[str(length)] = {
                "hashed_blocks": len(identities[4]),
                "cached_tokens": cached,
                "sequences": sequences,
                "matched_blocks": {str(k): len(v) for k, v in matched.items()},
            }
        state = subscriptions.snapshot()
        for view in state.values():
            if view["epoch"] is not None:
                view["epoch"] = EPOCH
        assert_golden(
            self,
            "residency_router_view",
            {"requests": self.requests, "views": state, "matches": matches},
        )


class ResidencyFeedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)

    def run_feed(self, publisher, index):
        feed = ResidencyFeed(
            index,
            publisher.endpoint,
            publisher.replay_endpoint,
            replay_timeout_s=1,
            poll_s=0.02,
            decoder=VLLM_EVENTS,
        )
        feed.start()
        self.addCleanup(feed.stop)

    def test_replay_and_live_frames(self):
        publisher = Publisher(self.directory)
        self.addCleanup(publisher.close)
        prompt = tuple(range(8))
        publisher.publish(batch(stored([1], prompt[:4])), deliver=False)
        publisher.publish(batch(stored([2], prompt[4:], parent=1)), deliver=False)
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(publisher, index)
        self.assertTrue(wait_for(lambda: index.sequence == 1 and index.snapshot()["known"]))
        states = {"replayed": index.snapshot()}
        publisher.publish(batch(removed([2])), deliver=False)
        publisher.publish(batch())
        self.assertTrue(wait_for(lambda: index.sequence == 3 and index.snapshot()["known"]))
        states["gap recovered"] = index.snapshot()
        with publisher.lock:
            states["replay requests"] = list(publisher.requests)
        assert_golden(self, "residency_feed_replay", states)

    def test_feed_failure_states(self):
        states = {}
        idle = Publisher(self.folder("idle"))
        self.addCleanup(idle.close)
        index = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(idle, index)
        self.assertTrue(wait_for(lambda: index.known))
        states["idle"] = index.snapshot()
        with self.assertLogs("narwhal.residency", level="ERROR"):
            self.assertTrue(
                wait_for(lambda: idle.pub.send_multipart([b"", b"short"]) or not index.known)
            )
        states["short frame"] = index.snapshot()
        silent = Publisher(self.folder("silent"), replay=False)
        self.addCleanup(silent.close)
        blind = ResidencyIndex(MODEL, TOKENIZER)
        self.run_feed(silent, blind)
        self.assertTrue(wait_for(lambda: "replay is unavailable" in blind.reason))
        states["no replay"] = blind.snapshot()
        assert_golden(self, "residency_feed_failures", states)

    def folder(self, name):
        path = self.directory / name
        path.mkdir()
        return path
