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
        "jsonlws1",
        workspace="ws1",
        created_at="2026-01-05T00:00:00+00:00",
        findings=[_finding("a", "high"), _finding("b", "low")],
    ),
    _scan(
        "jsonlws2",
        workspace="ws2",
        created_at="2026-01-06T00:00:00+00:00",
        findings=[_finding("c", "high")],
        suppressed=2,
    ),
]

AGGREGATES = (
    "scans",
    "total_findings",
    "by_severity",
    "open_findings",
    "high_open",
    "suppressed",
)


class JsonlExportUnitTest(unittest.TestCase):
    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            csv_export.to_jsonl({"buckets": []}, "nope")

    def test_empty_document_is_empty_string(self):
        self.assertEqual(csv_export.to_jsonl({"buckets": []}, "workspace"), "")

    def test_non_dict_document_is_tolerated(self):
        self.assertEqual(csv_export.to_jsonl(None, "workspace"), "")

    def test_workspace_emits_one_line_per_bucket_oldest_first(self):
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
        text = csv_export.to_jsonl(document, "workspace")
        self.assertTrue(text.endswith("\n"))
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["key"], "2026-W02")
        self.assertEqual(json.loads(lines[1])["key"], "2026-W03")

    def test_each_line_is_single_object_terminated_by_newline(self):
        document = buckets.build_workspace_buckets(
            GOLDEN_SCANS, "ws1", bucket="week"
        )
        text = csv_export.to_jsonl(document, "workspace")
        self.assertEqual(text.count("\n"), len(document["buckets"]))
        for line in text[:-1].split("\n"):
            self.assertNotIn("\n", line)
            self.assertIsInstance(json.loads(line), dict)

    def test_rollup_emits_one_line_per_bucket_per_workspace(self):
        document = buckets.build_rollup_buckets(GOLDEN_SCANS, bucket="week")
        text = csv_export.to_jsonl(document, "rollup")
        lines = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual({line["workspace"] for line in lines}, {"ws1", "ws2"})

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
        lines = csv_export.to_jsonl(document, "rollup").splitlines()
        self.assertEqual(len(lines), 1)
        line = json.loads(lines[0])
        self.assertEqual(line["workspace"], "")
        self.assertEqual(line["key"], "2026-W02")
        self.assertEqual(line["by_severity"], {"high": 1, "medium": 0, "low": 0})

    def test_lines_match_json_bucket_aggregates(self):
        document = buckets.build_rollup_buckets(GOLDEN_SCANS, bucket="week")
        text = csv_export.to_jsonl(document, "rollup")
        emitted = {(json.loads(line)["key"], json.loads(line)["workspace"]): json.loads(line)
                   for line in text.splitlines()}
        for bucket in document["buckets"]:
            for entry in bucket["workspaces"]:
                line = emitted[(bucket["key"], entry["workspace"])]
                for field in AGGREGATES:
                    self.assertEqual(line[field], entry[field], field)


