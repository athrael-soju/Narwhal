"""Check how `--first-start-primary` picks the router's start as primary or standby."""

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from narwhal.cli import serve
from narwhal.runtime import kubernetes_api as k
from narwhal.runtime.kubernetes_lease import KubernetesLease
from narwhal.runtime.lease import FileLease, LeaseRecord
from tests.deployment.kubernetes_cluster import LEASE, ApiError, ObjectStore


class FirstStartTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lease = Path(directory.name) / "lease.json"

    def start(self, *extra: str):
        argv = ["--fleet", "unused", "--port", "0", "--lease-path", str(self.lease), *extra]
        with (
            patch(
                "narwhal.cli.FleetConfig.load",
                return_value=SimpleNamespace(graceful_timeout_s=1, max_connections=8, resume=False),
            ),
            patch("narwhal.cli.create_app") as app,
            patch("narwhal.cli.YieldingServer"),
        ):
            self.assertEqual(serve(argv), 0)
        kwargs = app.call_args.kwargs
        # The peer stays set either way, so a lost claim still shadows it.
        self.assertEqual(kwargs["standby_of"], "http://peer:8000")
        return "claim" if kwargs["claim_first"] else "shadow"

    def test_the_first_start_finds_no_lease_and_starts_as_the_primary(self):
        self.assertEqual(
            "claim", self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        )

    def test_a_later_start_finds_the_lease_and_shadows_the_peer(self):
        self.write_lease(expires_in=4)
        standby = self.start("--standby-of", "http://peer:8000/", "--first-start-primary")
        self.assertEqual(standby, "shadow")

    def test_a_lease_a_standby_could_still_take_over_makes_it_shadow_the_peer(self):
        # Expired 10 s ago: a standby's 30 s handoff may still take it over.
        self.write_lease(expires_in=-10)
        standby = self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        self.assertEqual(standby, "shadow")

    def test_a_lease_no_standby_can_take_over_is_claimed_on_a_cold_start(self):
        # Both routers stopped long ago: no handoff is fresh enough for a takeover.
        self.write_lease(expires_in=-31)
        self.assertEqual(
            "claim", self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        )
        argv = ("--standby-of", "http://peer:8000", "--first-start-primary")
        standby = self.start(*argv, "--standby-max-handoff-age", "60")
        self.assertEqual(standby, "shadow")

    def test_a_malformed_lease_makes_it_shadow_the_peer(self):
        self.lease.write_text("{}")
        standby = self.start("--standby-of", "http://peer:8000", "--first-start-primary")
        self.assertEqual(standby, "shadow")

    def write_lease(self, expires_in: float) -> None:
        now = time.time()
        record = LeaseRecord(3, "peer:token", now + expires_in, now + expires_in - 5)
        FileLease(self.lease, "peer:token", 5)._write_unlocked(record)

    def test_without_its_witness_the_first_start_shadows_the_peer(self):
        # An unmounted lease directory holds neither the lease nor the witness; the router
        # must not take the absent lease for a first start.
        witness = self.lease.with_name(".narwhal-lease-witness")
        argv = ("--standby-of", "http://peer:8000", "--first-start-primary")
        standby = self.start(*argv, "--lease-witness", str(witness))
        self.assertEqual(standby, "shadow")
        witness.write_text("narwhal lease directory\n")
        self.assertEqual("claim", self.start(*argv, "--lease-witness", str(witness)))

    def test_the_flag_needs_a_peer(self):
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            self.start("--first-start-primary")
        self.assertIn("--first-start-primary requires --standby-of", errors.getvalue())


