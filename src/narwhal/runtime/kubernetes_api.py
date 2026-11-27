"""Kubernetes API access from inside the cluster: client, informer and workqueue.

`ApiClient` runs every verb but watch through lightkube, on objects held as plain dictionaries.
It sends each request through one transport that adds the ServiceAccount token, re-reads that
token on a 401 and retries 429 and 5xx responses with capped exponential backoff. Its watch
asks for bookmarks and ends when the server ends the request, so `Informer` sees each end, can
relist after a 410 and resumes from the last resourceVersion. `WorkQueue` coalesces keys the
way client-go's workqueue does.
"""

from __future__ import annotations

import copy
import heapq
import json
import logging
import math
import os
import random
import re
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from lightkube import ApiError as LightkubeApiError
from lightkube import Client as LightkubeClient
from lightkube import ConfigError as LightkubeConfigError
from lightkube import KubeConfig, NotReadyError
from lightkube.config.models import Cluster as ClusterEntry
from lightkube.config.models import User
from lightkube.core.resource import ApiInfo
from lightkube.generic_resource import (
    GenericGlobalResource,
    GenericGlobalStatus,
    GenericNamespacedResource,
    GenericNamespacedStatus,
    create_api_info,
)
from lightkube.types import CascadeType, PatchType

log = logging.getLogger("narwhal.kubernetes_api")

# The directory the kubelet mounts a pod's ServiceAccount token, CA and namespace into.
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
# client-go re-reads a token file once a minute; the kubelet rotates a projected token at 80%
# of its lifetime, so the old token stays valid well past the next read.
TOKEN_MAX_AGE_S = 60.0
TOKEN_MAX_BYTES = 64 * 1024
# client-go's reflector asks each watch to end after 5 to 10 minutes.
WATCH_TIMEOUT_S = 300.0
# A quiet watch sends nothing until the server ends it, so the read timeout must outlast it.
WATCH_READ_MARGIN_S = 30.0
# An informer waits this long before it watches again after a watch that ended at once.
MIN_WATCH_S = 1.0
LIST_CHUNK = 500
LIST_RESTARTS = 3
FIELD_MANAGER_MAX = 128
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PROPAGATION = ("Background", "Foreground", "Orphan")

_TOKEN = re.compile(rb"[\x21-\x7e]+")
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_SELECTOR_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,316}")
_SELECTOR_VALUE = re.compile(r"[A-Za-z0-9_./-]{0,253}")


class KubernetesError(RuntimeError):
    """An API request failed. `code` is the HTTP status, or 0 when no usable response arrived."""

    def __init__(self, code: int, reason: str, message: str) -> None:
        self.code = code
        self.reason = reason
        self.message = message
        super().__init__(f"{code} {reason}: {message}" if code else f"{reason}: {message}")


class NotFound(KubernetesError):
    """404: the object or its namespace does not exist."""


class Conflict(KubernetesError):
    """409: `AlreadyExists` on a create, `Conflict` on a stale resourceVersion."""


class Gone(KubernetesError):
    """410: the resourceVersion or continue token has expired. List again."""


class Unavailable(KubernetesError):
    """No response arrived, or a 429 or 5xx outlasted every retry."""


class InClusterConfigError(ValueError):
    """The pod's ServiceAccount token, CA or namespace cannot be used."""


def _error(code: int, reason: str, message: str) -> KubernetesError:
    if code == 404:
        return NotFound(code, reason or "NotFound", message)
    if code == 409:
        return Conflict(code, reason or "Conflict", message)
    if code == 410:
        return Gone(code, reason or "Expired", message)
    if code in RETRY_STATUSES:
        return Unavailable(code, reason or "Unavailable", message)
    return KubernetesError(code, reason or "Failed", message)


def _status_error(status: Mapping[str, Any], fallback: int = 0) -> KubernetesError:
    """The error a `Status` document describes."""
    code = status.get("code")
    return _error(
        code if isinstance(code, int) else fallback,
        str(status.get("reason") or ""),
        str(status.get("message") or "no message"),
    )


