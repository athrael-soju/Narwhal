"""Check the Kubernetes API client, informer and workqueue against stubbed API servers."""

import http.server
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.runtime import kubernetes_api as k

SERVER = "https://10.96.0.1:443"
NAMESPACE = "fleet-a"
LEASES = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases"
JOBS = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs"
QUICK = k.Backoff(base_s=0.5, cap_s=8.0, attempts=5, jitter=0.0)


def status(code, reason, message="refused"):
    """A Kubernetes `Status` failure response."""
    body = {"kind": "Status", "apiVersion": "v1", "status": "Failure"}
    body |= {"code": code, "reason": reason, "message": message}
    return httpx.Response(code, json=body)


def stream(*events):
    """A watch response holding one JSON event per line."""
    return httpx.Response(200, content="\n".join(json.dumps(e) for e in events).encode())


def job(name, version):
    return {"metadata": {"name": name, "namespace": NAMESPACE, "resourceVersion": version}}


def event(kind, name, version):
    return {"type": kind, "object": job(name, version)}


def bookmark(version):
    return {"type": "BOOKMARK", "object": {"kind": "Job", "metadata": {"resourceVersion": version}}}


class Rig:
    """An `ApiClient` whose requests reach `handler`, with recorded sleeps."""

    def __init__(self, handler, *, token=None, backoff=QUICK, field_manager="narwhal-controller"):
        self.requests = []
        self.sleeps = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        self.client = k.ApiClient(
            k.Connection(SERVER, NAMESPACE, token=token),
            field_manager=field_manager,
            backoff=backoff,
            transport=httpx.MockTransport(record),
            sleep=self.sleeps.append,
        )


