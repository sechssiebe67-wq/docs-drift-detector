"""GitHub connector that turns a PR suggestion into a real branch and pull request.

The connector is dependency-free: it talks to the GitHub REST API through
:mod:`urllib.request` only. It reuses
:func:`docs_drift_detector.pullrequest.build_suggestion` (and therefore
:func:`docs_drift_detector.notify.build_payload` and
:func:`docs_drift_detector.export.to_markdown`) for the branch name, title, body,
affected pages and checklist, so a PR, a webhook and a digest always describe the
same high-severity scope.

Everything is best-effort and never raises into the caller: a problem with the
token, the repository or the GitHub API is returned as
``{"ok": False, "error": ...}`` so a scan is never mutated. The connector never
force-pushes; if the branch already exists the call fails instead of moving it.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from . import pullrequest

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 10
DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"
USER_AGENT = "docs-drift-detector"
REPORT_DIR = "docs-drift"


class _GitHubError(Exception):
    """Internal transport/API failure carrying the upstream HTTP status."""

    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _quote(value: Any) -> str:
    """Percent-encode a path/ref while preserving ``/`` separators."""
    return urllib.parse.quote(str(value), safe="/")


def _opener(request: urllib.request.Request, timeout: float):
    """Indirection around :func:`urllib.request.urlopen` for easy testing."""
    return urllib.request.urlopen(request, timeout=timeout)


def _status_of(response: Any) -> Optional[int]:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            try:
                status = getcode()
            except Exception:  # pragma: no cover - defensive
                status = None
    return status if isinstance(status, int) else None


def build_branch_name(
    scan: Dict[str, Any], prefix: str = pullrequest.DEFAULT_BRANCH_PREFIX
) -> str:
    """Return the deterministic branch name for ``scan``.

    Delegates to :func:`pullrequest.build_suggestion` so the connector and the
    ``GET /api/scans/{id}/pull-request`` document never disagree about the
    branch name (``<prefix>/<scan_id>``).
    """
    return pullrequest.build_suggestion(scan, branch_prefix=prefix)["branch"]


def build_commit_body(suggestion: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic commit from a PR ``suggestion``.

    The connector cannot invent the corrected documentation prose, so it commits
    the exported drift ``checklist`` as a single report file under
    ``docs-drift/<scan_id>.md``. The commit message names the scan and lists the
    affected pages from the suggestion's ``files`` list, so the pull-request diff
    is a reviewable task list rather than a destructive edit of the docs.

    Returns ``{"message": str, "files": [{"path": str, "content": str}]}``.
    """
    suggestion = suggestion if isinstance(suggestion, dict) else {}
    scan_id = str(suggestion.get("scan_id") or "").strip()
    name = str(suggestion.get("name") or scan_id or "scan").strip()
    checklist = str(suggestion.get("checklist") or "")

    paths = []
    for entry in suggestion.get("files") or []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    paths.sort()

    lines = ["docs: address drift from %s" % name]
    if scan_id:
        lines[0] += " (%s)" % scan_id
    if paths:
        lines.append("")
        lines.append("Affected pages:")
        for path in paths:
            lines.append("- %s" % path)

    report_path = "%s/%s.md" % (REPORT_DIR, scan_id or "scan")
    return {
        "message": "\n".join(lines),
        "files": [{"path": report_path, "content": checklist}],
    }


def _request(
    opener,
    method: str,
    url: str,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
):
    """Perform one GitHub API call and return ``(status, body)``.

    Raises :class:`_GitHubError` for transport errors and non-2xx responses.
    """
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", "Bearer %s" % token)
    request.add_header("User-Agent", USER_AGENT)
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        response = opener(request, timeout)
    except urllib.error.HTTPError as err:
        raise _GitHubError(err.code, "HTTP %s" % err.code)
    except Exception as err:  # urllib.error.URLError and anything else
        raise _GitHubError(None, str(err) or err.__class__.__name__)

    status = _status_of(response)
    try:
        raw = response.read()
    except Exception:  # pragma: no cover - defensive
        raw = b""
    try:
        response.close()
    except Exception:  # pragma: no cover - defensive
        pass

    body: Dict[str, Any] = {}
    if raw:
        try:
            decoded = json.loads(raw.decode("utf-8"))
            if isinstance(decoded, dict):
                body = decoded
        except (ValueError, UnicodeDecodeError):
            body = {}

    if status is not None and not (200 <= status < 300):
        message = str(body.get("message") or "") or ("HTTP %s" % status)
        raise _GitHubError(status, message)
    return status, body


