import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector.store import Store  # noqa: E402


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
