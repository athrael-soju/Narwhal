"""Preserve measured peer profiles across managed engine replacement."""

import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from narwhal.profiling.management import combine_selected
from narwhal.profiling.store import ProfileStore
from tests.fixtures import profile


class ManagedProfileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.old = self.write("old", {"a": 1, "b": 2})
        self.new = self.write("new", {"b": 3})
        self.output = self.root / "combined.json"

    def write(self, name, values):
        path = self.root / (name + ".json")
        store = ProfileStore(path)
        rows = {}
        for iid, value in values.items():
            measured = replace(
                profile(iid), ttft_b=value, generation_digest="sha256:" + str(value) * 64
            )
            store.put(measured)
            rows[iid] = {
                "profile": asdict(measured),
                "generation_evidence": {"observed": value},
                "prefill": [[1024, value]],
                "decode": [{"seconds": value}],
            }
        path.with_suffix(".samples.json").write_text(json.dumps({"engines": rows}))
        return path

    def test_selected_generation_changes_and_peer_measurements_survive_repeated_updates(self):
        before = self.old.read_bytes()
        combine_selected(self.old, self.new, self.output, {"a", "b"})
        rows = json.loads(self.output.with_suffix(".samples.json").read_bytes())["engines"]
        original = json.loads(self.old.with_suffix(".samples.json").read_bytes())["engines"]
        self.assertEqual(rows["a"], original["a"])
        self.assertEqual(rows["b"]["profile"]["ttft_b"], 3)
        self.assertEqual(self.old.read_bytes(), before)
        next_output = self.root / "next.json"
        combine_selected(self.output, self.write("next-a", {"a": 4}), next_output, {"a", "b"})
        self.assertEqual(ProfileStore(next_output).get("b").ttft_b, 3)
        self.assertEqual(ProfileStore(next_output).get("a").ttft_b, 4)

    def test_foreign_or_incomplete_sources_do_not_publish(self):
        for old, new, ids in (
            (self.old, self.write("foreign", {"c": 3}), {"a", "b"}),
            (self.new, self.old, {"a", "b"}),
            (self.old, self.new, {"a"}),
        ):
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                combine_selected(old, new, self.output, ids)
            self.assertFalse(self.output.exists())

    def test_tampered_or_missing_measurement_evidence_does_not_publish(self):
        samples = self.new.with_suffix(".samples.json")
        original = samples.read_bytes()
        for change in ("profile", "generation_evidence", "engines"):
            document = json.loads(original)
            if change == "engines":
                document.pop("engines")
            elif change == "profile":
                document["engines"]["b"]["profile"]["ttft_b"] = 9
            else:
                document["engines"]["b"].pop(change)
            samples.write_text(json.dumps(document))
            with self.subTest(change=change), self.assertRaises(ValueError):
                combine_selected(self.old, self.new, self.output, {"a", "b"})
            self.assertFalse(self.output.exists())

    def test_existing_output_or_sidecar_is_preserved(self):
        samples = self.output.with_suffix(".samples.json")
        samples.write_bytes(b"retained")
        with self.assertRaises(FileExistsError):
            combine_selected(self.old, self.new, self.output, {"a", "b"})
        self.assertEqual(samples.read_bytes(), b"retained")
        self.assertFalse(self.output.exists())
