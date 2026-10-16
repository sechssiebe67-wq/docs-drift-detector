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

from docs_drift_detector import buckets, pagination, rollup, trend  # noqa: E402
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


WALK = [
    _scan("w01", workspace="wsA", created_at="2026-01-01T00:00:00+00:00",
          findings=[_finding("a", "high")]),
    _scan("w02", workspace="wsB", created_at="2026-01-02T00:00:00+00:00",
          findings=[_finding("b", "high")]),
    _scan("w03", workspace="wsA", created_at="2026-01-03T00:00:00+00:00",
          findings=[_finding("c", "high")]),
    _scan("w04", workspace="wsB", created_at="2026-01-04T00:00:00+00:00",
          findings=[_finding("d", "high")]),
    _scan("w05", workspace="wsA", created_at="2026-01-05T00:00:00+00:00",
          findings=[_finding("e", "high")]),
]

WS_A_CHRONO = ["w01", "w03", "w05"]
ALL_CHRONO = ["w01", "w02", "w03", "w04", "w05"]


class PaginationHelperTest(unittest.TestCase):
    def _items(self, count):
        return [
            {"id": "i%02d" % index, "created_at": "2026-01-%02dT00:00:00+00:00" % index}
            for index in range(1, count + 1)
        ]

    @staticmethod
    def _key(item):
        return (item["created_at"], item["id"])

    def test_clamp_limit(self):
        self.assertEqual(pagination.clamp_limit(None), pagination.DEFAULT_LIMIT)
        self.assertEqual(pagination.clamp_limit("nope"), pagination.DEFAULT_LIMIT)
        self.assertEqual(pagination.clamp_limit(0), 1)
        self.assertEqual(pagination.clamp_limit(-3), 1)
        self.assertEqual(pagination.clamp_limit(1000), pagination.MAX_LIMIT)
        self.assertEqual(pagination.clamp_limit(5), 5)

    def test_encode_decode_round_trip(self):
        key = ("2026-01-05T00:00:00+00:00", "w05")
        self.assertEqual(pagination.decode_cursor(pagination.encode_cursor(key)), key)

    def test_encoded_cursor_is_url_safe(self):
        cursor = pagination.encode_cursor(("2026-01-05T00:00:00+00:00", "w05"))
        for char in "+/=":
            self.assertNotIn(char, cursor)

    def test_malformed_cursor_raises(self):
        for value in ("%%%%", "not-valid!!", "", "   ", None, 12):
            with self.assertRaises(pagination.CursorError):
                pagination.decode_cursor(value)

    def test_default_page_is_newest_limit(self):
        items = self._items(5)
        page, next_cursor = pagination.paginate(items, None, 2, self._key)
        self.assertEqual([item["id"] for item in page], ["i04", "i05"])
        self.assertIsNotNone(next_cursor)

    def test_walk_yields_every_item_once_in_order(self):
        items = self._items(5)
        pages = []
        cursor = None
        while True:
            page, cursor = pagination.paginate(items, cursor, 2, self._key)
            pages.append([item["id"] for item in page])
            if cursor is None:
                break
        self.assertEqual(pages, [["i04", "i05"], ["i02", "i03"], ["i01"]])
        flattened = [item for page in reversed(pages) for item in page]
        self.assertEqual(flattened, [item["id"] for item in items])

    def test_unknown_cursor_is_empty_page(self):
        items = self._items(3)
        unknown = pagination.encode_cursor(("1999-01-01T00:00:00+00:00", "nope"))
        page, next_cursor = pagination.paginate(items, unknown, 2, self._key)
        self.assertEqual(page, [])
        self.assertIsNone(next_cursor)

    def test_blank_cursor_is_treated_as_absent(self):
        items = self._items(3)
        page, _ = pagination.paginate(items, "  ", 2, self._key)
        self.assertEqual([item["id"] for item in page], ["i02", "i03"])

    def test_new_item_inserted_does_not_shift_older_page(self):
        items = self._items(4)
        first, cursor = pagination.paginate(items, None, 2, self._key)
        self.assertEqual([item["id"] for item in first], ["i03", "i04"])
        inserted = items + [
            {"id": "i05", "created_at": "2026-01-05T00:00:00+00:00"}
        ]
        page, next_cursor = pagination.paginate(inserted, cursor, 2, self._key)
        self.assertEqual([item["id"] for item in page], ["i01", "i02"])
        self.assertIsNone(next_cursor)


class BuilderPaginationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))
        for scan in WALK:
            self.store._scans[scan["id"]] = scan

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_trend_default_page_unchanged(self):
        document = trend.build_trend(self.store, "wsA", limit=2)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], ["w03", "w05"]
        )
        self.assertEqual(document["summary"]["scans"], 2)
        self.assertIsNotNone(document["next_cursor"])

    @staticmethod
    def _flat_oldest_first(pages):
        return [item for page in reversed(pages) for item in page]

    def test_workspace_trend_walk_complete(self):
        pages = []
        cursor = None
        while True:
            document = trend.build_trend(self.store, "wsA", limit=2, cursor=cursor)
            pages.append([entry["scan_id"] for entry in document["scans"]])
            cursor = document["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(self._flat_oldest_first(pages), WS_A_CHRONO)

    def test_rollup_walk_complete(self):
        scans = self.store.list_all_scans()
        pages = []
        cursor = None
        while True:
            document = rollup.build_rollup(scans, limit=2, cursor=cursor)
            pages.append([entry["scan_id"] for entry in document["scans"]])
            cursor = document["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(self._flat_oldest_first(pages), ALL_CHRONO)

    def test_workspace_buckets_walk_complete(self):
        scans = self.store.list_workspace_scans("wsA")
        pages = []
        cursor = None
        while True:
            document = buckets.build_workspace_buckets(
                scans, "wsA", bucket="day", limit=1, cursor=cursor
            )
            pages.append([bucket["key"] for bucket in document["buckets"]])
            cursor = document["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            self._flat_oldest_first(pages),
            ["2026-01-01", "2026-01-03", "2026-01-05"],
        )

    def test_rollup_buckets_walk_complete(self):
        scans = self.store.list_all_scans()
        pages = []
        cursor = None
        while True:
            document = buckets.build_rollup_buckets(
                scans, bucket="day", limit=2, cursor=cursor
            )
            pages.append([bucket["key"] for bucket in document["buckets"]])
            cursor = document["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            self._flat_oldest_first(pages),
            [
                "2026-01-01",
                "2026-01-02",
                "2026-01-03",
                "2026-01-04",
                "2026-01-05",
            ],
        )

    def test_ties_walk_complete_with_scan_id_tiebreaker(self):
        ties = [
            _scan("t1", workspace="wsT", created_at="2026-02-01T00:00:00+00:00"),
            _scan("t2", workspace="wsT", created_at="2026-02-01T00:00:00+00:00"),
            _scan("t3", workspace="wsT", created_at="2026-02-01T00:00:00+00:00"),
        ]
        for scan in ties:
            self.store._scans[scan["id"]] = scan
        pages = []
        cursor = None
        while True:
            document = trend.build_trend(self.store, "wsT", limit=1, cursor=cursor)
            pages.append([entry["scan_id"] for entry in document["scans"]])
            cursor = document["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(self._flat_oldest_first(pages), ["t1", "t2", "t3"])

    def test_cursor_tiebreaker_uses_scan_id(self):
        document = trend.build_trend(self.store, "wsA", limit=1, cursor=None)
        decoded = pagination.decode_cursor(document["next_cursor"])
        self.assertEqual(decoded[0], "2026-01-05T00:00:00+00:00")
        self.assertEqual(decoded[1], "w05")

    def test_malformed_cursor_raises(self):
        with self.assertRaises(pagination.CursorError):
            trend.build_trend(self.store, "wsA", cursor="%%%%")
        with self.assertRaises(pagination.CursorError):
            rollup.build_rollup(self.store.list_all_scans(), cursor="%%%%")
        with self.assertRaises(pagination.CursorError):
            buckets.build_workspace_buckets([], "wsA", cursor="%%%%")
        with self.assertRaises(pagination.CursorError):
            buckets.build_rollup_buckets([], cursor="%%%%")

    def test_unknown_cursor_is_empty_page(self):
        unknown = pagination.encode_cursor(("1999-01-01T00:00:00+00:00", "nope"))
        document = trend.build_trend(self.store, "wsA", cursor=unknown)
        self.assertEqual(document["scans"], [])
        self.assertIsNone(document["next_cursor"])
        self.assertEqual(document["summary"]["scans"], 0)


class PaginationServerTest(unittest.TestCase):
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

    def _inject_all(self):
        for scan in WALK:
            self.store._scans[scan["id"]] = scan

    def _walk(self, base_path, extract, limit=2):
        cursor = None
        pages = []
        while True:
            path = "%s%scursor=%s&limit=%d" % (
                base_path,
                "&" if "?" in base_path else "?",
                urllib.parse.quote(cursor) if cursor else "",
                limit,
            )
            status, document = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("next_cursor", document)
            pages.append(extract(document))
            cursor = document["next_cursor"]
            if cursor is None:
                break
        return pages

    @staticmethod
    def _flat_oldest_first(pages):
        return [item for page in reversed(pages) for item in page]

    def test_all_four_endpoints_include_next_cursor(self):
        self._inject_all()
        for path in (
            "/api/workspaces/wsA/trend",
            "/api/workspaces/wsA/trend/buckets",
            "/api/trend",
            "/api/trend/buckets",
        ):
            status, document = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("next_cursor", document)

    def test_workspace_trend_walk_disjoint_and_ordered(self):
        self._inject_all()
        pages = self._walk(
            "/api/workspaces/wsA/trend",
            lambda doc: [entry["scan_id"] for entry in doc["scans"]],
        )
        self.assertEqual(self._flat_oldest_first(pages), WS_A_CHRONO)
        for page in pages:
            self.assertEqual(len(page), len(set(page)))

    def test_workspace_buckets_walk_disjoint_and_ordered(self):
        self._inject_all()
        pages = self._walk(
            "/api/workspaces/wsA/trend/buckets?bucket=day",
            lambda doc: [bucket["key"] for bucket in doc["buckets"]],
            limit=1,
        )
        self.assertEqual(
            self._flat_oldest_first(pages),
            ["2026-01-01", "2026-01-03", "2026-01-05"],
        )

    def test_rollup_walk_disjoint_and_ordered(self):
        self._inject_all()
        pages = self._walk(
            "/api/trend",
            lambda doc: [entry["scan_id"] for entry in doc["scans"]],
        )
        self.assertEqual(self._flat_oldest_first(pages), ALL_CHRONO)

    def test_rollup_buckets_walk_disjoint_and_ordered(self):
        self._inject_all()
        pages = self._walk(
            "/api/trend/buckets?bucket=day",
            lambda doc: [bucket["key"] for bucket in doc["buckets"]],
        )
        self.assertEqual(
            self._flat_oldest_first(pages),
            ["2026-01-%02d" % index for index in range(1, 6)],
        )

    def test_default_response_matches_when_fewer_than_limit(self):
        self._inject_all()
        status, document = self._request(
            "GET", "/api/workspaces/wsA/trend?limit=50"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [entry["scan_id"] for entry in document["scans"]], WS_A_CHRONO
        )
        self.assertIsNone(document["next_cursor"])

    def test_malformed_cursor_returns_400_on_all_four(self):
        self._inject_all()
        bad = urllib.parse.quote("%%%%")
        for base in (
            "/api/workspaces/wsA/trend",
            "/api/workspaces/wsA/trend/buckets",
            "/api/trend",
            "/api/trend/buckets",
        ):
            path = "%s?cursor=%s" % (base, bad)
            status, payload = self._request("GET", path)
            self.assertEqual(status, 400, path)
            self.assertIn("error", payload)

    def test_unknown_cursor_returns_empty_page(self):
        self._inject_all()
        unknown = urllib.parse.quote(
            pagination.encode_cursor(("1999-01-01T00:00:00+00:00", "nope"))
        )
        cases = (
            ("/api/workspaces/wsA/trend?cursor=%s" % unknown, "scans"),
            ("/api/workspaces/wsA/trend/buckets?cursor=%s" % unknown, "buckets"),
            ("/api/trend?cursor=%s" % unknown, "scans"),
            ("/api/trend/buckets?cursor=%s" % unknown, "buckets"),
        )
        for path, field in cases:
            status, document = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(document[field], [])
            self.assertIsNone(document["next_cursor"])
            self.assertEqual(document["summary"]["scans"], 0)

    def test_pagination_composes_with_from_window(self):
        self._inject_all()
        pages = self._walk(
            "/api/trend?from=2026-01-02&to=2026-01-04",
            lambda doc: [entry["scan_id"] for entry in doc["scans"]],
            limit=1,
        )
        self.assertEqual(
            self._flat_oldest_first(pages), ["w02", "w03", "w04"]
        )

    def test_pagination_composes_with_bucket_and_window(self):
        self._inject_all()
        pages = self._walk(
            "/api/trend/buckets?bucket=day&from=2026-01-02&to=2026-01-04",
            lambda doc: [bucket["key"] for bucket in doc["buckets"]],
            limit=1,
        )
        self.assertEqual(
            self._flat_oldest_first(pages),
            ["2026-01-02", "2026-01-03", "2026-01-04"],
        )

    def test_pagination_is_read_only(self):
        self.store.create_scan(
            "read only",
            "release",
            [_finding("a", "high")],
            workspace="wsReadOnly",
        )
        with open(self.store_path, "rb") as handle:
            before = handle.read()
        with mock.patch("docs_drift_detector.notify.dispatch") as dispatch, mock.patch(
            "docs_drift_detector.github.open_pull_request"
        ) as open_pr, mock.patch.object(
            self.store, "record_workspace_digest"
        ) as record:
            self._request("GET", "/api/workspaces/wsReadOnly/trend?limit=1")
            self._request("GET", "/api/trend?limit=1")
            self._request("GET", "/api/trend/buckets?limit=1")
        self.assertFalse(dispatch.called)
        self.assertFalse(open_pr.called)
        self.assertFalse(record.called)
        with open(self.store_path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
