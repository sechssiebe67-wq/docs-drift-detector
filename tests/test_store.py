import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector.store import MAX_RULE_HISTORY, Store  # noqa: E402


def _finding(fid="abc", severity="high"):
    return {
        "id": fid,
        "kind": "label",
        "page": "help/a.md",
        "title": "t",
        "detail": "d",
        "old_value": "Save",
        "new_value": "Save changes",
        "snippet": "",
        "occurrences": 1,
        "resolved": False,
        "metrics": {"views": 0.0, "tickets": 0.0},
        "score": 40.0,
        "severity": severity,
    }


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "scans.json")
        self.store = Store(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_get_list(self):
        scan = self.store.create_scan("R1", "release", [_finding()], pages=[{"path": "a", "content": ""}])
        self.assertTrue(scan["id"])
        self.assertEqual(scan["stats"]["findings"], 1)
        self.assertEqual(self.store.get_scan(scan["id"])["name"], "R1")
        self.assertEqual(len(self.store.list_scans()), 1)

    def test_create_scan_stats_include_suppressed(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        self.assertEqual(scan["stats"]["suppressed"], 0)
        scan = self.store.create_scan("R2", "release", [_finding()], suppressed=2)
        self.assertEqual(scan["stats"]["suppressed"], 2)
        self.assertEqual(self.store.get_scan(scan["id"])["stats"]["suppressed"], 2)

    def test_resolve_finding_keeps_suppressed(self):
        scan = self.store.create_scan("R1", "release", [_finding()], suppressed=2)
        updated = self.store.resolve_finding(scan["id"], "abc", True)
        self.assertEqual(updated["stats"]["suppressed"], 2)

    def test_resolve_finding_updates_stats(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        updated = self.store.resolve_finding(scan["id"], "abc", True)
        self.assertTrue(updated["findings"][0]["resolved"])
        self.assertEqual(updated["stats"]["unresolved"], 0)
        self.assertEqual(updated["stats"]["resolved"], 1)

    def test_resolve_missing_returns_none(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        self.assertIsNone(self.store.resolve_finding(scan["id"], "nope", True))
        self.assertIsNone(self.store.resolve_finding("missing", "abc", True))

    def test_persistence_across_instances(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        reopened = Store(self.path)
        self.assertIsNotNone(reopened.get_scan(scan["id"]))
        self.assertEqual(len(reopened.list_scans()), 1)

    def test_delete(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        self.assertTrue(self.store.delete_scan(scan["id"]))
        self.assertFalse(self.store.delete_scan(scan["id"]))
        self.assertIsNone(self.store.get_scan(scan["id"]))

    def test_create_scan_persists_workspace(self):
        scan = self.store.create_scan("R1", "release", [_finding()], workspace="ws1")
        self.assertEqual(scan["workspace"], "ws1")
        self.assertEqual(self.store.get_scan(scan["id"])["workspace"], "ws1")

    def test_create_scan_without_workspace_has_no_key(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        self.assertNotIn("workspace", scan)

    def test_workspace_rules_crud(self):
        self.assertEqual(self.store.list_workspace_rules("ws1"), [])
        saved = self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Save"}])
        self.assertEqual(saved[0]["value"], "Save")
        self.assertEqual(self.store.list_workspace_rules("ws1"), saved)
        self.assertTrue(self.store.delete_workspace_rules("ws1"))
        self.assertEqual(self.store.list_workspace_rules("ws1"), [])

    def test_workspace_rules_persist_across_instances(self):
        self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Save"}])
        reopened = Store(self.path)
        self.assertEqual(reopened.list_workspace_rules("ws1")[0]["value"], "Save")

    def test_workspace_rules_persist_id_and_origin_metadata(self):
        saved = self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Save"}])
        self.assertEqual(saved[0]["origin"], "workspace")
        self.assertTrue(saved[0]["id"])
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn('"id"', raw)
        self.assertIn('"origin"', raw)
        reopened = Store(self.path)
        loaded = reopened.list_workspace_rules("ws1")[0]
        self.assertEqual(loaded["origin"], "workspace")
        self.assertEqual(loaded["id"], saved[0]["id"])

    def test_legacy_workspace_rules_without_metadata_are_regenerated(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                '{"scans": {}, "workspace_rules": {"ws1": [{"kind": "label", "value": "Save"}]}}'
            )
        reopened = Store(self.path)
        rule = reopened.list_workspace_rules("ws1")[0]
        self.assertEqual(rule["origin"], "workspace")
        self.assertTrue(rule["id"])

    def test_create_scan_persists_suppressed_rules(self):
        breakdown = [
            {"id": "abc123", "origin": "workspace", "kind": "label", "page": "", "value": "Save", "count": 2}
        ]
        scan = self.store.create_scan(
            "R1", "release", [_finding()], suppressed=2, suppressed_rules=breakdown
        )
        self.assertEqual(scan["stats"]["suppressed"], 2)
        self.assertEqual(scan["stats"]["suppressed_rules"], breakdown)
        reopened = Store(self.path)
        self.assertEqual(reopened.get_scan(scan["id"])["stats"]["suppressed_rules"], breakdown)

    def test_create_scan_persists_suppressed_details(self):
        details = [
            {
                "rule_id": "abc123",
                "origin": "workspace",
                "kind": "label",
                "page": "",
                "value": "Save",
                "findings": [
                    {
                        "id": "f1",
                        "kind": "label",
                        "page": "help/a.md",
                        "old_value": "Save",
                        "new_value": "Save changes",
                        "score": 40.0,
                        "severity": "medium",
                    }
                ],
            }
        ]
        scan = self.store.create_scan(
            "R1", "release", [], suppressed=1, suppressed_details=details
        )
        self.assertEqual(scan["suppressed_details"], details)
        reopened = Store(self.path)
        self.assertEqual(reopened.get_scan(scan["id"])["suppressed_details"], details)

    def test_create_scan_without_details_has_empty_list(self):
        scan = self.store.create_scan("R1", "release", [_finding()])
        self.assertEqual(scan["suppressed_details"], [])

    def test_resolve_finding_keeps_suppressed_details(self):
        details = [
            {
                "rule_id": "abc123",
                "origin": "workspace",
                "kind": "label",
                "page": "",
                "value": "Save",
                "findings": [],
            }
        ]
        scan = self.store.create_scan(
            "R1", "release", [_finding()], suppressed=1, suppressed_details=details
        )
        updated = self.store.resolve_finding(scan["id"], "abc", True)
        self.assertEqual(updated["suppressed_details"], details)

    def test_old_store_without_suppressed_details_loads_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                '{"scans": {"oldid": {"id": "oldid", "name": "Old", "stats": {}}}}'
            )
        reopened = Store(self.path)
        scan = reopened.get_scan("oldid")
        self.assertIsNotNone(scan)
        self.assertEqual(scan["suppressed_details"], [])
        self.assertEqual(len(reopened.list_scans()), 1)

    def test_resolve_finding_keeps_suppressed_rules(self):
        breakdown = [
            {"id": "abc123", "origin": "workspace", "kind": "label", "page": "", "value": "Save", "count": 2}
        ]
        scan = self.store.create_scan(
            "R1", "release", [_finding()], suppressed=2, suppressed_rules=breakdown
        )
        updated = self.store.resolve_finding(scan["id"], "abc", True)
        self.assertEqual(updated["stats"]["suppressed_rules"], breakdown)

    def test_workspace_rules_malformed_input_tolerated(self):
        saved = self.store.set_workspace_rules(
            "ws1",
            [123, None, "  ", {"unknown": 1}, "Bare", {"kind": "label", "value": "Keep"}],
        )
        values = [rule["value"] for rule in saved]
        self.assertEqual(sorted(values), ["Bare", "Keep"])
        self.assertEqual(len(saved), 2)

    def test_workspace_rules_reject_invalid_names(self):
        for bad in ("bad id!", "", "a" * 65, "a/b", None):
            with self.assertRaises(ValueError):
                self.store.set_workspace_rules(bad, [])
            with self.assertRaises(ValueError):
                self.store.list_workspace_rules(bad)
            with self.assertRaises(ValueError):
                self.store.delete_workspace_rules(bad)

    def test_old_store_without_workspace_rules_key_loads(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"scans": {}}')
        reopened = Store(self.path)
        self.assertEqual(reopened.list_workspace_rules("ws1"), [])
        reopened.set_workspace_rules("ws1", ["Save"])
        self.assertEqual(reopened.list_workspace_rules("ws1")[0]["value"], "Save")

    def test_workspace_rule_history_newest_first(self):
        self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Save"}])
        self.store.set_workspace_rules(
            "ws1", [{"kind": "label", "value": "Save"}, {"kind": "route", "value": "/a"}]
        )
        history = self.store.list_workspace_rule_history("ws1")
        self.assertEqual([entry["revision"] for entry in history], [2, 1])
        self.assertEqual([entry["action"] for entry in history], ["set", "set"])
        self.assertEqual([entry["rule_count"] for entry in history], [2, 1])
        self.assertIsInstance(history[0]["created_at"], str)
        self.assertEqual(history[0]["rules"][1]["value"], "/a")

    def test_workspace_rule_history_records_import_and_clear(self):
        self.store.set_workspace_rules("ws1", [], action="import")
        self.store.delete_workspace_rules("ws1")
        history = self.store.list_workspace_rule_history("ws1")
        self.assertEqual([entry["action"] for entry in history], ["clear", "import"])

    def test_workspace_rule_history_capped(self):
        for index in range(MAX_RULE_HISTORY + 5):
            self.store.set_workspace_rules("ws1", [{"value": "v%d" % index}])
        history = self.store.list_workspace_rule_history("ws1")
        self.assertEqual(len(history), MAX_RULE_HISTORY)
        self.assertEqual(history[0]["revision"], MAX_RULE_HISTORY + 5)
        self.assertEqual(history[-1]["revision"], 6)
        self.assertEqual(history[0]["rules"][0]["value"], "v%d" % (MAX_RULE_HISTORY + 4))

    def test_workspace_rule_promote_restores_and_records(self):
        self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Old"}])
        self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "New"}])
        restored = self.store.promote_workspace_rule_revision("ws1", 1)
        self.assertEqual([rule["value"] for rule in restored], ["Old"])
        self.assertEqual(self.store.list_workspace_rules("ws1"), restored)
        history = self.store.list_workspace_rule_history("ws1")
        self.assertEqual(history[0]["action"], "promote")
        self.assertEqual(history[0]["rule_count"], 1)
        self.assertEqual(history[0]["rules"], restored)

    def test_workspace_rule_promote_unknown_returns_none(self):
        self.store.set_workspace_rules("ws1", [{"value": "Save"}])
        self.assertIsNone(self.store.promote_workspace_rule_revision("ws1", 999))
        self.assertEqual([rule["value"] for rule in self.store.list_workspace_rules("ws1")], ["Save"])

    def test_workspace_rule_history_persists(self):
        self.store.set_workspace_rules("ws1", [{"kind": "label", "value": "Save"}])
        reopened = Store(self.path)
        history = reopened.list_workspace_rule_history("ws1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["revision"], 1)

    def test_old_store_without_history_key_loads_empty(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"scans": {}, "workspace_rules": {"ws1": [{"value": "Save"}]}}')
        reopened = Store(self.path)
        self.assertEqual(reopened.list_workspace_rule_history("ws1"), [])
        reopened.set_workspace_rules("ws1", [{"value": "Save"}])
        self.assertEqual(reopened.list_workspace_rule_history("ws1")[0]["revision"], 1)

    def test_workspace_rule_history_ignores_malformed_entries(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                '{"scans": {}, "workspace_rule_history": {"ws1": '
                '[{"revision": "x"}, 5, {"action": "clear"}, '
                '{"revision": 2, "action": "set", "rules": [{"value": "Keep"}]}]}}'
            )
        reopened = Store(self.path)
        history = reopened.list_workspace_rule_history("ws1")
        self.assertEqual([entry["revision"] for entry in history], [2])
        self.assertEqual(history[0]["rules"][0]["value"], "Keep")

    def test_workspace_webhook_crud(self):
        self.assertEqual(self.store.get_workspace_webhook("ws1"), {"url": ""})
        saved = self.store.set_workspace_webhook("ws1", "https://hooks.example.com/ddr")
        self.assertEqual(saved, {"url": "https://hooks.example.com/ddr"})
        self.assertEqual(
            self.store.get_workspace_webhook("ws1"), {"url": "https://hooks.example.com/ddr"}
        )
        self.assertTrue(self.store.delete_workspace_webhook("ws1"))
        self.assertEqual(self.store.get_workspace_webhook("ws1"), {"url": ""})
        self.assertFalse(self.store.delete_workspace_webhook("ws1"))

    def test_workspace_webhook_empty_clears(self):
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/ddr")
        stored = self.store.set_workspace_webhook("ws1", "   ")
        self.assertEqual(stored, {"url": ""})
        self.assertEqual(self.store.get_workspace_webhook("ws1"), {"url": ""})

    def test_workspace_webhook_validation(self):
        for bad in (
            "ftp://x",
            "javascript:alert(1)",
            "https://",
            "https://" + "a" * 500,
            None,
            42,
        ):
            with self.assertRaises(ValueError):
                self.store.set_workspace_webhook("ws1", bad)

    def test_workspace_webhook_persists_across_instances(self):
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/ddr")
        reopened = Store(self.path)
        self.assertEqual(
            reopened.get_workspace_webhook("ws1")["url"], "https://hooks.example.com/ddr"
        )

    def test_workspace_webhook_rejects_invalid_names(self):
        for bad in ("bad id!", "", "a" * 65, "a/b", None):
            with self.assertRaises(ValueError):
                self.store.get_workspace_webhook(bad)
            with self.assertRaises(ValueError):
                self.store.set_workspace_webhook(bad, "https://x.example")
            with self.assertRaises(ValueError):
                self.store.delete_workspace_webhook(bad)

    def test_old_store_without_webhooks_key_loads(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"scans": {}}')
        reopened = Store(self.path)
        self.assertEqual(reopened.get_workspace_webhook("ws1"), {"url": ""})
        reopened.set_workspace_webhook("ws1", "https://x.example")
        self.assertEqual(reopened.get_workspace_webhook("ws1")["url"], "https://x.example")

    def test_malformed_webhooks_key_does_not_discard_scans_or_rules(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "scans": {"s1": {"id": "s1", "name": "Old", "stats": {}}},
                        "workspace_rules": {"ws1": [{"kind": "label", "value": "Save"}]},
                        "workspace_webhooks": {
                            "ws1": "ftp://bad",
                            "ws2": 123,
                            "ws3": "https://hooks.example.com/ok",
                            "bad id!": "https://x.example",
                        },
                    }
                )
            )
        reopened = Store(self.path)
        self.assertIsNotNone(reopened.get_scan("s1"))
        self.assertEqual(len(reopened.list_scans()), 1)
        self.assertEqual(reopened.list_workspace_rules("ws1")[0]["value"], "Save")
        self.assertEqual(reopened.get_workspace_webhook("ws1"), {"url": ""})
        self.assertEqual(
            reopened.get_workspace_webhook("ws3")["url"], "https://hooks.example.com/ok"
        )

    def test_workspace_github_crud(self):
        default = self.store.get_workspace_github("ws1")
        self.assertEqual(default["repo"], "")
        self.assertEqual(default["base_branch"], "main")
        self.assertEqual(default["token_env"], "GITHUB_TOKEN")
        saved = self.store.set_workspace_github("ws1", {"repo": "owner/name"})
        self.assertEqual(saved["repo"], "owner/name")
        self.assertEqual(saved["base_branch"], "main")
        self.assertEqual(saved["token_env"], "GITHUB_TOKEN")
        self.assertEqual(self.store.get_workspace_github("ws1"), saved)
        self.assertTrue(self.store.delete_workspace_github("ws1"))
        self.assertFalse(self.store.delete_workspace_github("ws1"))
        self.assertEqual(self.store.get_workspace_github("ws1")["repo"], "")

    def test_workspace_github_empty_repo_clears(self):
        self.store.set_workspace_github("ws1", {"repo": "owner/name"})
        stored = self.store.set_workspace_github("ws1", {"repo": "   "})
        self.assertEqual(stored["repo"], "")
        self.assertEqual(self.store.get_workspace_github("ws1")["repo"], "")

    def test_workspace_github_validation(self):
        for bad in (
            {"repo": "not-a-repo"},
            {"repo": "owner/name/extra"},
            {"repo": "owner/"},
            {"repo": "owner/name", "base_branch": "bad branch!"},
            {"repo": "owner/name", "base_branch": "x" * 201},
            {"repo": "owner/name", "token_env": "1BAD"},
            {"repo": "owner/name", "token_env": "has space"},
            None,
            "nope",
        ):
            with self.assertRaises(ValueError):
                self.store.set_workspace_github("ws1", bad)

    def test_workspace_github_accepts_custom_fields(self):
        saved = self.store.set_workspace_github(
            "ws1",
            {"repo": "owner/name", "base_branch": "release/4.2", "token_env": "DOCS_TOKEN"},
        )
        self.assertEqual(saved["base_branch"], "release/4.2")
        self.assertEqual(saved["token_env"], "DOCS_TOKEN")

    def test_workspace_github_persists_across_instances(self):
        self.store.set_workspace_github("ws1", {"repo": "owner/name"})
        reopened = Store(self.path)
        self.assertEqual(reopened.get_workspace_github("ws1")["repo"], "owner/name")

    def test_workspace_github_rejects_invalid_names(self):
        for bad in ("bad id!", "", "a" * 65, "a/b", None):
            with self.assertRaises(ValueError):
                self.store.get_workspace_github(bad)
            with self.assertRaises(ValueError):
                self.store.set_workspace_github(bad, {"repo": "owner/name"})
            with self.assertRaises(ValueError):
                self.store.delete_workspace_github(bad)

    def test_old_store_without_github_key_loads(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"scans": {}}')
        reopened = Store(self.path)
        self.assertEqual(reopened.get_workspace_github("ws1")["repo"], "")
        reopened.set_workspace_github("ws1", {"repo": "owner/name"})
        self.assertEqual(reopened.get_workspace_github("ws1")["repo"], "owner/name")

    def test_malformed_github_key_does_not_discard_scans_or_rules(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "scans": {"s1": {"id": "s1", "name": "Old", "stats": {}}},
                        "workspace_rules": {"ws1": [{"kind": "label", "value": "Save"}]},
                        "workspace_github": {
                            "ws1": {"repo": "not-a-repo"},
                            "ws2": 123,
                            "ws3": {"repo": "owner/name"},
                            "bad id!": {"repo": "owner/name"},
                        },
                    }
                )
            )
        reopened = Store(self.path)
        self.assertIsNotNone(reopened.get_scan("s1"))
        self.assertEqual(reopened.list_workspace_rules("ws1")[0]["value"], "Save")
        self.assertEqual(reopened.get_workspace_github("ws1")["repo"], "")
        self.assertEqual(reopened.get_workspace_github("ws3")["repo"], "owner/name")

    def test_workspace_schedule_crud(self):
        default = self.store.get_workspace_schedule("ws1")
        self.assertEqual(default["interval_hours"], 0)
        self.assertFalse(default["enabled"])
        saved = self.store.set_workspace_schedule(
            "ws1",
            {"interval_hours": 24, "enabled": True, "scan": {"name": "Nightly"}},
        )
        self.assertEqual(saved["workspace"], "ws1")
        self.assertEqual(saved["interval_hours"], 24)
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["next_run_at"])
        self.assertEqual(self.store.get_workspace_schedule("ws1"), saved)
        self.assertTrue(self.store.delete_workspace_schedule("ws1"))
        self.assertFalse(self.store.delete_workspace_schedule("ws1"))
        self.assertEqual(self.store.get_workspace_schedule("ws1")["interval_hours"], 0)

    def test_workspace_schedule_persists_across_instances(self):
        self.store.set_workspace_schedule(
            "ws1", {"interval_hours": 6, "enabled": True, "scan": {"name": "Six"}}
        )
        reopened = Store(self.path)
        loaded = reopened.get_workspace_schedule("ws1")
        self.assertEqual(loaded["interval_hours"], 6)
        self.assertEqual(loaded["scan"]["name"], "Six")
        self.assertTrue(loaded["next_run_at"])

    def test_workspace_schedule_preserves_explicit_next_run_at(self):
        when = "2020-01-01T00:00:00+00:00"
        saved = self.store.set_workspace_schedule(
            "ws1",
            {"interval_hours": 1, "enabled": True, "scan": {}, "next_run_at": when},
        )
        self.assertEqual(saved["next_run_at"], when)

    def test_workspace_schedule_validation(self):
        for bad in (
            {"interval_hours": 0, "scan": {}},
            {"interval_hours": 200, "scan": {}},
            {"interval_hours": "soon", "scan": {}},
            {"interval_hours": True, "scan": {}},
            {"interval_hours": 24},
            {"interval_hours": 24, "scan": None},
            {"interval_hours": 24, "scan": "nope"},
            None,
        ):
            with self.assertRaises(ValueError):
                self.store.set_workspace_schedule("ws1", bad)
        self.assertEqual(self.store.get_workspace_schedule("ws1")["interval_hours"], 0)

    def test_workspace_schedule_rejects_invalid_names(self):
        for bad in ("bad id!", "", "a" * 65, "a/b", None):
            with self.assertRaises(ValueError):
                self.store.get_workspace_schedule(bad)
            with self.assertRaises(ValueError):
                self.store.set_workspace_schedule(
                    bad, {"interval_hours": 1, "scan": {}}
                )
            with self.assertRaises(ValueError):
                self.store.delete_workspace_schedule(bad)
            with self.assertRaises(ValueError):
                self.store.latest_workspace_scan(bad)

    def test_list_due_schedules(self):
        past = "2020-01-01T00:00:00+00:00"
        future = "2999-01-01T00:00:00+00:00"
        self.store.set_workspace_schedule(
            "wsPast", {"interval_hours": 1, "enabled": True, "scan": {}, "next_run_at": past}
        )
        self.store.set_workspace_schedule(
            "wsFuture", {"interval_hours": 1, "enabled": True, "scan": {}, "next_run_at": future}
        )
        self.store.set_workspace_schedule(
            "wsOff", {"interval_hours": 1, "enabled": False, "scan": {}, "next_run_at": past}
        )
        due = self.store.list_due_schedules("2026-01-01T00:00:00+00:00")
        self.assertEqual([config["workspace"] for config in due], ["wsPast"])

    def test_list_due_schedules_accepts_datetime(self):
        past = "2020-01-01T00:00:00+00:00"
        self.store.set_workspace_schedule(
            "wsPast", {"interval_hours": 1, "enabled": True, "scan": {}, "next_run_at": past}
        )
        from datetime import datetime, timezone

        self.assertEqual(
            [c["workspace"] for c in self.store.list_due_schedules(datetime.now(timezone.utc))],
            ["wsPast"],
        )

    def test_list_workspace_schedules(self):
        self.store.set_workspace_schedule(
            "wsB", {"interval_hours": 1, "enabled": True, "scan": {"name": "b"}}
        )
        self.store.set_workspace_schedule(
            "wsA", {"interval_hours": 2, "enabled": False, "scan": {"name": "a"}}
        )
        schedules = self.store.list_workspace_schedules()
        self.assertEqual([c["workspace"] for c in schedules], ["wsA", "wsB"])

    def test_latest_workspace_scan(self):
        self.assertIsNone(self.store.latest_workspace_scan("ws1"))
        self.store.create_scan("one", "release", [_finding()], workspace="ws1")
        second = self.store.create_scan("two", "release", [_finding()], workspace="ws1")
        self.store.create_scan("other", "release", [_finding()], workspace="ws2")
        latest = self.store.latest_workspace_scan("ws1")
        self.assertEqual(latest["id"], second["id"])

    def test_old_store_without_schedules_key_loads(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"scans": {}}')
        reopened = Store(self.path)
        self.assertEqual(reopened.get_workspace_schedule("ws1")["interval_hours"], 0)
        reopened.set_workspace_schedule("ws1", {"interval_hours": 1, "scan": {}})
        self.assertEqual(reopened.list_workspace_schedules()[0]["workspace"], "ws1")

    def test_malformed_schedules_key_is_tolerated(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "scans": {"s1": {"id": "s1", "name": "Old", "stats": {}}},
                        "workspace_schedules": {
                            "ws1": {"interval_hours": 0, "scan": {}},
                            "ws2": "nonsense",
                            "ws3": {
                                "interval_hours": 3,
                                "enabled": True,
                                "scan": {"name": "Keep"},
                            },
                            "bad id!": {"interval_hours": 1, "scan": {}},
                        },
                    }
                )
            )
        reopened = Store(self.path)
        self.assertIsNotNone(reopened.get_scan("s1"))
        schedules = reopened.list_workspace_schedules()
        self.assertEqual([c["workspace"] for c in schedules], ["ws3"])
        self.assertEqual(schedules[0]["scan"]["name"], "Keep")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses filesystem permissions")
    def test_create_scan_survives_write_failure(self):
        directory = os.path.join(self.tmp.name, "ro")
        os.makedirs(directory)
        path = os.path.join(directory, "scans.json")
        os.chmod(directory, 0o500)
        try:
            store = Store(path)
            with self.assertRaises(OSError):
                store.create_scan("R1", "release", [_finding()])
        finally:
            os.chmod(directory, 0o700)


if __name__ == "__main__":
    unittest.main()