class JsonlServerTest(unittest.TestCase):
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

    def test_workspace_jsonl_headers_and_lines(self):
        self._inject_all()
        status, headers, body = self._raw(
            "/api/workspaces/ws1/trend/buckets.jsonl?bucket=week"
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            headers.get("Content-Type", "").startswith("application/x-ndjson")
        )
        self.assertEqual(
            headers.get("Content-Disposition"),
            'attachment; filename="docs-drift-ws1-trend-buckets.jsonl"',
        )
        lines = body.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        line = json.loads(lines[0])
        self.assertEqual(line["key"], "2026-W02")
        self.assertEqual(line["scans"], 1)

    def test_rollup_jsonl_one_line_per_bucket_per_workspace(self):
        self._inject_all()
        status, headers, body = self._raw("/api/trend/buckets.jsonl?bucket=week")
        self.assertEqual(status, 200)
        self.assertTrue(
            headers.get("Content-Type", "").startswith("application/x-ndjson")
        )
        self.assertEqual(
            headers.get("Content-Disposition"),
            'attachment; filename="docs-drift-trend-buckets.jsonl"',
        )
        lines = [json.loads(line) for line in body.decode("utf-8").splitlines()]
        self.assertEqual({line["workspace"] for line in lines}, {"ws1", "ws2"})

    def test_rollup_unknown_workspace_bucket_emits_empty_workspace(self):
        self.store._scans["orphan"] = _scan(
            "orphan", created_at="2026-01-05T00:00:00+00:00"
        )
        status, _, body = self._raw("/api/trend/buckets.jsonl?bucket=week")
        self.assertEqual(status, 200)
        lines = [json.loads(line) for line in body.decode("utf-8").splitlines()]
        self.assertEqual(lines[0]["workspace"], "")

    def test_unknown_workspace_returns_empty_body(self):
        status, headers, body = self._raw(
            "/api/workspaces/unknownWs/trend/buckets.jsonl"
        )
        self.assertEqual(status, 200)
        self.assertTrue(
            headers.get("Content-Type", "").startswith("application/x-ndjson")
        )
        self.assertEqual(body, b"")

    def test_lines_match_json_payload_for_same_query(self):
        self._inject_all()
        for base in (
            "/api/workspaces/ws1/trend/buckets",
            "/api/trend/buckets",
        ):
            _, _, json_body = self._raw(base + "?bucket=week")
            payload = json.loads(json_body.decode("utf-8"))
            _, _, jsonl_body = self._raw(base + ".jsonl?bucket=week")
            lines = [json.loads(line) for line in jsonl_body.decode("utf-8").splitlines()]
            if base.endswith("workspaces/ws1/trend/buckets"):
                expected = payload["buckets"]
                self.assertEqual(len(lines), len(expected))
                for line, bucket in zip(lines, expected):
                    for field in AGGREGATES:
                        self.assertEqual(line[field], bucket[field], field)
            else:
                expected = [
                    (bucket, entry)
                    for bucket in payload["buckets"]
                    for entry in bucket["workspaces"]
                ]
                self.assertEqual(len(lines), len(expected))
                for line, (bucket, entry) in zip(lines, expected):
                    self.assertEqual(line["key"], bucket["key"])
                    self.assertEqual(line["workspace"], entry["workspace"])
                    for field in AGGREGATES:
                        self.assertEqual(line[field], entry[field], field)

    def test_pagination_does_not_change_emitted_lines(self):
        for index in range(3):
            self.store._scans["p%d" % index] = _scan(
                "p%d" % index,
                workspace="ws1",
                created_at="2026-01-%02dT00:00:00+00:00" % (5 + index * 7),
                findings=[_finding("f%d" % index, "high")],
            )
        _, _, json_body = self._raw(
            "/api/workspaces/ws1/trend/buckets?bucket=week&limit=1"
        )
        payload = json.loads(json_body.decode("utf-8"))
        cursor = payload["next_cursor"]
        self.assertIsNotNone(cursor)
        _, _, jsonl_body = self._raw(
            "/api/workspaces/ws1/trend/buckets.jsonl?bucket=week&limit=1&cursor=%s"
            % urllib.parse.quote(cursor)
        )
        lines = [json.loads(line) for line in jsonl_body.decode("utf-8").splitlines()]
        _, _, page_json = self._raw(
            "/api/workspaces/ws1/trend/buckets?bucket=week&limit=1&cursor=%s"
            % urllib.parse.quote(cursor)
        )
        expected = json.loads(page_json.decode("utf-8"))["buckets"]
        self.assertEqual([line["key"] for line in lines], [b["key"] for b in expected])

    def test_malformed_query_returns_400_on_both(self):
        cases = (
            "?bucket=month",
            "?limit=abc",
            "?from=not-a-date",
            "?to=not-a-date",
        )
        for base in (
            "/api/workspaces/ws1/trend/buckets.jsonl",
            "/api/trend/buckets.jsonl",
        ):
            for suffix in cases:
                status, _, body = self._raw(base + suffix)
                self.assertEqual(status, 400, base + suffix)
                self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_malformed_cursor_returns_400_on_both(self):
        bad = urllib.parse.quote("%%%%")
        for base in (
            "/api/workspaces/ws1/trend/buckets.jsonl",
            "/api/trend/buckets.jsonl",
        ):
            status, _, body = self._raw("%s?cursor=%s" % (base, bad))
            self.assertEqual(status, 400, base)
            self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_invalid_workspace_id_returns_400(self):
        status, _, _ = self._raw("/api/workspaces/bad%20id!/trend/buckets.jsonl")
        self.assertEqual(status, 400)

    def test_jsonl_export_is_read_only(self):
        self._inject_all()
        self.store._persist()
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            self._raw("/api/workspaces/ws1/trend/buckets.jsonl")
            self._raw("/api/trend/buckets.jsonl?bucket=day")
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