@contextmanager
def _translate() -> Iterator[None]:
    """Raise this module's errors for lightkube's and httpx's."""
    try:
        yield
    except LightkubeApiError as exc:
        status = exc.status
        code = status.code or (exc.response.status_code if exc.response is not None else 0)
        raise _error(code, status.reason or "", status.message or "no message") from exc
    except httpx.HTTPStatusError as exc:
        response = exc.response
        raise _error(response.status_code, response.reason_phrase, response.text[:200]) from exc
    except httpx.TransportError as exc:
        raise Unavailable(0, "Unavailable", f"{type(exc).__name__}: {exc}") from exc
    except NotReadyError as exc:
        raise KubernetesError(0, "InvalidResponse", str(exc)) from exc
    except LightkubeConfigError as exc:
        raise InClusterConfigError(str(exc)) from exc


# Connection: the token, the CA and the server.


class ServiceAccountToken:
    """A projected ServiceAccount token, re-read once a minute and after a 401."""

    def __init__(
        self,
        path: Path,
        *,
        max_age_s: float = TOKEN_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("token max age must be finite and positive")
        self.path = path
        self.max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()
        self._value: str | None = None
        self._read_at = -math.inf

    def value(self) -> str:
        """The token, read again when the last read is older than `max_age_s`."""
        with self._lock:
            if self._value is None or self._clock() - self._read_at >= self.max_age_s:
                return self._load()
            return self._value

    def refresh(self) -> bool:
        """Read the file again and return whether the token changed."""
        with self._lock:
            before = self._value
            return self._load() != before

    def _load(self) -> str:
        try:
            with self.path.open("rb") as handle:
                data = handle.read(TOKEN_MAX_BYTES + 1)
        except OSError as exc:
            raise InClusterConfigError(
                f"cannot read the ServiceAccount token {self.path}: {exc.strerror or exc}"
            ) from None
        token = data.strip()
        if len(data) > TOKEN_MAX_BYTES or not _TOKEN.fullmatch(token):
            raise InClusterConfigError(f"{self.path} holds no usable ServiceAccount token")
        self._value = token.decode("ascii")
        self._read_at = self._clock()
        return self._value

    def __repr__(self) -> str:
        return f"ServiceAccountToken({str(self.path)!r})"


def cluster_ssl_context(ca: Path) -> ssl.SSLContext:
    """A TLS context that trusts only the cluster CA in `ca` and checks the server's name."""
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
    except (OSError, ssl.SSLError) as exc:
        raise InClusterConfigError(f"cannot load the cluster CA {ca}: {exc}") from None
    if not context.get_ca_certs():
        raise InClusterConfigError(f"{ca} holds no CA certificate")
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@dataclass(frozen=True)
class Connection:
    """Where the API server is, how to trust it, and the token to send it."""

    server: str
    namespace: str
    ssl_context: ssl.SSLContext | None = None
    token: ServiceAccountToken | None = field(default=None, repr=False)


def in_cluster(
    account: Path = SERVICE_ACCOUNT, environ: Mapping[str, str] = os.environ
) -> Connection:
    """The connection a pod's mounted ServiceAccount gives, refusing anything incomplete."""
    host = environ.get("KUBERNETES_SERVICE_HOST", "")
    port = environ.get("KUBERNETES_SERVICE_PORT", "")
    if not host or not port.isdigit():
        raise InClusterConfigError(
            "KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must name the API server"
        )
    if ":" in host:
        host = f"[{host}]"
    try:
        namespace = (account / "namespace").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InClusterConfigError(
            f"cannot read the namespace in {account}: {exc.strerror or exc}"
        ) from None
    if not _DNS_LABEL.fullmatch(namespace):
        raise InClusterConfigError(f"{account / 'namespace'} holds no namespace name")
    context = cluster_ssl_context(account / "ca.crt")
    token = ServiceAccountToken(account / "token")
    token.value()
    return Connection(f"https://{host}:{port}", namespace, context, token)


# Retries.


@dataclass(frozen=True)
class Backoff:
    """Capped exponential delays, `base_s * 2**failures`, each shortened by up to `jitter`."""

    base_s: float = 0.5
    cap_s: float = 30.0
    attempts: int = 5
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if not (math.isfinite(self.base_s) and math.isfinite(self.cap_s)):
            raise ValueError("backoff delays must be finite")
        if self.base_s <= 0 or self.cap_s < self.base_s:
            raise ValueError("backoff needs 0 < base_s <= cap_s")
        if self.attempts < 1:
            raise ValueError("backoff needs at least one attempt")
        if not 0 <= self.jitter <= 1:
            raise ValueError("backoff jitter must be between 0 and 1")

    def delay(
        self,
        failures: int,
        retry_after: float | None = None,
        rng: Callable[[], float] = random.random,
    ) -> float:
        """Seconds to wait after `failures + 1` failures, or the server's Retry-After, capped."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.cap_s)
        delay = min(self.cap_s, self.base_s * 2.0 ** min(failures, 62))
        return delay * (1.0 - self.jitter * rng())


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None


def _tls_failure(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _retry_error(request: httpx.Request, exc: httpx.TransportError) -> bool:
    """Retry a request that never reached the server, and a GET whatever happened."""
    if _tls_failure(exc):
        return False
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return True
    return request.method == "GET"


def _retry_status(request: httpx.Request, status: int) -> bool:
    """Retry a 429 always: priority and fairness rejects it before it runs.

    A 5xx may follow a write that took effect. Reads, deletes, applies and resourceVersion-bound
    replaces repeat safely, and a repeated create meets 409, which callers adopt. A JSON or merge
    patch may not repeat safely, so only its 429 is retried.
    """
    if status == 429:
        return True
    if request.method == "PATCH":
        return request.headers.get("content-type") == PatchType.APPLY.value
    return True


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.host, port


class _Transport(httpx.BaseTransport):
    """Adds the token for the API server only, re-reads it on a 401, and retries."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        server: httpx.URL,
        token: ServiceAccountToken | None,
        backoff: Backoff,
        sleep: Callable[[float], None],
    ) -> None:
        self._inner = inner
        self._origin = _origin(server)
        self._token = token
        self._backoff = backoff
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # The token goes only to the API server's own origin.
        token = self._token if _origin(request.url) == self._origin else None
        request.headers.pop("authorization", None)
        refreshed = False
        failures = 0
        while True:
            if token is not None:
                request.headers["authorization"] = f"Bearer {token.value()}"
            try:
                response = self._inner.handle_request(request)
            except httpx.TransportError as exc:
                failures += 1
                if failures >= self._backoff.attempts or not _retry_error(request, exc):
                    raise
                delay = self._backoff.delay(failures - 1)
                log.warning(
                    "%s %s: %s; retrying in %.1fs", request.method, request.url.path, exc, delay
                )
                self._sleep(delay)
                continue
            if response.status_code == 401 and token is not None and not refreshed:
                refreshed = True
                if token.refresh():
                    response.close()
                    continue
            status = response.status_code
            if status in RETRY_STATUSES and _retry_status(request, status):
                failures += 1
                if failures < self._backoff.attempts:
                    delay = self._backoff.delay(failures - 1, _retry_after(response))
                    response.close()
                    log.warning(
                        "%s %s: %d; retrying in %.1fs",
                        request.method,
                        request.url.path,
                        status,
                        delay,
                    )
                    self._sleep(delay)
                    continue
            if not response.is_success and "content-type" not in response.headers:
                # lightkube reads this header unguarded when it builds an error.
                response.headers["content-type"] = "text/plain"
            return response

    def close(self) -> None:
        self._inner.close()

    def __deepcopy__(self, memo: dict[int, Any]) -> _Transport:
        # lightkube deep-copies its connection parameters, transport included. A connection pool
        # holds locks, and both clients must share this one, so a copy is the same transport.
        return self


