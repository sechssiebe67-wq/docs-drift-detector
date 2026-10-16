import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import trend  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402


def _finding(fid="f1", severity="high", resolved=False):
    return {
        "id": fid,
        "kind": "label",
        "page": "help/a.md",
        "title": "t",
        "old_value": "Save",
        "new_value": "Save changes",
        "resolved": resolved,
        "score": 40.0,
        "severity": severity,
    }


class FakeStore:
    """Minimal store double returning a fixed list of possibly-malformed scans."""

    def __init__(self, scans):
        self._scans = scans

    def list_workspace_scans(self, workspace):
        return list(self._scans)


class TrendUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def _scan(self, workspace, findings, name="release"):
        return self.store.create_scan(name, "release", findings, workspace=workspace)

    def test_empty_workspace_is_zeroed(self):
        document = trend.build_trend(self.store, "ws1")
        self.assertEqual(document["workspace"], "ws1")
        self.assertEqual(document["scans"], [])
        self.assertEqual(
            document["summary"],
            {
                "scans": 0,
                "total_findings": 0,
                "by_severity": {"high": 0, "medium": 0, "low": 0},
                "open_findings": 0,
                "high_open": 0,
                "suppressed": 0,
                "delta_high": 0,
            },
        )

    def test_single_scan(self):
        self._scan("ws1", [_finding("a", "high"), _finding("b", "low")])
        document = trend.build_trend(self.store, "ws1")
        self.assertEqual(len(document["scans"]), 1)
        entry = document["scans"][0]
        self.assertEqual(
            set(entry),
            {
                "scan_id",
                "created_at",
                "name",
                "total_findings",
                "by_severity",
                "open_findings",
                "high_open",
                "suppressed",
            },
        )
        self.assertEqual(entry["name"], "release")
        self.assertEqual(entry["total_findings"], 2)
        self.assertEqual(entry["by_severity"], {"high": 1, "medium": 0, "low": 1})
        self.assertEqual(entry["open_findings"], 2)
        self.assertEqual(entry["high_open"], 1)
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_multiple_scans_series_is_oldest_first(self):
        self._scan("ws1", [_finding("a", "high")], name="first")
        self._scan("ws1", [_finding("b", "high"), _finding("c", "high")], name="second")
        self._scan("ws1", [_finding("d", "high")], name="third")
        document = trend.build_trend(self.store, "ws1")
        self.assertEqual(
            [entry["name"] for entry in document["scans"]],
            ["first", "second", "third"],
        )
        self.assertEqual(
            [entry["high_open"] for entry in document["scans"]], [1, 2, 1]
        )
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_limit_selects_newest_but_presents_oldest_first(self):
        for index in range(12):
            self._scan("ws1", [_finding("f%d" % index, "low")], name="s%02d" % index)
        document = trend.build_trend(self.store, "ws1", limit=5)
        names = [entry["name"] for entry in document["scans"]]
        self.assertEqual(names, ["s07", "s08", "s09", "s10", "s11"])
        self.assertEqual(document["summary"]["scans"], 5)

    def test_limit_is_clamped(self):
        for index in range(3):
            self._scan("ws1", [_finding("f%d" % index, "low")], name="s%d" % index)
        self.assertEqual(len(trend.build_trend(self.store, "ws1", limit=0)["scans"]), 1)
        self.assertEqual(
            len(trend.build_trend(self.store, "ws1", limit=999)["scans"]), 3
        )
        self.assertEqual(
            len(trend.build_trend(self.store, "ws1", limit=-5)["scans"]), 1
        )

    def test_limit_clamped_to_max(self):
        scans = [
            {
                "id": "s%d" % index,
                "name": "s%d" % index,
                "created_at": "2026-01-01T00:00:%02d+00:00" % index,
                "workspace": "ws1",
                "findings": [],
                "stats": {},
            }
            for index in range(trend.MAX_LIMIT + 10)
        ]
        document = trend.build_trend(FakeStore(scans), "ws1", limit=1000)
        self.assertEqual(len(document["scans"]), trend.MAX_LIMIT)

    def test_malformed_scans_are_tolerated(self):
        scans = [
            {
                "id": "s2",
                "workspace": "ws1",
                "findings": [None, {"severity": "high", "resolved": False}],
                "stats": {"suppressed": "nonsense"},
            },
            None,
            "not a scan",
            {"id": "s1", "workspace": "ws1"},
        ]
        document = trend.build_trend(FakeStore(scans), "ws1")
        self.assertEqual(len(document["scans"]), 2)
        first, second = document["scans"]
        self.assertEqual(first["scan_id"], "s1")
        self.assertEqual(first["total_findings"], 0)
        self.assertEqual(first["open_findings"], 0)
        self.assertEqual(first["high_open"], 0)
        self.assertEqual(first["suppressed"], 0)
        self.assertEqual(second["scan_id"], "s2")
        self.assertEqual(second["total_findings"], 1)
        self.assertEqual(second["high_open"], 1)
        self.assertEqual(second["suppressed"], 0)

    def test_delta_high_positive_and_negative(self):
        self._scan("ws1", [_finding("a", "high")], name="start")
        self._scan("ws1", [_finding("b", "high"), _finding("c", "high")], name="worse")
        document = trend.build_trend(self.store, "ws1")
        self.assertEqual(document["summary"]["delta_high"], 1)

        improving = Store(os.path.join(self.tmp.name, "improving.json"))
        improving.create_scan("start", "release", [_finding("a", "high")], workspace="ws2")
        improving.create_scan("better", "release", [], workspace="ws2")
        document = trend.build_trend(improving, "ws2")
        self.assertEqual(document["summary"]["delta_high"], -1)

    def test_resolved_findings_are_not_open(self):
        self._scan(
            "ws1",
            [_finding("a", "high", resolved=True), _finding("b", "high")],
        )
        document = trend.build_trend(self.store, "ws1")
        entry = document["scans"][0]
        self.assertEqual(entry["total_findings"], 2)
        self.assertEqual(entry["by_severity"], {"high": 2, "medium": 0, "low": 0})
        self.assertEqual(entry["open_findings"], 1)
        self.assertEqual(entry["high_open"], 1)

    def test_suppressed_is_aggregated(self):
        self.store.create_scan(
            "R", "release", [_finding("a")], workspace="ws1", suppressed=3
        )
        self.store.create_scan(
            "R", "release", [_finding("b")], workspace="ws1", suppressed=2
        )
        document = trend.build_trend(self.store, "ws1")
        self.assertEqual(document["summary"]["suppressed"], 5)
        self.assertEqual(
            [entry["suppressed"] for entry in document["scans"]], [3, 2]
        )

    def test_other_workspaces_are_ignored(self):
        self._scan("ws1", [_finding("a")])
        self._scan("ws2", [_finding("b"), _finding("c")])
        self.assertEqual(len(trend.build_trend(self.store, "ws1")["scans"]), 1)
        self.assertEqual(len(trend.build_trend(self.store, "ws2")["scans"]), 1)


if __name__ == "__main__":
    unittest.main()
