"""Per-workspace drift dashboard: one read-only document for "right now".

A documentation lead who wants to answer "what does drift look like for this
workspace right now?" otherwise has to call several endpoints
(:mod:`docs_drift_detector.trend`, :mod:`docs_drift_detector.buckets` and the
per-workspace digest history) and stitch the responses together. This module
composes those existing builders into a single JSON-serialisable document so a
UI or a ``curl | jq`` pipeline can render the current state in one request.

The module is intentionally read-only and dependency-free: it reuses
:func:`docs_drift_detector.trend.build_trend`,
:func:`docs_drift_detector.buckets.build_workspace_buckets` and the store's
``list_workspace_digests`` reader and never mutates the store, dispatches a
webhook, opens a pull request or records a digest. Malformed scans (non-dict, or
missing/invalid ``id`` or ``created_at``) are skipped exactly like
:func:`docs_drift_detector.rollup.build_rollup` /
:func:`docs_drift_detector.buckets._valid_entries`, so the dashboard never raises
on a partially written store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import buckets, pagination, rollup, trend

MAX_LIMIT = pagination.MAX_LIMIT


class _ScanSource:
    """Adapt a pre-fetched scan list to the store interface ``build_trend`` wants.

    :func:`docs_drift_detector.trend.build_trend` reads through
    ``store.list_workspace_scans(workspace)``. The dashboard already holds the
    workspace's scans, so this tiny adapter hands them back unchanged and keeps
    the trend builder (and its pagination/summary semantics) untouched.
    """

    def __init__(self, scans: Any):
        self._scans = scans if isinstance(scans, list) else []

    def list_workspace_scans(self, workspace: str) -> List[Dict[str, Any]]:
        return list(self._scans)


def _valid_scans(scans: Any) -> List[Dict[str, Any]]:
    """Filter ``scans`` to the well-formed ones the builders can summarise.

    This mirrors :func:`docs_drift_detector.buckets._valid_entries` /
    :func:`docs_drift_detector.rollup.build_rollup`: a non-dict scan, or one with
    a missing/invalid ``id`` or an unparseable ``created_at``, is skipped.
    """
    if not isinstance(scans, list):
        return []
    valid: List[Dict[str, Any]] = []
    for scan in scans:
        if not isinstance(scan, dict):
            continue
        scan_id = scan.get("id")
        if not isinstance(scan_id, str) or not rollup._ID_RE.match(scan_id):
            continue
        if rollup._parse_created_at(scan.get("created_at")) is None:
            continue
        valid.append(scan)
    return valid


def _iso(moment: Optional[datetime]) -> Optional[str]:
    """Echo an effective window bound as a UTC ISO-8601 string, or ``None``."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _generated_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_dashboard(
    scans: Any,
    workspace: str,
    limit: int = trend.DEFAULT_LIMIT,
    bucket: str = buckets.DEFAULT_BUCKET,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    store: Any = None,
) -> Dict[str, Any]:
    """Compose the per-workspace drift dashboard document.

    ``scans`` is the workspace's stored scan list (as from
    :meth:`docs_drift_detector.store.Store.list_workspace_scans`, newest-first).
    The response carries:

    - ``workspace``: the workspace name;
    - ``generated_at``: the UTC ISO-8601 timestamp of the request;
    - ``window``: ``{from, to, limit, bucket}`` echoing the effective query
      parameters (``from``/``to`` are ``None`` when unset);
    - ``trend``: the exact document from
      :func:`docs_drift_detector.trend.build_trend` (reused, not recomputed);
    - ``buckets``: the exact ``buckets`` list from
      :func:`docs_drift_detector.buckets.build_workspace_buckets`;
    - ``digests``: the workspace's digest history, newest-first, bounded by the
      same ``limit`` (read through ``store.list_workspace_digests`` when a
      ``store`` is supplied, otherwise empty);
    - ``summary``: the bucket aggregate merged with ``latest_digest`` (the newest
      digest entry or ``None``).

    ``limit`` is clamped into ``1..50`` and ``bucket`` accepts only ``day``/
    ``week`` (anything else falls back to ``week``) exactly like the underlying
    builders. ``from_time``/``to_time`` form the shared inclusive ``created_at``
    window. The function is strictly read-only.
    """
    effective_limit = pagination.clamp_limit(limit)
    effective_bucket = bucket if buckets.is_valid_bucket(bucket) else buckets.DEFAULT_BUCKET
    valid = _valid_scans(scans)

    trend_document = trend.build_trend(
        _ScanSource(valid),
        workspace,
        effective_limit,
        from_time=from_time,
        to_time=to_time,
    )
    bucket_document = buckets.build_workspace_buckets(
        valid,
        workspace,
        bucket=effective_bucket,
        limit=effective_limit,
        from_time=from_time,
        to_time=to_time,
    )

    digests: List[Dict[str, Any]] = []
    if store is not None:
        try:
            fetched = store.list_workspace_digests(workspace, effective_limit)
        except ValueError:
            fetched = []
        if isinstance(fetched, list):
            digests = [entry for entry in fetched if isinstance(entry, dict)]
    digests = digests[:effective_limit]

    summary = dict(bucket_document.get("summary") or {})
    summary["latest_digest"] = digests[0] if digests else None

    return {
        "workspace": workspace,
        "generated_at": _generated_at(),
        "window": {
            "from": _iso(from_time),
            "to": _iso(to_time),
            "limit": effective_limit,
            "bucket": effective_bucket,
        },
        "trend": trend_document,
        "buckets": bucket_document.get("buckets", []),
        "digests": digests,
        "summary": summary,
    }
