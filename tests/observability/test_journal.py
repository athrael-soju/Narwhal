"""Pin the request journal's JSONL line bytes."""

import tempfile
import unittest
from pathlib import Path

from narwhal.observability.journal import RunJournal


class JournalLineTests(unittest.TestCase):
    """Each request row serializes as schema identity, run, then the row's own fields."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "journal.jsonl"
        self.journal = RunJournal(self.path, run="run-1")
        self.journal.open()
        self.addCleanup(self.journal.close)

    def rows(self) -> list[str]:
        """Return written lines after the provenance row."""
        return self.path.read_text().splitlines(keepends=True)[1:]

    def test_rows_keep_their_exact_line_format(self):
        self.journal.write(
            {"rid": "a", "ttft_s": 0.125, "nested": {"k": [1, 2]}, "note": "é", "ok": True}
        )
        self.journal.write({"rid": "b", "run": "own", "none": None})
        self.journal.write({})
        self.assertEqual(
            self.rows(),
            [
                '{"schema": "narwhal.journal", "schema_version": 1, "run": "run-1", "rid": "a", '
                '"ttft_s": 0.125, "nested": {"k": [1, 2]}, "note": "\\u00e9", "ok": true}\n',
                '{"schema": "narwhal.journal", "schema_version": 1, "run": "own", "rid": "b", '
                '"none": null}\n',
                '{"schema": "narwhal.journal", "schema_version": 1, "run": "run-1"}\n',
            ],
        )

    def test_rows_cannot_set_schema_fields(self):
        for row, fields in (
            ({"schema": "other"}, "schema"),
            ({"schema_version": 2}, "schema_version"),
            ({"run": "x", "schema_version": 2, "schema": "other"}, "schema, schema_version"),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError) as caught:
                    self.journal.write(row)
                self.assertEqual(str(caught.exception), f"journal body cannot replace {fields}")
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