class FakeLeases:
    """An object store for Leases with resourceVersions and optimistic concurrency."""

    def __init__(self):
        self.objects = {}
        self.version = 0

    def __call__(self, request):
        name = request.url.path.removeprefix(LEASES).strip("/")
        if request.method == "GET":
            if name not in self.objects:
                return status(404, "NotFound", f'leases "{name}" not found')
            return httpx.Response(200, json=self.objects[name])
        body = json.loads(request.content)
        name = name or body["metadata"]["name"]
        current = self.objects.get(name)
        if request.method == "POST":
            if current is not None:
                return status(409, "AlreadyExists", f'leases "{name}" already exists')
        elif request.method == "PUT":
            if current is None:
                return status(404, "NotFound", f'leases "{name}" not found')
            if body["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
                return status(409, "Conflict", "the object has been modified")
        self.version += 1
        body["metadata"]["resourceVersion"] = str(self.version)
        self.objects[name] = body
        return httpx.Response(201 if request.method == "POST" else 200, json=body)


def lease(holder, version=None):
    metadata = {"name": "narwhal-controller"}
    if version is not None:
        metadata["resourceVersion"] = version
    return {"metadata": metadata, "spec": {"holderIdentity": holder, "leaseDurationSeconds": 15}}


class ListTests(unittest.TestCase):
    def test_a_list_reads_every_page_and_keeps_the_snapshot_version(self):
        def handler(request):
            if request.url.params.get("continue") == "page-2":
                return httpx.Response(
                    200, json={"metadata": {"resourceVersion": "7"}, "items": [job("b", "6")]}
                )
            meta = {"resourceVersion": "7", "continue": "page-2"}
            return httpx.Response(200, json={"metadata": meta, "items": [job("a", "5")]})

        rig = Rig(handler)
        listing = rig.client.list(k.JOB, labels={"app.kubernetes.io/name": "narwhal"})

        self.assertEqual([item["metadata"]["name"] for item in listing.items], ["a", "b"])
        self.assertEqual(listing.resource_version, "7")
        self.assertEqual(listing.items[0]["kind"], "Job")
        first, second = (request.url.params for request in rig.requests)
        self.assertEqual(first["limit"], "500")
        self.assertNotIn("continue", first)
        self.assertEqual(second["continue"], "page-2")
        self.assertEqual(second["labelSelector"], "app.kubernetes.io/name=narwhal")

    def test_an_expired_continue_token_lists_again_from_the_first_page(self):
        pages = iter(
            [
                httpx.Response(
                    200,
                    json={"metadata": {"resourceVersion": "7", "continue": "c"}, "items": []},
                ),
                status(410, "Expired", "the provided continue parameter is too old"),
                httpx.Response(200, json={"metadata": {"resourceVersion": "9"}, "items": []}),
            ]
        )
        rig = Rig(lambda request: next(pages))

        self.assertEqual(rig.client.list(k.JOB).resource_version, "9")
        self.assertNotIn("continue", rig.requests[2].url.params)

    def test_a_continue_token_that_keeps_expiring_raises_gone(self):
        def handler(request):
            if "continue" in request.url.params:
                return status(410, "Expired")
            meta = {"resourceVersion": "7", "continue": "c"}
            return httpx.Response(200, json={"metadata": meta, "items": []})

        with self.assertRaises(k.Gone):
            Rig(handler).client.list(k.JOB)

    def test_a_selector_that_is_not_a_plain_equality_is_refused_before_any_request(self):
        rig = Rig(lambda request: self.fail("no request expected"))
        for labels in ({"app": "a,b"}, {"app": "a=b"}, {"a b": "c"}, {"app": "x!"}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                rig.client.list(k.JOB, labels=labels)


class WatchTests(unittest.TestCase):
    def test_a_watch_asks_for_bookmarks_and_outlasts_its_server_timeout(self):
        rig = Rig(lambda request: stream(event("ADDED", "a", "8"), bookmark("12")))

        events = list(
            rig.client.watch(k.JOB, resource_version="7", labels={"app": "x"}, timeout_s=300)
        )

        self.assertEqual([e.type for e in events], ["ADDED", "BOOKMARK"])
        params = rig.requests[0].url.params
        self.assertEqual(params["watch"], "true")
        self.assertEqual(params["allowWatchBookmarks"], "true")
        self.assertEqual(params["resourceVersion"], "7")
        self.assertEqual(params["timeoutSeconds"], "300")
        self.assertEqual(params["labelSelector"], "app=x")
        self.assertGreater(rig.requests[0].extensions["timeout"]["read"], 300)

    def test_an_expired_resource_version_raises_gone_as_an_event_or_a_response(self):
        expired = {"type": "ERROR", "object": {"kind": "Status", "code": 410, "reason": "Expired"}}
        for response in (stream(expired), status(410, "Gone", "too old resource version")):
            with self.subTest(response=response.status_code):
                rig = Rig(lambda request, response=response: response)
                with self.assertRaises(k.Gone):
                    list(rig.client.watch(k.JOB, resource_version="7"))

    def test_a_malformed_event_raises(self):
        for line in (b"not json", b'{"type": "ADDED"}', b'{"type": "WHAT", "object": {}}'):
            with self.subTest(line=line):
                rig = Rig(lambda request, line=line: httpx.Response(200, content=line))
                with self.assertRaises(k.KubernetesError):
                    list(rig.client.watch(k.JOB, resource_version="7"))

    def test_a_watch_needs_the_resource_version_of_a_list(self):
        with self.assertRaises(ValueError):
            list(Rig(lambda request: stream()).client.watch(k.JOB, resource_version=""))


class ScriptedApi:
    """Answers lists and watches from separate scripts, in order."""

    def __init__(self, lists, watches):
        self.lists = list(lists)
        self.watches = list(watches)

    def __call__(self, request):
        script = self.watches if request.url.params.get("watch") else self.lists
        return script.pop(0)


def listed(version, *items):
    return httpx.Response(
        200, json={"metadata": {"resourceVersion": version}, "items": list(items)}
    )


class InformerTests(unittest.TestCase):
    def informer(self, api, **options):
        self.rig = Rig(api)
        self.heard = []
        self.now = 0.0
        return k.Informer(
            self.rig.client,
            k.JOB,
            handler=lambda e: self.heard.append((e.type, e.object["metadata"].get("name"))),
            clock=lambda: self.now,
            rng=lambda: 0.0,
            **options,
        )

    def watched_versions(self):
        return [
            request.url.params["resourceVersion"]
            for request in self.rig.requests
            if request.url.params.get("watch")
        ]

    def test_a_watch_resumes_from_its_last_bookmark(self):
        api = ScriptedApi(
            [listed("10", job("a", "9"))],
            [
                stream(event("MODIFIED", "a", "11"), bookmark("20")),
                stream(event("ADDED", "b", "21")),
            ],
        )
        informer = self.informer(api)

        informer.pump()
        informer.pump()

        self.assertEqual(self.watched_versions(), ["10", "20"])
        self.assertEqual(informer.resource_version, "21")
        self.assertEqual(self.heard, [("ADDED", "a"), ("MODIFIED", "a"), ("ADDED", "b")])
        self.assertEqual(informer.get(f"{NAMESPACE}/a")["metadata"]["resourceVersion"], "11")
        self.assertEqual(len(api.lists), 0)

    def test_a_410_relists_and_reports_what_the_lost_watch_missed(self):
        expired = {"type": "ERROR", "object": {"kind": "Status", "code": 410, "reason": "Expired"}}
        api = ScriptedApi(
            [
                listed("10", job("a", "9"), job("b", "9"), job("c", "9")),
                listed("40", job("a", "9"), job("b", "33"), job("d", "35")),
            ],
            [stream(expired), stream(event("DELETED", "a", "41"))],
        )
        informer = self.informer(api)

        informer.pump()

        self.assertEqual(
            self.heard,
            [
                ("ADDED", "a"),
                ("ADDED", "b"),
                ("ADDED", "c"),
                ("MODIFIED", "b"),
                ("ADDED", "d"),
                ("DELETED", "c"),
                ("DELETED", "a"),
            ],
        )
        self.assertEqual(self.watched_versions(), ["10", "40"])
        self.assertEqual([item["metadata"]["name"] for item in informer.items()], ["b", "d"])

    def test_a_resync_replays_the_cache_without_reading_the_api(self):
        api = ScriptedApi([listed("10", job("a", "9"), job("b", "9"))], [stream(), stream()])
        informer = self.informer(api, resync_s=60)
        informer.pump()
        self.heard.clear()
        requests = len(self.rig.requests)

        self.now = 59.0
        self.assertFalse(informer.resync())
        self.now = 60.0
        self.assertTrue(informer.resync())

        self.assertEqual(self.heard, [("SYNC", "a"), ("SYNC", "b")])
        self.assertEqual(len(self.rig.requests), requests)
        self.assertFalse(informer.resync())

    def test_the_cache_hands_out_copies(self):
        informer = self.informer(ScriptedApi([listed("10", job("a", "9"))], [stream()]))
        informer.pump()

        informer.get(f"{NAMESPACE}/a")["metadata"]["name"] = "changed"
        informer.items()[0]["metadata"]["resourceVersion"] = "0"

        self.assertEqual(informer.get(f"{NAMESPACE}/a")["metadata"], job("a", "9")["metadata"])

    def test_run_backs_off_after_a_failure_and_stops_when_told(self):
        stop = threading.Event()
        calls = []

        class Failing:
            def list(self, kind, **options):
                calls.append("list")
                if len(calls) == 2:
                    stop.set()
                raise k.Unavailable(503, "ServiceUnavailable", "down")

        informer = k.Informer(Failing(), k.JOB, backoff=k.Backoff(base_s=0.001, cap_s=0.001))
        informer.run(stop)

        self.assertEqual(calls, ["list", "list"])
        self.assertFalse(informer.synced)


class RetryTests(unittest.TestCase):
    def test_a_429_waits_for_its_retry_after(self):
        replies = iter([httpx.Response(429, headers={"Retry-After": "3"}), status(404, "NotFound")])
        rig = Rig(lambda request: next(replies))

        self.assertIsNone(rig.client.get(k.LEASE, "narwhal-controller"))
        self.assertEqual(rig.sleeps, [3.0])

    def test_a_retry_after_is_capped(self):
        replies = iter(
            [httpx.Response(429, headers={"Retry-After": "600"}), status(404, "NotFound")]
        )
        rig = Rig(lambda request: next(replies))
        rig.client.get(k.LEASE, "narwhal-controller")
        self.assertEqual(rig.sleeps, [8.0])

    def test_5xx_backs_off_exponentially_then_gives_up_as_unavailable(self):
        rig = Rig(lambda request: httpx.Response(503))

        with self.assertRaises(k.Unavailable) as caught:
            rig.client.get(k.LEASE, "narwhal-controller")

        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(len(rig.requests), 5)
        self.assertEqual(rig.sleeps, [0.5, 1.0, 2.0, 4.0])

    def test_a_server_that_recovers_answers_the_same_call(self):
        replies = iter([httpx.Response(502), httpx.Response(500), status(404, "NotFound")])
        rig = Rig(lambda request: next(replies))
        self.assertIsNone(rig.client.get(k.LEASE, "narwhal-controller"))
        self.assertEqual(rig.sleeps, [0.5, 1.0])

    def test_jitter_only_shortens_a_delay(self):
        backoff = k.Backoff(base_s=1.0, cap_s=30.0, jitter=0.5)
        self.assertEqual(backoff.delay(2, rng=lambda: 0.0), 4.0)
        self.assertEqual(backoff.delay(2, rng=lambda: 1.0), 2.0)
        self.assertEqual(backoff.delay(40, rng=lambda: 0.0), 30.0)

    def test_a_json_patch_retries_a_429_but_not_a_5xx(self):
        operations = [{"op": "replace", "path": "/spec/x", "value": 1}]
        rig = Rig(lambda request: httpx.Response(503))
        with self.assertRaises(k.Unavailable):
            rig.client.patch(k.JOB, "a", operations)
        self.assertEqual(len(rig.requests), 1)

        replies = iter([httpx.Response(429), httpx.Response(200, json=job("a", "2"))])
        rig = Rig(lambda request: next(replies))
        rig.client.patch(k.JOB, "a", operations)
        self.assertEqual(len(rig.requests), 2)

    def test_an_apply_retries_a_5xx(self):
        replies = iter([httpx.Response(504), httpx.Response(200, json=job("a", "2"))])
        rig = Rig(lambda request: next(replies))
        rig.client.apply(k.JOB, {"metadata": {"name": "a"}})
        self.assertEqual(len(rig.requests), 2)

    def test_a_request_that_never_connected_is_retried_for_any_verb(self):
        replies = iter([httpx.ConnectError("refused"), httpx.Response(201, json=lease("a", "1"))])

        def handler(request):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        rig = Rig(handler)
        rig.client.create(k.LEASE, lease("a"))
        self.assertEqual(len(rig.requests), 2)

    def test_a_write_whose_reply_was_lost_is_not_retried(self):
        def handler(request):
            raise httpx.ReadTimeout("no reply")

        rig = Rig(handler)
        with self.assertRaises(k.Unavailable):
            rig.client.create(k.LEASE, lease("a"))
        self.assertEqual(len(rig.requests), 1)

        rig = Rig(handler)
        with self.assertRaises(k.Unavailable):
            rig.client.get(k.LEASE, "a")
        self.assertEqual(len(rig.requests), 5)


class TokenTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "token"
        self.path.write_text("first\n")
        self.now = 0.0
        self.token = k.ServiceAccountToken(self.path, clock=lambda: self.now)
        self.accepted = "Bearer first"
        self.sent = []

    def handler(self, request):
        self.sent.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != self.accepted:
            return status(401, "Unauthorized", "Unauthorized")
        return status(404, "NotFound")

    def test_a_401_reads_a_rotated_token_and_sends_the_request_again(self):
        rig = Rig(self.handler, token=self.token)
        rig.client.get(k.LEASE, "narwhal-controller")

        self.path.write_text("second\n")
        self.accepted = "Bearer second"
        self.assertIsNone(rig.client.get(k.LEASE, "narwhal-controller"))

        self.assertEqual(self.sent, ["Bearer first", "Bearer first", "Bearer second"])

    def test_a_401_with_an_unchanged_token_is_not_sent_again(self):
        self.accepted = "Bearer other"
        rig = Rig(self.handler, token=self.token)

        with self.assertRaises(k.KubernetesError) as caught:
            rig.client.get(k.LEASE, "narwhal-controller")

        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.sent, ["Bearer first"])

    def test_the_token_is_read_again_once_a_minute_without_waiting_for_a_401(self):
        rig = Rig(
            lambda request: (
                self.sent.append(request.headers["authorization"]) or status(404, "NotFound")
            ),
            token=self.token,
        )
        rig.client.get(k.LEASE, "narwhal-controller")
        self.path.write_text("second\n")

        self.now = 59.0
        rig.client.get(k.LEASE, "narwhal-controller")
        self.now = 60.0
        rig.client.get(k.LEASE, "narwhal-controller")

        self.assertEqual(self.sent, ["Bearer first", "Bearer first", "Bearer second"])

    def test_the_token_goes_only_to_the_api_server(self):
        transport = k._Transport(
            httpx.MockTransport(self.handler), httpx.URL(SERVER), self.token, QUICK, lambda s: None
        )
        with httpx.Client(transport=transport) as client:
            client.get("https://elsewhere.example:443/", headers={"Authorization": "Bearer x"})
            client.get("http://10.96.0.1:443/")
            client.get(f"{SERVER}/api")

        self.assertEqual(self.sent, [None, None, "Bearer first"])

    def test_a_token_is_never_sent_over_plain_http(self):
        with self.assertRaises(ValueError):
            k.ApiClient(
                k.Connection("http://10.96.0.1:80", NAMESPACE, token=self.token),
                field_manager="narwhal-controller",
            )

    def test_an_unusable_token_file_is_refused(self):
        for content in (b"", b"  \n", b"two words", b"x" * (k.TOKEN_MAX_BYTES + 1)):
            with self.subTest(content=content[:12]):
                self.path.write_bytes(content)
                with self.assertRaises(k.InClusterConfigError):
                    k.ServiceAccountToken(self.path).value()
        self.path.unlink()
        with self.assertRaises(k.InClusterConfigError):
            k.ServiceAccountToken(self.path).value()

    def test_the_token_stays_out_of_reprs(self):
        connection = k.Connection(SERVER, NAMESPACE, token=self.token)
        self.token.value()
        self.assertNotIn("first", repr(connection))
        self.assertNotIn("first", repr(self.token))


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def make_ca(folder, name):
    openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", f"{name}.key", "-out", f"{name}.crt", "-days", "1", "-subj", f"/CN={name}",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        cwd=folder,
    )  # fmt: skip


