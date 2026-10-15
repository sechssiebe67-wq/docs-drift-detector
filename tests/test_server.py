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

    def test_analyze_rules_suppress_findings(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
            "rules": [{"kind": "label", "value": "Save"}],
        }
        status, payload = self._json("POST", "/api/analyze", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["stats"]["suppressed"], 1)

    def test_scan_with_rules_persists_suppression(self):
        body = {
            "name": "Suppressed scan",
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "rules": [{"kind": "label", "value": "Save"}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertGreaterEqual(scan["stats"]["suppressed"], 1)
        self.assertEqual([f["kind"] for f in scan["findings"]], ["route"])
        self.assertNotIn('UI label "Save"', json.dumps(scan["findings"]))

        status, fetched = self._json("GET", "/api/scans/" + scan["id"])
        self.assertEqual(fetched["stats"]["suppressed"], scan["stats"]["suppressed"])

        status, raw, _ = self._request("GET", "/api/scans/%s/export.md" % scan["id"])
        self.assertEqual(status, 200)
        self.assertNotIn(b'UI label "Save"', raw)
        self.assertIn(b"/settings", raw)

    def test_scan_without_rules_has_zero_suppressed(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertEqual(scan["stats"]["suppressed"], 0)
        self.assertEqual(len(scan["findings"]), 1)

    def test_malformed_rules_do_not_error(self):
        for bad in (123, "not-a-list", [123], [{"unknown": 1}], None):
            body = {
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
                "rules": bad,
            }
            status, payload = self._json("POST", "/api/analyze", body)
            self.assertEqual(status, 200)
            self.assertEqual(payload["stats"]["suppressed"], 0)

    def test_workspace_rules_lifecycle(self):
        ws = "wsLifecycle"
        status, payload = self._json("GET", "/api/workspaces/%s/rules" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["rules"], [])

        status, payload = self._json(
            "PUT",
            "/api/workspaces/%s/rules" % ws,
            {"rules": [{"kind": "label", "value": "Save"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["rules"][0]["value"], "Save")

        status, payload = self._json("GET", "/api/workspaces/%s/rules" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["rules"][0]["value"], "Save")

        status, _ = self._json("DELETE", "/api/workspaces/%s/rules" % ws)
        self.assertEqual(status, 200)

        status, payload = self._json("GET", "/api/workspaces/%s/rules" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["rules"], [])

    def test_workspace_rules_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/rules")
        self.assertEqual(status, 400)

        status, _ = self._json("PUT", "/api/workspaces/bad%20id!/rules", {"rules": []})
        self.assertEqual(status, 400)

        status, _ = self._json("DELETE", "/api/workspaces/bad%20id!/rules")
        self.assertEqual(status, 400)

    def test_scan_with_workspace_applies_stored_rules(self):
        ws = "wsScanRules"
        status, _ = self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Save"}]}
        )
        self.assertEqual(status, 200)
        body = {
            "name": "Workspace scan",
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertEqual(scan["workspace"], ws)
        self.assertEqual(scan["findings"], [])
        self.assertEqual(scan["stats"]["suppressed"], 1)

    def test_analyze_combines_workspace_and_inline_rules(self):
        ws = "wsCombine"
        status, _ = self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Save"}]}
        )
        self.assertEqual(status, 200)
        body = {
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "rules": [{"kind": "route", "value": "/settings"}],
        }
        status, payload = self._json("POST", "/api/analyze", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["stats"]["suppressed"], 2)

    def test_scan_without_workspace_still_uses_inline_rules(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
            "rules": [{"kind": "label", "value": "Save"}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertNotIn("workspace", scan)
        self.assertEqual(scan["stats"]["suppressed"], 1)

    def test_scan_with_invalid_workspace_returns_400(self):
        body = {
            "workspace": "bad id!",
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, _ = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 400)

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

    def test_rules_export_document_and_attachment(self):
        ws = "wsExport"
        self._json(
            "PUT",
            "/api/workspaces/%s/rules" % ws,
            {"rules": [{"kind": "label", "value": "Save"}, {"value": "Projects"}]},
        )
        req = urllib.request.Request(self._url("/api/workspaces/%s/rules/export" % ws), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
            self.assertIn(ws, resp.headers.get("Content-Disposition", ""))
            document = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["workspace"], ws)
        self.assertEqual(len(document["rules"]), 2)
        for rule in document["rules"]:
            self.assertTrue(rule["id"])
            self.assertEqual(rule["origin"], "workspace")

    def test_rules_export_import_roundtrip(self):
        source = "wsRoundTrip"
        target = "wsRoundTrip2"
        original = [
            {"kind": "label", "page": "help/*.md", "value": "Save", "reason": "example"},
            {"kind": "route", "value": "/settings"},
        ]
        self._json("PUT", "/api/workspaces/%s/rules" % source, {"rules": original})

        req = urllib.request.Request(
            self._url("/api/workspaces/%s/rules/export" % source), method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            document = json.loads(resp.read().decode("utf-8"))

        status, payload = self._json("POST", "/api/workspaces/%s/rules/import" % target, document)
        self.assertEqual(status, 200)
        self.assertEqual(payload["imported"], 2)
        self.assertEqual(len(payload["rules"]), 2)

        status, stored = self._json("GET", "/api/workspaces/%s/rules" % target)
        self.assertEqual(status, 200)
        self.assertEqual(
            [(r["kind"], r["page"], r["value"]) for r in stored["rules"]],
            [("label", "help/*.md", "Save"), ("route", "", "/settings")],
        )
        self.assertEqual(stored["rules"], payload["rules"])

        status, again = self._json("POST", "/api/workspaces/%s/rules/import" % target, document)
        self.assertEqual(status, 200)
        self.assertEqual(again["imported"], 2)
        self.assertEqual(again["rules"], payload["rules"])

    def test_rules_import_accepts_bare_rules(self):
        status, payload = self._json(
            "POST",
            "/api/workspaces/wsBare/rules/import",
            {"rules": [{"kind": "label", "value": "Save"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["imported"], 1)

    def test_rules_import_replaces_library(self):
        ws = "wsReplace"
        self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Old"}]}
        )
        status, _ = self._json(
            "POST",
            "/api/workspaces/%s/rules/import" % ws,
            {"rules": [{"kind": "label", "value": "New"}]},
        )
        self.assertEqual(status, 200)
        status, stored = self._json("GET", "/api/workspaces/%s/rules" % ws)
        self.assertEqual([r["value"] for r in stored["rules"]], ["New"])

    def test_rules_import_malformed_body_returns_400(self):
        ws = "wsMalformed"
        for body in ({}, {"rules": None}, {"rules": "not-a-list"}, {"rules": {"kind": "label"}}):
            status, payload = self._json("POST", "/api/workspaces/%s/rules/import" % ws, body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)

    def test_rules_export_import_invalid_workspace_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/rules/export")
        self.assertEqual(status, 400)
        status, _ = self._json(
            "POST", "/api/workspaces/bad%20id!/rules/import", {"rules": []}
        )
        self.assertEqual(status, 400)

    def test_analyze_reports_rule_provenance(self):
        ws = "wsProvenance"
        self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Save"}]}
        )
        body = {
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "rules": [{"kind": "route", "value": "/settings"}],
        }
        status, payload = self._json("POST", "/api/analyze", body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["findings"], [])
        stats = payload["stats"]
        self.assertEqual(stats["suppressed"], 2)
        origins = {entry["origin"]: entry for entry in stats["suppressed_rules"]}
        self.assertEqual(set(origins), {"request", "workspace"})
        self.assertEqual(origins["workspace"]["kind"], "label")
        self.assertEqual(origins["workspace"]["count"], 1)
        self.assertEqual(origins["request"]["kind"], "route")
        self.assertEqual(origins["request"]["count"], 1)

    def test_scan_persists_suppressed_rule_provenance(self):
        ws = "wsScanProv"
        self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Save"}]}
        )
        body = {
            "name": "Provenance scan",
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertEqual(scan["stats"]["suppressed"], 1)
        breakdown = scan["stats"]["suppressed_rules"]
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0]["origin"], "workspace")
        status, fetched = self._json("GET", "/api/scans/" + scan["id"])
        self.assertEqual(fetched["stats"]["suppressed_rules"], breakdown)


if __name__ == "__main__":
    unittest.main()
