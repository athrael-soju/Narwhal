"""Check the local stub launcher's port and generated-config safeguards."""

import json
import socket
import tempfile
import unittest
from pathlib import Path

from narwhal.config import FleetConfig
from tools import stub_fleet
from tools.tests.fixtures import ROOT


class StubFleetLauncherTests(unittest.TestCase):
    def test_generated_fleet_matches_selected_ports_and_profile_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.json"
            stub_fleet._write_fleet(path, 18101, 6, "my-stub")
            fleet = json.loads(path.read_text())
            loaded = FleetConfig.load(path)
            self.assertEqual(fleet["model"], "my-stub")
            self.assertEqual(loaded.model, "my-stub")
            self.assertEqual(fleet["profiles"]["path"], str(path.parent / "profiles.json"))
            for index, engine in enumerate(fleet["engines"]):
                url = f"http://127.0.0.1:{18101 + index}"
                self.assertEqual(engine["url"], url)
                self.assertEqual(engine["attestation_url"], f"{url}/v1/attestation")
            stub_fleet._write_fleet(path, 18101, 6, "my-stub")
            with self.assertRaisesRegex(SystemExit, "different contents"):
                stub_fleet._write_fleet(path, 18102, 6, "my-stub")

    def test_tracked_stub_template_cannot_be_overwritten(self):
        with self.assertRaisesRegex(SystemExit, "refusing to overwrite"):
            stub_fleet._write_fleet(ROOT / "config/fleet.stub.json", 18101, 6, "stub")

    def test_occupied_port_is_reported_before_processes_start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(SystemExit, f"127.0.0.1:{port} is unavailable"):
                stub_fleet._check_ports_available(port, 1)

    def test_port_range_must_fit_tcp(self):
        with self.assertRaisesRegex(SystemExit, "port range"):
            stub_fleet._check_ports_available(65534, 3)
