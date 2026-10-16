"""Reusable scan preparation and storage.

The HTTP layer and the background scheduler both need to turn a scan request
body into findings and a stored scan. The logic lives here (rather than on the
request handler) so a scheduled scan follows exactly the same analysis, rule
merge and notification path as a manual ``POST /api/scans``.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from . import analyze, github, notify, pullrequest

MAX_PAGES = 2000


def validate_pages(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    pages = []
    for item in raw[:MAX_PAGES]:
        if isinstance(item, dict):
            path = str(item.get("path") or item.get("url") or "(unnamed)")
            content = item.get("content")
            if content is None:
                content = item.get("text") or ""
            pages.append({"path": path, "content": str(content)})
        elif isinstance(item, str):
            pages.append({"path": "(unnamed)", "content": item})
    return pages


def merge_rules(
    store: Any, request_rules: Any, workspace: Optional[str]
) -> List[Dict[str, str]]:
    """Combine per-scan rules with the stored workspace rule library.

    Per-scan rules are normalised first and tagged ``origin=request``; stored
    workspace rules are tagged ``origin=workspace``. Both sets are applied so
    their suppressions are additive and provenance is preserved.
    """
    requested = analyze._normalise_rules(request_rules, origin="request")
    stored = analyze._normalise_rules(
        store.list_workspace_rules(workspace) if workspace else [],
        origin="workspace",
    )
    return requested + stored


def prepare_scan(
    store: Any, data: Dict[str, Any], workspace: Optional[str] = None
) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, Any]], Dict[str, Any]]:
    pages = validate_pages(data.get("pages") or data.get("docs") or [])
    changelog = data.get("changelog") or ""
    diff = data.get("diff") or ""
    text = changelog + "\n" + diff
    changes = analyze.extract_changes(
        text, labels=data.get("labels"), routes=data.get("routes")
    )
    screenshots = data.get("screenshots") or []
    metrics = data.get("metrics") or {}
    report: Dict[str, Any] = {}
    findings = analyze.detect_drift(
        changes,
        pages=pages,
        screenshots=screenshots,
        metrics=metrics,
        rules=merge_rules(store, data.get("rules"), workspace),
        stats=report,
    )
    suppressed = report.get("suppressed", 0)
    suppressed_rules = report.get("suppressed_rules", [])
    suppressed_details = report.get("suppressed_details", [])
    return changes, findings, {
        "pages": pages,
        "metrics": metrics,
        "suppressed": suppressed,
        "suppressed_rules": suppressed_rules,
        "suppressed_details": suppressed_details,
    }


def notify_webhook(store: Any, scan: Dict[str, Any]) -> None:
    """Best-effort outbound webhook dispatch for a stored scan.

    Delivery never raises and never changes the scan response; failures are
    written to stderr only.
    """
    workspace = scan.get("workspace")
    if not workspace:
        return
    try:
        config = store.get_workspace_webhook(workspace)
        url = config.get("url") or ""
        if not url:
            return
        result = notify.dispatch(url, notify.build_payload(scan))
        if not result.get("ok"):
            sys.stderr.write(
                "docs-drift: webhook delivery failed (workspace=%s, url=%s): %s\n"
                % (workspace, url, result.get("error") or result.get("status"))
            )
    except Exception as err:  # never let notification break the response
        sys.stderr.write("docs-drift: webhook dispatch error: %s\n" % err)


def open_github_pr(store: Any, scan: Dict[str, Any]) -> None:
    """Best-effort automatic pull request for a stored scan.

    The scan's workspace must have a stored GitHub config with a ``repo``; the
    configured token is read from the environment by
    :func:`docs_drift_detector.github.open_pull_request`. Opening never raises
    and never changes the scan response; a missing config is a no-op and any
    other failure is written to stderr only.
    """
    workspace = scan.get("workspace")
    if not workspace:
        return
    try:
        config = store.get_workspace_github(workspace)
        repo = str(config.get("repo") or "")
        if not repo:
            return
        base_branch = str(
            config.get("base_branch") or pullrequest.DEFAULT_BASE_BRANCH
        )
        suggestion = pullrequest.build_suggestion(scan, base_branch=base_branch)
        result = github.open_pull_request(config, suggestion)
        if not result.get("ok"):
            sys.stderr.write(
                "docs-drift: pull request failed (workspace=%s, repo=%s): %s\n"
                % (workspace, repo, result.get("error") or result.get("status"))
            )
    except Exception as err:  # never let the connector break the response
        sys.stderr.write("docs-drift: pull request error: %s\n" % err)


def prune_workspace_scans(store: Any, scan: Dict[str, Any]) -> None:
    """Best-effort retention pruning for a stored scan's workspace.

    Looks up the workspace's retention limit and prunes the oldest scans beyond
    it. Pruning never raises into the scan path and never changes the scan
    response; failures are written to stderr only.
    """
    workspace = scan.get("workspace")
    if not workspace:
        return
    try:
        limit = store.get_workspace_retention(workspace)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return
        store.prune_workspace_scans(workspace, limit)
    except Exception as err:  # never let pruning break the response
        sys.stderr.write(
            "docs-drift: retention prune error (workspace=%s): %s\n" % (workspace, err)
        )


def run_scan(
    store: Any,
    data: Dict[str, Any],
    workspace: Optional[str] = None,
    dispatch: bool = True,
) -> Dict[str, Any]:
    """Analyse ``data``, store the scan and (optionally) notify the webhook."""
    changes, findings, extra = prepare_scan(store, data, workspace)
    scan = store.create_scan(
        name=data.get("name") or "Release scan",
        source=data.get("source") or "release",
        findings=findings,
        pages=extra["pages"],
        suppressed=extra["suppressed"],
        workspace=workspace,
        suppressed_rules=extra["suppressed_rules"],
        suppressed_details=extra["suppressed_details"],
    )
    prune_workspace_scans(store, scan)
    if dispatch:
        notify_webhook(store, scan)
        open_github_pr(store, scan)
    return scan