# Kinds.


@dataclass(frozen=True)
class Kind:
    """One API resource: its group, version, kind and plural."""

    group: str
    version: str
    kind: str
    plural: str
    namespaced: bool = True

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    def path(self, namespace: str | None) -> str:
        """The collection path, within `namespace` for a namespaced kind."""
        root = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
        if self.namespaced:
            if not namespace:
                raise ValueError(f"{self.plural} are namespaced")
            return f"{root}/namespaces/{namespace}/{self.plural}"
        return f"{root}/{self.plural}"


CONFIG_MAP = Kind("", "v1", "ConfigMap", "configmaps")
POD = Kind("", "v1", "Pod", "pods")
SERVICE = Kind("", "v1", "Service", "services")
NODE = Kind("", "v1", "Node", "nodes", namespaced=False)
JOB = Kind("batch", "v1", "Job", "jobs")
STATEFUL_SET = Kind("apps", "v1", "StatefulSet", "statefulsets")
DEPLOYMENT = Kind("apps", "v1", "Deployment", "deployments")
LEASE = Kind("coordination.k8s.io", "v1", "Lease", "leases")
EVENT = Kind("events.k8s.io", "v1", "Event", "events")
CUSTOM_RESOURCE_DEFINITION = Kind(
    "apiextensions.k8s.io", "v1", "CustomResourceDefinition", "customresourcedefinitions", False
)


