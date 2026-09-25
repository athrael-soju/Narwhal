"""Exercise configured listener conflicts with Linux TCP sockets."""

import errno
import socket
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from narwhal.runtime.listeners import check_engine_bind, check_http_bind


@contextmanager
def listener(host, *, v6only=None, port=0):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if v6only is not None:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        sock.bind((host, port))
        sock.listen(1)
        yield sock.getsockname()[1]


class ListenerBindTests(unittest.TestCase):
    def require_ipv6(self):
        try:
            with listener("::1"):
                pass
        except OSError as error:
            self.skipTest(f"IPv6 loopback bind: {error}")

    def test_distinct_ipv4_addresses_share_a_port(self):
        with listener("127.0.0.1") as port:
            for check in (check_http_bind, check_engine_bind):
                with self.subTest(check=check.__name__):
                    check("127.0.0.2", port)
                    with self.assertRaises(OSError) as failure:
                        check("127.0.0.1", port)
                    self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def test_ipv4_wildcard_conflicts_with_specific_listener(self):
        for occupied, requested in (("127.0.0.2", "0.0.0.0"), ("0.0.0.0", "127.0.0.2")):
            with listener(occupied) as port:
                for check in (check_http_bind, check_engine_bind):
                    with self.subTest(occupied=occupied, check=check.__name__):
                        with self.assertRaises(OSError) as failure:
                            check(requested, port)
                        self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def test_ipv6_occupied_endpoint_and_ipv4_independence(self):
        self.require_ipv6()
        with listener("::1", v6only=1) as port:
            for check in (check_http_bind, check_engine_bind):
                with self.subTest(check=check.__name__):
                    check("127.0.0.1", port)
                    with self.assertRaises(OSError) as failure:
                        check("::1", port)
                    self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def test_uvicorn_ipv6_wildcard_uses_ipv6_only(self):
        self.require_ipv6()
        with listener("127.0.0.1") as port:
            check_http_bind("::", port)
            with self.assertRaises(OSError):
                check_http_bind("", port)
        with listener("::1", v6only=1) as port, self.assertRaises(OSError):
            check_http_bind("::", port)

    def test_vllm_ipv6_wildcard_preserves_kernel_dual_stack_default(self):
        self.require_ipv6()
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            v6only = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
        with listener("127.0.0.2") as port:
            if v6only:
                check_engine_bind("::", port)
            else:
                with self.assertRaises(OSError) as failure:
                    check_engine_bind("::", port)
                self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def test_nixl_ipv6_wildcard_enables_dual_stack(self):
        self.require_ipv6()
        with listener("127.0.0.2") as port:
            with self.assertRaises(OSError) as failure:
                check_engine_bind("::", port, nixl=True)
            self.assertEqual(failure.exception.errno, errno.EADDRINUSE)

    def test_localhost_resolves_both_families_for_uvicorn(self):
        self.require_ipv6()
        families = {info[0] for info in socket.getaddrinfo("localhost", 0, type=socket.SOCK_STREAM)}
        if socket.AF_INET6 not in families:
            self.skipTest("localhost resolves to IPv4 on this host")
        with listener("::1", v6only=1) as port:
            check_engine_bind("localhost", port)
            with self.assertRaises(OSError):
                check_http_bind("localhost", port)

    def test_unavailable_requested_family_fails_and_ipv4_still_binds(self):
        real_socket = socket.socket

        def ipv4_socket(family=socket.AF_INET, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "IPv6 disabled")
            return real_socket(family, *args, **kwargs)

        with (
            patch("socket.socket", side_effect=ipv4_socket),
            patch("uvicorn.Config.get_loop_factory", return_value=None),
        ):
            for check in (check_http_bind, check_engine_bind):
                with self.subTest(check=check.__name__):
                    check("127.0.0.1", 0)
                    with self.assertRaises(OSError):
                        check("::1", 0)
            check_http_bind("", 0)

    def test_unavailable_local_address_fails(self):
        with socket.socket() as sock:
            try:
                sock.bind(("192.0.2.123", 0))
            except OSError as error:
                self.assertEqual(error.errno, errno.EADDRNOTAVAIL)
            else:
                self.skipTest("192.0.2.123 is configured on this host")
        for check in (check_http_bind, check_engine_bind):
            with self.subTest(check=check.__name__), self.assertRaises(OSError):
                check("192.0.2.123", 0)

    def test_successful_probe_releases_the_listener(self):
        for check in (check_http_bind, check_engine_bind):
            with self.subTest(check=check.__name__):
                with listener("127.0.0.1") as port:
                    pass
                check("127.0.0.1", port)
                with listener("127.0.0.1", port=port):
                    pass


if __name__ == "__main__":
    unittest.main()
