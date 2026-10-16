import json
import io
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import github, notify  # noqa: E402
from docs_drift_detector.server import create_server  # noqa: E402
from docs_drift_detector.store import MAX_DIGEST_HISTORY, Store  # noqa: E402
from docs_drift_detector.trend import MAX_LIMIT  # noqa: E402


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


    def test_scan_suppressed_endpoint(self):
        ws = "wsSuppressed"
        self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"kind": "label", "value": "Save"}]}
        )
        body = {
            "name": "Suppressed scan",
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "rules": [{"kind": "route", "value": "/settings"}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertIn("suppressed_details", scan)

        status, payload = self._json("GET", "/api/scans/%s/suppressed" % scan["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["scan"], scan["id"])
        self.assertEqual(payload["suppressed"], 2)
        self.assertEqual(len(payload["rules"]), 2)
        by_origin = {entry["origin"]: entry for entry in payload["rules"]}
        self.assertEqual(set(by_origin), {"workspace", "request"})
        for entry in payload["rules"]:
            self.assertEqual(
                set(entry), {"rule_id", "origin", "kind", "page", "value", "count", "findings"}
            )
            self.assertEqual(entry["count"], len(entry["findings"]))
            self.assertEqual(entry["count"], 1)
        finding = by_origin["workspace"]["findings"][0]
        self.assertEqual(
            set(finding),
            {"id", "kind", "page", "old_value", "new_value", "score", "severity"},
        )
        self.assertEqual(finding["old_value"], "Save")
        self.assertEqual(finding["new_value"], "Save changes")
        self.assertEqual(finding["page"], "help/a.md")

    def test_scan_suppressed_endpoint_unknown_returns_404(self):
        status, payload = self._json("GET", "/api/scans/doesnotexist123/suppressed")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_scan_suppressed_endpoint_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/scans/bad%20id!/suppressed")
        self.assertEqual(status, 400)
        status, _ = self._json("GET", "/api/scans/bad/id/suppressed")
        self.assertEqual(status, 400)

    def test_scan_without_suppression_has_empty_suppressed_endpoint(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        status, payload = self._json("GET", "/api/scans/%s/suppressed" % scan["id"])
        self.assertEqual(status, 200)
        self.assertEqual(payload["suppressed"], 0)
        self.assertEqual(payload["rules"], [])

    def test_analyze_response_shape_unchanged_by_details(self):
        body = {
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
            "rules": [{"kind": "label", "value": "Save"}],
        }
        status, payload = self._json("POST", "/api/analyze", body)
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"findings", "stats", "changes"})
        self.assertIsInstance(payload["stats"]["suppressed"], int)
        self.assertNotIn("suppressed_details", payload["stats"])

    def test_rules_history_fresh_workspace_is_empty(self):
        status, payload = self._json("GET", "/api/workspaces/wsHistFresh/rules/history")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": "wsHistFresh", "history": []})

    def test_rules_history_invalid_workspace_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/rules/history")
        self.assertEqual(status, 400)

    def test_rules_history_records_actions(self):
        ws = "wsHistActions"
        self._json("PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"value": "Save"}]})
        self._json("POST", "/api/workspaces/%s/rules/import" % ws, {"rules": [{"value": "New"}]})
        self._json("DELETE", "/api/workspaces/%s/rules" % ws)
        status, payload = self._json("GET", "/api/workspaces/%s/rules/history" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"], ws)
        actions = [entry["action"] for entry in payload["history"]]
        self.assertEqual(actions, ["clear", "import", "set"])
        revisions = [entry["revision"] for entry in payload["history"]]
        self.assertEqual(revisions, [3, 2, 1])
        for entry in payload["history"]:
            self.assertIsInstance(entry["revision"], int)
            self.assertIsInstance(entry["created_at"], str)
            self.assertIsInstance(entry["rule_count"], int)
            self.assertIsInstance(entry["rules"], list)
        self.assertEqual(payload["history"][0]["rule_count"], 0)

    def test_rules_promote_restores_revision(self):
        ws = "wsPromote"
        status, first = self._json(
            "PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"value": "Old"}]}
        )
        self.assertEqual(status, 200)
        self._json("PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"value": "New"}]})

        status, payload = self._json(
            "POST", "/api/workspaces/%s/rules/history/1/promote" % ws
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["promoted"], 1)
        self.assertEqual([r["value"] for r in payload["rules"]], ["Old"])
        self.assertEqual(payload["rules"], first["rules"])

        status, stored = self._json("GET", "/api/workspaces/%s/rules" % ws)
        self.assertEqual(status, 200)
        self.assertEqual([r["value"] for r in stored["rules"]], ["Old"])

        status, history = self._json("GET", "/api/workspaces/%s/rules/history" % ws)
        self.assertEqual(history["history"][0]["action"], "promote")

    def test_rules_promote_unknown_revision_returns_404(self):
        ws = "wsPromoteMissing"
        self._json("PUT", "/api/workspaces/%s/rules" % ws, {"rules": [{"value": "Save"}]})
        status, payload = self._json(
            "POST", "/api/workspaces/%s/rules/history/999/promote" % ws
        )
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_rules_promote_invalid_inputs_return_400(self):
        status, _ = self._json(
            "POST", "/api/workspaces/bad%20id!/rules/history/1/promote"
        )
        self.assertEqual(status, 400)
        status, _ = self._json(
            "POST", "/api/workspaces/wsBadRev/rules/history/notanint/promote"
        )
        self.assertEqual(status, 400)

    def test_workspace_webhook_lifecycle(self):
        ws = "wsWebhookLife"
        status, payload = self._json("GET", "/api/workspaces/%s/webhook" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "url": ""})

        status, payload = self._json(
            "PUT",
            "/api/workspaces/%s/webhook" % ws,
            {"url": "https://hooks.example.com/ddr"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "https://hooks.example.com/ddr")

        status, payload = self._json("GET", "/api/workspaces/%s/webhook" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "url": "https://hooks.example.com/ddr"})

        status, payload = self._json("DELETE", "/api/workspaces/%s/webhook" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": ws})

        status, payload = self._json("GET", "/api/workspaces/%s/webhook" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "")

    def test_workspace_webhook_invalid_url_returns_400(self):
        ws = "wsWebhookBad"
        for bad in ("ftp://x", "https://" + "a" * 500, "not-a-url"):
            status, payload = self._json(
                "PUT", "/api/workspaces/%s/webhook" % ws, {"url": bad}
            )
            self.assertEqual(status, 400, bad)
            self.assertIn("error", payload)

    def test_workspace_webhook_missing_url_returns_400(self):
        status, _ = self._json("PUT", "/api/workspaces/wsWebhookMissing/webhook", {})
        self.assertEqual(status, 400)

    def test_workspace_webhook_empty_clears(self):
        ws = "wsWebhookClear"
        self._json(
            "PUT", "/api/workspaces/%s/webhook" % ws, {"url": "https://hooks.example.com/x"}
        )
        status, payload = self._json("PUT", "/api/workspaces/%s/webhook" % ws, {"url": ""})
        self.assertEqual(status, 200)
        self.assertEqual(payload["url"], "")
        status, payload = self._json("GET", "/api/workspaces/%s/webhook" % ws)
        self.assertEqual(payload["url"], "")

    def test_workspace_webhook_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/webhook")
        self.assertEqual(status, 400)
        status, _ = self._json(
            "PUT", "/api/workspaces/bad%20id!/webhook", {"url": "https://x.example"}
        )
        self.assertEqual(status, 400)
        status, _ = self._json("DELETE", "/api/workspaces/bad%20id!/webhook")
        self.assertEqual(status, 400)

    def test_workspace_webhook_survives_store_reload(self):
        ws = "wsWebhookDisk"
        self._json(
            "PUT", "/api/workspaces/%s/webhook" % ws, {"url": "https://hooks.example.com/ddr"}
        )
        reopened = Store(os.path.join(self.tmp.name, "scans.json"))
        self.assertEqual(
            reopened.get_workspace_webhook(ws)["url"], "https://hooks.example.com/ddr"
        )

    def test_scan_dispatches_high_severity_payload(self):
        ws = "wsWebhookPayload"
        self._json(
            "PUT", "/api/workspaces/%s/webhook" % ws, {"url": "https://hooks.example.com/x"}
        )
        captured = {}
        original = notify.dispatch

        def fake_dispatch(url, payload, timeout=5):
            captured["url"] = url
            captured["payload"] = payload
            return {"ok": True, "status": 200, "error": ""}

        notify.dispatch = fake_dispatch
        try:
            body = {
                "name": "Hook payload",
                "workspace": ws,
                "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
                "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
                "metrics": {"help/a.md": {"views": 100000, "tickets": 5}},
            }
            status, scan = self._json("POST", "/api/scans", body)
        finally:
            notify.dispatch = original
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])

        self.assertEqual(status, 201)
        self.assertEqual(captured["url"], "https://hooks.example.com/x")
        payload = captured["payload"]
        self.assertEqual(payload["scan_id"], scan["id"])
        self.assertEqual(payload["workspace"], ws)
        self.assertTrue(payload["findings"])
        self.assertEqual({f["severity"] for f in payload["findings"]}, {"high"})
        scores = [f["score"] for f in payload["findings"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_scan_with_unreachable_webhook_still_returns_201(self):
        ws = "wsWebhookDown"
        self._json(
            "PUT", "/api/workspaces/%s/webhook" % ws, {"url": "http://127.0.0.1:1/nope"}
        )
        body = {
            "name": "Hook down",
            "workspace": ws,
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        self.assertEqual(status, 201)
        self.assertEqual(scan["name"], "Hook down")
        self.assertEqual(scan["stats"]["findings"], 1)

    def test_workspace_schedule_lifecycle(self):
        ws = "wsSchedLife"
        body = {
            "interval_hours": 24,
            "enabled": True,
            "scan": {
                "name": "Nightly",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            },
        }
        status, payload = self._json("PUT", "/api/workspaces/%s/schedule" % ws, body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["interval_hours"], 24)
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["next_run_at"])
        self.assertEqual(payload["scan"]["name"], "Nightly")

        status, fetched = self._json("GET", "/api/workspaces/%s/schedule" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(fetched, payload)

        status, deleted = self._json("DELETE", "/api/workspaces/%s/schedule" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(deleted, {"deleted": ws})

        status, after = self._json("GET", "/api/workspaces/%s/schedule" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(after["interval_hours"], 0)
        self.assertFalse(after["enabled"])

    def test_workspace_schedule_unknown_default(self):
        status, payload = self._json("GET", "/api/workspaces/wsNeverSched/schedule")
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"], "wsNeverSched")
        self.assertEqual(payload["interval_hours"], 0)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["scan"], {})

    def test_workspace_schedule_validation_errors(self):
        ws = "wsSchedBad"
        for interval in (0, 200, "soon"):
            status, payload = self._json(
                "PUT", "/api/workspaces/%s/schedule" % ws,
                {"interval_hours": interval, "scan": {}},
            )
            self.assertEqual(status, 400, interval)
            self.assertIn("error", payload)
        for body in ({"interval_hours": 24}, {"interval_hours": 24, "scan": "nope"}, {"interval_hours": 24, "scan": None}):
            status, payload = self._json("PUT", "/api/workspaces/%s/schedule" % ws, body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)

    def test_workspace_schedule_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/schedule")
        self.assertEqual(status, 400)
        status, _ = self._json(
            "PUT", "/api/workspaces/bad%20id!/schedule", {"interval_hours": 1, "scan": {}}
        )
        self.assertEqual(status, 400)
        status, _ = self._json("DELETE", "/api/workspaces/bad%20id!/schedule")
        self.assertEqual(status, 400)

    def test_workspace_schedule_empty_body_disables_but_keeps_config(self):
        ws = "wsSchedDisable"
        self._json(
            "PUT", "/api/workspaces/%s/schedule" % ws,
            {"interval_hours": 12, "enabled": True, "scan": {"name": "Keep"}},
        )
        status, payload = self._json("PUT", "/api/workspaces/%s/schedule" % ws, {})
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["interval_hours"], 12)
        self.assertEqual(payload["scan"]["name"], "Keep")

        status, payload = self._json(
            "PUT", "/api/workspaces/%s/schedule" % ws, {"enabled": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["interval_hours"], 12)

        status, payload = self._json(
            "PUT", "/api/workspaces/%s/schedule" % ws, {"enabled": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])

    def test_schedule_status_shape(self):
        status, payload = self._json("GET", "/api/schedule/status")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"enabled", "tick_seconds", "due", "schedules"}
        )
        self.assertIsInstance(payload["enabled"], bool)
        self.assertIsInstance(payload["tick_seconds"], int)
        self.assertIsInstance(payload["due"], list)
        self.assertIsInstance(payload["schedules"], list)

    def test_schedule_status_lists_schedules_and_due(self):
        ws = "wsSchedStatus"
        self._json(
            "PUT", "/api/workspaces/%s/schedule" % ws,
            {"interval_hours": 1, "enabled": True, "scan": {"name": "S"}},
        )
        status, payload = self._json("GET", "/api/schedule/status")
        self.assertEqual(status, 200)
        self.assertIn(ws, [s["workspace"] for s in payload["schedules"]])
        self.assertEqual(payload["due"], [])
        self.assertEqual(
            set(payload["schedules"][0]),
            {"workspace", "interval_hours", "enabled", "scan", "next_run_at"},
        )

    def test_schedule_status_works_when_disabled(self):
        os.environ["DDR_SCHEDULER_ENABLED"] = "0"
        try:
            status, payload = self._json("GET", "/api/schedule/status")
        finally:
            os.environ.pop("DDR_SCHEDULER_ENABLED", None)
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(
            set(payload), {"enabled", "tick_seconds", "due", "schedules"}
        )

    def _record_digest(self, ws, scan_id, total=1, high=0, ok=True, error=""):
        self.server.store.record_workspace_digest(
            ws,
            {
                "scan_id": scan_id,
                "total_findings": total,
                "by_severity": {"high": high, "medium": total - high, "low": 0},
                "new_high": [{} for _ in range(high)],
            },
            {"ok": ok, "status": 200 if ok else 500, "error": error},
        )

    def test_workspace_digests_unknown_returns_empty(self):
        status, payload = self._json("GET", "/api/workspaces/wsNeverDigests/digests")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": "wsNeverDigests", "digests": []})

    def test_workspace_digests_invalid_id_returns_400(self):
        status, payload = self._json("GET", "/api/workspaces/bad%20id!/digests")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_workspace_digests_returns_newest_first(self):
        ws = "wsDigestList"
        self._record_digest(ws, "d1")
        self._record_digest(ws, "d2")
        self._record_digest(ws, "d3", total=2, high=1)
        status, payload = self._json("GET", "/api/workspaces/%s/digests" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"], ws)
        self.assertEqual([e["scan_id"] for e in payload["digests"]], ["d3", "d2", "d1"])
        entry = payload["digests"][0]
        self.assertEqual(
            set(entry),
            {
                "created_at",
                "scan_id",
                "total_findings",
                "by_severity",
                "new_high_count",
                "delivery_ok",
                "delivery_error",
            },
        )
        self.assertEqual(set(entry["by_severity"]), {"high", "medium", "low"})
        self.assertEqual(entry["new_high_count"], 1)
        self.assertTrue(entry["delivery_ok"])

    def test_workspace_digests_respects_limit(self):
        ws = "wsDigestLimit"
        for index in range(5):
            self._record_digest(ws, "d%d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/digests?limit=2" % ws)
        self.assertEqual(status, 200)
        self.assertEqual([e["scan_id"] for e in payload["digests"]], ["d4", "d3"])

    def test_workspace_digests_default_limit_is_ten(self):
        ws = "wsDigestDefault"
        for index in range(12):
            self._record_digest(ws, "d%02d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/digests" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["digests"]), 10)
        self.assertEqual(payload["digests"][0]["scan_id"], "d11")

    def test_workspace_digests_limit_is_clamped(self):
        ws = "wsDigestClamp"
        for index in range(3):
            self._record_digest(ws, "d%d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/digests?limit=0" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["digests"]), 1)
        status, payload = self._json("GET", "/api/workspaces/%s/digests?limit=999" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["digests"]), 3)

    def test_workspace_digests_limit_clamped_to_max(self):
        ws = "wsDigestMax"
        for index in range(MAX_DIGEST_HISTORY + 5):
            self._record_digest(ws, "d%d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/digests?limit=999" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["digests"]), MAX_DIGEST_HISTORY)

    def test_workspace_digests_non_integer_limit_returns_400(self):
        status, payload = self._json(
            "GET", "/api/workspaces/wsDigestBad/digests?limit=abc"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    @staticmethod
    def _trend_finding(fid="f1", severity="high", resolved=False):
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

    def _trend_scan(self, ws, findings, name="trend scan"):
        return self.server.store.create_scan(name, "release", findings, workspace=ws)

    def test_workspace_trend_unknown_returns_empty(self):
        status, payload = self._json("GET", "/api/workspaces/wsNeverTrends/trend")
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"], "wsNeverTrends")
        self.assertEqual(payload["scans"], [])
        self.assertEqual(
            payload["summary"],
            {
                "scans": 0,
                "total_findings": 0,
                "by_severity": {"high": 0, "medium": 0, "low": 0},
                "open_findings": 0,
                "high_open": 0,
                "suppressed": 0,
                "delta_high": 0,
            },
        )

    def test_workspace_trend_invalid_id_returns_400(self):
        status, payload = self._json("GET", "/api/workspaces/bad%20id!/trend")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_workspace_trend_shape_and_order(self):
        ws = "wsTrendShape"
        self._trend_scan(ws, [self._trend_finding("a")], name="first")
        self._trend_scan(
            ws,
            [self._trend_finding("b"), self._trend_finding("c")],
            name="second",
        )
        self._trend_scan(ws, [self._trend_finding("d")], name="third")
        status, payload = self._json("GET", "/api/workspaces/%s/trend" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspace"], ws)
        self.assertEqual(
            [entry["name"] for entry in payload["scans"]],
            ["first", "second", "third"],
        )
        self.assertEqual(
            [entry["high_open"] for entry in payload["scans"]], [1, 2, 1]
        )
        for entry in payload["scans"]:
            self.assertEqual(
                set(entry),
                {
                    "scan_id",
                    "created_at",
                    "name",
                    "total_findings",
                    "by_severity",
                    "open_findings",
                    "high_open",
                    "suppressed",
                },
            )
            self.assertEqual(set(entry["by_severity"]), {"high", "medium", "low"})
        self.assertEqual(
            set(payload["summary"]),
            {
                "scans",
                "total_findings",
                "by_severity",
                "open_findings",
                "high_open",
                "suppressed",
                "delta_high",
            },
        )
        self.assertEqual(payload["summary"]["scans"], 3)
        self.assertEqual(payload["summary"]["delta_high"], 0)

    def test_workspace_trend_non_integer_limit_returns_400(self):
        status, payload = self._json(
            "GET", "/api/workspaces/wsTrendBad/trend?limit=abc"
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_workspace_trend_limit_is_clamped(self):
        ws = "wsTrendClamp"
        for index in range(3):
            self._trend_scan(ws, [self._trend_finding("f%d" % index)], name="s%d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/trend?limit=0" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["scans"]), 1)
        status, payload = self._json("GET", "/api/workspaces/%s/trend?limit=999" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["scans"]), 3)

    def test_workspace_trend_limit_clamped_to_max(self):
        ws = "wsTrendMax"
        for index in range(MAX_LIMIT + 5):
            self._trend_scan(ws, [], name="s%02d" % index)
        status, payload = self._json("GET", "/api/workspaces/%s/trend?limit=999" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["scans"]), MAX_LIMIT)
        self.assertEqual(payload["summary"]["scans"], MAX_LIMIT)

    def _high_severity_scan(self, name="PR scan"):
        body = {
            "name": name,
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "metrics": {"help/a.md": {"views": 100000, "tickets": 5}},
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.assertEqual(scan["stats"]["high"], 2)
        return scan

    def test_scan_pull_request_endpoint(self):
        scan = self._high_severity_scan()
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        status, raw, ctype = self._request(
            "GET", "/api/scans/%s/pull-request" % scan["id"]
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            set(document),
            {
                "scan_id",
                "name",
                "workspace",
                "created_at",
                "branch",
                "base_branch",
                "title",
                "body",
                "files",
                "checklist",
            },
        )
        self.assertEqual(document["scan_id"], scan["id"])
        self.assertEqual(document["branch"], "docs-drift/%s" % scan["id"])
        self.assertEqual(document["base_branch"], "main")
        self.assertEqual(document["title"], "docs: address drift from %s" % scan["name"])
        self.assertEqual([f["path"] for f in document["files"]], ["help/a.md"])
        self.assertEqual(
            set(document["files"][0]), {"path", "action", "reason"}
        )
        self.assertEqual(document["files"][0]["action"], "update")
        self.assertIn("Save", document["body"])
        self.assertTrue(document["checklist"])

    def test_scan_pull_request_query_options(self):
        scan = self._high_severity_scan()
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        status, document = self._json(
            "GET",
            "/api/scans/%s/pull-request?base=develop&prefix=release" % scan["id"],
        )
        self.assertEqual(status, 200)
        self.assertEqual(document["base_branch"], "develop")
        self.assertEqual(document["branch"], "release/%s" % scan["id"])

    def test_scan_pull_request_no_high_findings(self):
        body = {
            "name": "Quiet release",
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        status, document = self._json("GET", "/api/scans/%s/pull-request" % scan["id"])
        self.assertEqual(status, 200)
        self.assertEqual(document["files"], [])
        self.assertIn("no action is required", document["body"].lower())

    def test_scan_pull_request_unknown_returns_404(self):
        status, payload = self._json("GET", "/api/scans/doesnotexist123/pull-request")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_scan_pull_request_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/scans/bad%20id!/pull-request")
        self.assertEqual(status, 400)
        status, _ = self._json("GET", "/api/scans/bad/id/pull-request")
        self.assertEqual(status, 400)

    def test_scan_pull_request_invalid_options_returns_400(self):
        scan = self._high_severity_scan()
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        for query in ("base=", "prefix=", "base=bad%20base", "prefix=bad%20prefix", "base=a^b"):
            status, payload = self._json(
                "GET", "/api/scans/%s/pull-request?%s" % (scan["id"], query)
            )
            self.assertEqual(status, 400, query)
            self.assertIn("error", payload)

    def _workspace_scan(self, workspace, name="Workspace scan"):
        body = {
            "name": name,
            "workspace": workspace,
            "changelog": 'renamed "Save" to "Save changes"\n"/settings" -> "/account/settings"',
            "pages": [{"path": "help/a.md", "content": "Click Save. Go to /settings."}],
            "metrics": {"help/a.md": {"views": 100000, "tickets": 5}},
        }
        status, scan = self._json("POST", "/api/scans", body)
        self.assertEqual(status, 201)
        return scan

    def test_workspace_github_lifecycle(self):
        ws = "wsGithubLife"
        status, payload = self._json("GET", "/api/workspaces/%s/github" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "workspace": ws,
                "repo": "",
                "base_branch": "main",
                "token_env": "GITHUB_TOKEN",
            },
        )

        status, payload = self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "base_branch": "develop", "token_env": "DOCS_TOKEN"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["repo"], "owner/name")
        self.assertEqual(payload["base_branch"], "develop")
        self.assertEqual(payload["token_env"], "DOCS_TOKEN")

        status, payload = self._json("GET", "/api/workspaces/%s/github" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload["repo"], "owner/name")

        status, payload = self._json("DELETE", "/api/workspaces/%s/github" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": ws})
        status, payload = self._json("GET", "/api/workspaces/%s/github" % ws)
        self.assertEqual(payload["repo"], "")

    def test_workspace_github_validation_returns_400(self):
        ws = "wsGithubBad"
        for body in (
            {},
            {"repo": "not-a-repo"},
            {"repo": "owner/name/extra"},
            {"repo": "owner/name", "base_branch": "bad branch!"},
            {"repo": "owner/name", "token_env": "1BAD"},
        ):
            status, payload = self._json("PUT", "/api/workspaces/%s/github" % ws, body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)

    def test_workspace_github_invalid_id_returns_400(self):
        status, _ = self._json("GET", "/api/workspaces/bad%20id!/github")
        self.assertEqual(status, 400)
        status, _ = self._json(
            "PUT", "/api/workspaces/bad%20id!/github", {"repo": "owner/name"}
        )
        self.assertEqual(status, 400)
        status, _ = self._json("DELETE", "/api/workspaces/bad%20id!/github")
        self.assertEqual(status, 400)

    def test_workspace_github_survives_store_reload(self):
        ws = "wsGithubDisk"
        self._json(
            "PUT", "/api/workspaces/%s/github" % ws, {"repo": "owner/name"}
        )
        reopened = Store(os.path.join(self.tmp.name, "scans.json"))
        self.assertEqual(reopened.get_workspace_github(ws)["repo"], "owner/name")

    def test_open_pull_request_endpoint_returns_connector_result(self):
        ws = "wsGithubOpen"
        scan = self._workspace_scan(ws)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {
                "repo": "owner/name",
                "base_branch": "develop",
                "token_env": "DDR_SERVER_GITHUB_TOKEN",
            },
        )

        captured = {}
        original = github.open_pull_request

        def fake(config, suggestion, opener=None):
            captured["config"] = config
            captured["suggestion"] = suggestion
            return {
                "ok": True,
                "status": 201,
                "error": "",
                "pr_url": "https://github.com/owner/name/pull/1",
                "branch": suggestion["branch"],
            }

        github.open_pull_request = fake
        try:
            status, payload = self._json(
                "POST", "/api/scans/%s/pull-request/open" % scan["id"]
            )
        finally:
            github.open_pull_request = original

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["pr_url"], "https://github.com/owner/name/pull/1")
        self.assertEqual(payload["branch"], "docs-drift/%s" % scan["id"])
        self.assertEqual(captured["config"]["repo"], "owner/name")
        self.assertEqual(captured["suggestion"]["scan_id"], scan["id"])
        self.assertEqual(captured["suggestion"]["base_branch"], "develop")

    def test_open_pull_request_upstream_failure_returns_200(self):
        ws = "wsGithubOpenFail"
        scan = self._workspace_scan(ws)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_GITHUB_TOKEN"},
        )

        original = github.open_pull_request
        github.open_pull_request = lambda config, suggestion, opener=None: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
            "pr_url": "",
            "branch": suggestion["branch"],
        }
        try:
            status, payload = self._json(
                "POST", "/api/scans/%s/pull-request/open" % scan["id"]
            )
        finally:
            github.open_pull_request = original

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], 500)

    def test_open_pull_request_prefix_query(self):
        ws = "wsGithubOpenPrefix"
        scan = self._workspace_scan(ws)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_GITHUB_TOKEN"},
        )

        captured = {}
        original = github.open_pull_request

        def fake(config, suggestion, opener=None):
            captured["suggestion"] = suggestion
            return {"ok": True, "status": 201, "error": "", "pr_url": "u", "branch": suggestion["branch"]}

        github.open_pull_request = fake
        try:
            status, payload = self._json(
                "POST",
                "/api/scans/%s/pull-request/open?prefix=release" % scan["id"],
            )
        finally:
            github.open_pull_request = original

        self.assertEqual(status, 200)
        self.assertEqual(payload["branch"], "release/%s" % scan["id"])
        self.assertEqual(captured["suggestion"]["branch"], "release/%s" % scan["id"])

    def test_open_pull_request_missing_config_returns_400(self):
        scan = self._workspace_scan("wsGithubUnset")
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        status, payload = self._json(
            "POST", "/api/scans/%s/pull-request/open" % scan["id"]
        )
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_open_pull_request_unset_token_returns_400(self):
        ws = "wsGithubNoToken"
        scan = self._workspace_scan(ws)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        os.environ.pop("DDR_SERVER_MISSING_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_MISSING_TOKEN"},
        )
        status, payload = self._json(
            "POST", "/api/scans/%s/pull-request/open" % scan["id"]
        )
        self.assertEqual(status, 400)
        self.assertIn("DDR_SERVER_MISSING_TOKEN", payload["error"])

    def test_open_pull_request_unknown_and_invalid_ids(self):
        status, _ = self._json("POST", "/api/scans/doesnotexist123/pull-request/open")
        self.assertEqual(status, 404)
        status, _ = self._json("POST", "/api/scans/bad%20id!/pull-request/open")
        self.assertEqual(status, 400)
        status, _ = self._json("POST", "/api/scans/bad/id/pull-request/open")
        self.assertEqual(status, 400)

    def test_open_pull_request_invalid_prefix_returns_400(self):
        ws = "wsGithubBadPrefix"
        scan = self._workspace_scan(ws)
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_GITHUB_TOKEN"},
        )
        status, _ = self._json(
            "POST", "/api/scans/%s/pull-request/open?prefix=bad%%20prefix" % scan["id"]
        )
        self.assertEqual(status, 400)

    def test_scan_auto_opens_pull_request(self):
        ws = "wsGithubAuto"
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        status, _ = self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_GITHUB_TOKEN"},
        )
        self.assertEqual(status, 200)

        captured = {}
        original = github.open_pull_request

        def fake(config, suggestion, opener=None):
            captured["config"] = config
            captured["suggestion"] = suggestion
            return {"ok": True, "status": 201, "error": "", "pr_url": "u", "branch": suggestion["branch"]}

        github.open_pull_request = fake
        try:
            scan = self._workspace_scan(ws, name="Auto PR")
        finally:
            github.open_pull_request = original
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])

        self.assertEqual(captured["config"]["repo"], "owner/name")
        self.assertEqual(captured["suggestion"]["scan_id"], scan["id"])
        self.assertEqual(captured["suggestion"]["name"], "Auto PR")

    def test_scan_with_failing_github_still_returns_201(self):
        ws = "wsGithubAutoFail"
        os.environ["DDR_SERVER_GITHUB_TOKEN"] = "x"
        self.addCleanup(os.environ.pop, "DDR_SERVER_GITHUB_TOKEN", None)
        self._json(
            "PUT",
            "/api/workspaces/%s/github" % ws,
            {"repo": "owner/name", "token_env": "DDR_SERVER_GITHUB_TOKEN"},
        )
        original = github.open_pull_request
        github.open_pull_request = lambda config, suggestion, opener=None: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
            "pr_url": "",
            "branch": suggestion["branch"],
        }
        try:
            scan = self._workspace_scan(ws, name="Auto PR fail")
        finally:
            github.open_pull_request = original
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        self.assertEqual(scan["name"], "Auto PR fail")
        self.assertEqual(scan["stats"]["high"], 2)

    def test_workspace_retention_lifecycle(self):
        ws = "wsRetLife"
        status, payload = self._json("GET", "/api/workspaces/%s/retention" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "max_scans": 0})

        status, payload = self._json(
            "PUT", "/api/workspaces/%s/retention" % ws, {"max_scans": 3}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "max_scans": 3})

        status, payload = self._json("GET", "/api/workspaces/%s/retention" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "max_scans": 3})

        status, payload = self._json(
            "PUT", "/api/workspaces/%s/retention" % ws, {"max_scans": 0}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"workspace": ws, "max_scans": 0})
        status, payload = self._json("GET", "/api/workspaces/%s/retention" % ws)
        self.assertEqual(payload["max_scans"], 0)

        self._json("PUT", "/api/workspaces/%s/retention" % ws, {"max_scans": 5})
        status, payload = self._json("DELETE", "/api/workspaces/%s/retention" % ws)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"deleted": ws})
        status, payload = self._json("GET", "/api/workspaces/%s/retention" % ws)
        self.assertEqual(payload["max_scans"], 0)

    def test_workspace_retention_validation_returns_400(self):
        ws = "wsRetBad"
        for bad in (
            {"max_scans": "3"},
            {"max_scans": -1},
            {"max_scans": 10001},
            {"max_scans": 1.5},
            {"max_scans": True},
            {},
        ):
            status, payload = self._json(
                "PUT", "/api/workspaces/%s/retention" % ws, bad
            )
            self.assertEqual(status, 400, bad)
            self.assertIn("error", payload)

    def test_workspace_retention_invalid_id_returns_400(self):
        for method in ("GET", "PUT", "DELETE"):
            body = {"max_scans": 2} if method == "PUT" else None
            status, _ = self._json(
                method, "/api/workspaces/bad%20id!/retention", body
            )
            self.assertEqual(status, 400, method)

    def test_retention_prunes_automatically_on_scan(self):
        ws = "wsRetPrune"
        other = "wsRetOther"
        self._json("PUT", "/api/workspaces/%s/retention" % ws, {"max_scans": 2})

        scan_ids = [
            self._workspace_scan(ws, name=label)["id"]
            for label in ("RetP1", "RetP2", "RetP3")
        ]
        other_scan = self._workspace_scan(other, name="RetOther1")
        status, plain = self._json(
            "POST",
            "/api/scans",
            {
                "name": "RetPlain1",
                "changelog": 'renamed "Save" to "Save changes"',
                "pages": [{"path": "help/a.md", "content": "Click Save."}],
            },
        )
        self.assertEqual(status, 201)

        for scan_id in scan_ids[1:] + [other_scan["id"], plain["id"]]:
            self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan_id)

        _, listing = self._json("GET", "/api/scans")
        ids = {scan["id"] for scan in listing["scans"]}
        self.assertNotIn(scan_ids[0], ids)
        self.assertIn(scan_ids[1], ids)
        self.assertIn(scan_ids[2], ids)
        self.assertIn(other_scan["id"], ids)
        self.assertIn(plain["id"], ids)

    def test_scan_prune_failure_still_returns_201(self):
        ws = "wsRetFail"
        self._json("PUT", "/api/workspaces/%s/retention" % ws, {"max_scans": 1})
        store = self.server.store
        original = store.get_workspace_retention

        def boom(workspace):
            raise RuntimeError("kaboom")

        store.get_workspace_retention = boom
        stderr = io.StringIO()
        try:
            with redirect_stderr(stderr):
                scan = self._workspace_scan(ws, name="RetFail")
        finally:
            store.get_workspace_retention = original
        self.addCleanup(self._json, "DELETE", "/api/scans/%s" % scan["id"])
        self.assertEqual(scan["name"], "RetFail")
        self.assertIn("retention prune error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
