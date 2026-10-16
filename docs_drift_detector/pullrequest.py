"""Pull-request suggestion documents derived from a stored scan.

This module is dependency-free. It reuses
:func:`docs_drift_detector.notify.build_payload` for the high-severity finding
selection so a PR suggestion and an outbound webhook always describe the same
scope, and :func:`docs_drift_detector.export.to_markdown` for the checklist so
the Markdown formatting lives in exactly one place.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import export, notify

DEFAULT_BASE_BRANCH = "main"
DEFAULT_BRANCH_PREFIX = "docs-drift"


def _reason(entries: List[Dict[str, Any]]) -> str:
    """Summarise the high-severity findings that touch a single page."""
    titles: List[str] = []
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        if not title:
            title = "High-severity %s drift" % (entry.get("kind") or "change")
        if title not in titles:
            titles.append(title)
    return "; ".join(titles)


def build_suggestion(
    scan: Dict[str, Any],
    base_branch: str = DEFAULT_BASE_BRANCH,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> Dict[str, Any]:
    """Build a stable, JSON-serialisable PR suggestion for ``scan``.

    The returned document contains ``scan_id``, ``name``, ``workspace``,
    ``created_at``, ``branch``, ``base_branch``, ``title``, ``body``, ``files``
    and ``checklist``. ``files`` is the deduplicated, path-sorted list of pages
    touched by the scan's high-severity findings. A scan without high-severity
    findings yields an empty ``files`` list and a body that says no action is
    required.
    """
    scan = scan if isinstance(scan, dict) else {}
    payload = notify.build_payload(scan)
    scan_id = payload.get("scan_id") or scan.get("id") or ""
    name = payload.get("name") or ""
    findings = payload.get("findings") or []

    by_page: Dict[str, List[Dict[str, Any]]] = {}
    for finding in findings:
        page = str(finding.get("page") or "")
        if not page:
            continue
        by_page.setdefault(page, []).append(finding)

    files = [
        {"path": page, "action": "update", "reason": _reason(by_page[page])}
        for page in sorted(by_page)
    ]

    checklist = export.to_markdown(scan)
    if findings:
        body_lines = [
            "## Docs drift update: %s" % (name or scan_id),
            "",
            "%d high-severity documentation finding(s) from scan `%s` require an update."
            % (len(findings), scan_id),
            "",
        ]
        for finding in findings:
            body_lines.append(
                "- `%s` — %s → %s (%s)"
                % (
                    finding.get("page") or "(no page)",
                    finding.get("old_value") or "(none)",
                    finding.get("new_value") or "(none)",
                    finding.get("kind") or "change",
                )
            )
    else:
        body_lines = [
            "## Docs drift update: %s" % (name or scan_id),
            "",
            "No high-severity drift was found for scan `%s`; no action is required."
            % scan_id,
        ]

    title = "docs: address drift from %s" % (name or scan_id)
    return {
        "scan_id": scan_id,
        "name": name,
        "workspace": payload.get("workspace", ""),
        "created_at": payload.get("created_at", ""),
        "branch": "%s/%s" % (branch_prefix, scan_id),
        "base_branch": base_branch,
        "title": title,
        "body": "\n".join(body_lines),
        "files": files,
        "checklist": checklist,
    }
