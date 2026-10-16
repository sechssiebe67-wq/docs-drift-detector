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

from docs_drift_detector import buckets, csv_export  # noqa: E402
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


# Two scans in two buckets across two workspaces, used by the HTTP tests.
GOLDEN_SCANS = [
    _scan(
        "csvws1",
        workspace="ws1",
        created_at="2026-01-05T00:00:00+00:00",
        findings=[_finding("a", "high"), _finding("b", "low")],
    ),
    _scan(
        "csvws2",
        workspace="ws2",
        created_at="2026-01-06T00:00:00+00:00",
        findings=[_finding("c", "high")],
        suppressed=2,
    ),
]

# Captured before this change: the JSON siblings must keep these bytes exactly.
GOLDEN_WORKSPACE_JSON = (
    '{"workspace": "ws1", "bucket": "week", "buckets": [{"key": "2026-W02", '
    '"start": "2026-01-05T00:00:00+00:00", "end": "2026-01-11T23:59:59+00:00", '
    '"scans": 1, "total_findings": 2, "by_severity": {"high": 1, "medium": 0, '
    '"low": 1}, "open_findings": 2, "high_open": 1, "suppressed": 0}], '
    '"summary": {"scans": 1, "buckets": 1, "total_findings": 2, "by_severity": '
    '{"high": 1, "medium": 0, "low": 1}, "open_findings": 2, "high_open": 1, '
    '"suppressed": 0, "delta_high": 0}, "next_cursor": null}'
)

GOLDEN_ROLLUP_JSON = (
    '{"bucket": "week", "buckets": [{"key": "2026-W02", "start": '
    '"2026-01-05T00:00:00+00:00", "end": "2026-01-11T23:59:59+00:00", '
    '"scans": 2, "total_findings": 3, "by_severity": {"high": 2, "medium": 0, '
    '"low": 1}, "open_findings": 3, "high_open": 2, "suppressed": 2, '
    '"workspaces": [{"workspace": "ws1", "scans": 1, "total_findings": 2, '
    '"by_severity": {"high": 1, "medium": 0, "low": 1}, "open_findings": 2, '
    '"high_open": 1, "suppressed": 0, "delta_high": 0}, {"workspace": "ws2", '
    '"scans": 1, "total_findings": 1, "by_severity": {"high": 1, "medium": 0, '
    '"low": 0}, "open_findings": 1, "high_open": 1, "suppressed": 2, '
    '"delta_high": 0}]}], "workspaces": [{"workspace": "ws1", "scans": 1, '
    '"total_findings": 2, "by_severity": {"high": 1, "medium": 0, "low": 1}, '
    '"open_findings": 2, "high_open": 1, "suppressed": 0, "delta_high": 0}, '
    '{"workspace": "ws2", "scans": 1, "total_findings": 1, "by_severity": '
    '{"high": 1, "medium": 0, "low": 0}, "open_findings": 1, "high_open": 1, '
    '"suppressed": 2, "delta_high": 0}], "summary": {"scans": 2, "buckets": 1, '
    '"total_findings": 3, "by_severity": {"high": 2, "medium": 0, "low": 1}, '
    '"open_findings": 3, "high_open": 2, "suppressed": 2, "delta_high": 0, '
    '"workspaces": 2}, "next_cursor": null}'
)

WORKSPACE_HEADER = (
    "key,start,end,scans,total_findings,by_severity_high,"
    "by_severity_medium,by_severity_low,open_findings,high_open,suppressed"
)
ROLLUP_HEADER = WORKSPACE_HEADER + ",workspace"


class CsvExportUnitTest(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            csv_export.to_csv({"buckets": []}, "nope")

    def test_empty_document_has_header_only(self):
        text = csv_export.to_csv({"buckets": []}, "workspace")
        self.assertEqual(text, WORKSPACE_HEADER + "\r\n")

    def test_non_dict_document_is_tolerated(self):
        self.assertEqual(
            csv_export.to_csv(None, "workspace"), WORKSPACE_HEADER + "\r\n"
        )

    def test_workspace_rows_match_buckets_oldest_first(self):
        document = buckets.build_workspace_buckets(
            [
                _scan(
                    "s2",
                    workspace="ws1",
                    created_at="2026-01-12T00:00:00+00:00",
                    findings=[_finding("b", "high")],
                ),
                _scan(
                    "s1",
                    workspace="ws1",
                    created_at="2026-01-05T00:00:00+00:00",
                    findings=[_finding("a", "high")],
                ),
            ],
            "ws1",
            bucket="week",
        )
        lines = csv_export.to_csv(document, "workspace").split("\r\n")
        self.assertEqual(lines[0], WORKSPACE_HEADER)
        self.assertTrue(lines[1].startswith("2026-W02,"))
        self.assertTrue(lines[2].startswith("2026-W03,"))
        self.assertEqual(lines[3], "")

    def test_rollup_emits_one_row_per_bucket_per_workspace(self):
        document = buckets.build_rollup_buckets(GOLDEN_SCANS, bucket="week")
        lines = csv_export.to_csv(document, "rollup").split("\r\n")
        self.assertEqual(lines[0], ROLLUP_HEADER)
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[1].endswith(",ws1"))
        self.assertTrue(lines[2].endswith(",ws2"))

    def test_rollup_bucket_without_breakdown_emits_empty_workspace(self):
        document = {
            "bucket": "week",
            "buckets": [
                {
                    "key": "2026-W02",
                    "start": "2026-01-05T00:00:00+00:00",
                    "end": "2026-01-11T23:59:59+00:00",
                    "scans": 1,
                    "total_findings": 1,
                    "by_severity": {"high": 1, "medium": 0, "low": 0},
                    "open_findings": 1,
                    "high_open": 1,
                    "suppressed": 0,
                }
            ],
        }
        lines = csv_export.to_csv(document, "rollup").split("\r\n")
        self.assertEqual(lines[0], ROLLUP_HEADER)
        self.assertEqual(lines[1], "2026-W02,2026-01-05T00:00:00+00:00,"
                         "2026-01-11T23:59:59+00:00,1,1,1,0,0,1,1,0,")

    def test_rfc4180_quoting_only_when_needed(self):
        document = {
            "buckets": [
                {
                    "key": "a,b",
                    "start": "2026-01-01T00:00:00+00:00",
                    "end": 'x"\ny',
                    "scans": 1,
                    "total_findings": 1,
                    "by_severity": {"high": 1, "medium": 0, "low": 0},
                    "open_findings": 1,
                    "high_open": 1,
                    "suppressed": 0,
                }
            ]
        }
        text = csv_export.to_csv(document, "workspace")
        self.assertIn('"a,b"', text)
        self.assertIn('"x""\ny"', text)
        # Plain values (the untouched numeric columns) stay unquoted.
        lines = text.split("\r\n")
        self.assertTrue(lines[1].startswith('"a,b",2026-01-01T00:00:00+00:00,'))
        self.assertTrue(lines[1].endswith(",1,1,1,0,0,1,1,0"))


