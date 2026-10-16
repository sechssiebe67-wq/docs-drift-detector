import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import export, notify, pullrequest  # noqa: E402


def _finding(fid, severity, score, page="help/a.md", title="UI label drift"):
    return {
        "id": fid,
        "kind": "label",
        "page": page,
        "title": title,
        "old_value": "Save",
        "new_value": "Save changes",
        "score": score,
        "severity": severity,
    }


def _scan(**overrides):
    scan = {
        "id": "abc123",
        "name": "Release 4.2",
        "workspace": "ws1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "source": "release",
        "stats": {"findings": 0, "unresolved": 0, "high": 0},
        "findings": [],
    }
    scan.update(overrides)
    return scan


class BuildSuggestionTest(unittest.TestCase):
    def test_document_shape(self):
        scan = _scan(findings=[_finding("a", "high", 95.0)])
        document = pullrequest.build_suggestion(scan)
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

    def test_branch_defaults_and_prefix(self):
        scan = _scan(findings=[_finding("a", "high", 95.0)])
        document = pullrequest.build_suggestion(scan)
        self.assertEqual(document["branch"], "docs-drift/abc123")
        self.assertEqual(document["base_branch"], "main")

        custom = pullrequest.build_suggestion(scan, base_branch="develop", branch_prefix="release")
        self.assertEqual(custom["branch"], "release/abc123")
        self.assertEqual(custom["base_branch"], "develop")

    def test_title_uses_scan_name(self):
        scan = _scan(name="Release 4.2", findings=[_finding("a", "high", 95.0)])
        document = pullrequest.build_suggestion(scan)
        self.assertEqual(document["title"], "docs: address drift from Release 4.2")

    def test_files_are_deduped_and_sorted(self):
        scan = _scan(
            findings=[
                _finding("a", "high", 90.0, page="help/b.md"),
                _finding("b", "high", 80.0, page="help/a.md"),
                _finding("c", "high", 70.0, page="help/b.md"),
                _finding("d", "medium", 55.0, page="help/c.md"),
            ]
        )
        document = pullrequest.build_suggestion(scan)
        self.assertEqual([f["path"] for f in document["files"]], ["help/a.md", "help/b.md"])
        for entry in document["files"]:
            self.assertEqual(entry["action"], "update")
            self.assertTrue(entry["reason"])

    def test_files_only_include_high_severity_pages(self):
        scan = _scan(
            findings=[
                _finding("a", "high", 90.0, page="help/a.md"),
                _finding("b", "medium", 55.0, page="help/medium.md"),
                _finding("c", "low", 10.0, page="help/low.md"),
            ]
        )
        document = pullrequest.build_suggestion(scan)
        self.assertEqual([f["path"] for f in document["files"]], ["help/a.md"])

    def test_empty_high_severity(self):
        scan = _scan(
            name="Quiet release",
            findings=[_finding("a", "medium", 55.0), _finding("b", "low", 10.0)],
        )
        document = pullrequest.build_suggestion(scan)
        self.assertEqual(document["files"], [])
        self.assertIn("no action is required", document["body"].lower())
        self.assertEqual(document["branch"], "docs-drift/abc123")

    def test_scan_without_findings_key(self):
        document = pullrequest.build_suggestion({"id": "s4"})
        self.assertEqual(document["files"], [])
        self.assertEqual(document["scan_id"], "s4")
        self.assertEqual(document["branch"], "docs-drift/s4")

    def test_reuses_notify_build_payload_selection(self):
        scan = _scan(
            findings=[
                _finding("a", "high", 90.0, page="help/b.md"),
                _finding("b", "medium", 55.0),
                _finding("c", "high", 80.0, page="help/a.md"),
            ]
        )
        payload = notify.build_payload(scan)
        expected_pages = sorted(
            {f["page"] for f in payload["findings"] if f.get("page")}
        )
        document = pullrequest.build_suggestion(scan)
        self.assertEqual([f["path"] for f in document["files"]], expected_pages)

    def test_body_lists_findings_by_descending_score(self):
        scan = _scan(
            findings=[
                _finding("a", "high", 71.0, page="help/b.md"),
                _finding("b", "high", 95.0, page="help/c.md"),
            ]
        )
        document = pullrequest.build_suggestion(scan)
        self.assertLess(
            document["body"].index("help/c.md"), document["body"].index("help/b.md")
        )

    def test_checklist_reuses_export_markdown(self):
        scan = _scan(findings=[_finding("a", "high", 95.0)])
        document = pullrequest.build_suggestion(scan)
        self.assertEqual(document["checklist"], export.to_markdown(scan))

    def test_document_is_json_serialisable(self):
        scan = _scan(findings=[_finding("a", "high", 95.0)])
        encoded = json.dumps(pullrequest.build_suggestion(scan))
        self.assertIn('"files"', encoded)
        self.assertIn("docs-drift/abc123", encoded)


if __name__ == "__main__":
    unittest.main()
