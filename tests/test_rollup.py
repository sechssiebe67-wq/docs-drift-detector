import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import rollup  # noqa: E402
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


class RollupUnitTest(unittest.TestCase):
    def test_empty_input_is_zeroed(self):
        document = rollup.build_rollup([])
        self.assertEqual(document["scans"], [])
        self.assertEqual(document["workspaces"], [])
        self.assertEqual(
            document["summary"],
            {
                "scans": 0,
                "workspaces": 0,
                "total_findings": 0,
                "by_severity": {"high": 0, "medium": 0, "low": 0},
                "open_findings": 0,
                "high_open": 0,
                "suppressed": 0,
                "delta_high": 0,
            },
        )

    def test_non_list_input_is_tolerated(self):
        document = rollup.build_rollup(None)
        self.assertEqual(document["scans"], [])
        self.assertEqual(document["summary"]["scans"], 0)

    def test_single_scan(self):
        scans = [
            _scan(
                "s1",
                workspace="ws1",
                findings=[_finding("a", "high"), _finding("b", "low")],
                suppressed=2,
            )
        ]
        document = rollup.build_rollup(scans)
        self.assertEqual(len(document["scans"]), 1)
        entry = document["scans"][0]
        self.assertEqual(
            set(entry),
            {
                "scan_id",
                "workspace",
                "created_at",
                "name",
                "total_findings",
                "by_severity",
                "open_findings",
                "high_open",
                "suppressed",
            },
        )
        self.assertEqual(entry["workspace"], "ws1")
        self.assertEqual(entry["total_findings"], 2)
        self.assertEqual(entry["by_severity"], {"high": 1, "medium": 0, "low": 1})
        self.assertEqual(entry["open_findings"], 2)
        self.assertEqual(entry["high_open"], 1)
        self.assertEqual(entry["suppressed"], 2)
        self.assertEqual(document["summary"]["delta_high"], 0)
        self.assertEqual(document["workspaces"][0]["delta_high"], 0)

    def test_multiple_workspaces_sorted_by_high_open_then_name(self):
        scans = [
            _scan(
                "s1",
                workspace="wsB",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "s2",
                workspace="wsA",
                created_at="2026-01-02T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            ),
            _scan(
                "s3",
                workspace="wsC",
                created_at="2026-01-03T00:00:00+00:00",
                findings=[_finding("d", "high"), _finding("e", "high")],
            ),
        ]
        document = rollup.build_rollup(scans)
        self.assertEqual(
            [entry["workspace"] for entry in document["workspaces"]],
            ["wsA", "wsC", "wsB"],
        )
        self.assertEqual(
            [entry["high_open"] for entry in document["workspaces"]], [2, 2, 1]
        )

    def test_scans_without_a_workspace_use_empty_bucket(self):
        scans = [
            _scan(
                "s1",
                workspace="ws1",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "s2",
                workspace=None,
                created_at="2026-01-02T00:00:00+00:00",
                findings=[_finding("b", "low")],
            ),
        ]
        document = rollup.build_rollup(scans)
        buckets = {entry["workspace"]: entry for entry in document["workspaces"]}
        self.assertEqual(set(buckets), {"ws1", ""})
        self.assertEqual(buckets[""]["total_findings"], 1)
        self.assertEqual(document["scans"][1]["workspace"], "")

    def test_scans_are_ordered_oldest_first(self):
        scans = [
            _scan("s3", workspace="ws1", created_at="2026-01-03T00:00:00+00:00"),
            _scan("s1", workspace="ws1", created_at="2026-01-01T00:00:00+00:00"),
            _scan("s2", workspace="ws1", created_at="2026-01-02T00:00:00+00:00"),
        ]
        document = rollup.build_rollup(scans)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ["s1", "s2", "s3"]
        )

    def test_malformed_scans_are_skipped(self):
        scans = [
            None,
            "not a scan",
            123,
            {"id": "missing-time", "created_at": "", "findings": []},
            {"id": "", "created_at": "2026-01-01T00:00:00+00:00", "findings": []},
            {"id": "bad-time", "created_at": "not-a-date", "findings": []},
            {"created_at": "2026-01-01T00:00:00+00:00", "findings": []},
            _scan("ok", workspace="ws1", findings=[_finding("a", "high")]),
        ]
        document = rollup.build_rollup(scans)
        self.assertEqual([entry["scan_id"] for entry in document["scans"]], ["ok"])

    def test_limit_clamping(self):
        scans = [
            _scan("s%02d" % index, workspace="ws1", created_at="2026-01-01T00:00:%02d+00:00" % index)
            for index in range(5)
        ]
        self.assertEqual(len(rollup.build_rollup(scans, limit=0)["scans"]), 1)
        self.assertEqual(len(rollup.build_rollup(scans, limit=-10)["scans"]), 1)
        self.assertEqual(len(rollup.build_rollup(scans, limit=3)["scans"]), 3)
        self.assertEqual(len(rollup.build_rollup(scans, limit=999)["scans"]), 5)

    def test_limit_clamped_to_max(self):
        scans = [
            _scan(
                "s%03d" % index,
                workspace="ws1",
                created_at="2026-01-01T00:00:00+00:00",
            )
            for index in range(MAX_LIMIT + 10)
        ]
        document = rollup.build_rollup(scans, limit=1000)
        self.assertEqual(len(document["scans"]), MAX_LIMIT)

    def test_delta_high_sign(self):
        worsening = [
            _scan(
                "a1",
                workspace="wsWorse",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a", "high")],
            ),
            _scan(
                "a2",
                workspace="wsWorse",
                created_at="2026-01-02T00:00:00+00:00",
                findings=[_finding("b", "high"), _finding("c", "high")],
            ),
        ]
        document = rollup.build_rollup(worsening)
        self.assertEqual(document["workspaces"][0]["delta_high"], 1)
        self.assertEqual(document["summary"]["delta_high"], 1)

        improving = [
            _scan(
                "b1",
                workspace="wsBetter",
                created_at="2026-01-01T00:00:00+00:00",
                findings=[_finding("a", "high"), _finding("b", "high")],
            ),
            _scan(
                "b2",
                workspace="wsBetter",
                created_at="2026-01-02T00:00:00+00:00",
                findings=[],
            ),
        ]
        document = rollup.build_rollup(improving)
        self.assertEqual(document["workspaces"][0]["delta_high"], -2)
        self.assertEqual(document["summary"]["delta_high"], -2)

    def test_resolved_high_findings_are_not_open(self):
        scans = [
            _scan(
                "s1",
                workspace="ws1",
                findings=[
                    _finding("a", "high", resolved=True),
                    _finding("b", "high"),
                ],
            )
        ]
        entry = rollup.build_rollup(scans)["scans"][0]
        self.assertEqual(entry["by_severity"], {"high": 2, "medium": 0, "low": 0})
        self.assertEqual(entry["open_findings"], 1)
        self.assertEqual(entry["high_open"], 1)

    def test_summary_equals_sum_of_per_workspace_aggregates(self):
        scans = [
            _scan("s1", workspace="ws1", findings=[_finding("a", "high")]),
            _scan("s2", workspace="ws1", findings=[_finding("b", "low")]),
            _scan("s3", workspace="ws2", findings=[_finding("c", "high"), _finding("d", "high")]),
            _scan("s4", workspace=None, findings=[_finding("e", "medium")]),
        ]
        document = rollup.build_rollup(scans)
        summary = document["summary"]
        workspaces = document["workspaces"]
        self.assertEqual(summary["scans"], sum(w["scans"] for w in workspaces))
        self.assertEqual(
            summary["total_findings"],
            sum(w["total_findings"] for w in workspaces),
        )
        self.assertEqual(
            summary["open_findings"],
            sum(w["open_findings"] for w in workspaces),
        )
        self.assertEqual(summary["high_open"], sum(w["high_open"] for w in workspaces))
        self.assertEqual(
            summary["suppressed"], sum(w["suppressed"] for w in workspaces)
        )
        for key in ("high", "medium", "low"):
            self.assertEqual(
                summary["by_severity"][key],
                sum(w["by_severity"][key] for w in workspaces),
            )


class RollupServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store_path = os.path.join(cls.tmp.name, "scans.json")
        store = Store(cls.store_path)
        cls.server = create_server("127.0.0.1", 0, store)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

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

    def _create_scan(self, body):
        status, scan = self._request("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.addCleanup(self._request, "DELETE", "/api/scans/%s" % scan["id"])
        return scan

    def test_empty_store(self):
        # A fresh store instance keeps this test independent of the shared one.
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "empty.json"))
            server = create_server("127.0.0.1", 0, store)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/trend" % port, timeout=5
                ) as resp:
                    self.assertEqual(resp.status, 200)
                    document = json.loads(resp.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
        self.assertEqual(document["scans"], [])
        self.assertEqual(document["workspaces"], [])
        self.assertEqual(document["summary"]["scans"], 0)
        self.assertEqual(document["summary"]["delta_high"], 0)

    def test_rollup_covers_all_workspaces_and_empty_bucket(self):
        self._create_scan(
            {
                "name": "ws1 scan",
                "workspace": "wsRoll1",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            }
        )
        self._create_scan(
            {
                "name": "ws2 scan",
                "workspace": "wsRoll2",
                "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
                "pages": [{"path": "help/b.md", "content": "Click Save. Go to /settings."}],
                "metrics": {"help/b.md": {"views": 100000, "tickets": 5}},
            }
        )
        self._create_scan(
            {
                "name": "no workspace",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/c.md", "content": "Click Save."}],
            }
        )
        status, document = self._request("GET", "/api/trend")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(document), {"scans", "workspaces", "summary", "next_cursor"}
        )
        buckets = {entry["workspace"] for entry in document["workspaces"]}
        self.assertEqual(buckets, {"wsRoll1", "wsRoll2", ""})
        by_workspace = {e["workspace"]: e for e in document["workspaces"]}
        self.assertEqual(
            document["summary"]["total_findings"],
            sum(e["total_findings"] for e in document["workspaces"]),
        )
        self.assertEqual(
            document["summary"]["open_findings"],
            sum(e["open_findings"] for e in document["workspaces"]),
        )
        self.assertEqual(document["summary"]["workspaces"], 3)
        self.assertGreaterEqual(by_workspace["wsRoll2"]["high_open"], 1)

    def test_non_integer_limit_returns_400(self):
        status, payload = self._request("GET", "/api/trend?limit=abc")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_limit_is_clamped(self):
        for index in range(3):
            self._create_scan(
                {
                    "name": "clamp %d" % index,
                    "workspace": "wsRollClamp",
                    "changelog": 'renamed "Save" to "Save changes"',
                    "pages": [{"path": "help/a.md", "content": "Click Save."}],
                }
            )
        status, document = self._request("GET", "/api/trend?limit=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(document["scans"]), 1)
        status, document = self._request("GET", "/api/trend?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(document["scans"]), 1)
        status, document = self._request("GET", "/api/trend?limit=999")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(document["scans"]), MAX_LIMIT)

    def test_malformed_stored_scan_is_skipped(self):
        scan = self._create_scan(
            {
                "name": "malformed host",
                "workspace": "wsRollMalformed",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            }
        )
        self.server.store._scans["malformed-id"] = {"id": "malformed-id", "created_at": ""}
        self.addCleanup(self.server.store._scans.pop, "malformed-id", None)
        status, document = self._request("GET", "/api/trend")
        self.assertEqual(status, 200)
        ids = [entry["scan_id"] for entry in document["scans"]]
        self.assertNotIn("malformed-id", ids)
        self.assertIn(scan["id"], ids)

    def test_rollup_shares_per_scan_summary_with_workspace_trend(self):
        ws = "wsRollShare"
        self._create_scan(
            {
                "name": "shared",
                "workspace": ws,
                "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
                "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
                "metrics": {"help/a.md": {"views": 100000, "tickets": 5}},
            }
        )
        status, rollup_document = self._request("GET", "/api/trend")
        self.assertEqual(status, 200)
        status, trend_document = self._request("GET", "/api/workspaces/%s/trend" % ws)
        self.assertEqual(status, 200)
        rollup_entry = next(
            entry for entry in rollup_document["scans"] if entry["workspace"] == ws
        )
        trend_entry = trend_document["scans"][0]
        self.assertEqual(rollup_entry["scan_id"], trend_entry["scan_id"])
        for key in (
            "total_findings",
            "by_severity",
            "open_findings",
            "high_open",
            "suppressed",
        ):
            self.assertEqual(rollup_entry[key], trend_entry[key])

    def test_explicit_limit_selects_newest_window(self):
        ids = []
        for index in range(4):
            scan = self._create_scan(
                {
                    "name": "window %d" % index,
                    "workspace": "wsRollWindow",
                    "changelog": 'renamed "Save" to "Save changes"',
                    "pages": [{"path": "help/a.md", "content": "Click Save."}],
                }
            )
            ids.append(scan["id"])
        status, document = self._request("GET", "/api/trend?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(document["scans"]), 2)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ids[2:]
        )

    def test_read_only_leaves_store_unchanged(self):
        from unittest import mock

        self._create_scan(
            {
                "name": "read only",
                "workspace": "wsRollReadOnly",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            }
        )
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.server.store, "record_workspace_digest"
        ) as record:
            self._request("GET", "/api/trend")
            self._request("GET", "/api/trend?limit=5")
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
