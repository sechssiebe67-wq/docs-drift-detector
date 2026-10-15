import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import export  # noqa: E402


def _finding(fid, kind, page, old, new, score, severity, resolved=False):
    return {
        "id": fid,
        "kind": kind,
        "page": page,
        "old_value": old,
        "new_value": new,
        "score": score,
        "severity": severity,
        "resolved": resolved,
    }


class SummaryTest(unittest.TestCase):
    def test_empty_scan(self):
        summary = export.summarise_scan({"id": "x", "findings": [], "stats": {}})
        self.assertEqual(summary["id"], "x")
        self.assertNotIn("workspace", summary)
        self.assertEqual(summary["top_findings"], [])
        self.assertEqual(summary["kinds"], {"label": 0, "route": 0, "screenshot": 0})
        self.assertEqual(summary["severities"], {"high": 0, "medium": 0, "low": 0})
        self.assertEqual(summary["stats"]["findings"], 0)
        self.assertEqual(summary["stats"]["unresolved"], 0)
        self.assertEqual(summary["stats"]["suppressed"], 0)
        self.assertEqual(summary["stats"]["suppressed_rules"], [])

    def test_workspace_included_only_when_present(self):
        base = {"id": "x", "name": "n", "source": "s", "created_at": "t", "findings": [], "stats": {}}
        self.assertNotIn("workspace", export.summarise_scan(base))
        with_ws = dict(base, workspace="ws1")
        self.assertEqual(export.summarise_scan(with_ws)["workspace"], "ws1")

    def test_scan_identity_and_stats_passthrough(self):
        scan = {
            "id": "abc",
            "name": "Release 4.2",
            "source": "release-4.2",
            "created_at": "2026-01-01T00:00:00+00:00",
            "workspace": "ws1",
            "findings": [_finding("f1", "label", "a.md", "Save", "Save changes", 73.2, "high")],
            "stats": {
                "findings": 1,
                "unresolved": 1,
                "high": 1,
                "suppressed": 2,
                "suppressed_rules": [{"id": "r1"}],
            },
        }
        summary = export.summarise_scan(scan)
        self.assertEqual(
            set(summary),
            {
                "id",
                "name",
                "source",
                "created_at",
                "workspace",
                "stats",
                "top_findings",
                "kinds",
                "severities",
            },
        )
        self.assertEqual(summary["id"], "abc")
        self.assertEqual(summary["name"], "Release 4.2")
        self.assertEqual(summary["source"], "release-4.2")
        self.assertEqual(summary["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(summary["workspace"], "ws1")
        self.assertEqual(summary["stats"]["findings"], 1)
        self.assertEqual(summary["stats"]["unresolved"], 1)
        self.assertEqual(summary["stats"]["high"], 1)
        self.assertEqual(summary["stats"]["suppressed"], 2)
        self.assertEqual(summary["stats"]["suppressed_rules"], [{"id": "r1"}])

    def test_kinds_and_severities_counts(self):
        findings = [
            _finding("f1", "label", "a.md", "Save", "Save changes", 73.2, "high"),
            _finding("f2", "label", "b.md", "Go", "Continue", 45.0, "medium"),
            _finding("f3", "route", "c.md", "/old", "/new", 12.0, "low"),
            _finding("f4", "screenshot", "d.md", "img.png", "", 8.0, "low"),
        ]
        summary = export.summarise_scan({"id": "x", "findings": findings, "stats": {}})
        self.assertEqual(summary["kinds"], {"label": 2, "route": 1, "screenshot": 1})
        self.assertEqual(summary["severities"], {"high": 1, "medium": 1, "low": 2})
        self.assertEqual(sum(summary["kinds"].values()), len(findings))
        self.assertEqual(sum(summary["severities"].values()), len(findings))

    def test_top_findings_cap_and_ordering(self):
        findings = [
            _finding("f%d" % i, "label", "page.md", "o", "n", 10.0 * i, "low")
            for i in range(1, 9)
        ]
        summary = export.summarise_scan({"id": "x", "findings": findings, "stats": {}})
        self.assertEqual(len(summary["top_findings"]), 5)
        self.assertEqual(
            [f["id"] for f in summary["top_findings"]],
            ["f8", "f7", "f6", "f5", "f4"],
        )
        self.assertEqual(
            set(summary["top_findings"][0]),
            {"id", "kind", "page", "old_value", "new_value", "score", "severity"},
        )

    def test_top_findings_ties_ordered_by_page_then_id(self):
        findings = [
            _finding("b2", "label", "z.md", "o", "n", 50.0, "medium"),
            _finding("a1", "label", "a.md", "o", "n", 50.0, "medium"),
            _finding("c3", "label", "a.md", "o", "n", 50.0, "medium"),
        ]
        summary = export.summarise_scan({"id": "x", "findings": findings, "stats": {}})
        self.assertEqual(
            [f["id"] for f in summary["top_findings"]],
            ["a1", "c3", "b2"],
        )

    def test_top_findings_excludes_resolved(self):
        findings = [
            _finding("open1", "label", "a.md", "o", "n", 30.0, "low"),
            _finding("done1", "label", "b.md", "o", "n", 99.0, "high", resolved=True),
        ]
        summary = export.summarise_scan({"id": "x", "findings": findings, "stats": {}})
        self.assertEqual([f["id"] for f in summary["top_findings"]], ["open1"])
        self.assertEqual(summary["stats"]["unresolved"], 1)
        self.assertEqual(summary["stats"]["findings"], 2)

    def test_deterministic_output(self):
        findings = [
            _finding("f1", "label", "a.md", "o", "n", 10.0, "low"),
            _finding("f2", "route", "b.md", "o", "n", 20.0, "low"),
        ]
        scan = {"id": "x", "findings": findings, "stats": {}}
        import json

        first = json.dumps(export.summarise_scan(scan))
        second = json.dumps(export.summarise_scan(scan))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
