import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector.server import create_server  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(cls.tmp.name, "scans.json"))
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
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, raw, ctype
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers.get("Content-Type", "")

    def _json(self, method, path, body=None):
        status, raw, _ = self._request(method, path, body)
        return status, json.loads(raw.decode("utf-8"))

    def test_healthz(self):
        status, payload = self._json("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_index_served(self):
        status, raw, ctype = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Docs Drift Detector", raw)

    def test_analyze_is_stateless(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, payload = self._json("POST", "/api/analyze", body)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["findings"]), 1)
        status, listing = self._json("GET", "/api/scans")
        self.assertEqual(listing["scans"], [])

    def test_scan_lifecycle(self):
        body = {
            "name": "Release 4.2",
            "changelog": '"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Go to /settings."}],
            "metrics": {"help/a.md": {"views": 1000, "tickets": 2}},
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        scan_id = scan["id"]
        finding_id = scan["findings"][0]["id"]
        self.assertEqual(scan["stats"]["findings"], 1)

        status, listing = self._json("GET", "/api/scans")
        self.assertEqual(len(listing["scans"]), 1)

        status, fetched = self._json("GET", "/api/scans/" + scan_id)
        self.assertEqual(status, 200)
        self.assertEqual(fetched["name"], "Release 4.2")

        status, raw, ctype = self._request("GET", "/api/scans/%s/export.md" % scan_id)
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", ctype)
        self.assertIn(b"- [ ]", raw)

        status, resolved = self._json(
            "POST",
            "/api/scans/%s/findings/%s/resolve" % (scan_id, finding_id),
            {"resolved": True},
        )
        self.assertEqual(status, 200)
        self.assertTrue(resolved["findings"][0]["resolved"])
        self.assertEqual(resolved["stats"]["unresolved"], 0)

        status, payload = self._json("DELETE", "/api/scans/" + scan_id)
        self.assertEqual(status, 200)
        status, _ = self._json("GET", "/api/scans/" + scan_id)
        self.assertEqual(status, 404)

    def test_bad_json_returns_400(self):
        req = urllib.request.Request(self._url("/api/scans"), data=b"not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 400)

    def test_unknown_route(self):
        status, payload = self._json("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_static_path_traversal_blocked(self):
        status, _, _ = self._request("GET", "/static/../server.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