def make_server_cert(folder, ca):
    (folder / "server.ext").write_text(
        "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n"
    )
    openssl(
        "req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=kubernetes",
        cwd=folder,
    )  # fmt: skip
    openssl(
        "x509", "-req", "-in", "server.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key",
        "-CAcreateserial", "-out", "server.crt", "-days", "1", "-extfile", "server.ext",
        cwd=folder,
    )  # fmt: skip


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make test certificates")
class InClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.certs = Path(cls._folder.name)
        make_ca(cls.certs, "cluster-ca")
        make_ca(cls.certs, "other-ca")
        make_server_cert(cls.certs, "cluster-ca")

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.account = Path(folder.name)
        (self.account / "token").write_text("projected-token\n")
        (self.account / "namespace").write_text(NAMESPACE)
        shutil.copy(self.certs / "cluster-ca.crt", self.account / "ca.crt")
        self.env = {"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "443"}

    def test_a_pod_connects_to_its_service_over_tls_with_the_mounted_namespace(self):
        connection = k.in_cluster(self.account, self.env)

        self.assertEqual(connection.server, "https://10.96.0.1:443")
        self.assertEqual(connection.namespace, NAMESPACE)
        self.assertEqual(connection.token.value(), "projected-token")
        ipv6 = k.in_cluster(self.account, self.env | {"KUBERNETES_SERVICE_HOST": "fd00::1"})
        self.assertEqual(ipv6.server, "https://[fd00::1]:443")

    def test_the_cluster_ca_is_the_only_trusted_root_and_names_are_checked(self):
        # Were the default roots loaded, OpenSSL would read this file into the store.
        defaults = {"SSL_CERT_FILE": str(self.certs / "other-ca.crt"), "SSL_CERT_DIR": ""}
        with patch.dict(os.environ, defaults):
            context = k.in_cluster(self.account, self.env).ssl_context

        roots = context.get_ca_certs()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["subject"], ((("commonName", "cluster-ca"),),))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_missing_or_broken_mount_is_refused_instead_of_trusting_other_roots(self):
        cases = {
            "no CA": lambda: (self.account / "ca.crt").unlink(),
            "garbage CA": lambda: (self.account / "ca.crt").write_text("not a certificate"),
            "no token": lambda: (self.account / "token").unlink(),
            "no namespace": lambda: (self.account / "namespace").unlink(),
            "bad namespace": lambda: (self.account / "namespace").write_text("Not/A/Name"),
        }
        for name, breakage in cases.items():
            with self.subTest(name):
                self.setUp()
                breakage()
                with self.assertRaises(k.InClusterConfigError):
                    k.in_cluster(self.account, self.env)
        for env in ({}, {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}):
            with self.subTest(env=env), self.assertRaises(k.InClusterConfigError):
                k.in_cluster(self.account, env)

    def serve(self):
        """An HTTPS server with a certificate the cluster CA signed, recording each token."""
        tokens = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                tokens.append(self.headers.get("Authorization"))
                body = json.dumps(lease("a", "3")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certs / "server.crt", self.certs / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.server_address[1], tokens

    def client(self, host, port, ca):
        sleeps = []
        connection = k.Connection(
            f"https://{host}:{port}",
            NAMESPACE,
            k.cluster_ssl_context(ca),
            k.ServiceAccountToken(self.account / "token"),
        )
        client = k.ApiClient(connection, field_manager="narwhal-controller", sleep=sleeps.append)
        self.addCleanup(client.close)
        return client, sleeps

    def test_tls_verifies_against_the_cluster_ca_and_refuses_others_at_once(self):
        port, tokens = self.serve()

        client, _ = self.client("127.0.0.1", port, self.certs / "cluster-ca.crt")
        self.assertEqual(client.get(k.LEASE, "a")["spec"]["holderIdentity"], "a")
        self.assertEqual(tokens, ["Bearer projected-token"])

        for host, ca in (("127.0.0.1", "other-ca.crt"), ("localhost", "cluster-ca.crt")):
            with self.subTest(host=host, ca=ca):
                client, sleeps = self.client(host, port, self.certs / ca)
                with self.assertRaises(k.Unavailable):
                    client.get(k.LEASE, "a")
                self.assertEqual(sleeps, [])
        self.assertEqual(len(tokens), 1)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(lambda request: httpx.Response(200, json=job("a", "2")))

    def test_an_apply_is_a_server_side_apply_as_the_named_field_manager(self):
        self.rig.client.apply(k.JOB, {"metadata": {"name": "a"}, "spec": {"parallelism": 1}})

        request = self.rig.requests[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.url.path, f"{JOBS}/a")
        self.assertEqual(request.headers["content-type"], "application/apply-patch+yaml")
        self.assertEqual(request.url.params["fieldManager"], "narwhal-controller")
        self.assertNotIn("force", request.url.params)
        body = json.loads(request.content)
        self.assertEqual((body["apiVersion"], body["kind"]), ("batch/v1", "Job"))

    def test_force_dry_run_and_status_change_only_their_parameters(self):
        self.rig.client.apply(k.JOB, {"metadata": {"name": "a"}}, force=True, dry_run=True)
        self.rig.client.apply(k.JOB, {"metadata": {"name": "a"}}, status=True)

        forced, on_status = self.rig.requests
        self.assertEqual(forced.url.params["force"], "true")
        self.assertEqual(forced.url.params["dryRun"], "All")
        self.assertEqual(on_status.url.path, f"{JOBS}/a/status")
        self.assertEqual(on_status.url.params["fieldManager"], "narwhal-controller")

    def test_every_write_names_the_field_manager(self):
        self.rig.client.patch(k.JOB, "a", {"metadata": {"labels": {"x": "y"}}}, patch_type="merge")
        self.rig.client.create(k.JOB, {"metadata": {"name": "a"}})
        self.rig.client.replace(k.JOB, job("a", "1"))

        for request in self.rig.requests:
            self.assertEqual(request.url.params["fieldManager"], "narwhal-controller")
        self.assertEqual(
            self.rig.requests[0].headers["content-type"], "application/merge-patch+json"
        )

    def test_a_body_of_another_kind_or_without_a_name_is_refused(self):
        bodies = (
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "a"}},
            {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "a"}},
            {"metadata": {}},
            {},
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.rig.client.apply(k.JOB, body)
        with self.assertRaises(ValueError):
            self.rig.client.patch(k.JOB, "a", {"not": "a list"})
        self.assertEqual(self.rig.requests, [])

    def test_the_field_manager_is_required(self):
        for name in ("", "x" * 129):
            with self.subTest(length=len(name)), self.assertRaises(ValueError):
                Rig(lambda request: None, field_manager=name)


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(lambda request: httpx.Response(200, json={"kind": "Status"}))

    def test_a_job_delete_propagates_in_the_background(self):
        self.assertTrue(self.rig.client.delete(k.JOB, "gate-1"))

        request = self.rig.requests[0]
        self.assertEqual((request.method, request.url.path), ("DELETE", f"{JOBS}/gate-1"))
        self.assertEqual(request.url.params["propagationPolicy"], "Background")

    def test_a_job_delete_that_would_orphan_or_wait_is_refused_before_any_request(self):
        for policy in ("Orphan", "Foreground"):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.rig.client.delete(k.JOB, "gate-1", propagation=policy)
        same_kind = k.Kind("batch", "v1", "Job", "jobs")
        with self.assertRaises(ValueError):
            self.rig.client.delete(same_kind, "gate-1", propagation="Orphan")
        self.assertEqual(self.rig.requests, [])

    def test_other_kinds_default_to_background_and_may_choose(self):
        self.rig.client.delete(k.CONFIG_MAP, "hooks")
        self.rig.client.delete(k.NODE, "n1", propagation="Orphan")

        first, second = self.rig.requests
        self.assertEqual(first.url.params["propagationPolicy"], "Background")
        self.assertEqual(second.url.path, "/api/v1/nodes/n1")
        self.assertEqual(second.url.params["propagationPolicy"], "Orphan")
        with self.assertRaises(ValueError):
            self.rig.client.delete(k.CONFIG_MAP, "hooks", propagation="Later")

    def test_an_object_already_gone_is_reported_not_raised(self):
        rig = Rig(lambda request: status(404, "NotFound"))
        self.assertFalse(rig.client.delete(k.JOB, "gate-1"))


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeLeases()
        self.rig = Rig(self.store)
        self.client = self.rig.client

    def test_a_missing_lease_reads_as_none(self):
        self.assertIsNone(self.client.get(k.LEASE, "narwhal-controller"))

    def test_a_second_create_meets_already_exists(self):
        created = self.client.create(k.LEASE, lease("a"))
        self.assertEqual(created["metadata"]["resourceVersion"], "1")

        with self.assertRaises(k.Conflict) as caught:
            self.client.create(k.LEASE, lease("b"))

        self.assertEqual(caught.exception.reason, "AlreadyExists")
        self.assertEqual(self.store.objects["narwhal-controller"]["spec"]["holderIdentity"], "a")

    def test_of_two_updates_from_one_read_only_the_first_lands(self):
        self.client.create(k.LEASE, lease("a"))
        read_a = self.client.get(k.LEASE, "narwhal-controller")
        read_b = self.client.get(k.LEASE, "narwhal-controller")

        read_a["spec"]["holderIdentity"] = "a"
        renewed = self.client.replace(k.LEASE, read_a)
        read_b["spec"]["holderIdentity"] = "b"
        with self.assertRaises(k.Conflict) as caught:
            self.client.replace(k.LEASE, read_b)

        self.assertEqual(caught.exception.reason, "Conflict")
        self.assertEqual(renewed["metadata"]["resourceVersion"], "2")
        self.assertEqual(self.store.objects["narwhal-controller"]["spec"]["holderIdentity"], "a")

    def test_an_update_without_the_version_it_read_is_refused_before_any_request(self):
        self.client.create(k.LEASE, lease("a"))
        sent = len(self.rig.requests)

        with self.assertRaises(ValueError):
            self.client.replace(k.LEASE, lease("b"))

        self.assertEqual(len(self.rig.requests), sent)

    def test_an_update_of_a_deleted_lease_is_not_found(self):
        with self.assertRaises(k.NotFound):
            self.client.replace(k.LEASE, lease("a", "1"))


class WorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.queue = k.WorkQueue(clock=lambda: self.now)

    def test_a_burst_of_adds_is_one_reconcile(self):
        for _ in range(3):
            self.queue.add("fleet-a/fleet")
        self.queue.add("fleet-b/fleet")

        self.assertEqual(len(self.queue), 2)
        self.assertEqual(self.queue.get(0), "fleet-a/fleet")
        self.assertEqual(self.queue.get(0), "fleet-b/fleet")
        self.assertIsNone(self.queue.get(0))

    def test_a_key_added_while_held_runs_again_only_after_done(self):
        self.queue.add("a")
        held = self.queue.get(0)
        self.queue.add("a")
        self.queue.add("a")

        self.assertIsNone(self.queue.get(0))
        self.queue.done(held)
        self.assertEqual(self.queue.get(0), "a")
        self.queue.done("a")
        self.assertIsNone(self.queue.get(0))

    def test_a_delayed_key_waits_for_the_clock_and_the_sooner_delay_wins(self):
        self.queue.add_after("a", 10)
        self.queue.add_after("a", 30)
        self.queue.add_after("b", 5)
        self.queue.add_after("b", 2)

        self.now = 2.0
        self.assertEqual(self.queue.get(0), "b")
        self.assertIsNone(self.queue.get(0))
        self.now = 10.0
        self.assertEqual(self.queue.get(0), "a")
        self.now = 30.0
        self.assertIsNone(self.queue.get(0))

    def test_an_add_replaces_a_pending_delay(self):
        self.queue.add_after("a", 10)
        self.queue.add("a")
        self.assertEqual(self.queue.get(0), "a")
        self.queue.done("a")
        self.now = 10.0
        self.assertIsNone(self.queue.get(0))

    def test_failures_back_off_exponentially_until_forgotten(self):
        due = []
        for _ in range(3):
            self.queue.add_rate_limited("a")
            start = self.now
            while self.queue.get(0) is None:
                self.now += 0.5
            due.append(self.now - start)
            self.queue.done("a")

        self.assertEqual(due, [1.0, 2.0, 4.0])
        self.assertEqual(self.queue.failures("a"), 3)
        self.queue.forget("a")
        self.assertEqual(self.queue.failures("a"), 0)

    def test_a_waiting_worker_wakes_for_an_add_and_for_shutdown(self):
        queue = k.WorkQueue()
        got = []
        worker = threading.Thread(target=lambda: got.extend([queue.get(10), queue.get(10)]))
        worker.start()
        queue.add("a")
        while queue.__len__() and worker.is_alive():
            threading.Event().wait(0.01)
        queue.shutdown()
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(got, ["a", None])
        queue.add("b")
        self.assertIsNone(queue.get(0))


class KeyTests(unittest.TestCase):
    def test_keys_follow_client_go(self):
        self.assertEqual(k.key(job("a", "1")), f"{NAMESPACE}/a")
        self.assertEqual(k.key({"metadata": {"name": "n1"}}), "n1")

    def test_paths_name_the_group_version_and_namespace(self):
        self.assertEqual(k.LEASE.path(NAMESPACE), LEASES)
        self.assertEqual(k.POD.path(NAMESPACE), f"/api/v1/namespaces/{NAMESPACE}/pods")
        self.assertEqual(k.NODE.path(None), "/api/v1/nodes")
        with self.assertRaises(ValueError):
            k.JOB.path(None)


if __name__ == "__main__":
    unittest.main()
