"""Per-workspace drift trend built from the most recent stored scans.

The module turns a workspace's recent scans into a small, JSON-serialisable
time series so a documentation lead can see whether drift is getting better or
worse over time. It is intentionally read-only and dependency-free: it only
reads through the store's ``list_workspace_scans`` method and never mutates,
notifies, opens a pull request or records a digest.

Severity and openness semantics match the rest of the service: a finding is
*open* while it is not resolved, and severity is the band already stored on the
finding by :func:`docs_drift_detector.analyze.detect_drift`, so the numbers here
agree with webhooks, digests and PR suggestions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import pagination, timewindow

DEFAULT_LIMIT = pagination.DEFAULT_LIMIT
MAX_LIMIT = pagination.MAX_LIMIT

_SEVERITIES = ("high", "medium", "low")


def _clamp_limit(limit: Any) -> int:
    """Clamp ``limit`` into ``1..MAX_LIMIT``; fall back to ``DEFAULT_LIMIT``."""
    return pagination.clamp_limit(limit)


def _scan_key(scan: Dict[str, Any]) -> tuple:
    """Return the stable cursor sort key ``(created_at, scan_id)`` for a scan."""
    return (str(scan.get("created_at") or ""), str(scan.get("id") or ""))


def _zero_severity() -> Dict[str, int]:
    return {"high": 0, "medium": 0, "low": 0}


def summarise_scan(scan: Any) -> Dict[str, Any]:
    """Normalise one stored scan into a trend/roll-up point.

    This is the shared per-scan summarisation used by both the per-workspace
    trend (:func:`build_trend`) and the cross-workspace roll-up
    (``docs_drift_detector.rollup.build_rollup``), so both endpoints report the
    same ``total_findings``/``by_severity``/``open_findings``/``high_open``/
    ``suppressed`` for a given scan. Malformed values are tolerated and
    defaulted rather than raising.
    """
    scan = scan if isinstance(scan, dict) else {}
    raw_findings = scan.get("findings")
    findings = (
        [f for f in raw_findings if isinstance(f, dict)]
        if isinstance(raw_findings, list)
        else []
    )
    by_severity = _zero_severity()
    open_findings = 0
    high_open = 0
    for finding in findings:
        severity = finding.get("severity")
        if severity in by_severity:
            by_severity[severity] += 1
        if not finding.get("resolved"):
            open_findings += 1
            if severity == "high":
                high_open += 1
    stats = scan.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    suppressed = stats.get("suppressed")
    if (
        isinstance(suppressed, bool)
        or not isinstance(suppressed, int)
        or suppressed < 0
    ):
        suppressed = 0
    return {
        "scan_id": scan.get("id") or "",
        "created_at": scan.get("created_at") or "",
        "name": scan.get("name") or "",
        "total_findings": len(findings),
        "by_severity": by_severity,
        "open_findings": open_findings,
        "high_open": high_open,
        "suppressed": suppressed,
    }


def _summary(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
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
    delta_high = 0
    if len(entries) >= 2:
        delta_high = entries[-1]["high_open"] - entries[0]["high_open"]
    return {
        "scans": len(entries),
        "total_findings": total_findings,
        "by_severity": by_severity,
        "open_findings": open_findings,
        "high_open": high_open,
        "suppressed": suppressed,
        "delta_high": delta_high,
    }


def build_trend(
    store: Any,
    workspace: str,
    limit: int = DEFAULT_LIMIT,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the per-workspace drift trend document.

    The store's stored scans are filtered to the optional inclusive
    ``from_time``/``to_time`` ``created_at`` window (either side may be
    ``None``), then paginated oldest-first. Without a ``cursor`` the most recent
    ``limit`` scans are returned (clamped into ``1..MAX_LIMIT``) exactly as
    before; with one, the ``limit`` scans immediately older than the cursor's
    anchor are returned. ``next_cursor`` is the opaque cursor for the oldest scan
    on the page, or ``None`` on the last page. The ``summary`` block aggregates
    the returned page and sets ``delta_high`` to
    ``newest.high_open - oldest.high_open`` (``0`` when fewer than two scans are
    in the page). Malformed scans and missing fields are skipped or defaulted
    to ``0`` so the function never raises; when a window is supplied a scan
    whose ``created_at`` cannot be parsed is excluded. A malformed ``cursor``
    raises :class:`docs_drift_detector.pagination.CursorError`.
    """
    scans = store.list_workspace_scans(workspace)
    if not isinstance(scans, list):
        scans = []
    windowed = bool(from_time is not None or to_time is not None)
    valid: List[Dict[str, Any]] = []
    for scan in scans:
        if not isinstance(scan, dict):
            continue
        if windowed:
            moment = timewindow.parse_stored(scan.get("created_at"))
            if moment is None or not timewindow.in_window(moment, from_time, to_time):
                continue
        valid.append(scan)
    page, next_cursor = pagination.paginate(
        list(reversed(valid)), cursor, limit, _scan_key
    )
    entries = [summarise_scan(scan) for scan in page]
    return {
        "workspace": workspace,
        "scans": entries,
        "summary": _summary(entries),
        "next_cursor": next_cursor,
    }
