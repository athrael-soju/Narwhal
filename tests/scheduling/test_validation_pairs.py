"""Check shared preflight and recovery transfer selection on pinned fleets."""

import unittest

from narwhal.diagnostics import check
from narwhal.engines.validation import can_consume, can_produce, recovery_pairs, validation_pairs
from narwhal.runtime import lifecycle
from tests.fixtures import validation_topologies


class ValidationPairTests(unittest.TestCase):
    def test_all_role_pin_combinations(self):
        """Every permitted producer and consumer with a peer receives a transfer."""
        self.assertIs(check.validation_pairs, validation_pairs)
        self.assertIs(lifecycle.validation_pairs, validation_pairs)
        for specs in validation_topologies():
            with self.subTest(specs=[(s.role, s.pin) for s in specs]):
                mesh = [
                    (a.iid, b.iid)
                    for a in specs
                    for b in specs
                    if a.iid != b.iid and can_produce(a) and can_consume(b)
                ]
                ring = validation_pairs(specs)
                self.assertEqual(validation_pairs(specs, mesh=True), mesh)
                self.assertEqual(validation_pairs(specs), ring)
                self.assertEqual(len(ring), len(set(ring)))
                self.assertTrue(set(ring) <= set(mesh))
                self.assertEqual({a for a, _ in ring}, {a for a, _ in mesh})
                self.assertEqual({b for _, b in ring}, {b for _, b in mesh})
                for target in specs:
                    required = int(can_produce(target)) + int(can_consume(target))
                    available = int(any(a == target.iid for a, _ in mesh)) + int(
                        any(b == target.iid for _, b in mesh)
                    )
                    if required != available:
                        with self.assertRaises(ValueError):
                            recovery_pairs(target, specs)
                    else:
                        pairs = recovery_pairs(target, specs)
                        self.assertEqual(len(pairs), required)
                        self.assertTrue(set(pairs) <= set(mesh))

    def test_flexible_fleet_rotates_consumers(self):
        """Four flexible engines require four transfers in configured order."""
        specs = next(specs for specs in validation_topologies() if len(specs) == 4)
        self.assertEqual(
            validation_pairs(specs),
            [("e0", "e1"), ("e1", "e2"), ("e2", "e3"), ("e3", "e0")],
        )
