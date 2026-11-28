"""The dashboard the Helm chart's Grafana serves is the one `make observe` renders."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal.observability.stack import chart_dashboard
from narwhal.observability.stack.artifacts import (
    BASE,
    narwhal_dashboard,
    render_dashboard,
    scope_to_fleet,
)


class ChartDashboardTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((BASE / "grafana-narwhal.json").read_text())

    def test_the_dashboard_is_rendered_from_the_shipped_source(self):
        self.assertEqual(
            json.loads(chart_dashboard.chart_dashboard()),
            scope_to_fleet(render_dashboard(self.source)),
        )
        console = "https://console.example/console"
        self.assertEqual(
            json.loads(chart_dashboard.chart_dashboard(console)),
            narwhal_dashboard(self.source, console),
        )

    def test_the_command_writes_the_dashboard_and_refuses_a_bad_console(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "dashboard.json"
            argv = ["chart_dashboard", str(out), "--console-url", "/console"]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                self.assertEqual(chart_dashboard.main(), 0)
            self.assertEqual(out.read_text(), chart_dashboard.chart_dashboard("/console"))
            argv = ["chart_dashboard", str(out), "--console-url", "ftp://console"]
            with (
                patch.object(sys, "argv", argv),
                redirect_stderr(io.StringIO()) as error,
                self.assertRaises(SystemExit),
            ):
                chart_dashboard.main()
            self.assertIn("http or https URL", error.getvalue())


if __name__ == "__main__":
    unittest.main()