class KubernetesFirstStartTests(unittest.TestCase):
    """`--first-start-primary` on a Kubernetes Lease, read through the stub API server."""

    def setUp(self):
        self.store = ObjectStore()
        self.store.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "fleet"}})
        self.built: list[tuple] = []

    def lease(self, name, holder, ttl_s, *, safety_margin_s):
        self.built.append((name, holder, ttl_s, safety_margin_s))
        client = k.ApiClient(
            k.Connection("https://10.96.0.1:443", "fleet"),
            field_manager="narwhal-router",
            transport=self.store.transport(),
        )
        return KubernetesLease(client, name, holder, ttl_s, safety_margin_s=safety_margin_s)

    def start(self, *extra: str, unreadable: bool = False):
        argv = ["--fleet", "unused", "--port", "0", "--kubernetes-lease", "narwhal-router"]
        argv += ["--standby-of", "http://peer:8000", "--first-start-primary", *extra]
        factory = Mock(side_effect=k.InClusterConfigError("no token")) if unreadable else None
        with (
            patch(
                "narwhal.cli.FleetConfig.load",
                return_value=SimpleNamespace(graceful_timeout_s=1, max_connections=8, resume=False),
            ),
            patch(
                "narwhal.runtime.kubernetes_lease.KubernetesLease.in_cluster",
                factory or self.lease,
            ),
            patch("narwhal.cli.create_app") as app,
            patch("narwhal.cli.YieldingServer"),
        ):
            self.assertEqual(serve(argv), 0)
        kwargs = app.call_args.kwargs
        self.assertEqual(kwargs["kubernetes_lease"], "narwhal-router")
        self.assertIsNone(kwargs["lease_path"])
        return "claim" if kwargs["claim_first"] else "shadow"

    def holder(self, name: str) -> KubernetesLease:
        lease = self.lease("narwhal-router", name, 5, safety_margin_s=1.0)
        self.assertTrue(lease.claim())
        return lease

    def test_an_absent_lease_makes_it_the_primary(self):
        self.assertEqual(self.start(), "claim")
        self.assertEqual(self.built, [("narwhal-router", "first-start", 5.0, 1.0)])

    def test_a_held_lease_makes_it_shadow_the_peer(self):
        self.holder("peer:token")
        self.assertEqual(self.start(), "shadow")

    def test_a_released_lease_makes_it_the_primary(self):
        self.holder("peer:token").release()
        self.assertEqual(self.start(), "claim")

    def test_a_lease_it_cannot_read_makes_it_shadow_the_peer(self):
        self.assertEqual(self.start(unreadable=True), "shadow")

    def test_the_first_start_check_writes_nothing(self):
        self.start()
        with self.assertRaises(ApiError):
            self.store.get(LEASE, "fleet", "narwhal-router")


class KubernetesLeaseOptionTests(unittest.TestCase):
    def refused(self, *argv: str) -> str:
        with (
            patch(
                "narwhal.cli.FleetConfig.load",
                return_value=SimpleNamespace(graceful_timeout_s=1, max_connections=8, resume=False),
            ),
            patch("narwhal.cli.create_app") as app,
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit),
        ):
            serve(["--fleet", "unused", "--port", "0", *argv])
        app.assert_not_called()
        return errors.getvalue()

    def test_one_router_takes_one_kind_of_lease(self):
        errors = self.refused("--lease-path", "/l", "--kubernetes-lease", "narwhal-router")
        self.assertIn("give --lease-path or --kubernetes-lease, not both", errors)

    def test_a_standby_needs_a_lease_of_either_kind(self):
        errors = self.refused("--standby-of", "http://peer:8000")
        self.assertIn("--standby-of requires --lease-path or --kubernetes-lease", errors)

    def test_a_kubernetes_lease_needs_whole_seconds_and_a_margin(self):
        for extra, why in (
            (("--lease-ttl", "5.5"), "whole positive number of seconds"),
            (("--lease-safety-margin", "0"), "safety margin must be positive"),
            (("--kubernetes-lease", "Not_A_Name"), "DNS subdomain"),
        ):
            with self.subTest(extra=extra):
                argv = ("--kubernetes-lease", "narwhal-router", *extra)
                self.assertIn(why, self.refused(*argv))

    def test_a_witness_belongs_to_a_lease_file(self):
        errors = self.refused("--kubernetes-lease", "narwhal-router", "--lease-witness", "/w")
        self.assertIn("--lease-witness requires --lease-path", errors)


if __name__ == "__main__":
    unittest.main()
