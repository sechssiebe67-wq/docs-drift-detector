import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import buckets  # noqa: E402
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


class BucketUnitTest(unittest.TestCase):
    def test_empty_input_is_zeroed(self):
        document = buckets.build_workspace_buckets([], "ws1")
        self.assertEqual(document["workspace"], "ws1")
        self.assertEqual(document["bucket"], "week")
        self.assertEqual(document["buckets"], [])
        self.assertEqual(
            document["summary"],
            {
                "scans": 0,
                "buckets": 0,
                "total_findings": 0,
                "by_severity": {"high": 0, "medium": 0, "low": 0},
                "open_findings": 0,
                "high_open": 0,
                "suppressed": 0,
                "delta_high": 0,
            },
        )

    def test_non_list_input_is_tolerated(self):
        document = buckets.build_workspace_buckets(None, "ws1")
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["summary"]["scans"], 0)

    def test_day_bucketing_keys_and_ranges(self):
        scans = [
            _scan("a", workspace="ws1", created_at="2026-01-05T08:00:00+00:00"),
            _scan("b", workspace="ws1", created_at="2026-01-05T23:30:00+00:00"),
            _scan("c", workspace="ws1", created_at="2026-01-06T00:10:00+00:00"),
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="day")
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-05", "2026-01-06"],
        )
        first = document["buckets"][0]
        self.assertEqual(first["start"], "2026-01-05T00:00:00+00:00")
        self.assertEqual(first["end"], "2026-01-05T23:59:59+00:00")
        self.assertEqual(first["scans"], 2)
        self.assertEqual(document["summary"]["buckets"], 2)

    def test_week_bucketing_uses_iso_weeks_starting_monday(self):
        scans = [
            _scan("a", workspace="ws1", created_at="2026-01-05T00:00:00+00:00"),
            _scan("b", workspace="ws1", created_at="2026-01-11T23:59:59+00:00"),
            _scan("c", workspace="ws1", created_at="2026-01-12T00:00:01+00:00"),
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="week")
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-W02", "2026-W03"],
        )
        first = document["buckets"][0]
        self.assertEqual(first["start"], "2026-01-05T00:00:00+00:00")
        self.assertEqual(first["end"], "2026-01-11T23:59:59+00:00")
        self.assertEqual(first["scans"], 2)

    def test_buckets_are_ordered_oldest_first(self):
        scans = [
            _scan("c", workspace="ws1", created_at="2026-01-07T00:00:00+00:00"),
            _scan("a", workspace="ws1", created_at="2026-01-05T00:00:00+00:00"),
            _scan("b", workspace="ws1", created_at="2026-01-06T00:00:00+00:00"),
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="day")
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-05", "2026-01-06", "2026-01-07"],
        )

    def test_bucket_aggregates_reuse_per_scan_summaries(self):
        scans = [
            _scan(
                "a",
                workspace="ws1",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high"), _finding("b", "low")],
            ),
            _scan(
                "b",
                workspace="ws1",
                created_at="2026-01-05T12:00:00+00:00",
                findings=[_finding("c", "high", resolved=True)],
                suppressed=4,
            ),
        ]
        bucket = buckets.build_workspace_buckets(scans, "ws1", bucket="day")[
            "buckets"
        ][0]
        self.assertEqual(
            set(bucket),
            {
                "key",
                "start",
                "end",
                "scans",
                "total_findings",
                "by_severity",
                "open_findings",
                "high_open",
                "suppressed",
            },
        )
        self.assertEqual(bucket["scans"], 2)
        self.assertEqual(bucket["total_findings"], 3)
        self.assertEqual(bucket["by_severity"], {"high": 2, "medium": 0, "low": 1})
        self.assertEqual(bucket["open_findings"], 2)
        self.assertEqual(bucket["high_open"], 1)
        self.assertEqual(bucket["suppressed"], 4)

    def test_malformed_scans_are_skipped(self):
        scans = [
            None,
            "not a scan",
            123,
            {"id": "missing-time", "created_at": "", "findings": []},
            {"id": "", "created_at": "2026-01-05T00:00:00+00:00", "findings": []},
            {"id": "bad-time", "created_at": "not-a-date", "findings": []},
            {"created_at": "2026-01-05T00:00:00+00:00", "findings": []},
            _scan("ok", workspace="ws1", created_at="2026-01-05T00:00:00+00:00"),
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="day")
        self.assertEqual(len(document["buckets"]), 1)
        self.assertEqual(document["buckets"][0]["scans"], 1)
        self.assertEqual(document["buckets"][0]["key"], "2026-01-05")

    def test_limit_keeps_most_recent_buckets_oldest_first(self):
        scans = [
            _scan(
                "s%02d" % index,
                workspace="ws1",
                created_at="2026-01-%02dT00:00:00+00:00" % (index + 1),
            )
            for index in range(5)
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="day", limit=2)
        self.assertEqual(
            [bucket["key"] for bucket in document["buckets"]],
            ["2026-01-04", "2026-01-05"],
        )
        self.assertEqual(
            buckets.build_workspace_buckets(scans, "ws1", bucket="day", limit=0)[
                "summary"
            ]["buckets"],
            1,
        )
        self.assertEqual(
            len(
                buckets.build_workspace_buckets(
                    scans, "ws1", bucket="day", limit=999
                )["buckets"]
            ),
            5,
        )

    def test_limit_clamped_to_max(self):
        base = date(2026, 1, 1)
        scans = [
            _scan(
                "s%03d" % index,
                workspace="ws1",
                created_at=(base + timedelta(days=index)).isoformat()
                + "T00:00:00+00:00",
            )
            for index in range(MAX_LIMIT + 20)
        ]
        document = buckets.build_workspace_buckets(
            scans, "ws1", bucket="day", limit=1000
        )
        self.assertEqual(len(document["buckets"]), MAX_LIMIT)

    def test_delta_high_newest_minus_oldest_bucket(self):
        scans = [
            _scan(
                "a",
                workspace="ws1",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "b",
                workspace="ws1",
                created_at="2026-01-12T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            ),
        ]
        document = buckets.build_workspace_buckets(scans, "ws1", bucket="week")
        self.assertEqual(document["summary"]["delta_high"], 1)

        single = buckets.build_workspace_buckets(
            [
                _scan(
                    "a",
                    workspace="ws1",
                    created_at="2026-01-05T00:00:00+00:00",
                    findings=[_finding("a", "high")],
                )
            ],
            "ws1",
            bucket="week",
        )
        self.assertEqual(single["summary"]["delta_high"], 0)

    def test_rollup_bucket_breakdown_and_workspace_ordering(self):
        scans = [
            _scan(
                "s1",
                workspace="wsB",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "s2",
                workspace="wsA",
                created_at="2026-01-05T01:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            ),
            _scan(
                "s3",
                workspace="wsA",
                created_at="2026-01-06T00:00:00+00:00",
                findings=[_finding("d", "high")],
            ),
        ]
        document = buckets.build_rollup_buckets(scans, bucket="week")
        self.assertEqual(
            set(document), {"bucket", "buckets", "workspaces", "summary", "next_cursor"}
        )
        self.assertEqual(len(document["buckets"]), 1)
        breakdown = document["buckets"][0]["workspaces"]
        self.assertEqual([entry["workspace"] for entry in breakdown], ["wsA", "wsB"])
        self.assertEqual([entry["high_open"] for entry in breakdown], [3, 1])
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]],
            ["wsA", "wsB"],
        )
        self.assertEqual(document["summary"]["workspaces"], 2)
        self.assertEqual(document["buckets"][0]["scans"], 3)

    def test_rollup_scans_without_workspace_use_empty_bucket(self):
        scans = [
            _scan(
                "s1",
                workspace=None,
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        ]
        document = buckets.build_rollup_buckets(scans, bucket="week")
        self.assertEqual(document["workspaces"][0]["workspace"], "")


class BucketServerTest(unittest.TestCase):
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

    def _create_scan(self, body):
        status, scan = self._request("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        return scan

    def test_empty_workspace_returns_200_with_empty_buckets(self):
        status, document = self._request(
            "GET", "/api/workspaces/unknownWs/trend/buckets"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["workspace"], "unknownWs")
        self.assertEqual(document["bucket"], "week")
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["buckets"], 0)
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_invalid_workspace_returns_400(self):
        status, payload = self._request(
            "GET", "/api/workspaces/bad%20id!/trend/buckets"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_unsupported_bucket_returns_400(self):
        for path in (
            "/api/workspaces/ws1/trend/buckets?bucket=month",
            "/api/trend/buckets?bucket=month",
            "/api/workspaces/ws1/trend/buckets?bucket=",
        ):
            status, _ = self._request("GET", path)
            self.assertEqual(status, 400, path)

    def test_non_integer_limit_returns_400(self):
        for path in (
            "/api/workspaces/ws1/trend/buckets?limit=abc",
            "/api/trend/buckets?limit=abc",
        ):
            status, _ = self._request("GET", path)
            self.assertEqual(status, 400, path)

    def test_day_and_week_bucketing_through_http(self):
        self._inject(
            _scan(
                "bk1",
                workspace="wsBucket",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        self._inject(
            _scan(
                "bk2",
                workspace="wsBucket",
                created_at="2026-01-12T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            )
        )
        status, week = self._request(
            "GET", "/api/workspaces/wsBucket/trend/buckets?bucket=week&limit=5"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [bucket["key"] for bucket in week["buckets"]], ["2026-W02", "2026-W03"]
        )
        self.assertEqual(week["summary"]["delta_high"], 1)

        status, day = self._request(
            "GET", "/api/workspaces/wsBucket/trend/buckets?bucket=day"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [bucket["key"] for bucket in day["buckets"]],
            ["2026-01-05", "2026-01-12"],
        )

    def test_limit_is_clamped(self):
        for index in range(3):
            self._inject(
                _scan(
                    "clamp%d" % index,
                    workspace="wsClamp",
                    created_at="2026-01-%02dT00:00:00+00:00" % (index + 1),
                    findings=[_finding("f%d" % index, "low")],
                )
            )
        status, document = self._request(
            "GET", "/api/workspaces/wsClamp/trend/buckets?bucket=day&limit=0"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(document["buckets"]), 1)
        status, document = self._request(
            "GET", "/api/workspaces/wsClamp/trend/buckets?bucket=day&limit=999"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(document["buckets"]), 3)
        status, document = self._request(
            "GET", "/api/trend/buckets?bucket=day&limit=999"
        )
        self.assertEqual(status, 200)
        self.assertLessEqual(len(document["buckets"]), MAX_LIMIT)

    def test_malformed_stored_scan_is_skipped(self):
        self._inject(
            _scan(
                "good",
                workspace="wsMal",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        self.store._scans["malformed"] = {
            "id": "malformed",
            "created_at": "",
            "workspace": "wsMal",
        }
        status, document = self._request(
            "GET", "/api/workspaces/wsMal/trend/buckets?bucket=day"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["summary"]["scans"], 1)

    def test_cross_workspace_buckets_breakdown_and_order(self):
        self._inject(
            _scan(
                "cr1",
                workspace="wsCrB",
                created_at="2026-01-05T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        self._inject(
            _scan(
                "cr2",
                workspace="wsCrA",
                created_at="2026-01-05T01:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            )
        )
        status, document = self._request("GET", "/api/trend/buckets?bucket=week")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(document), {"bucket", "buckets", "workspaces", "summary", "next_cursor"}
        )
        breakdown = document["buckets"][0]["workspaces"]
        self.assertEqual(
            [entry["workspace"] for entry in breakdown], ["wsCrA", "wsCrB"]
        )
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]],
            ["wsCrA", "wsCrB"],
        )
        self.assertEqual(document["summary"]["workspaces"], 2)

    def test_default_bucket_is_week(self):
        self._inject(
            _scan(
                "def1",
                workspace="wsDefault",
                created_at="2026-01-07T00:00:00+00:00",
                findings=[_finding("a", "high")],
            )
        )
        status, document = self._request(
            "GET", "/api/workspaces/wsDefault/trend/buckets"
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["bucket"], "week")
        self.assertEqual([bucket["key"] for bucket in document["buckets"]], ["2026-W02"])

    def test_read_only_leaves_store_unchanged(self):
        self._create_scan(
            {
                "name": "read only",
                "workspace": "wsBucketReadOnly",
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
            self._request("GET", "/api/workspaces/wsBucketReadOnly/trend/buckets")
            self._request("GET", "/api/trend/buckets?bucket=day")
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