@cache
def _resources(kind: Kind) -> tuple[type[Any], type[Any]]:
    """lightkube classes for `kind` and its status, kept out of lightkube's global registry."""
    info = create_api_info(kind.group, kind.version, kind.kind, kind.plural)
    status_info = ApiInfo(
        resource=info.resource,
        parent=info.resource,
        plural=kind.plural,
        verbs=["get", "patch", "put"],
        action="status",
    )
    if kind.namespaced:
        base: type[Any] = GenericNamespacedResource
        status_base: type[Any] = GenericNamespacedStatus
    else:
        base, status_base = GenericGlobalResource, GenericGlobalStatus
    status = type(f"{kind.kind}Status", (status_base,), {"_api_info": status_info})
    main = type(kind.kind, (base,), {"_api_info": info, "Status": status})
    return main, status


def _is_job(kind: Kind) -> bool:
    return kind.group == "batch" and kind.kind == "Job"


def key(obj: Mapping[str, Any]) -> str:
    """`namespace/name`, or `name` for a cluster-scoped object, as client-go keys objects."""
    metadata = obj.get("metadata") or {}
    name = metadata.get("name") or ""
    namespace = metadata.get("namespace") or ""
    return f"{namespace}/{name}" if namespace else name


def _resource_version(obj: Mapping[str, Any]) -> str:
    return str((obj.get("metadata") or {}).get("resourceVersion") or "")


def _selector(pairs: Mapping[str, str] | None) -> str | None:
    """An equality selector, refusing keys and values that would change its meaning."""
    if not pairs:
        return None
    for name, value in pairs.items():
        if not _SELECTOR_KEY.fullmatch(name) or not _SELECTOR_VALUE.fullmatch(value):
            raise ValueError(f"selector {name}={value!r} is not a plain equality")
    return ",".join(f"{name}={value}" for name, value in sorted(pairs.items()))


# Client.


@dataclass(frozen=True)
class Listing:
    """One consistent list: its items and the resourceVersion to watch from."""

    items: list[dict[str, Any]]
    resource_version: str


@dataclass(frozen=True)
class WatchEvent:
    """`ADDED`, `MODIFIED`, `DELETED` or `BOOKMARK` from a watch, or `SYNC` from a resync."""

    type: str
    object: dict[str, Any]


class Source(Protocol):
    """What an informer reads: `ApiClient`, or a test's object store."""

    def list(
        self, kind: Kind, *, namespace: str | None = None, labels: Mapping[str, str] | None = None
    ) -> Listing: ...

    def watch(
        self,
        kind: Kind,
        *,
        resource_version: str,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        timeout_s: float = WATCH_TIMEOUT_S,
    ) -> Iterator[WatchEvent]: ...


