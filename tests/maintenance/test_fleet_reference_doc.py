"""The NarwhalFleet reference page, whose field tables come from the CRD schema."""

import unittest

from narwhal.deployment import fleet_resource
from tools.maintenance import fleet_reference_doc


class FleetReferenceDocTests(unittest.TestCase):
    def test_the_page_holds_the_tables_of_the_schema(self):
        text = fleet_reference_doc.PAGE.read_text()
        self.assertEqual(
            fleet_reference_doc.render(text),
            text,
            "run tools/maintenance/fleet_reference_doc.py after changing fleet_resource.SCHEMA",
        )

    def test_every_field_of_the_schema_has_a_row(self):
        tables = fleet_reference_doc.tables()
        for part in ("spec", "status"):
            node = fleet_resource.SCHEMA["properties"][part]
            for field, _, _ in fleet_reference_doc.rows(node, part):
                with self.subTest(field=field):
                    self.assertIn(f"| `{field}` |", tables)
        self.assertIn("| `spec.timeouts.stages.<stage>` |", tables)
        for stage in fleet_resource.STAGES:
            self.assertNotIn(f"`spec.timeouts.stages.{stage}`", tables)

    def test_a_changed_schema_makes_the_page_stale(self):
        text = fleet_reference_doc.PAGE.read_text()
        description = fleet_resource.SCHEMA["properties"]["spec"]["properties"]["until"]
        original = description["description"]
        try:
            description["description"] = "A changed description"
            self.assertNotEqual(fleet_reference_doc.render(text), text)
        finally:
            description["description"] = original

    def test_the_page_states_the_group_is_a_placeholder(self):
        text = fleet_reference_doc.PAGE.read_text()
        self.assertIn(f"The API group `{fleet_resource.GROUP}` is a placeholder", text)
        self.assertIn(f"`{fleet_resource.VERSION}` will change before", text)
        self.assertIn(f"apiVersion: {fleet_resource.API_VERSION}", text)
