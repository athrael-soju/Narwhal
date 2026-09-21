"""Check schema identity, JSONL diagnostics and versioned writer boundaries."""

import json
import tempfile
import unittest
from pathlib import Path

from narwhal.contracts import (
    CONTRACTS,
    JOURNAL,
    ContractVersionError,
    current,
    manifest,
    read_json,
    read_jsonl,
    validate_any_document,
    validate_document,
    versioned,
)


class ContractTests(unittest.TestCase):
    """Every registered contract accepts exactly its declared current version."""

    def test_all_current_contracts_round_trip_and_reject_other_versions(self):
        """Schema validation rejects booleans, old revisions and future revisions."""
        self.assertNotIn("launcher_fleet", CONTRACTS)
        self.assertNotIn("payload", CONTRACTS)
        self.assertEqual({contract.current for contract in CONTRACTS.values()}, {1})
        for name in CONTRACTS:
            doc = versioned(name, {"value": 1})
            self.assertEqual(validate_document(doc, name), current(name))
            for value in (True, -1, 0, current(name) + 1):
                with self.subTest(name=name, value=value), self.assertRaises(ContractVersionError):
                    validate_document({**doc, "schema_version": value}, name)
        self.assertIsInstance(manifest(), dict)

    def test_writer_owns_schema_fields(self):
        """Supplying schema or schema_version to the writer raises ValueError."""
        for field in ("schema", "schema_version"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                versioned(JOURNAL, {field: 1})
        with self.assertRaises(ValueError):
            validate_any_document({}, ())

    def test_jsonl_accepts_metadata_and_numbers_bad_lines(self):
        """The JSONL reader accepts metadata and data rows and reports malformed line numbers."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.jsonl"
            rows = [
                {"meta": versioned(JOURNAL, {"kind": "fixture"})},
                versioned(JOURNAL, {"rid": "r"}),
            ]
            path.write_text("\n" + "\n".join(json.dumps(row) for row in rows))
            self.assertEqual(read_jsonl(path, JOURNAL), rows)
            path.write_text(json.dumps(rows[0]) + "\n{bad")
            with self.assertRaisesRegex(ContractVersionError, ":2:"):
                read_jsonl(path, JOURNAL)
            path.write_text("{bad")
            with self.assertRaisesRegex(ContractVersionError, "malformed JSON"):
                read_json(path, JOURNAL)