class CsvServerTest(unittest.TestCase):
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

    def _raw(self, path):
        req = urllib.request.Request(self._url(path), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def _inject_all(self):
        for scan in GOLDEN_SCANS:
            self.store._scans[scan["id"]] = scan

    def test_workspace_csv_headers_and_rows(self):
        self._inject_all()
        status, headers, body = self._raw(
            "/api/workspaces/ws1/trend/buckets.csv?bucket=week"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertEqual(
            headers.get("Content-Disposition"),
            'attachment; filename="docs-drift-ws1-trend-buckets.csv"',
        )
        text = body.decode("utf-8")
        self.assertTrue(text.startswith(WORKSPACE_HEADER + "\r\n"))
        self.assertEqual(len(text.strip().split("\r\n")), 2)

    def test_rollup_csv_has_workspace_column(self):
        self._inject_all()
        status, headers, body = self._raw("/api/trend/buckets.csv?bucket=week")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        text = body.decode("utf-8")
        self.assertTrue(text.startswith(ROLLUP_HEADER + "\r\n"))
        self.assertIn(",ws1\r\n", text)
        self.assertIn(",ws2\r\n", text)

    def test_unknown_workspace_returns_header_only(self):
        status, _, body = self._raw("/api/workspaces/unknownWs/trend/buckets.csv")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), WORKSPACE_HEADER + "\r\n")

    def test_empty_window_returns_header_only(self):
        self._inject_all()
        for path in (
            "/api/workspaces/ws1/trend/buckets.csv?from=2030-01-01&to=2030-12-31",
            "/api/trend/buckets.csv?from=2030-01-01&to=2030-12-31",
        ):
            status, _, body = self._raw(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(len(body.decode("utf-8").strip().split("\r\n")), 1)

    def test_workspace_name_with_comma_is_quoted(self):
        self.store._scans["comma"] = _scan(
            "comma", workspace="ws,one", created_at="2026-01-05T00:00:00+00:00"
        )
        status, _, body = self._raw("/api/trend/buckets.csv?bucket=week")
        self.assertEqual(status, 200)
        self.assertIn('"ws,one"', body.decode("utf-8"))

    def test_malformed_query_returns_400_on_both(self):
        cases = {
            "bucket": "?bucket=month",
            "limit": "?limit=abc",
            "from": "?from=not-a-date",
            "to": "?to=not-a-date",
        }
        for base in (
            "/api/workspaces/ws1/trend/buckets.csv",
            "/api/trend/buckets.csv",
        ):
            for suffix in cases.values():
                status, _, body = self._raw(base + suffix)
                self.assertEqual(status, 400, base + suffix)
                self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_malformed_cursor_returns_400_on_both(self):
        bad = urllib.parse.quote("%%%%")
        for base in (
            "/api/workspaces/ws1/trend/buckets.csv",
            "/api/trend/buckets.csv",
        ):
            status, _, body = self._raw("%s?cursor=%s" % (base, bad))
            self.assertEqual(status, 400, base)
            self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_invalid_workspace_id_returns_400(self):
        status, _, _ = self._raw("/api/workspaces/bad%20id!/trend/buckets.csv")
        self.assertEqual(status, 400)

    def test_json_siblings_are_byte_for_byte_unchanged(self):
        self._inject_all()
        cases = (
            ("/api/workspaces/ws1/trend/buckets?bucket=week", GOLDEN_WORKSPACE_JSON),
            ("/api/trend/buckets?bucket=week", GOLDEN_ROLLUP_JSON),
        )
        for path, expected in cases:
            status, headers, body = self._raw(path)
            self.assertEqual(status, 200, path)
            self.assertIn("application/json", headers.get("Content-Type", ""))
            self.assertEqual(body.decode("utf-8"), expected, path)

    def test_csv_export_is_read_only(self):
        self._inject_all()
        self.store._persist()
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            self._raw("/api/workspaces/ws1/trend/buckets.csv")
            self._raw("/api/trend/buckets.csv?bucket=day")
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
