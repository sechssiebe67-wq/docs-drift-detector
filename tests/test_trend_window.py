import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import buckets, rollup, timewindow, trend  # noqa: E402
from docs_drift_detector.server import create_server  # noqa: E402
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


def _scan(
    scan_id,
    workspace=None,
    created_at="2026-01-01T00:00:00+00:00",
    findings=(),
    suppressed=0,
    name="release",
):
    scan = {
        "id": scan_id,
        "name": name,
        "created_at": created_at,
        "findings": list(findings),
        "stats": {"suppressed": suppressed},
    }
    if workspace is not None:
        scan["workspace"] = workspace
    return scan


SERIES = [
    _scan("s01", workspace="ws1", created_at="2026-01-01T00:00:00+00:00",
          findings=[_finding("a", "high")]),
    _scan("s10", workspace="ws1", created_at="2026-01-10T00:00:00+00:00",
          findings=[_finding("b", "high"), _finding("c", "high")]),
    _scan("s20", workspace="ws1", created_at="2026-01-20T00:00:00+00:00",
          findings=[_finding("d", "high"), _finding("e", "high"),
                    _finding("f", "high")]),
    _scan("s31", workspace="ws1", created_at="2026-01-31T00:00:00+00:00",
          findings=[_finding("g", "high")]),
    _scan("s40", workspace="ws1", created_at="2026-02-10T00:00:00+00:00",
          findings=[_finding("h", "high"), _finding("i", "high")]),
]


class TrendWindowBuilderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))
        for scan in SERIES:
            self.store._scans[scan["id"]] = scan

    def tearDown(self):
        self.tmp.cleanup()

    def _ids(self, document):
        return [entry["scan_id"] for entry in document["scans"]]

    def test_window_keeps_subset_oldest_first(self):
        start, end = timewindow.parse_window("2026-01-05", "2026-01-20")
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        self.assertEqual(self._ids(document), ["s10", "s20"])
        self.assertEqual(document["summary"]["scans"], 2)
        self.assertEqual(document["summary"]["high_open"], 5)
        self.assertEqual(document["summary"]["delta_high"], 1)

    def test_open_ended_from_only(self):
        start, end = timewindow.parse_window("2026-01-15", None)
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        self.assertEqual(self._ids(document), ["s20", "s31", "s40"])

    def test_open_ended_to_only(self):
        start, end = timewindow.parse_window(None, "2026-01-15")
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        self.assertEqual(self._ids(document), ["s01", "s10"])

    def test_empty_window_is_zeroed(self):
        start, end = timewindow.parse_window("2026-03-01", "2026-03-31")
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
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

    def test_single_scan_window_delta_is_zero(self):
        start, end = timewindow.parse_window("2026-01-15", "2026-01-25")
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        self.assertEqual(self._ids(document), ["s20"])
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_limit_applies_after_filtering(self):
        start, end = timewindow.parse_window("2026-01-05", "2026-01-25")
        document = trend.build_trend(
            self.store, "ws1", limit=1, from_time=start, to_time=end
        )
        self.assertEqual(self._ids(document), ["s20"])

    def test_no_window_matches_legacy_call(self):
        self.assertEqual(
            trend.build_trend(self.store, "ws1"),
            trend.build_trend(self.store, "ws1", from_time=None, to_time=None),
        )

    def test_unparseable_created_at_excluded_when_window_active(self):
        self.store._scans["bad"] = {
            "id": "bad",
            "workspace": "ws1",
            "created_at": "not-a-date",
            "findings": [_finding("x", "high")],
            "stats": {},
        }
        start, end = timewindow.parse_window(None, None)
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        # An unbounded (None, None) window is a no-op: malformed is tolerated.
        self.assertEqual(len(document["scans"]), 6)
        start, end = timewindow.parse_window("2026-01-01", "2026-12-31")
        document = trend.build_trend(
            self.store, "ws1", from_time=start, to_time=end
        )
        self.assertNotIn("bad", self._ids(document))


class BucketWindowBuilderTest(unittest.TestCase):
    def test_workspace_buckets_filter_out_of_window(self):
        start, end = timewindow.parse_window("2026-01-05", "2026-01-20")
        document = buckets.build_workspace_buckets(
            SERIES, "ws1", bucket="day", from_time=start, to_time=end
        )
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-10", "2026-01-20"],
        )
        self.assertEqual(document["summary"]["scans"], 2)
        self.assertEqual(document["summary"]["buckets"], 2)

    def test_workspace_buckets_empty_window_is_zeroed(self):
        start, end = timewindow.parse_window("2026-03-01", "2026-03-31")
        document = buckets.build_workspace_buckets(
            SERIES, "ws1", bucket="week", from_time=start, to_time=end
        )
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["buckets"], 0)
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_rollup_buckets_filter_per_workspace(self):
        scans = SERIES + [
            _scan("r1", workspace="ws2", created_at="2026-01-19T00:00:00+00:00",
                  findings=[_finding("r", "high")]),
        ]
        start, end = timewindow.parse_window("2026-01-05", "2026-01-20")
        document = buckets.build_rollup_buckets(
            scans, bucket="day", from_time=start, to_time=end
        )
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]], ["ws1", "ws2"]
        )
        # Only ws1 has scans outside the window, so its count reflects filtering.
        self.assertEqual(document["summary"]["scans"], 3)

    def test_rollup_buckets_empty_window(self):
        start, end = timewindow.parse_window("2026-03-01", "2026-03-31")
        document = buckets.build_rollup_buckets(
            SERIES, bucket="week", from_time=start, to_time=end
        )
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["workspaces"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["workspaces"], 0)


