"""Time-bucketed drift trend built from stored scans.

The per-workspace trend (:mod:`docs_drift_detector.trend`) and the
cross-workspace roll-up (:mod:`docs_drift_detector.rollup`) both present scans
in *scan order* within a single chronological window. This module answers a
different question: "is high-severity drift accumulating week over week?" by
grouping stored scans into calendar periods using each scan's ``created_at``.

Two bucket widths are supported: ``day`` (UTC calendar day) and ``week`` (ISO
week starting Monday, UTC). Each bucket carries a stable ``key``
(``2026-01-05`` for a day, ``2026-W02`` for a week), a UTC ISO-8601
``start``/``end`` range and the same per-scan aggregate fields the trend
summary exposes. Per-scan counts come from
:func:`docs_drift_detector.trend.summarise_scan`, so they never diverge from the
existing endpoints.

The module is intentionally read-only and dependency-free: it only reads the
list of stored scan dicts handed to it and never mutates, notifies, opens a
pull request or records a digest. Malformed scans (non-dict, or missing/invalid
``id`` or ``created_at``) are skipped exactly like
:func:`docs_drift_detector.rollup.build_rollup`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import pagination, rollup, timewindow
from .trend import DEFAULT_LIMIT, _clamp_limit, summarise_scan

BUCKETS = ("day", "week")
DEFAULT_BUCKET = "week"


def is_valid_bucket(value: Any) -> bool:
    """Return True when ``value`` names a supported bucket width."""
    return isinstance(value, str) and value in BUCKETS


def _bounds(moment: datetime, bucket: str) -> Tuple[datetime, datetime]:
    """Return the inclusive UTC ``(start, end)`` of ``moment``'s bucket."""
    start = moment.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if bucket == "week":
        start -= timedelta(days=start.weekday())
        span = timedelta(days=7)
    else:
        span = timedelta(days=1)
    end = start + span - timedelta(seconds=1)
    return start, end


def _key(start: datetime, bucket: str) -> str:
    """Return the stable bucket key for a bucket ``start``."""
    if bucket == "week":
        iso = start.isocalendar()
        return "%04d-W%02d" % (iso[0], iso[1])
    return start.strftime("%Y-%m-%d")


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _valid_entries(
    scans: Any,
    with_workspace: bool = False,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
) -> List[Tuple[datetime, int, Dict[str, Any]]]:
    """Normalise valid stored scans into ``(moment, index, entry)`` tuples.

    Scans that are not dicts, lack a well-formed ``id`` or have a
    missing/invalid ``created_at`` are skipped exactly like
    :func:`docs_drift_detector.rollup.build_rollup`. An optional inclusive
    ``from_time``/``to_time`` ``created_at`` window drops scans outside it
    (either side may be ``None`` to stay unbounded). The result is sorted
    oldest-first; scans created in the same second keep a deterministic order
    (newest input index first).
    """
    if not isinstance(scans, list):
        scans = []
    valid: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for index, scan in enumerate(scans):
        if not isinstance(scan, dict):
            continue
        scan_id = scan.get("id")
        if not isinstance(scan_id, str) or not rollup._ID_RE.match(scan_id):
            continue
        moment = rollup._parse_created_at(scan.get("created_at"))
        if moment is None:
            continue
        if not timewindow.in_window(moment, from_time, to_time):
            continue
        entry = summarise_scan(scan)
        if with_workspace:
            entry["workspace"] = rollup._workspace_of(scan)
        valid.append((moment, index, entry))
    valid.sort(key=lambda item: (item[0], -item[1]))
    return valid


def _group(
    valid: List[Tuple[datetime, int, Dict[str, Any]]], bucket: str
) -> List[Dict[str, Any]]:
    """Group pre-sorted scan entries into bucket records keyed by bucket key."""
    records: Dict[str, Dict[str, Any]] = {}
    for moment, _, entry in valid:
        start, end = _bounds(moment, bucket)
        key = _key(start, bucket)
        record = records.get(key)
        if record is None:
            record = {"key": key, "start": start, "end": end, "entries": []}
            records[key] = record
        record["entries"].append(entry)
    return list(records.values())


def _bucket_doc(record: Dict[str, Any]) -> Dict[str, Any]:
    """Render a bucket record into its JSON document."""
    aggregate = rollup._aggregate(record["entries"])
    return {
        "key": record["key"],
        "start": _iso(record["start"]),
        "end": _iso(record["end"]),
        "scans": len(record["entries"]),
        "total_findings": aggregate["total_findings"],
        "by_severity": aggregate["by_severity"],
        "open_findings": aggregate["open_findings"],
        "high_open": aggregate["high_open"],
        "suppressed": aggregate["suppressed"],
    }