class ApiClient:
    """Kubernetes verbs on plain dictionaries, as one field manager in one namespace."""

    def __init__(
        self,
        connection: Connection,
        *,
        field_manager: str,
        backoff: Backoff | None = None,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not field_manager or len(field_manager) > FIELD_MANAGER_MAX:
            raise ValueError(f"field manager must have 1 to {FIELD_MANAGER_MAX} characters")
        if not _DNS_LABEL.fullmatch(connection.namespace):
            raise ValueError(f"namespace {connection.namespace!r} is not a DNS label")
        server = httpx.URL(connection.server)
        if connection.token is not None and server.scheme != "https":
            raise ValueError("a ServiceAccount token is sent only over https")
        self.namespace = connection.namespace
        self.field_manager = field_manager
        inner = transport or httpx.HTTPTransport(
            verify=connection.ssl_context if connection.ssl_context is not None else True,
            trust_env=False,
        )
        self._transport = _Transport(inner, server, connection.token, backoff or Backoff(), sleep)
        self._timeout = httpx.Timeout(timeout_s)
        config = KubeConfig.from_one(
            cluster=ClusterEntry(server=connection.server), user=User(), namespace=self.namespace
        )
        self._kube: Any = LightkubeClient(
            config=config,
            namespace=self.namespace,
            timeout=self._timeout,
            field_manager=field_manager,
            trust_env=False,
            transport=self._transport,
        )
        self._http = httpx.Client(
            base_url=connection.server,
            transport=self._transport,
            timeout=self._timeout,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()
        self._kube.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _namespace(self, kind: Kind, namespace: str | None) -> str | None:
        if not kind.namespaced:
            if namespace:
                raise ValueError(f"{kind.plural} are cluster-scoped")
            return None
        return namespace or self.namespace

    def _body(self, kind: Kind, obj: Mapping[str, Any]) -> tuple[dict[str, Any], str, str | None]:
        """A copy of `obj` with its apiVersion and kind, its name and its namespace."""
        body = copy.deepcopy(dict(obj))
        body.setdefault("apiVersion", kind.api_version)
        body.setdefault("kind", kind.kind)
        if (body["apiVersion"], body["kind"]) != (kind.api_version, kind.kind):
            raise ValueError(
                f"{body['apiVersion']} {body['kind']} is not a {kind.api_version} {kind.kind}"
            )
        metadata = body.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            raise ValueError(f"a {kind.kind} needs metadata.name")
        namespace = self._namespace(kind, metadata.get("namespace"))
        return body, metadata["name"], namespace

    def get(self, kind: Kind, name: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        """The object, or None when it does not exist."""
        resource, _ = _resources(kind)
        try:
            with _translate():
                return dict(
                    self._kube.get(resource, name, namespace=self._namespace(kind, namespace))
                )
        except NotFound:
            return None

    def list(
        self,
        kind: Kind,
        *,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Listing:
        """Every matching object, read in pages, starting again if a continue token expires."""
        resource, _ = _resources(kind)
        _selector(labels)
        _selector(fields)
        for restart in range(LIST_RESTARTS):
            try:
                with _translate():
                    pages = self._kube.list(
                        resource,
                        namespace=self._namespace(kind, namespace),
                        chunk_size=LIST_CHUNK,
                        labels=dict(labels) if labels else None,
                        fields=dict(fields) if fields else None,
                    )
                    items = [dict(item) for item in pages]
                    version = pages.resourceVersion
            except Gone:
                if restart + 1 == LIST_RESTARTS:
                    raise
                log.info("%s list: continue token expired; listing again", kind.plural)
                continue
            return Listing(items, version)
        raise AssertionError("unreachable")

    def watch(
        self,
        kind: Kind,
        *,
        resource_version: str,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        timeout_s: float = WATCH_TIMEOUT_S,
    ) -> Iterator[WatchEvent]:
        """Events after `resource_version`, with bookmarks, until the server ends the request.

        A 410, as a response or as an `ERROR` event, raises `Gone`.
        """
        if not resource_version:
            raise ValueError("a watch starts from the resourceVersion of a list")
        params = {
            "watch": "true",
            "allowWatchBookmarks": "true",
            "resourceVersion": resource_version,
            "timeoutSeconds": str(max(1, int(timeout_s))),
        }
        selector = _selector(labels)
        if selector:
            params["labelSelector"] = selector
        timeout = httpx.Timeout(self._timeout.connect, read=timeout_s + WATCH_READ_MARGIN_S)
        path = kind.path(self._namespace(kind, namespace))
        with _translate(), self._http.stream("GET", path, params=params, timeout=timeout) as reply:
            if not reply.is_success:
                reply.read()
                raise _response_error(reply)
            for line in reply.iter_lines():
                if line.strip():
                    yield _watch_event(line)

    def create(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        """Create `obj`. A name that exists raises `Conflict` with reason `AlreadyExists`."""
        resource, _ = _resources(kind)
        body, _, namespace = self._body(kind, obj)
        with _translate():
            return dict(self._kube.create(resource(body), namespace=namespace))

    def replace(self, kind: Kind, obj: Mapping[str, Any]) -> dict[str, Any]:
        """Replace `obj` if it is still at its `metadata.resourceVersion`, or raise `Conflict`."""
        resource, _ = _resources(kind)
        body, _, namespace = self._body(kind, obj)
        if not body["metadata"].get("resourceVersion"):
            raise ValueError(f"replacing a {kind.kind} needs the resourceVersion it was read at")
        with _translate():
            return dict(self._kube.replace(resource(body), namespace=namespace))

    def apply(
        self,
        kind: Kind,
        obj: Mapping[str, Any],
        *,
        force: bool = False,
        status: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Server-side apply `obj` as this client's field manager, to its status if `status`."""
        main, status_resource = _resources(kind)
        body, name, namespace = self._body(kind, obj)
        with _translate():
            result = self._kube.patch(
                status_resource if status else main,
                name,
                body,
                namespace=namespace,
                patch_type=PatchType.APPLY,
                force=force,
                dry_run=dry_run,
            )
        return dict(result)

    def patch(
        self,
        kind: Kind,
        name: str,
        body: Sequence[Mapping[str, Any]] | Mapping[str, Any],
        *,
        patch_type: Literal["json", "merge"] = "json",
        status: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """A JSON patch (a list of operations) or a merge patch (a mapping)."""
        if patch_type == "json":
            if isinstance(body, Mapping):
                raise ValueError("a JSON patch is a list of operations")
            data: Any = [dict(operation) for operation in body]
            kind_of_patch = PatchType.JSON
        else:
            if not isinstance(body, Mapping):
                raise ValueError("a merge patch is a mapping")
            data, kind_of_patch = dict(body), PatchType.MERGE
        main, status_resource = _resources(kind)
        with _translate():
            result = self._kube.patch(
                status_resource if status else main,
                name,
                data,
                namespace=self._namespace(kind, namespace),
                patch_type=kind_of_patch,
            )
        return dict(result)

    def delete(
        self,
        kind: Kind,
        name: str,
        *,
        namespace: str | None = None,
        propagation: str = "Background",
    ) -> bool:
        """Delete the object and let the garbage collector remove its dependents.

        Returns False when it was already gone. The API's own default for Jobs orphans their
        pods, so a Job delete always sends `Background`.
        """
        if propagation not in PROPAGATION:
            raise ValueError(f"propagation must be one of {', '.join(PROPAGATION)}")
        if _is_job(kind) and propagation != "Background":
            raise ValueError("a Job delete propagates in the background, or its pods stay behind")
        resource, _ = _resources(kind)
        try:
            with _translate():
                self._kube.delete(
                    resource,
                    name,
                    namespace=self._namespace(kind, namespace),
                    cascade=CascadeType(propagation),
                )
        except NotFound:
            return False
        return True

    def log(self, pod: str, *, container: str | None = None, namespace: str | None = None) -> str:
        """The container's whole log."""
        with _translate():
            lines = self._kube.log(
                pod, namespace=self._namespace(POD, namespace), container=container
            )
            return "".join(lines)


def _response_error(response: httpx.Response) -> KubernetesError:
    try:
        status = response.json()
    except ValueError:
        status = None
    if isinstance(status, dict) and status.get("kind") == "Status":
        return _status_error(status, response.status_code)
    return _error(response.status_code, response.reason_phrase, response.text[:200])


def _watch_event(line: str) -> WatchEvent:
    try:
        event = json.loads(line)
    except ValueError:
        raise KubernetesError(0, "InvalidResponse", "watch sent a line that is not JSON") from None
    kind = event.get("type") if isinstance(event, dict) else None
    obj = event.get("object") if isinstance(event, dict) else None
    if not isinstance(obj, dict):
        raise KubernetesError(0, "InvalidResponse", "watch sent an event without an object")
    if kind == "ERROR":
        raise _status_error(obj)
    if kind not in ("ADDED", "MODIFIED", "DELETED", "BOOKMARK"):
        raise KubernetesError(0, "InvalidResponse", f"watch sent an event of type {kind!r}")
    return WatchEvent(kind, obj)


# Informer.


class Informer:
    """A local cache of one kind, kept by a list and then a watch.

    `pump()` lists when the cache has no resourceVersion, then follows one watch request until
    the server ends it. Bookmarks move the resourceVersion forward, so the next watch resumes
    from it. A 410 clears the resourceVersion and lists again; the handler then hears what that
    list changed, including deletions the expired watch never sent. `resync()` replays every
    cached object as `SYNC` once `resync_s` has passed, without reading the API.

    The handler runs under the informer's lock, so it must not block. Objects it and `get` hand
    out are copies.
    """

    def __init__(
        self,
        source: Source,
        kind: Kind,
        *,
        namespace: str | None = None,
        labels: Mapping[str, str] | None = None,
        handler: Callable[[WatchEvent], None] | None = None,
        resync_s: float | None = None,
        watch_timeout_s: float = WATCH_TIMEOUT_S,
        backoff: Backoff | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if resync_s is not None and (not math.isfinite(resync_s) or resync_s <= 0):
            raise ValueError("resync interval must be finite and positive")
        self.kind = kind
        self.namespace = namespace
        self.labels = dict(labels) if labels else None
        self._source = source
        self._handler = handler
        self._resync_s = resync_s
        self._watch_timeout_s = watch_timeout_s
        self._backoff = backoff or Backoff(base_s=1.0, cap_s=60.0)
        self._clock = clock
        self._rng = rng
        self._lock = threading.RLock()
        self._store: dict[str, dict[str, Any]] = {}
        self._version: str | None = None
        self._synced = False
        self._last_resync = 0.0

    @property
    def synced(self) -> bool:
        """Whether a list has filled the cache."""
        return self._synced

    @property
    def resource_version(self) -> str | None:
        """The resourceVersion the next watch starts from; None until a list sets it."""
        return self._version

    def get(self, object_key: str) -> dict[str, Any] | None:
        """A copy of the cached object with this `key()`, or None."""
        with self._lock:
            obj = self._store.get(object_key)
            return copy.deepcopy(obj) if obj is not None else None

    def items(self) -> list[dict[str, Any]]:
        """Copies of every cached object, in key order."""
        with self._lock:
            return [copy.deepcopy(self._store[k]) for k in sorted(self._store)]

    def pump(self) -> None:
        """List if needed, then follow one watch to its end, relisting once after a 410."""
        for _ in range(2):
            version = self._version or self._relist()
            try:
                self._follow(version)
            except Gone as exc:
                log.info("%s watch: %s; listing again", self.kind.plural, exc)
                with self._lock:
                    self._version = None
                continue
            return

    def run(self, stop: threading.Event) -> None:
        """Pump until `stop` is set, backing off after each failure."""
        failures = 0
        while not stop.is_set():
            started = self._clock()
            try:
                self.pump()
            except KubernetesError as exc:
                delay = self._backoff.delay(failures, rng=self._rng)
                failures += 1
                log.warning("%s informer: %s; retrying in %.1fs", self.kind.plural, exc, delay)
                stop.wait(delay)
                continue
            failures = 0
            if self._clock() - started < MIN_WATCH_S:
                stop.wait(MIN_WATCH_S)

    def resync(self) -> bool:
        """Replay every cached object as `SYNC` if `resync_s` has passed since the last time."""
        if self._resync_s is None:
            return False
        with self._lock:
            now = self._clock()
            if not self._synced or now - self._last_resync < self._resync_s:
                return False
            self._last_resync = now
            for name in sorted(self._store):
                self._notify("SYNC", self._store[name])
        return True

    def _relist(self) -> str:
        listing = self._source.list(self.kind, namespace=self.namespace, labels=self.labels)
        fresh = {key(obj): obj for obj in listing.items}
        with self._lock:
            before, self._store = self._store, fresh
            self._version = listing.resource_version
            if not self._synced:
                self._last_resync = self._clock()
            self._synced = True
            for name in sorted(fresh):
                old = before.get(name)
                if old is None:
                    self._notify("ADDED", fresh[name])
                elif _resource_version(old) != _resource_version(fresh[name]):
                    self._notify("MODIFIED", fresh[name])
            for name in sorted(before.keys() - fresh.keys()):
                self._notify("DELETED", before[name])
        return listing.resource_version

    def _follow(self, version: str) -> None:
        timeout_s = self._watch_timeout_s * (1.0 + self._rng())
        events = self._source.watch(
            self.kind,
            resource_version=version,
            namespace=self.namespace,
            labels=self.labels,
            timeout_s=timeout_s,
        )
        for event in events:
            self._apply(event)
            self.resync()
        self.resync()

    def _apply(self, event: WatchEvent) -> None:
        version = _resource_version(event.object)
        with self._lock:
            if event.type != "BOOKMARK":
                name = key(event.object)
                if event.type == "DELETED":
                    self._store.pop(name, None)
                else:
                    self._store[name] = event.object
                self._notify(event.type, event.object)
            if version:
                self._version = version

    def _notify(self, kind: str, obj: dict[str, Any]) -> None:
        if self._handler is not None:
            self._handler(WatchEvent(kind, copy.deepcopy(obj)))


# Workqueue.


class WorkQueue:
    """Keys waiting for a worker, with client-go's workqueue rules.

    A key waits at most once, so a burst of events for one object becomes one reconcile. A key
    a worker holds is queued again only after `done`, so two workers never hold the same key.
    `add_rate_limited` delays a key by its failure count until `forget`.
    """

    def __init__(
        self,
        *,
        backoff: Backoff | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backoff = backoff or Backoff(base_s=1.0, cap_s=300.0, jitter=0.0)
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: deque[str] = deque()
        self._dirty: set[str] = set()
        self._processing: set[str] = set()
        self._delayed: dict[str, float] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._sequence = 0
        self._failures: dict[str, int] = {}
        self._shut = False

    def __len__(self) -> int:
        with self._cond:
            return len(self._queue)

    def add(self, key: str) -> None:
        """Queue `key` now, dropping any delayed add of it."""
        with self._cond:
            self._add(key)

    def _add(self, key: str) -> None:
        if self._shut:
            return
        self._delayed.pop(key, None)
        if key in self._dirty:
            return
        self._dirty.add(key)
        if key not in self._processing:
            self._queue.append(key)
            self._cond.notify()

    def add_after(self, key: str, delay_s: float) -> None:
        """Queue `key` after `delay_s`, unless it is already due sooner."""
        if delay_s <= 0:
            self.add(key)
            return
        with self._cond:
            if self._shut or key in self._dirty:
                return
            due = self._clock() + delay_s
            current = self._delayed.get(key)
            if current is not None and current <= due:
                return
            self._delayed[key] = due
            self._sequence += 1
            heapq.heappush(self._heap, (due, self._sequence, key))
            self._cond.notify()

    def add_rate_limited(self, key: str) -> None:
        """Queue `key` after the backoff its failures so far earn."""
        with self._cond:
            failures = self._failures.get(key, 0)
            self._failures[key] = failures + 1
        self.add_after(key, self._backoff.delay(failures))

    def forget(self, key: str) -> None:
        """Clear the failure count of `key`."""
        with self._cond:
            self._failures.pop(key, None)

    def failures(self, key: str) -> int:
        with self._cond:
            return self._failures.get(key, 0)

    def get(self, timeout_s: float | None = None) -> str | None:
        """The next key, or None on timeout or shutdown. The caller must call `done`."""
        with self._cond:
            deadline = None if timeout_s is None else self._clock() + timeout_s
            while True:
                self._promote()
                if self._shut:
                    return None
                if self._queue:
                    item = self._queue.popleft()
                    self._dirty.discard(item)
                    self._processing.add(item)
                    return item
                now = self._clock()
                waits = [self._heap[0][0] - now] if self._heap else []
                if deadline is not None:
                    if now >= deadline:
                        return None
                    waits.append(deadline - now)
                self._cond.wait(max(0.0, min(waits)) if waits else None)

    def done(self, key: str) -> None:
        """Release `key`; queue it again if it was added while a worker held it."""
        with self._cond:
            self._processing.discard(key)
            if key in self._dirty and not self._shut:
                self._queue.append(key)
                self._cond.notify()

    def shutdown(self) -> None:
        """Wake every waiting worker; `get` returns None from now on."""
        with self._cond:
            self._shut = True
            self._cond.notify_all()

    def _promote(self) -> None:
        now = self._clock()
        while self._heap and self._heap[0][0] <= now:
            due, _, key = heapq.heappop(self._heap)
            if self._delayed.get(key) == due:
                del self._delayed[key]
                self._add(key)