def _get_base_sha(opener, repo: str, base_branch: str, token: str) -> str:
    url = "%s/repos/%s/git/ref/heads/%s" % (
        GITHUB_API,
        _quote(repo),
        _quote(base_branch),
    )
    _, body = _request(opener, "GET", url, token)
    obj = body.get("object")
    sha = ""
    if isinstance(obj, dict):
        sha = str(obj.get("sha") or "")
    if not sha:
        raise _GitHubError(None, "could not resolve base branch %s" % base_branch)
    return sha


def _create_ref(opener, repo: str, branch: str, sha: str, token: str) -> None:
    url = "%s/repos/%s/git/refs" % (GITHUB_API, _quote(repo))
    _request(
        opener,
        "POST",
        url,
        token,
        {"ref": "refs/heads/%s" % branch, "sha": sha},
    )


def _put_contents(
    opener, repo: str, path: str, message: str, content: str, branch: str, token: str
) -> None:
    url = "%s/repos/%s/contents/%s" % (GITHUB_API, _quote(repo), _quote(path))
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    _request(
        opener,
        "PUT",
        url,
        token,
        {"message": message, "content": encoded, "branch": branch},
    )


def _open_pr(
    opener,
    repo: str,
    branch: str,
    base_branch: str,
    suggestion: Dict[str, Any],
    token: str,
):
    url = "%s/repos/%s/pulls" % (GITHUB_API, _quote(repo))
    payload = {
        "title": str(suggestion.get("title") or "docs: address drift"),
        "head": branch,
        "base": base_branch,
        "body": str(suggestion.get("body") or ""),
    }
    return _request(opener, "POST", url, token, payload)


def open_pull_request(
    config: Dict[str, Any],
    suggestion: Dict[str, Any],
    opener=None,
) -> Dict[str, Any]:
    """Open a branch and pull request for ``suggestion`` in ``config``'s repo.

    ``config`` carries ``repo`` (``owner/name``), ``base_branch`` and
    ``token_env``; the token itself is read from that environment variable and is
    never stored. ``opener`` defaults to :func:`_opener` and can be replaced in
    tests.

    Returns ``{"ok", "status", "error", "pr_url", "branch"}`` and never raises.
    The call sequence is: resolve the base branch SHA, create the new ref, put
    each commit file, then open the pull request.
    """
    opener = opener or _opener
    config = config if isinstance(config, dict) else {}
    suggestion = suggestion if isinstance(suggestion, dict) else {}

    branch = str(suggestion.get("branch") or "")
    repo = str(config.get("repo") or "")
    base_branch = str(
        config.get("base_branch")
        or suggestion.get("base_branch")
        or pullrequest.DEFAULT_BASE_BRANCH
    )
    token_env = str(config.get("token_env") or DEFAULT_TOKEN_ENV)
    token = os.environ.get(token_env, "") if token_env else ""

    result: Dict[str, Any] = {
        "ok": False,
        "status": None,
        "error": "",
        "pr_url": "",
        "branch": branch,
    }

    if not repo:
        result["error"] = "no repository configured"
        return result
    if not branch:
        result["error"] = "no branch in suggestion"
        return result
    if not token:
        result["error"] = "token env %s is not set" % token_env
        return result

    try:
        base_sha = _get_base_sha(opener, repo, base_branch, token)
        _create_ref(opener, repo, branch, base_sha, token)
        commit = build_commit_body(suggestion)
        for entry in commit["files"]:
            _put_contents(
                opener,
                repo,
                entry["path"],
                commit["message"],
                entry["content"],
                branch,
                token,
            )
        status, pr = _open_pr(opener, repo, branch, base_branch, suggestion, token)
    except _GitHubError as err:
        result["status"] = err.status
        result["error"] = err.message
        return result
    except Exception as err:  # never raise into the caller
        result["error"] = str(err) or err.__class__.__name__
        return result

    result["ok"] = True
    result["status"] = status
    result["pr_url"] = str(pr.get("html_url") or "")
    return result
