"""Reusable scan preparation and storage.

The HTTP layer and the background scheduler both need to turn a scan request
body into findings and a stored scan. The logic lives here (rather than on the
request handler) so a scheduled scan follows exactly the same analysis, rule
merge and notification path as a manual ``POST /api/scans``.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from . import analyze, notify

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
    if dispatch:
        notify_webhook(store, scan)
    return scan
