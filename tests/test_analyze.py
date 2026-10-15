import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import analyze  # noqa: E402


class ExtractChangesTest(unittest.TestCase):
    def test_labels_from_unified_diff(self):
        diff = (
            "@@ -1,3 +1,3 @@\n"
            '-  label: "Save",\n'
            '+  label: "Save changes",\n'
            '   disabled: false,\n'
        )
        changes = analyze.extract_changes(diff)
        self.assertEqual(changes["labels"], [{"old": "Save", "new": "Save changes", "source": "diff"}])
        self.assertEqual(changes["routes"], [])

    def test_routes_from_unified_diff(self):
        diff = '@@ -1 +1 @@\n-  path: "/settings",\n+  path: "/account/settings",\n'
        changes = analyze.extract_changes(diff)
        self.assertEqual(
            changes["routes"],
            [{"old": "/settings", "new": "/account/settings", "source": "diff"}],
        )

    def test_changelog_rename_phrasing(self):
        changes = analyze.extract_changes('renamed "Projects" to "Workspaces"')
        self.assertEqual(changes["labels"][0]["old"], "Projects")
        self.assertEqual(changes["labels"][0]["new"], "Workspaces")

    def test_arrow_route(self):
        changes = analyze.extract_changes('"/settings" -> "/account/settings"')
        self.assertEqual(changes["routes"][0]["old"], "/settings")
        self.assertEqual(changes["routes"][0]["new"], "/account/settings")

    def test_structured_input(self):
        changes = analyze.extract_changes(labels=[["A", "B"]], routes=[{"from": "/x", "to": "/y"}])
        self.assertEqual(changes["labels"][0]["old"], "A")
        self.assertEqual(changes["routes"][0]["new"], "/y")

    def test_deduplication(self):
        text = 'renamed "Save" to "Store"\nrenamed "Save" to "Store"'
        changes = analyze.extract_changes(text)
        self.assertEqual(len(changes["labels"]), 1)


