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

from docs_drift_detector import digest_export  # noqa: E402
from docs_drift_detector.server import create_server  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402

HEADER = (
    "created_at,scan_id,total_findings,by_severity_high,by_severity_medium,"
    "by_severity_low,new_high_count,delivery_ok,delivery_error,attempts"
)

FIELDS = {
    "created_at",
    "scan_id",
    "total_findings",
    "by_severity",
    "new_high_count",
    "delivery_ok",
    "delivery_error",
    "attempts",
}


def _entry(scan_id="d1", created_at="2026-01-01T00:00:00+00:00"):
    return {
        "created_at": created_at,
        "scan_id": scan_id,
        "total_findings": 3,
        "by_severity": {"high": 1, "medium": 1, "low": 1},
        "new_high_count": 1,
        "delivery_ok": True,
        "delivery_error": "",
        "attempts": 2,
    }


class DigestExportUnitTest(unittest.TestCase):
    def test_columns(self):
        self.assertEqual(digest_export.columns(), tuple(HEADER.split(",")))

    def test_empty_document_has_header_only(self):
        text = digest_export.to_csv({"workspace": "ws1", "digests": []})
        self.assertEqual(text, HEADER + "\r\n")
        self.assertEqual(digest_export.to_jsonl({"digests": []}), "")

    def test_non_dict_document_is_tolerated(self):
        self.assertEqual(digest_export.to_csv(None), HEADER + "\r\n")
        self.assertEqual(digest_export.to_jsonl(None), "")

    def test_malformed_digest_list_is_treated_as_empty(self):
        for document in ({"digests": "nope"}, {"digests": 5}, []):
            self.assertEqual(digest_export.to_csv(document), HEADER + "\r\n")
            self.assertEqual(digest_export.to_jsonl(document), "")

    def test_bare_list_document_is_supported(self):
        text = digest_export.to_csv([_entry("only")])
        self.assertTrue(text.startswith(HEADER + "\r\n"))
        self.assertIn(",only,", text)

    def test_rows_keep_newest_first_order(self):
        document = {
            "workspace": "ws1",
            "digests": [_entry("newest"), _entry("middle"), _entry("oldest")],
        }
        lines = digest_export.to_csv(document).split("\r\n")
        self.assertEqual(lines[0], HEADER)
        self.assertTrue(lines[1].startswith("2026-01-01T00:00:00+00:00,newest,"))
        self.assertIn(",middle,", lines[2])
        self.assertIn(",oldest,", lines[3])
        self.assertEqual(lines[4], "")

    def test_non_dict_entry_is_skipped(self):
        document = {"digests": ["bad", _entry("good"), 7]}
        text = digest_export.to_csv(document)
        lines = text.split("\r\n")
        self.assertEqual(len(lines), 3)  # header + one row + trailing
        self.assertIn(",good,", lines[1])
        lines = digest_export.to_jsonl(document).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["scan_id"], "good")

    def test_missing_fields_default_safely(self):
        text = digest_export.to_csv({"digests": [{"created_at": "t"}]})
        row = text.split("\r\n")[1]
        self.assertEqual(
            row,
            "t,,0,0,0,0,0,False,,0",
        )

    def test_csv_quoting_only_when_needed(self):
        entry = _entry("q")
        entry["delivery_error"] = 'boom, "x"\nnext'
        text = digest_export.to_csv({"digests": [entry]})
        self.assertIn('"boom, ""x""\nnext"', text)
        # The plain columns (including the boolean delivery_ok) stay unquoted.
        self.assertIn(",True,", text)
        self.assertTrue(text.split("\r\n")[1].endswith(",2"))

    def test_jsonl_lines_are_single_objects_with_trailing_newline(self):
        text = digest_export.to_jsonl({"digests": [_entry("a"), _entry("b")]})
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(len(text.splitlines()), 2)
        for line in text.splitlines():
            payload = json.loads(line)
            self.assertEqual(set(payload), FIELDS)
            self.assertEqual(set(payload["by_severity"]), {"high", "medium", "low"})

    def test_csv_columns_cover_nested_jsonl_fields(self):
        entry = _entry("shape")
        document = {"digests": [entry]}
        csv_columns = digest_export.to_csv(document).split("\r\n")[0].split(",")
        line = json.loads(digest_export.to_jsonl(document))
        self.assertEqual(
            set(csv_columns),
            {
                "created_at",
                "scan_id",
                "total_findings",
                "by_severity_high",
                "by_severity_medium",
                "by_severity_low",
                "new_high_count",
                "delivery_ok",
                "delivery_error",
                "attempts",
            },
        )
        self.assertEqual(line["by_severity"]["high"], entry["by_severity"]["high"])


