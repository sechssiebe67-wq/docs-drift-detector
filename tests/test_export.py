import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import export  # noqa: E402


class ExportTest(unittest.TestCase):
    def _scan(self, resolved=False):
        return {
            "id": "abc123",
            "name": "Release 4.2",
            "source": "release-4.2",
            "created_at": "2026-01-01T00:00:00+00:00",
            "stats": {"findings": 1, "unresolved": 0 if resolved else 1, "high": 1},
            "findings": [
                {
                    "id": "f1",
                    "kind": "label",
                    "page": "help/getting-started.md",
                    "title": 'UI label "Save" changed',
                    "detail": 'Update it to "Save changes".',
                    "snippet": "Click Save to store your work.",
                    "severity": "high",
                    "score": 73.2,
                    "resolved": resolved,
                }
            ],
        }

    def test_markdown_contains_checkbox(self):
        markdown = export.to_markdown(self._scan())
        self.assertIn("# Docs drift report: Release 4.2", markdown)
        self.assertIn("- [ ] **[HIGH] UI label \"Save\" changed**", markdown)
        self.assertIn("help/getting-started.md", markdown)
        self.assertIn("Context:", markdown)

    def test_markdown_resolved_checkbox(self):
        self.assertIn("- [x]", export.to_markdown(self._scan(resolved=True)))

    def test_empty_scan(self):
        markdown = export.to_markdown({"id": "x", "findings": [], "stats": {}})
        self.assertIn("No documentation drift detected", markdown)


if __name__ == "__main__":
    unittest.main()
