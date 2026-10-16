import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import github, runner  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402


def _finding(fid="a", severity="high", score=95.0, page="help/a.md"):
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


def _scan(workspace="ws1", **overrides):
    scan = {
        "id": "abc123",
        "name": "Release 4.2",
        "workspace": workspace,
        "created_at": "2026-01-01T00:00:00+00:00",
        "findings": [_finding()],
    }
    scan.update(overrides)
    return scan


class OpenGithubPrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))
        self.original = github.open_pull_request

    def tearDown(self):
        github.open_pull_request = self.original
        self.tmp.cleanup()

    def test_no_workspace_is_noop(self):
        called = []
        github.open_pull_request = lambda *a, **k: called.append(1)
        runner.open_github_pr(self.store, {"id": "x"})
        self.assertEqual(called, [])

    def test_missing_repo_is_noop(self):
        called = []
        github.open_pull_request = lambda *a, **k: called.append(1)
        runner.open_github_pr(self.store, _scan(workspace="wsNoConfig"))
        self.assertEqual(called, [])

    def test_success_calls_connector_without_stderr(self):
        self.store.set_workspace_github("ws1", {"repo": "owner/name"})
        captured = {}

        def fake(config, suggestion, opener=None):
            captured["config"] = config
            captured["suggestion"] = suggestion
            return {"ok": True, "status": 201, "error": "", "pr_url": "u", "branch": suggestion["branch"]}

        github.open_pull_request = fake
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            runner.open_github_pr(self.store, _scan())
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(captured["config"]["repo"], "owner/name")
        self.assertEqual(captured["suggestion"]["scan_id"], "abc123")

    def test_failure_is_logged_to_stderr(self):
        self.store.set_workspace_github("ws1", {"repo": "owner/name"})
        github.open_pull_request = lambda config, suggestion, opener=None: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
            "pr_url": "",
            "branch": suggestion["branch"],
        }
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            runner.open_github_pr(self.store, _scan())
        output = stderr.getvalue()
        self.assertIn("pull request failed", output)
        self.assertIn("owner/name", output)
        self.assertIn("HTTP 500", output)

    def test_exception_is_logged_to_stderr(self):
        self.store.set_workspace_github("ws1", {"repo": "owner/name"})

        def boom(config, suggestion, opener=None):
            raise RuntimeError("kaboom")

        github.open_pull_request = boom
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            runner.open_github_pr(self.store, _scan())
        self.assertIn("kaboom", stderr.getvalue())

    def test_run_scan_dispatches_github_pr(self):
        calls = []
        original_pr = runner.open_github_pr

        def fake_pr(store, scan):
            calls.append(scan["id"])

        runner.open_github_pr = fake_pr
        try:
            scan = runner.run_scan(
                self.store,
                {
                    "name": "Auto",
                    "workspace": "ws1",
                    "changelog": 'renamed "Save" to "Save changes"',
                    "pages": [{"path": "help/a.md", "content": "Click Save."}],
                },
                "ws1",
            )
        finally:
            runner.open_github_pr = original_pr
        self.assertEqual(calls, [scan["id"]])

    def test_run_scan_skips_dispatch_when_disabled(self):
        calls = []
        original_pr = runner.open_github_pr
        runner.open_github_pr = lambda store, scan: calls.append(scan["id"])
        try:
            runner.run_scan(
                self.store,
                {
                    "name": "No dispatch",
                    "workspace": "ws1",
                    "changelog": 'renamed "Save" to "Save changes"',
                    "pages": [{"path": "help/a.md", "content": "Click Save."}],
                },
                "ws1",
                dispatch=False,
            )
        finally:
            runner.open_github_pr = original_pr
        self.assertEqual(calls, [])


class RetentionPruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def _body(self, name="Scan"):
        return {
            "name": name,
            "workspace": "ws1",
            "changelog": 'renamed "Save" to "Save changes"',
            "pages": [{"path": "help/a.md", "content": "Click Save."}],
        }

    def test_no_workspace_is_noop(self):
        runner.prune_workspace_scans(self.store, {"id": "x"})

    def test_unset_limit_is_noop(self):
        self.store.create_scan("one", "release", [_finding()], workspace="ws1")
        runner.prune_workspace_scans(self.store, _scan())
        self.assertEqual(len(self.store.list_scans()), 1)

    def test_positive_limit_prunes(self):
        self.store.set_workspace_retention("ws1", 2)
        for name in ("s1", "s2", "s3"):
            self.store.create_scan(name, "release", [_finding()], workspace="ws1")
        runner.prune_workspace_scans(self.store, _scan())
        self.assertEqual(len(self.store.list_scans()), 2)

    def test_failure_is_logged_and_does_not_raise(self):
        def boom(workspace):
            raise RuntimeError("kaboom")

        self.store.get_workspace_retention = boom
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            runner.prune_workspace_scans(self.store, _scan())
        self.assertIn("retention prune error", stderr.getvalue())
        self.assertIn("kaboom", stderr.getvalue())

    def test_run_scan_applies_retention(self):
        self.store.set_workspace_retention("ws1", 1)
        runner.run_scan(self.store, self._body("first"), "ws1", dispatch=False)
        runner.run_scan(self.store, self._body("second"), "ws1", dispatch=False)
        scans = self.store.list_scans()
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["name"], "second")

    def test_run_scan_survives_prune_failure(self):
        self.store.set_workspace_retention("ws1", 1)

        def boom(workspace):
            raise RuntimeError("kaboom")

        self.store.get_workspace_retention = boom
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            scan = runner.run_scan(self.store, self._body("first"), "ws1", dispatch=False)
        self.assertEqual(scan["name"], "first")
        self.assertIn("retention prune error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
