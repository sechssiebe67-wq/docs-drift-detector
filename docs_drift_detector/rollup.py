"""Cross-workspace drift roll-up built from the most recent stored scans.

Where :mod:`docs_drift_detector.trend` answers "is *this* workspace getting
better or worse?", the roll-up answers "which workspaces are accumulating open
high-severity findings?" by aggregating the recent stored scans of *all*
workspaces (plus scans with no workspace, grouped under the empty-string
bucket) into one chronological window.

The module is intentionally read-only and dependency-free: it only reads the
list of stored scan dicts handed to it and never mutates, notifies, opens a
pull request or records a digest. Per-scan counts are produced by
:func:`docs_drift_detector.trend.summarise_scan` so the roll-up and the
per-workspace trend always agree on the same scan.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .trend import (
    DEFAULT_LIMIT,
    _clamp_limit,
    _zero_severity,
    summarise_scan,
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _parse_created_at(value: Any) -> Optional[datetime]:
    """Return an aware UTC datetime for a stored ``created_at``, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _workspace_of(scan: Dict[str, Any]) -> str:
    """Return the scan's workspace, or the empty string when unset/malformed."""
    workspace = scan.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        return workspace.strip()
    return ""


def _aggregate(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum the per-scan counts over ``entries`` into a fresh aggregate."""
    by_severity = _zero_severity()
    total_findings = 0
    open_findings = 0
    high_open = 0
    suppressed = 0
    for entry in entries:
        total_findings += entry["total_findings"]
        open_findings += entry["open_findings"]
        high_open += entry["high_open"]
        suppressed += entry["suppressed"]
        for key in by_severity:
            by_severity[key] += entry["by_severity"][key]
    return {
        "total_findings": total_findings,
        "by_severity": by_severity,
        "open_findings": open_findings,
        "high_open": high_open,
        "suppressed": suppressed,
    }


def _workspaces(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group the window into per-workspace aggregates, highest ``high_open`` first."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["workspace"], []).append(entry)
    workspaces = []
    for workspace, scans in grouped.items():
        aggregate = _aggregate(scans)
        delta_high = 0
        if len(scans) >= 2:
            delta_high = scans[-1]["high_open"] - scans[0]["high_open"]
        workspaces.append(
            {
                "workspace": workspace,
                "scans": len(scans),
                "total_findings": aggregate["total_findings"],
                "by_severity": aggregate["by_severity"],
                "open_findings": aggregate["open_findings"],
                "high_open": aggregate["high_open"],
                "suppressed": aggregate["suppressed"],
                "delta_high": delta_high,
            }
        )
    workspaces.sort(key=lambda item: (-item["high_open"], item["workspace"]))
    return workspaces


def _summary(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the whole window plus the cross-window ``delta_high``."""
    aggregate = _aggregate(entries)
    delta_high = 0
    if len(entries) >= 2:
        delta_high = entries[-1]["high_open"] - entries[0]["high_open"]
    return {
        "scans": len(entries),
        "workspaces": len({entry["workspace"] for entry in entries}),
        "total_findings": aggregate["total_findings"],
        "by_severity": aggregate["by_severity"],
        "open_findings": aggregate["open_findings"],
        "high_open": aggregate["high_open"],
        "suppressed": aggregate["suppressed"],
        "delta_high": delta_high,
    }


def build_rollup(scans: Any, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """Build the cross-workspace drift roll-up document.

    ``scans`` is the list of stored scan dicts (as returned by the store's
    scan-listing helper) and ``limit`` is clamped into ``1..MAX_LIMIT``
    (default :data:`docs_drift_detector.trend.DEFAULT_LIMIT`). Malformed scans
    (non-dict, or missing/invalid ``id`` or ``created_at``) are skipped rather
    than raising.

    The returned ``scans`` window holds up to ``limit`` of the most recent
    valid scans ordered oldest-first; each entry adds the scan's ``workspace``
    (empty string when unset) to the shared per-scan summary. ``workspaces``
    aggregates the window per workspace, sorted by descending ``high_open`` then
    workspace name, and ``summary`` aggregates the whole window. Both
    ``delta_high`` fields are ``newest.high_open - oldest.high_open`` (``0``
    when fewer than two relevant scans are present).
    """
    bounded = _clamp_limit(limit)
    if not isinstance(scans, list):
        scans = []
    valid: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for index, scan in enumerate(scans):
        if not isinstance(scan, dict):
            continue
        scan_id = scan.get("id")
        if not isinstance(scan_id, str) or not _ID_RE.match(scan_id):
            continue
        moment = _parse_created_at(scan.get("created_at"))
        if moment is None:
            continue
        entry = summarise_scan(scan)
        entry["workspace"] = _workspace_of(scan)
        valid.append((moment, index, entry))
    # ``scans`` is newest-first, so for equal timestamps the earlier input index
    # is newer; ``-index`` makes the oldest-first sort stable under that rule.
    valid.sort(key=lambda item: (item[0], -item[1]))
    window = valid[-bounded:]
    entries = [entry for _, _, entry in window]
    return {
        "scans": entries,
        "workspaces": _workspaces(entries),
        "summary": _summary(entries),
    }