class RollupWindowBuilderTest(unittest.TestCase):
    def test_window_filters_workspaces_and_summary(self):
        scans = SERIES + [
            _scan("r1", workspace="ws2", created_at="2026-01-19T00:00:00+00:00",
                  findings=[_finding("r", "high")]),
        ]
        start, end = timewindow.parse_window("2026-01-05", "2026-01-20")
        document = rollup.build_rollup(scans, from_time=start, to_time=end)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ["s10", "r1", "s20"]
        )
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]], ["ws1", "ws2"]
        )
        self.assertEqual(document["summary"]["scans"], 3)

    def test_empty_window_is_zeroed(self):
        start, end = timewindow.parse_window("2026-03-01", "2026-03-31")
        document = rollup.build_rollup(SERIES, from_time=start, to_time=end)
        self.assertEqual(document["scans"], [])
        self.assertEqual(document["workspaces"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["delta_high"], 0)


class WindowServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = os.path.join(self.tmp.name, "scans.json")
        self.store = Store(self.store_path)
        self.server = create_server("127.0.0.1", 0, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def _inject(self, scan):
        self.store._scans[scan["id"]] = scan
        return scan

    def test_workspace_trend_applies_window(self):
        for scan in SERIES:
            self._inject(scan)
        status, document = self._request(
            "GET", "/api/workspaces/ws1/trend?from=2026-01-05&to=2026-01-20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ["s10", "s20"]
        )
        self.assertEqual(document["summary"]["scans"], 2)
        self.assertEqual(document["summary"]["delta_high"], 1)

    def test_full_timestamp_bound_accepted(self):
        for scan in SERIES:
            self._inject(scan)
        path = (
            "/api/workspaces/ws1/trend?from="
            + urllib.parse.quote("2026-01-05T00:00:00+00:00")
        )
        status, document = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]],
            ["s10", "s20", "s31", "s40"],
        )

    def test_empty_window_returns_empty_not_error(self):
        for scan in SERIES:
            self._inject(scan)
        status, document = self._request(
            "GET", "/api/workspaces/ws1/trend?from=2026-03-01&to=2026-03-31"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["scans"], [])
        self.assertEqual(document["summary"]["scans"], 0)

    def test_workspace_buckets_apply_window(self):
        for scan in SERIES:
            self._inject(scan)
        status, document = self._request(
            "GET",
            "/api/workspaces/ws1/trend/buckets?bucket=day"
            "&from=2026-01-05&to=2026-01-20",
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-10", "2026-01-20"],
        )
        self.assertEqual(document["summary"]["scans"], 2)

    def test_cross_workspace_rollup_applies_window(self):
        for scan in SERIES:
            self._inject(scan)
        self._inject(
            _scan("r1", workspace="ws2", created_at="2026-01-19T00:00:00+00:00",
                  findings=[_finding("r", "high")])
        )
        status, document = self._request(
            "GET", "/api/trend?from=2026-01-05&to=2026-01-20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ["s10", "r1", "s20"]
        )
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]], ["ws1", "ws2"]
        )

    def test_cross_workspace_buckets_apply_window(self):
        for scan in SERIES:
            self._inject(scan)
        status, document = self._request(
            "GET", "/api/trend/buckets?bucket=day&from=2026-01-05&to=2026-01-20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-10", "2026-01-20"],
        )

    def test_invalid_from_or_to_returns_400_on_all_four(self):
        paths = [
            "/api/workspaces/ws1/trend?from=not-a-date",
            "/api/workspaces/ws1/trend?to=not-a-date",
            "/api/workspaces/ws1/trend/buckets?from=not-a-date",
            "/api/workspaces/ws1/trend/buckets?to=not-a-date",
            "/api/trend?from=not-a-date",
            "/api/trend?to=not-a-date",
            "/api/trend/buckets?from=not-a-date",
            "/api/trend/buckets?to=not-a-date",
        ]
        for path in paths:
            status, payload = self._request("GET", path)
            self.assertEqual(status, 400, path)
            self.assertIn("error", payload)

    def test_blank_bound_is_unbounded(self):
        for scan in SERIES:
            self._inject(scan)
        status, document = self._request(
            "GET", "/api/workspaces/ws1/trend?from=&to="
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(document["scans"]), len(SERIES))

    def test_read_only_with_windows_leaves_store_unchanged(self):
        for scan in SERIES:
            self._inject(scan)
        self.store._persist()
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            self._request(
                "GET", "/api/workspaces/ws1/trend?from=2026-01-05&to=2026-01-20"
            )
            self._request(
                "GET",
                "/api/workspaces/ws1/trend/buckets?from=2026-01-05&to=2026-01-20",
            )
            self._request("GET", "/api/trend?from=2026-01-05&to=2026-01-20")
            self._request(
                "GET", "/api/trend/buckets?from=2026-01-05&to=2026-01-20"
            )
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
