import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import notify  # noqa: E402


def _finding(fid, severity, score, page="help/a.md"):
    return {
        "id": fid,
        "kind": "label",
        "page": page,
        "title": 'UI label "Save" changed',
        "old_value": "Save",
        "new_value": "Save changes",
        "score": score,
        "severity": severity,
    }


class BuildPayloadTest(unittest.TestCase):
    def test_filters_to_high_and_sorts_by_score(self):
        scan = {
            "id": "s1",
            "name": "Release 4.2",
            "workspace": "ws1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "findings": [
                _finding("a", "medium", 55.0),
                _finding("b", "high", 71.0, page="help/b.md"),
                _finding("c", "high", 95.0, page="help/c.md"),
                _finding("d", "low", 10.0),
            ],
        }
        payload = notify.build_payload(scan)
        self.assertEqual(payload["scan_id"], "s1")
        self.assertEqual(payload["name"], "Release 4.2")
        self.assertEqual(payload["workspace"], "ws1")
        self.assertEqual(payload["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertIn("url", payload)
        self.assertEqual([f["id"] for f in payload["findings"]], ["c", "b"])
        self.assertEqual(
            [f["score"] for f in payload["findings"]], [95.0, 71.0]
        )
        for finding in payload["findings"]:
            self.assertEqual(finding["severity"], "high")
            self.assertEqual(
                set(finding),
                {
                    "id",
                    "kind",
                    "page",
                    "title",
                    "old_value",
                    "new_value",
                    "score",
                    "severity",
                },
            )

    def test_scan_with_no_high_findings(self):
        scan = {
            "id": "s2",
            "findings": [_finding("a", "medium", 50.0), _finding("b", "low", 10.0)],
        }
        payload = notify.build_payload(scan)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["workspace"], "")

    def test_payload_is_json_serialisable(self):
        scan = {"id": "s3", "findings": [_finding("b", "high", 71.0)]}
        encoded = json.dumps(notify.build_payload(scan))
        self.assertIn('"severity": "high"', encoded)

    def test_scan_without_findings_key(self):
        payload = notify.build_payload({"id": "s4"})
        self.assertEqual(payload["findings"], [])


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self._original = notify._opener

    def tearDown(self):
        notify._opener = self._original

    def test_dispatch_success_posts_json(self):
        captured = {}

        def fake_opener(request, timeout):
            captured["url"] = request.full_url
            captured["data"] = request.data
            captured["ctype"] = request.get_header("Content-type")
            captured["timeout"] = timeout
            return _FakeResponse(200)

        notify._opener = fake_opener
        result = notify.dispatch("https://hooks.example.com/ddr", {"a": 1}, timeout=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["error"], "")
        self.assertEqual(captured["url"], "https://hooks.example.com/ddr")
        self.assertEqual(captured["timeout"], 3)
        self.assertIn("application/json", captured["ctype"])
        self.assertEqual(json.loads(captured["data"].decode("utf-8")), {"a": 1})

    def test_dispatch_http_500_is_not_ok(self):
        notify._opener = lambda request, timeout: _FakeResponse(500)
        result = notify.dispatch("https://hooks.example.com/ddr", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 500)
        self.assertTrue(result["error"])

    def test_dispatch_url_error_never_raises(self):
        def fake_opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        notify._opener = fake_opener
        result = notify.dispatch("https://hooks.example.com/ddr", {})
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("connection refused", result["error"])

    def test_dispatch_http_error_never_raises(self):
        def fake_opener(request, timeout):
            raise urllib.error.HTTPError(
                "https://hooks.example.com/ddr", 503, "unavailable", {}, None
            )

        notify._opener = fake_opener
        result = notify.dispatch("https://hooks.example.com/ddr", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 503)


if __name__ == "__main__":
    unittest.main()