class DigestExportServerTest(unittest.TestCase):
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

    def _record(self, ws, scan_id, total=1, high=0, ok=True, error=""):
        self.store.record_workspace_digest(
            ws,
            {
                "scan_id": scan_id,
                "total_findings": total,
                "by_severity": {"high": high, "medium": total - high, "low": 0},
                "new_high": [{} for _ in range(high)],
            },
            {"ok": ok, "status": 200 if ok else 500, "error": error},
        )

    def test_csv_headers_and_rows(self):
        ws = "wsDigestCsv"
        self._record(ws, "d1")
        self._record(ws, "d2", total=3, high=1)
        status, headers, body = self._raw("/api/workspaces/%s/digests.csv" % ws)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertEqual(
            headers.get("Content-Disposition"),
            'attachment; filename="docs-drift-%s-digests.csv"' % ws,
        )
        text = body.decode("utf-8")
        self.assertTrue(text.startswith(HEADER + "\r\n"))
        lines = text.split("\r\n")
        self.assertEqual(len(lines), 4)  # header + 2 rows + trailing
        self.assertTrue(lines[1].startswith("20"))  # newest first
        self.assertIn(",d2,", lines[1])
        self.assertIn(",d1,", lines[2])

    def test_jsonl_headers_and_rows(self):
        ws = "wsDigestJsonl"
        self._record(ws, "d1")
        self._record(ws, "d2", total=3, high=1)
        status, headers, body = self._raw("/api/workspaces/%s/digests.jsonl" % ws)
        self.assertEqual(status, 200)
        self.assertIn("application/x-ndjson", headers.get("Content-Type", ""))
        self.assertEqual(
            headers.get("Content-Disposition"),
            'attachment; filename="docs-drift-%s-digests.jsonl"' % ws,
        )
        text = body.decode("utf-8")
        self.assertTrue(text.endswith("\n"))
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["scan_id"], "d2")
        self.assertEqual(json.loads(lines[1])["scan_id"], "d1")
        self.assertEqual(set(json.loads(lines[0])), FIELDS)

    def test_unknown_workspace_returns_header_only_and_empty(self):
        status, _, body = self._raw("/api/workspaces/wsNeverDigests/digests.csv")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), HEADER + "\r\n")
        status, _, body = self._raw("/api/workspaces/wsNeverDigests/digests.jsonl")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), "")

    def test_invalid_workspace_id_returns_400(self):
        for suffix in ("digests.csv", "digests.jsonl"):
            status, _, body = self._raw("/api/workspaces/bad%20id!/" + suffix)
            self.assertEqual(status, 400, suffix)
            self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_limit_clamping_matches_json_sibling(self):
        ws = "wsDigestClamp"
        for index in range(5):
            self._record(ws, "d%d" % index)
        # limit=2 -> newest two
        status, _, body = self._raw(
            "/api/workspaces/%s/digests.jsonl?limit=2" % ws
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [json.loads(line)["scan_id"] for line in body.decode().splitlines()],
            ["d4", "d3"],
        )
        # limit=0 clamps up to 1
        _, _, body = self._raw("/api/workspaces/%s/digests.csv?limit=0" % ws)
        self.assertEqual(len(body.decode("utf-8").strip().split("\r\n")), 2)
        # an invalid limit falls back to the default (10) rather than erroring
        status, _, body = self._raw(
            "/api/workspaces/%s/digests.jsonl?limit=abc" % ws
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body.decode().splitlines()), 5)

    def test_limit_clamped_to_max(self):
        ws = "wsDigestMax"
        for index in range(25):
            self._record(ws, "d%d" % index)
        status, _, body = self._raw(
            "/api/workspaces/%s/digests.jsonl?limit=999" % ws
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body.decode().splitlines()), 20)

    def test_malformed_entry_is_skipped(self):
        ws = "wsDigestMalformed"
        self._record(ws, "good")
        self.store._workspace_digests[ws] = ["not-a-dict", self.store._workspace_digests[ws][-1]]
        status, _, body = self._raw("/api/workspaces/%s/digests.csv" % ws)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertEqual(len(text.strip().split("\r\n")), 2)
        self.assertIn(",good,", text)

    def test_exports_match_json_document_entry_for_entry(self):
        ws = "wsDigestParity"
        for index in range(4):
            self._record(ws, "d%d" % index, total=index + 1, high=index % 2)
        _, _, json_body = self._raw(
            "/api/workspaces/%s/digests?limit=3" % ws
        )
        expected = json.loads(json_body.decode("utf-8"))["digests"]
        _, _, csv_body = self._raw(
            "/api/workspaces/%s/digests.csv?limit=3" % ws
        )
        _, _, jsonl_body = self._raw(
            "/api/workspaces/%s/digests.jsonl?limit=3" % ws
        )
        lines = jsonl_body.decode("utf-8").splitlines()
        self.assertEqual(len(lines), len(expected))
        for entry, line in zip(expected, lines):
            payload = json.loads(line)
            self.assertEqual(payload, entry)
        rows = csv_body.decode("utf-8").split("\r\n")[1:-1]
        self.assertEqual(len(rows), len(expected))
        for entry, row in zip(expected, rows):
            self.assertIn(entry["scan_id"], row)

    def test_exports_are_read_only(self):
        ws = "wsDigestReadOnly"
        self._record(ws, "d1")
        self.store._persist()
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            self._raw("/api/workspaces/%s/digests.csv" % ws)
            self._raw("/api/workspaces/%s/digests.jsonl" % ws)
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