def _summary(
    window: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Aggregate a window of bucket records plus the bucket-level ``delta_high``."""
    entries = [entry for record in window for entry in record["entries"]]
    aggregate = rollup._aggregate(entries)
    delta_high = 0
    if len(window) >= 2:
        newest = rollup._aggregate(window[-1]["entries"])["high_open"]
        oldest = rollup._aggregate(window[0]["entries"])["high_open"]
        delta_high = newest - oldest
    summary = {
        "scans": len(entries),
        "buckets": len(window),
        "total_findings": aggregate["total_findings"],
        "by_severity": aggregate["by_severity"],
        "open_findings": aggregate["open_findings"],
        "high_open": aggregate["high_open"],
        "suppressed": aggregate["suppressed"],
        "delta_high": delta_high,
    }
    if extra:
        summary.update(extra)
    return summary


def _bucket_key(record: Dict[str, Any]) -> Tuple[str, str]:
    """Return the stable cursor sort key ``(start, key)`` for a bucket record."""
    return (_iso(record["start"]), str(record.get("key") or ""))


def _window(
    valid: List[Tuple[datetime, int, Dict[str, Any]]],
    bucket: str,
    limit: Any,
    cursor: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return ``(window, next_cursor)`` of the most recent pages, oldest-first.

    ``window`` holds up to ``limit`` bucket records ordered oldest-first: the
    most recent buckets without a ``cursor``, or the ``limit`` buckets
    immediately older than the cursor anchor otherwise. ``next_cursor`` is the
    opaque cursor for the oldest bucket on the page, or ``None`` on the last
    page.
    """
    records = sorted(_group(valid, bucket), key=lambda record: record["start"])
    return pagination.paginate(records, cursor, limit, _bucket_key)


def build_workspace_buckets(
    scans: Any,
    workspace: str,
    bucket: str = DEFAULT_BUCKET,
    limit: int = DEFAULT_LIMIT,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the per-workspace time-bucketed trend document.

    ``scans`` is the workspace's stored scan list (newest-first from
    :meth:`docs_drift_detector.store.Store.list_workspace_scans`). An optional
    inclusive ``from_time``/``to_time`` ``created_at`` window excludes scans
    outside it before bucketing (either side may be ``None``). Malformed scans
    are skipped and ``limit`` (clamped into ``1..50``) bounds the number of
    buckets, keeping the most recent ones but presenting them **oldest-first**.
    Without a ``cursor`` the most recent ``limit`` buckets are returned exactly
    as before; with one, the ``limit`` buckets immediately older than the
    cursor's anchor are returned. ``next_cursor`` is the opaque cursor for the
    oldest bucket on the page, or ``None`` on the last page. ``summary``
    aggregates the page and sets ``delta_high`` to ``newest bucket high_open -
    oldest bucket high_open`` (``0`` when fewer than two buckets are in the
    page). A malformed ``cursor`` raises
    :class:`docs_drift_detector.pagination.CursorError`.
    """
    valid = _valid_entries(scans, from_time=from_time, to_time=to_time)
    window, next_cursor = _window(valid, bucket, limit, cursor)
    return {
        "workspace": workspace,
        "bucket": bucket,
        "buckets": [_bucket_doc(record) for record in window],
        "summary": _summary(window),
        "next_cursor": next_cursor,
    }


def build_rollup_buckets(
    scans: Any,
    bucket: str = DEFAULT_BUCKET,
    limit: int = DEFAULT_LIMIT,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the cross-workspace time-bucketed drift roll-up.

    ``scans`` is the list of every stored scan dict (as returned by
    :meth:`docs_drift_detector.store.Store.list_all_scans`). An optional
    inclusive ``from_time``/``to_time`` ``created_at`` window excludes scans
    outside it before bucketing (either side may be ``None``). Each bucket adds
    a ``workspaces`` breakdown sorted by descending ``high_open`` then workspace
    name, the top-level ``workspaces`` list aggregates the whole window the same
    way, and ``summary`` aggregates the window plus the bucket-level
    ``delta_high`` and a distinct-workspace count. Malformed scans are skipped
    and ``limit`` (clamped into ``1..50``) bounds the number of buckets. Without
    a ``cursor`` the most recent ``limit`` buckets are returned exactly as
    before; with one, the ``limit`` buckets immediately older than the cursor's
    anchor are returned and ``next_cursor`` is the opaque cursor for the oldest
    bucket on the page (``None`` on the last page). A malformed ``cursor``
    raises :class:`docs_drift_detector.pagination.CursorError`.
    """
    valid = _valid_entries(
        scans, with_workspace=True, from_time=from_time, to_time=to_time
    )
    window, next_cursor = _window(valid, bucket, limit, cursor)
    documents = []
    for record in window:
        document = _bucket_doc(record)
        document["workspaces"] = rollup._workspaces(record["entries"])
        documents.append(document)
    entries = [entry for record in window for entry in record["entries"]]
    workspaces = len({entry["workspace"] for entry in entries})
    return {
        "bucket": bucket,
        "buckets": documents,
        "workspaces": rollup._workspaces(entries),
        "summary": _summary(window, {"workspaces": workspaces}),
        "next_cursor": next_cursor,
    }
