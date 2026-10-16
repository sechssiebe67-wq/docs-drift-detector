import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import buckets, dashboard, timewindow, trend  # noqa: E402
from docs_drift_detector.server import create_server  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402
from docs_drift_detector.trend import MAX_LIMIT  # noqa: E402


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
    workspace="ws1",
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


class _ScanStore:
    """Minimal store double exposing only the scan-list reader."""

    def __init__(self, scans):
        self._scans = scans

    def list_workspace_scans(self, workspace):
        return list(self._scans)


class _DigestStore:
    """Minimal store double exposing only the digest-history reader."""

    def __init__(self, digests):
        self._digests = digests

    def list_workspace_digests(self, workspace, limit=10):
        items = list(self._digests)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            items = items[:limit]
        return items


class DashboardUnitTest(unittest.TestCase):
    def test_empty_workspace_is_zeroed(self):
        document = dashboard.build_dashboard([], "ws1")
        self.assertEqual(document["workspace"], "ws1")
        self.assertIn("generated_at", document)
        self.assertEqual(
            document["window"],
            {"from": None, "to": None, "limit": 10, "bucket": "week"},
        )
        self.assertEqual(document["trend"]["workspace"], "ws1")
        self.assertEqual(document["trend"]["scans"], [])
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["digests"], [])
        summary = document["summary"]
        self.assertEqual(summary["total_findings"], 0)
        self.assertEqual(summary["by_severity"], {"high": 0, "medium": 0, "low": 0})
        self.assertEqual(summary["open_findings"], 0)
        self.assertEqual(summary["high_open"], 0)
        self.assertEqual(summary["suppressed"], 0)
        self.assertEqual(summary["delta_high"], 0)
        self.assertEqual(summary["scans"], 0)
        self.assertEqual(summary["buckets"], 0)
        self.assertIsNone(summary["latest_digest"])

    def test_document_reuses_trend_and_bucket_builders(self):
        scans = [
            _scan(
                "a",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "b",
                created_at="2026-01-12T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            ),
        ]
        document = dashboard.build_dashboard(scans, "ws1", limit=5, bucket="week")
        self.assertEqual(
            set(document),
            {
                "workspace",
                "generated_at",
                "window",
                "trend",
                "buckets",
                "digests",
                "summary",
            },
        )
        expected_trend = trend.build_trend(
            _ScanStore(scans), "ws1", 5
        )
        expected_buckets = buckets.build_workspace_buckets(
            scans, "ws1", bucket="week", limit=5
        )
        self.assertEqual(document["trend"], expected_trend)
        self.assertEqual(document["buckets"], expected_buckets["buckets"])
        self.assertEqual(document["summary"]["total_findings"], 3)
        self.assertEqual(document["summary"]["high_open"], 3)
        self.assertEqual(document["summary"]["scans"], 2)
        self.assertEqual(document["summary"]["buckets"], 2)
        self.assertEqual(document["summary"]["delta_high"], 1)

    def test_malformed_scans_are_skipped(self):
        scans = [
            None,
            "not a scan",
            123,
            {"id": "", "created_at": "2026-01-05T00:00:00+00:00", "findings": []},
            {"id": "bad-time", "created_at": "not-a-date", "findings": []},
            {"created_at": "2026-01-05T00:00:00+00:00", "findings": []},
            _scan("ok", created_at="2026-01-05T00:00:00+00:00", findings=[_finding("a")]),
        ]
        document = dashboard.build_dashboard(scans, "ws1")
        self.assertEqual(document["summary"]["scans"], 1)
        self.assertEqual(len(document["trend"]["scans"]), 1)
        self.assertEqual(len(document["buckets"]), 1)
        self.assertEqual(document["trend"]["scans"][0]["scan_id"], "ok")

    def test_limit_is_clamped(self):
        scans = [
            _scan(
                "s%02d" % index,
                created_at="2026-01-%02dT00:00:00+00:00" % (index + 1),
            )
            for index in range(5)
        ]
        low = dashboard.build_dashboard(scans, "ws1", limit=0, bucket="day")
        self.assertEqual(low["window"]["limit"], 1)
        self.assertEqual(len(low["buckets"]), 1)
        high = dashboard.build_dashboard(scans, "ws1", limit=999, bucket="day")
        self.assertEqual(high["window"]["limit"], MAX_LIMIT)

    def test_bucket_defaults_to_week_and_rejects_others(self):
        default = dashboard.build_dashboard([], "ws1")
        self.assertEqual(default["window"]["bucket"], "week")
        unknown = dashboard.build_dashboard([], "ws1", bucket="month")
        self.assertEqual(unknown["window"]["bucket"], "week")
        day = dashboard.build_dashboard([], "ws1", bucket="day")
        self.assertEqual(day["window"]["bucket"], "day")

    def test_from_to_filter_scans(self):
        scans = [
            _scan(
                "old",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a")],
            ),
            _scan(
                "new",
                created_at="2026-02-01T00:00:00+00:00",
                findings=[_finding("b"), _finding("c")],
            ),
        ]
        start = timewindow.parse_bound("2026-01-15", "from")
        document = dashboard.build_dashboard(scans, "ws1", from_time=start)
        self.assertEqual(document["summary"]["scans"], 1)
        self.assertEqual(document["summary"]["total_findings"], 2)
        self.assertEqual(document["window"]["from"], "2026-01-15T00:00:00+00:00")
        self.assertIsNone(document["window"]["to"])
        self.assertEqual(
            [entry["scan_id"] for entry in document["trend"]["scans"]], ["new"]
        )

    def test_digest_history_is_embedded_newest_first(self):
        digests = [
            {"scan_id": "newer", "total_findings": 2},
            {"scan_id": "older", "total_findings": 1},
        ]
        document = dashboard.build_dashboard(
            [], "ws1", limit=1, store=_DigestStore(digests)
        )
        self.assertEqual(len(document["digests"]), 1)
        self.assertEqual(document["digests"][0]["scan_id"], "newer")
        self.assertEqual(document["summary"]["latest_digest"]["scan_id"], "newer")

    def test_unknown_workspace_zeroed_summary(self):
        document = dashboard.build_dashboard([], "ghost")
        self.assertEqual(document["trend"]["scans"], [])
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertIsNone(document["summary"]["latest_digest"])


class DashboardHTTPTest(unittest.TestCase):
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
        self.store._persist()
        return scan

    def _create_scan(self, body):
        status, scan = self._request("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        return scan

    def test_dashboard_shape_and_matches_sibling_endpoints(self):
        self._inject(
            _scan(
                "d1",
                workspace="wsDash",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        self._inject(
            _scan(
                "d2",
                workspace="wsDash",
                created_at="2026-01-12T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "low")],
            )
        )
        status, document = self._request(
            "GET", "/api/workspaces/wsDash/dashboard?limit=5&bucket=week"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(document),
            {
                "workspace",
                "generated_at",
                "window",
                "trend",
                "buckets",
                "digests",
                "summary",
            },
        )
        self.assertEqual(document["workspace"], "wsDash")
        self.assertEqual(document["window"]["limit"], 5)
        self.assertEqual(document["window"]["bucket"], "week")

        status, trend_doc = self._request(
            "GET", "/api/workspaces/wsDash/trend?limit=5"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["trend"], trend_doc)

        status, bucket_doc = self._request(
            "GET", "/api/workspaces/wsDash/trend/buckets?limit=5&bucket=week"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["buckets"], bucket_doc["buckets"])

        for key in (
            "total_findings",
            "by_severity",
            "open_findings",
            "high_open",
            "suppressed",
            "delta_high",
            "scans",
            "buckets",
            "latest_digest",
        ):
            self.assertIn(key, document["summary"])
        self.assertIsNone(document["summary"]["latest_digest"])

    def test_digest_history_through_http(self):
        self._inject(
            _scan(
                "dg1",
                workspace="wsDigest",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        self.store.record_workspace_digest(
            "wsDigest",
            {"scan_id": "dg1", "total_findings": 1, "by_severity": {"high": 1}},
            {"ok": True, "attempts": 1},
        )
        status, document = self._request(
            "GET", "/api/workspaces/wsDigest/dashboard"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(document["digests"]), 1)
        self.assertEqual(document["digests"][0]["scan_id"], "dg1")
        self.assertEqual(document["summary"]["latest_digest"]["scan_id"], "dg1")

    def test_invalid_workspace_returns_400(self):
        status, payload = self._request(
            "GET", "/api/workspaces/bad%20id!/dashboard"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_malformed_from_returns_400(self):
        status, _ = self._request(
            "GET", "/api/workspaces/ws1/dashboard?from=not-a-date"
        )
        self.assertEqual(status, 400)

    def test_unknown_bucket_returns_400(self):
        status, _ = self._request(
            "GET", "/api/workspaces/ws1/dashboard?bucket=month"
        )
        self.assertEqual(status, 400)

    def test_non_integer_limit_returns_400(self):
        status, _ = self._request(
            "GET", "/api/workspaces/ws1/dashboard?limit=abc"
        )
        self.assertEqual(status, 400)

    def test_unknown_workspace_returns_empty_200(self):
        status, document = self._request(
            "GET", "/api/workspaces/ghostWs/dashboard"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["trend"]["scans"], [])
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["total_findings"], 0)
        self.assertIsNone(document["summary"]["latest_digest"])

    def test_from_to_filter_through_http(self):
        self._inject(
            _scan(
                "old",
                workspace="wsWindow",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a")],
            )
        )
        self._inject(
            _scan(
                "new",
                workspace="wsWindow",
                created_at="2026-02-01T00:00:00+00:00",
                findings=[_finding("b")],
            )
        )
        status, document = self._request(
            "GET", "/api/workspaces/wsWindow/dashboard?from=2026-01-15"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["summary"]["scans"], 1)
        self.assertEqual(
            [entry["scan_id"] for entry in document["trend"]["scans"]], ["new"]
        )

    def test_read_only_and_no_side_effects(self):
        self._create_scan(
            {
                "name": "read only",
                "workspace": "wsDashReadOnly",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            }
        )
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            status, _ = self._request(
                "GET", "/api/workspaces/wsDashReadOnly/dashboard"
            )
        self.assertEqual(status, 200)
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
