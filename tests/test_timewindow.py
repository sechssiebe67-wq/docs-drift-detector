import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import timewindow  # noqa: E402


class ParseBoundTest(unittest.TestCase):
    def test_plain_date_is_midnight_utc(self):
        self.assertEqual(
            timewindow.parse_bound("2026-01-05"),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )

    def test_full_iso_timestamp_keeps_offset(self):
        parsed = timewindow.parse_bound("2026-01-05T12:30:00+02:00")
        self.assertEqual(parsed.utcoffset().total_seconds(), 2 * 3600)
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 10)

    def test_zulu_suffix_is_utc(self):
        self.assertEqual(
            timewindow.parse_bound("2026-01-05T12:00:00Z"),
            datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertEqual(
            timewindow.parse_bound("2026-01-05T12:00:00"),
            datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_missing_or_blank_is_unbounded(self):
        self.assertIsNone(timewindow.parse_bound(None))
        self.assertIsNone(timewindow.parse_bound(""))
        self.assertIsNone(timewindow.parse_bound("   "))

    def test_malformed_raises_window_error(self):
        for value in ("not-a-date", "2026-13-45", "2026-01-05T99:00:00", "hello"):
            with self.assertRaises(timewindow.WindowError, msg=value):
                timewindow.parse_bound(value)

    def test_non_string_raises_window_error(self):
        with self.assertRaises(timewindow.WindowError):
            timewindow.parse_bound(123)


class ParseWindowTest(unittest.TestCase):
    def test_both_sides_parse(self):
        start, end = timewindow.parse_window("2026-01-01", "2026-01-31")
        self.assertEqual(start, datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 1, 31, tzinfo=timezone.utc))

    def test_each_side_may_be_absent(self):
        self.assertEqual(timewindow.parse_window("2026-01-01", None)[1], None)
        self.assertEqual(timewindow.parse_window(None, "2026-01-31")[0], None)
        self.assertEqual(timewindow.parse_window(None, None), (None, None))

    def test_malformed_to_raises(self):
        with self.assertRaises(timewindow.WindowError):
            timewindow.parse_window("2026-01-01", "nope")


class InWindowTest(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 1, 31, tzinfo=timezone.utc)

    def test_bounds_are_inclusive(self):
        self.assertTrue(timewindow.in_window(self.start, self.start, self.end))
        self.assertTrue(timewindow.in_window(self.end, self.start, self.end))

    def test_outside_is_excluded(self):
        self.assertFalse(
            timewindow.in_window(
                datetime(2025, 12, 31, tzinfo=timezone.utc), self.start, self.end
            )
        )
        self.assertFalse(
            timewindow.in_window(
                datetime(2026, 2, 1, tzinfo=timezone.utc), self.start, self.end
            )
        )

    def test_open_ended_sides(self):
        moment = datetime(2030, 6, 1, tzinfo=timezone.utc)
        self.assertTrue(timewindow.in_window(moment, self.start, None))
        self.assertFalse(timewindow.in_window(moment, None, self.end))
        self.assertTrue(timewindow.in_window(moment, None, None))


class ParseStoredTest(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(
            timewindow.parse_stored("2026-01-05T00:00:00+00:00"),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            timewindow.parse_stored("2026-01-05T00:00:00Z"),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
        )

    def test_malformed_values_return_none(self):
        self.assertIsNone(timewindow.parse_stored(""))
        self.assertIsNone(timewindow.parse_stored("not-a-date"))
        self.assertIsNone(timewindow.parse_stored(None))
        self.assertIsNone(timewindow.parse_stored(5))


if __name__ == "__main__":
    unittest.main()
