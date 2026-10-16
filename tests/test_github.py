import base64
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import github, pullrequest  # noqa: E402

TOKEN_ENV = "DDR_TEST_GITHUB_TOKEN"


def _finding(fid, severity="high", score=95.0, page="help/a.md"):
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


def _scan(**overrides):
    scan = {
        "id": "abc123",
        "name": "Release 4.2",
        "workspace": "ws1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "source": "release",
        "stats": {"findings": 1, "unresolved": 1, "high": 1},
        "findings": [_finding("a")],
    }
    scan.update(overrides)
    return scan


class _FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = json.dumps(body or {}).encode("utf-8")
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class _Recorder:
    """Opener that records requests and returns queued responses/errors."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BuildBranchNameTest(unittest.TestCase):
    def test_default_prefix(self):
        self.assertEqual(
            github.build_branch_name(_scan()), "docs-drift/abc123"
        )

    def test_custom_prefix(self):
        self.assertEqual(
            github.build_branch_name(_scan(), prefix="release"), "release/abc123"
        )

    def test_matches_pullrequest_suggestion(self):
        scan = _scan()
        suggestion = pullrequest.build_suggestion(scan)
        self.assertEqual(github.build_branch_name(scan), suggestion["branch"])


class BuildCommitBodyTest(unittest.TestCase):
    def test_reports_checklist_under_docs_drift(self):
        suggestion = pullrequest.build_suggestion(_scan())
        commit = github.build_commit_body(suggestion)
        self.assertEqual(len(commit["files"]), 1)
        self.assertEqual(commit["files"][0]["path"], "docs-drift/abc123.md")
        self.assertEqual(commit["files"][0]["content"], suggestion["checklist"])
        self.assertIn("docs: address drift from Release 4.2 (abc123)", commit["message"])
        self.assertIn("Affected pages:", commit["message"])
        self.assertIn("- help/a.md", commit["message"])

    def test_affected_pages_are_sorted_and_deduped(self):
        scan = _scan(
            findings=[
                _finding("a", page="help/b.md"),
                _finding("b", page="help/a.md"),
                _finding("c", page="help/b.md"),
            ]
        )
        commit = github.build_commit_body(pullrequest.build_suggestion(scan))
        self.assertLess(commit["message"].index("- help/a.md"), commit["message"].index("- help/b.md"))
        self.assertEqual(commit["message"].count("- help/b.md"), 1)

    def test_tolerates_non_dict(self):
        commit = github.build_commit_body(None)
        self.assertEqual(commit["files"][0]["path"], "docs-drift/scan.md")
        self.assertIn("docs: address drift from scan", commit["message"])


class OpenPullRequestTest(unittest.TestCase):
    def setUp(self):
        self._original = os.environ.get(TOKEN_ENV)
        os.environ[TOKEN_ENV] = "secret-token"

    def tearDown(self):
        if self._original is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = self._original

    def _config(self, **overrides):
        config = {
            "repo": "owner/name",
            "base_branch": "main",
            "token_env": TOKEN_ENV,
        }
        config.update(overrides)
        return config

    def test_success_sends_expected_request_sequence(self):
        scan = _scan()
        suggestion = pullrequest.build_suggestion(scan)
        recorder = _Recorder(
            [
                _FakeResponse(200, {"object": {"sha": "base-sha"}}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {"html_url": "https://github.com/owner/name/pull/7"}),
            ]
        )
        result = github.open_pull_request(self._config(), suggestion, opener=recorder)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["pr_url"], "https://github.com/owner/name/pull/7")
        self.assertEqual(result["branch"], "docs-drift/abc123")

        base, ref, contents, pull = recorder.requests
        self.assertEqual(base.method, "GET")
        self.assertEqual(
            base.full_url,
            "https://api.github.com/repos/owner/name/git/ref/heads/main",
        )
        self.assertEqual(ref.method, "POST")
        self.assertEqual(
            ref.full_url, "https://api.github.com/repos/owner/name/git/refs"
        )
        self.assertEqual(
            json.loads(ref.data.decode("utf-8")),
            {"ref": "refs/heads/docs-drift/abc123", "sha": "base-sha"},
        )
        self.assertEqual(contents.method, "PUT")
        self.assertEqual(
            contents.full_url,
            "https://api.github.com/repos/owner/name/contents/docs-drift/abc123.md",
        )
        body = json.loads(contents.data.decode("utf-8"))
        self.assertEqual(body["branch"], "docs-drift/abc123")
        self.assertEqual(
            base64.b64decode(body["content"]).decode("utf-8"),
            suggestion["checklist"],
        )
        self.assertEqual(pull.method, "POST")
        self.assertEqual(
            pull.full_url, "https://api.github.com/repos/owner/name/pulls"
        )
        pull_body = json.loads(pull.data.decode("utf-8"))
        self.assertEqual(pull_body["head"], "docs-drift/abc123")
        self.assertEqual(pull_body["base"], "main")
        self.assertEqual(pull_body["title"], suggestion["title"])
        self.assertEqual(pull_body["body"], suggestion["body"])

        for request in recorder.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
            self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
            self.assertEqual(request.get_header("User-agent"), "docs-drift-detector")
        for request in (ref, contents, pull):
            self.assertIn("application/json", request.get_header("Content-type"))

    def test_config_base_branch_overrides_suggestion(self):
        scan = _scan()
        suggestion = pullrequest.build_suggestion(scan, base_branch="develop")
        recorder = _Recorder(
            [
                _FakeResponse(200, {"object": {"sha": "s"}}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {"html_url": "u"}),
            ]
        )
        github.open_pull_request(self._config(base_branch="trunk"), suggestion, opener=recorder)
        self.assertEqual(
            recorder.requests[0].full_url,
            "https://api.github.com/repos/owner/name/git/ref/heads/trunk",
        )

    def test_missing_repo_skips_network(self):
        recorder = _Recorder([])
        result = github.open_pull_request(
            self._config(repo=""), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no repository configured")
        self.assertEqual(recorder.requests, [])

    def test_missing_branch_skips_network(self):
        recorder = _Recorder([])
        result = github.open_pull_request(
            self._config(), {"scan_id": "x"}, opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no branch in suggestion")
        self.assertEqual(recorder.requests, [])

    def test_unset_token_skips_network(self):
        os.environ.pop(TOKEN_ENV, None)
        recorder = _Recorder([])
        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertIn(TOKEN_ENV, result["error"])
        self.assertEqual(recorder.requests, [])

    def test_http_error_returns_status_without_raising(self):
        def boom(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 422, "exists", {}, None)

        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=boom
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 422)
        self.assertEqual(result["error"], "HTTP 422")

    def test_transport_error_returns_error_without_raising(self):
        recorder = _Recorder([urllib.error.URLError("connection refused")])
        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("connection refused", result["error"])

    def test_non_2xx_status_uses_body_message(self):
        recorder = _Recorder([_FakeResponse(404, {"message": "base branch not found"})])
        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 404)
        self.assertEqual(result["error"], "base branch not found")

    def test_missing_base_sha_fails(self):
        recorder = _Recorder([_FakeResponse(200, {"object": {}})])
        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        self.assertFalse(result["ok"])
        self.assertIn("base branch", result["error"])

    def test_result_is_json_serialisable(self):
        recorder = _Recorder(
            [
                _FakeResponse(200, {"object": {"sha": "s"}}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {}),
                _FakeResponse(201, {"html_url": "u"}),
            ]
        )
        result = github.open_pull_request(
            self._config(), pullrequest.build_suggestion(_scan()), opener=recorder
        )
        encoded = json.dumps(result)
        self.assertIn('"ok": true', encoded)
        self.assertIn('"branch": "docs-drift/abc123"', encoded)


if __name__ == "__main__":
    unittest.main()