class DetectDriftTest(unittest.TestCase):
    def _pages(self):
        return [
            {"path": "help/a.md", "content": "Click Save to continue. Visit /settings."},
            {"path": "help/b.md", "content": "Unrelated page."},
        ]

    def test_label_and_route_drift(self):
        changes = {
            "labels": [{"old": "Save", "new": "Save changes"}],
            "routes": [{"old": "/settings", "new": "/account/settings"}],
        }
        findings = analyze.detect_drift(changes, pages=self._pages())
        pages = {f["page"] for f in findings}
        self.assertEqual(pages, {"help/a.md"})
        kinds = {f["kind"] for f in findings}
        self.assertEqual(kinds, {"label", "route"})

    def test_no_drift_when_old_value_absent(self):
        changes = {"labels": [{"old": "Nonexistent", "new": "Other"}], "routes": []}
        self.assertEqual(analyze.detect_drift(changes, pages=self._pages()), [])

    def test_route_scores_higher_than_label(self):
        changes = {
            "labels": [{"old": "Save", "new": "Save changes"}],
            "routes": [{"old": "/settings", "new": "/account/settings"}],
        }
        findings = analyze.detect_drift(changes, pages=self._pages())
        self.assertEqual(findings[0]["kind"], "route")
        self.assertGreater(findings[0]["score"], findings[1]["score"])

    def test_metrics_raise_severity_and_score(self):
        changes = {"labels": [{"old": "Save", "new": "Save changes"}], "routes": []}
        low = analyze.detect_drift(changes, pages=self._pages())[0]
        high = analyze.detect_drift(
            changes,
            pages=self._pages(),
            metrics={"help/a.md": {"views": 5000, "tickets": 6}},
        )[0]
        self.assertGreater(high["score"], low["score"])
        self.assertEqual(high["severity"], "high")

    def test_screenshot_hash_change_is_drift(self):
        pages = [{"path": "help/b.md", "content": "![shot](img/dashboard.png)"}]
        shots = [
            {
                "image": "img/dashboard.png",
                "baseline_hash": "a1",
                "current_hash": "b2",
            }
        ]
        findings = analyze.detect_drift({"labels": [], "routes": []}, pages=pages, screenshots=shots)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "screenshot")

    def test_unchanged_screenshot_ignored(self):
        pages = [{"path": "help/b.md", "content": "![shot](img/dashboard.png)"}]
        shots = [{"image": "img/dashboard.png", "baseline_hash": "a1", "current_hash": "a1"}]
        self.assertEqual(
            analyze.detect_drift({"labels": [], "routes": []}, pages=pages, screenshots=shots), []
        )

    def test_summarise_counts(self):
        findings = [
            {"resolved": False, "severity": "high"},
            {"resolved": True, "severity": "low"},
        ]
        stats = analyze.summarise(findings)
        self.assertEqual(stats["findings"], 2)
        self.assertEqual(stats["unresolved"], 1)
        self.assertEqual(stats["resolved"], 1)

    def test_rule_suppresses_label_but_keeps_route(self):
        changes = {
            "labels": [{"old": "Save", "new": "Save changes"}],
            "routes": [{"old": "/settings", "new": "/account/settings"}],
        }
        findings = analyze.detect_drift(
            changes, pages=self._pages(), rules=[{"kind": "label", "value": "Save"}]
        )
        self.assertEqual([f["kind"] for f in findings], ["route"])

    def test_page_glob_rule_only_suppresses_matching_pages(self):
        changes = {"labels": [{"old": "Projects", "new": "Workspaces"}], "routes": []}
        pages = [
            {"path": "help/a.md", "content": "Projects here."},
            {"path": "help/b.md", "content": "Projects there."},
        ]
        findings = analyze.detect_drift(
            changes, pages=pages, rules=[{"kind": "label", "page": "help/b.*"}]
        )
        self.assertEqual([f["page"] for f in findings], ["help/a.md"])

    def test_rules_are_backwards_compatible(self):
        changes = {"labels": [{"old": "Save", "new": "Save changes"}], "routes": []}
        without = analyze.detect_drift(changes, pages=self._pages())
        with_empty = analyze.detect_drift(changes, pages=self._pages(), rules=[])
        self.assertEqual(without, with_empty)

    def test_malformed_rules_do_not_raise(self):
        changes = {"labels": [{"old": "Save", "new": "Save changes"}], "routes": []}
        for bad in (None, 123, "not-a-list", True, [123], [None], ["  "], [["x"]], [{"unknown": 1}]):
            findings = analyze.detect_drift(changes, pages=self._pages(), rules=bad)
            self.assertEqual(len(findings), 1)

    def test_detect_drift_reports_suppressed_via_stats(self):
        changes = {"labels": [{"old": "Save", "new": "Save changes"}], "routes": []}
        report = {}
        findings = analyze.detect_drift(
            changes, pages=self._pages(), rules=[{"kind": "label", "value": "Save"}], stats=report
        )
        self.assertEqual(findings, [])
        self.assertEqual(report["suppressed"], 1)

    def test_suppressed_rules_breakdown_records_provenance(self):
        changes = {
            "labels": [{"old": "Save", "new": "Save changes"}],
            "routes": [{"old": "/settings", "new": "/account/settings"}],
        }
        report = {}
        rules = analyze._normalise_rules(
            [{"kind": "label", "value": "Save"}], origin="workspace"
        ) + analyze._normalise_rules(
            [{"kind": "route", "value": "/settings"}], origin="request"
        )
        findings = analyze.detect_drift(changes, pages=self._pages(), rules=rules, stats=report)
        self.assertEqual(findings, [])
        self.assertEqual(report["suppressed"], 2)
        breakdown = report["suppressed_rules"]
        self.assertEqual(len(breakdown), 2)
        by_origin = {entry["origin"]: entry for entry in breakdown}
        self.assertEqual(by_origin["workspace"]["kind"], "label")
        self.assertEqual(by_origin["workspace"]["value"], "Save")
        self.assertEqual(by_origin["workspace"]["count"], 1)
        self.assertEqual(by_origin["request"]["kind"], "route")
        self.assertEqual(by_origin["request"]["value"], "/settings")
        self.assertEqual(by_origin["request"]["count"], 1)
        for entry in breakdown:
            self.assertEqual(
                set(entry), {"id", "origin", "kind", "page", "value", "count"}
            )

    def test_suppressed_rules_aggregates_and_sorts(self):
        changes = {
            "labels": [
                {"old": "Save", "new": "Save changes"},
                {"old": "Projects", "new": "Workspaces"},
            ],
            "routes": [],
        }
        pages = [
            {"path": "help/a.md", "content": "Save here."},
            {"path": "help/b.md", "content": "Save there."},
            {"path": "help/c.md", "content": "Projects list."},
        ]
        report = {}
        rules = analyze._normalise_rules(
            [
                {"kind": "label", "value": "Save"},
                {"kind": "label", "value": "Projects"},
            ]
        )
        analyze.detect_drift(changes, pages=pages, rules=rules, stats=report)
        breakdown = report["suppressed_rules"]
        self.assertEqual([entry["count"] for entry in breakdown], [2, 1])
        self.assertEqual(breakdown[0]["value"], "Save")
        self.assertEqual(breakdown[0]["count"], 2)
        self.assertEqual(report["suppressed"], 3)

    def test_detect_drift_without_stats_does_not_raise(self):
        changes = {"labels": [{"old": "Save", "new": "Save changes"}], "routes": []}
        findings = analyze.detect_drift(
            changes, pages=self._pages(), rules=[{"kind": "label", "value": "Save"}]
        )
        self.assertEqual(findings, [])

    def test_summarise_reports_suppressed(self):
        self.assertEqual(analyze.summarise([])["suppressed"], 0)
        self.assertEqual(analyze.summarise([], suppressed=3)["suppressed"], 3)
        self.assertEqual(analyze.summarise([], suppressed="bad")["suppressed"], 0)
        self.assertEqual(analyze.summarise([])["suppressed_rules"], [])
        rules = [{"id": "x", "origin": "request", "kind": "label", "page": "", "value": "S", "count": 2}]
        self.assertEqual(analyze.summarise([], suppressed_rules=rules)["suppressed_rules"], rules)

    def test_summarise_ignores_non_list_suppressed_rules(self):
        self.assertEqual(analyze.summarise([], suppressed_rules="bad")["suppressed_rules"], [])

    def test_normalise_rules_accepts_store_shapes(self):
        rules = analyze._normalise_rules(
            [
                {"kind": "label", "value": "Save"},
                {"value": "Projects"},
                "Bare string",
            ]
        )
        self.assertEqual([r["value"] for r in rules], ["Save", "Projects", "Bare string"])
        self.assertEqual(rules[0]["kind"], "label")
        self.assertEqual(rules[1]["kind"], "*")
        self.assertEqual(
            {k: rules[2][k] for k in ("kind", "page", "value", "reason")},
            {"kind": "*", "page": "", "value": "Bare string", "reason": ""},
        )
        for rule in rules:
            self.assertTrue(rule["id"])
            self.assertEqual(rule["origin"], "request")

    def test_normalise_rules_tags_id_and_origin(self):
        rules = analyze._normalise_rules(
            [{"kind": "label", "page": "help/*.md", "value": "Save"}], origin="workspace"
        )
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["origin"], "workspace")
        self.assertEqual(rule["id"], analyze._rule_id("label", "help/*.md", "Save"))
        self.assertEqual(len(rule["id"]), 12)

    def test_rule_id_is_stable_and_content_sensitive(self):
        a = analyze._normalise_rules([{"kind": "label", "value": "Save"}])[0]["id"]
        b = analyze._normalise_rules([{"kind": "label", "value": "Save"}])[0]["id"]
        c = analyze._normalise_rules([{"kind": "label", "value": "Store"}])[0]["id"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_normalise_rules_preserves_existing_origin(self):
        rules = analyze._normalise_rules([{"kind": "label", "value": "Save", "origin": "workspace"}])
        self.assertEqual(rules[0]["origin"], "workspace")
        forced = analyze._normalise_rules(
            [{"kind": "label", "value": "Save", "origin": "workspace"}], origin="request"
        )
        self.assertEqual(forced[0]["origin"], "request")

    def test_normalise_rules_accepts_bare_dict(self):
        rules = analyze._normalise_rules({"kind": "route", "value": "/settings"})
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["kind"], "route")
        self.assertEqual(rules[0]["value"], "/settings")

    def test_normalise_rules_drops_malformed(self):
        for bad in (None, 123, "not-a-list", True, [123], [None], ["  "], [{"unknown": 1}]):
            self.assertEqual(analyze._normalise_rules(bad), [])


if __name__ == "__main__":
    unittest.main()
