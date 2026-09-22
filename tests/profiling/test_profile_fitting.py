"""Check fitted coefficients against known curves and held-out observations."""

import unittest

from narwhal.profiling.fitting import (
    decode_cross_validation_mape,
    decode_mape,
    fit_decode_plane,
    fit_quadratic,
)


class ProfileFittingTests(unittest.TestCase):
    """Synthetic samples separate numerical fitting from engine measurement."""

    def test_quadratic_recovers_coefficients_at_token_scale(self):
        """Input scaling preserves the coefficients in original token units."""
        expected = (2e-9, 3e-5, 0.25)
        samples = [
            (x, expected[0] * x * x + expected[1] * x + expected[2])
            for x in (0, 1024, 8192, 32768, 131072)
        ]
        actual = fit_quadratic(samples)
        for observed, wanted in zip(actual, expected, strict=True):
            self.assertAlmostEqual(observed / wanted, 1.0, places=8)

    def test_quadratic_boundary_refits_the_intercept(self):
        """A falling curve has its constrained optimum at the sample mean."""
        self.assertEqual(fit_quadratic([(0, 3), (1, 2), (2, 1)]), (0.0, 0.0, 2.0))

    def test_zero_prefill_measurements_fit_the_zero_curve(self):
        """Zero-duration samples produce three zero coefficients."""
        self.assertEqual(fit_quadratic([(0, 0), (1, 0), (2, 0)]), (0.0, 0.0, 0.0))

    def test_nearly_equal_lengths_retain_a_stable_constant_fit(self):
        """Numerically singular faces still permit an identifiable constant fit."""
        samples = [(1_000_000_000 + offset, 2) for offset in range(3)]
        self.assertEqual(fit_quadratic(samples), (0.0, 0.0, 2.0))

    def test_quadratic_requires_three_distinct_lengths(self):
        """Sample count and independent input lengths have separate checks."""
        for samples, message in (
            ([(0, 1), (1, 2)], "at least three samples"),
            ([(0, 1), (0, 2), (1, 3)], "three distinct input lengths"),
        ):
            with self.subTest(samples=samples), self.assertRaisesRegex(ValueError, message):
                fit_quadratic(samples)

    def test_quadratic_rejects_invalid_coordinates(self):
        """Both input lengths and observed times must be finite and nonnegative."""
        for coordinate in range(2):
            for value in (-1, float("nan"), float("inf"), -float("inf")):
                row = [2, 3]
                row[coordinate] = value
                with (
                    self.subTest(coordinate=coordinate, value=value),
                    self.assertRaisesRegex(ValueError, "finite and nonnegative"),
                ):
                    fit_quadratic([(0, 1), (1, 2), tuple(row)])

    def test_decode_plane_preserves_axis_order_and_units(self):
        """Returned coefficients order KV slope before request slope."""
        samples = [
            (r, k, 0.02 * r + 3e-7 * k + 0.004) for r in (1, 4, 16) for k in (1024, 8192, 65536)
        ]
        actual = fit_decode_plane(samples)
        for observed, wanted in zip(actual, (3e-7, 0.02, 0.004), strict=True):
            self.assertAlmostEqual(observed / wanted, 1.0, places=8)

    def test_decode_boundary_refits_the_remaining_axes(self):
        """Removing a negative request slope shifts the intercept to eight."""
        samples = [(r, k, 10 - r + 0.5 * k) for r in (1, 3) for k in (10, 30)]
        for observed, wanted in zip(fit_decode_plane(samples), (0.5, 0.0, 8.0), strict=True):
            self.assertAlmostEqual(observed, wanted, places=10)

    def test_decode_requires_independent_request_and_kv_axes(self):
        """Three collinear samples leave the decode plane unidentifiable."""
        for samples, message in (
            ([(1, 10, 1), (2, 20, 2)], "at least three samples"),
            ([(1, 10, 1), (2, 20, 2), (3, 30, 3)], "singular system"),
        ):
            with self.subTest(samples=samples), self.assertRaisesRegex(ValueError, message):
                fit_decode_plane(samples)

    def test_decode_rejects_invalid_coordinates(self):
        """Each request count, KV count and timing must be finite and positive."""
        for coordinate in range(3):
            for value in (0, -1, float("nan"), float("inf"), -float("inf")):
                row = [2, 30, 3]
                row[coordinate] = value
                with (
                    self.subTest(coordinate=coordinate, value=value),
                    self.assertRaisesRegex(ValueError, "finite and positive"),
                ):
                    fit_decode_plane([(1, 10, 1), (1, 20, 2), tuple(row)])

    def test_mape_averages_relative_errors_per_observation(self):
        """Each observation contributes equally after division by its measured time."""
        # Predictions are 10 and 20; relative errors are 1 and 0.5.
        self.assertEqual(decode_mape([(1, 10, 5), (2, 20, 40)], (1, 0, 0)), 0.75)
        self.assertEqual(decode_mape([], (1, 0, 0)), 0.0)
        self.assertAlmostEqual(decode_mape([(1, 1, 0)], (1, 0, 0)) / 1e9, 1.0)

    def test_cross_validation_scores_each_held_out_corner(self):
        """The four corner fits each miss the held-out observation by four."""
        samples = [(1, 10, 10), (1, 20, 20), (2, 10, 30), (2, 20, 44)]
        # The nonnegative constraint binds on some three-point fits, so use
        # a positive intercept to keep every held-out fit in the interior.
        samples = [(r, k, y + 100) for r, k, y in samples]
        expected = sum(4 / y for _, _, y in samples) / 4
        self.assertAlmostEqual(decode_cross_validation_mape(samples), expected, places=10)

    def test_cross_validation_requires_identifiable_training_folds(self):
        """A unique point can make the full fit identifiable while its fold is singular."""
        samples = [(1, 10, 1), (2, 20, 2), (3, 30, 3), (1, 30, 2)]
        self.assertIsNone(decode_cross_validation_mape(samples[:3]))
        self.assertEqual(len(fit_decode_plane(samples)), 3)
        self.assertIsNone(decode_cross_validation_mape(samples))


if __name__ == "__main__":
    unittest.main()
