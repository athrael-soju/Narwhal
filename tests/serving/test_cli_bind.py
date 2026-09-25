"""Check router address selection before fleet loading."""

import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.cli import serve
from tests.runtime.test_listeners import listener


class RouterBindTests(unittest.TestCase):
    def test_occupied_distinct_address_reaches_configured_uvicorn_bind(self):
        with (
            listener("127.0.0.1") as port,
            patch(
                "narwhal.cli.FleetConfig.load", return_value=SimpleNamespace(graceful_timeout_s=1)
            ),
            patch("narwhal.cli.create_app") as app,
            patch("narwhal.cli.uvicorn.run") as run,
        ):
            result = serve(["--fleet", "unused", "--host", "127.0.0.2", "--port", str(port)])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.args, (app.return_value,))
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.2")
        self.assertEqual(run.call_args.kwargs["port"], port)

    def test_occupied_configured_address_fails_before_fleet_loading(self):
        with (
            listener("127.0.0.2") as port,
            patch("narwhal.cli.FleetConfig.load") as load,
            redirect_stderr(io.StringIO()) as errors,
        ):
            result = serve(["--fleet", "unused", "--host", "127.0.0.2", "--port", str(port)])
        self.assertEqual(result, 2)
        self.assertIn(f"127.0.0.2:{port}", errors.getvalue())
        self.assertIn("already in use", errors.getvalue())
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
