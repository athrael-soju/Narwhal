"""checked.json and shared-start.json use engine-neutral names and still read earlier ones."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import current_fields, load, names
from narwhal.deployment.attestation_contract.evidence import read_shared_start
from narwhal.deployment.launch_engine.check import check, read_checked
from tests.backends.contract import Stages, fixtures, prepared


class EvidenceNameTests(unittest.TestCase):
    def checked(self, name: str) -> tuple[Path, dict, Stages]:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        fixture = fixtures(name)
        run, plan, _ = prepared(Path(folder.name), load(name).launcher(), fixture, native=False)
        stages = Stages(plan, fixture.check_output(plan))
        self.check(run, plan, stages)
        return run, plan, stages

    def check(self, run: Path, plan: dict, stages: Stages) -> None:
        with (
            patch("narwhal.deployment.stages.run", side_effect=stages),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            check(run, plan)

    def test_each_backend_writes_and_reads_the_neutral_names(self):
        for name in names():
            renamed = load(name).renamed_fields
            with self.subTest(backend=name):
                run, plan, stages = self.checked(name)
                written = json.loads((run / "checked.json").read_text())
                self.assertIn("engine_version", written)
                self.assertFalse(set(written) & set(renamed["checked"]))
                # A repeated check accepts a file with the earlier names and the same values.
                earlier = {
                    {v: k for k, v in renamed["checked"].items()}.get(key, key): value
                    for key, value in written.items()
                }
                (run / "checked.json").write_text(json.dumps(earlier))
                self.assertEqual(read_checked(run), written)
                self.check(run, plan, stages)
                self.assertEqual(json.loads((run / "checked.json").read_text()), earlier)

    def test_a_file_with_an_earlier_and_a_current_name_is_rejected(self):
        for name in names():
            for section, label in (
                ("checked", "checked.json"),
                ("shared_start", "shared-start.json"),
            ):
                for old, new in load(name).renamed_fields[section].items():
                    with (
                        self.subTest(backend=name, field=old),
                        tempfile.TemporaryDirectory() as folder,
                    ):
                        run = Path(folder)
                        (run / label).write_text(json.dumps({old: "a", new: "a"}))
                        reader = read_checked if section == "checked" else read_shared_start
                        with self.assertRaisesRegex(
                            ValueError, f"^{label} sets both {old} and {new}$"
                        ):
                            reader(run)

    def test_earlier_names_resolve_through_one_mapping(self):
        self.assertEqual(
            current_fields("shared_start", {"vllm_version": "1", "sglang_args": ["x"]}),
            {"engine_version": "1", "engine_args": ["x"]},
        )
        with self.assertRaisesRegex(ValueError, "sets both vllm_api_version and sglang_version"):
            current_fields("checked", {"vllm_api_version": "1", "sglang_version": "2"})


if __name__ == "__main__":
    unittest.main()
