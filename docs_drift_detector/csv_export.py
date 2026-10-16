"""Dependency-free tabular export of the time-bucketed drift trend.

The time-bucketed trend endpoints (:mod:`docs_drift_detector.buckets`) already
answer "how many open high-severity findings did each workspace carry at the end
of each week?" as JSON, but a documentation lead preparing a monthly report still
has to eyeball the JSON. This module renders that same document as CSV (for a
spreadsheet) or JSONL (newline-delimited JSON, for a data warehouse, log
pipeline or notebook that reads line-delimited records).

The module deliberately reuses the JSON builders' output — it takes the document
returned by :func:`docs_drift_detector.buckets.build_workspace_buckets` or
:func:`docs_drift_detector.buckets.build_rollup_buckets` and never recomputes a
count, so the exports and the JSON can never diverge. It is standard-library only
and never mutates state.

Two ``kind`` values are supported by both :func:`to_csv` and :func:`to_jsonl`:

- ``workspace`` renders the per-workspace document, one row/line per bucket;
- ``rollup`` renders the cross-workspace document, one row/line per bucket per
  workspace (a bucket with no workspace breakdown still emits one row/line with
  an empty ``workspace``).

CSV values are RFC 4180 quoted only when they contain ``,``, ``"`` or a newline,
so a plain value stays unquoted. JSONL lines are single JSON objects serialised
with :func:`json.dumps` (``ensure_ascii=False``, ``sort_keys=True``) and each
terminated by ``\\n``.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Optional, Sequence, Tuple

KINDS = ("workspace", "rollup")

_BASE_COLUMNS: Tuple[str, ...] = (
    "key",
    "start",
    "end",
    "scans",
    "total_findings",
    "by_severity_high",
    "by_severity_medium",
    "by_severity_low",
    "open_findings",
    "high_open",
    "suppressed",
)


def columns(kind: str) -> Tuple[str, ...]:
    """Return the CSV header columns for a ``kind`` (``workspace``/``rollup``).

    The rollup header appends a ``workspace`` column; the workspace header is
    the shared bucket columns only, so a generic parser can read either.
    """
    if kind == "rollup":
        return _BASE_COLUMNS + ("workspace",)
    if kind == "workspace":
        return _BASE_COLUMNS
    raise ValueError("unknown csv export kind: %r" % (kind,))


def _severity(source: Dict[str, Any], name: str) -> int:
    by_severity = source.get("by_severity")
    if not isinstance(by_severity, dict):
        return 0
    value = by_severity.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _row(
    bucket: Dict[str, Any],
    source: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Render one CSV row as a column-keyed mapping.

    ``bucket`` supplies ``key``/``start``/``end`` while ``source`` (defaulting to
    ``bucket``) supplies the aggregate counts. For a rollup per-workspace row,
    ``source`` is the bucket's workspace breakdown entry and ``workspace`` is its
    name; an empty ``workspace`` is emitted for a bucket with no breakdown.
    """
    source = source if isinstance(source, dict) else bucket
    values: Dict[str, Any] = {
        "key": bucket.get("key", ""),
        "start": bucket.get("start", ""),
        "end": bucket.get("end", ""),
        "scans": source.get("scans", 0),
        "total_findings": source.get("total_findings", 0),
        "by_severity_high": _severity(source, "high"),
        "by_severity_medium": _severity(source, "medium"),
        "by_severity_low": _severity(source, "low"),
        "open_findings": source.get("open_findings", 0),
        "high_open": source.get("high_open", 0),
        "suppressed": source.get("suppressed", 0),
    }
    if workspace is not None:
        values["workspace"] = workspace
    return values


def _write(writer: Any, header: Sequence[str], row: Dict[str, Any]) -> None:
    writer.writerow([row.get(name, "") for name in header])


def to_csv(document: Any, kind: str = "workspace") -> str:
    """Render a bucket-trend ``document`` as RFC 4180 CSV text.

    ``document`` is the output of
    :func:`docs_drift_detector.buckets.build_workspace_buckets` (``kind ==
    "workspace"``) or :func:`docs_drift_detector.buckets.build_rollup_buckets`
    (``kind == "rollup"``); the export never recomputes a value. It returns a
    header row plus one row per bucket (workspace) or per bucket per workspace
    (rollup), oldest-first, using CRLF line endings and RFC 4180 minimal
    quoting. An unknown ``kind`` raises :class:`ValueError`.
    """
    if kind not in KINDS:
        raise ValueError("unknown csv export kind: %r" % (kind,))
    header = columns(kind)
    if not isinstance(document, dict):
        document = {}
    buckets = document.get("buckets")
    if not isinstance(buckets, list):
        buckets = []

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(header))
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        if kind == "workspace":
            _write(writer, header, _row(bucket))
            continue
        breakdown = bucket.get("workspaces")
        if isinstance(breakdown, list) and breakdown:
            for entry in breakdown:
                if not isinstance(entry, dict):
                    continue
                workspace = entry.get("workspace")
                if not isinstance(workspace, str):
                    workspace = "" if workspace is None else str(workspace)
                _write(writer, header, _row(bucket, entry, workspace))
        else:
            _write(writer, header, _row(bucket, workspace=""))
    return buffer.getvalue()


def _by_severity(source: Dict[str, Any]) -> Dict[str, int]:
    """Return the nested ``by_severity`` mapping, defaulting missing counts to 0."""
    return {name: _severity(source, name) for name in ("high", "medium", "low")}


def _line(
    bucket: Dict[str, Any],
    source: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Render one JSONL line as a JSON-serialisable mapping.

    ``bucket`` supplies ``key``/``start``/``end`` while ``source`` (defaulting to
    ``bucket``) supplies the aggregate counts. For a rollup per-workspace line,
    ``source`` is the bucket's workspace breakdown entry and ``workspace`` is its
    name; an empty ``workspace`` is emitted for a bucket with no breakdown. The
    shape mirrors the JSON ``buckets`` entries (nested ``by_severity``) so a line
    round-trips and matches its JSON counterpart.
    """
    source = source if isinstance(source, dict) else bucket
    values: Dict[str, Any] = {
        "key": bucket.get("key", ""),
        "start": bucket.get("start", ""),
        "end": bucket.get("end", ""),
        "scans": source.get("scans", 0),
        "total_findings": source.get("total_findings", 0),
        "by_severity": _by_severity(source),
        "open_findings": source.get("open_findings", 0),
        "high_open": source.get("high_open", 0),
        "suppressed": source.get("suppressed", 0),
    }
    if workspace is not None:
        values["workspace"] = workspace
    return values


def to_jsonl(document: Any, kind: str = "workspace") -> str:
    """Render a bucket-trend ``document`` as newline-delimited JSON text.

    ``document`` is the output of
    :func:`docs_drift_detector.buckets.build_workspace_buckets` (``kind ==
    "workspace"``) or :func:`docs_drift_detector.buckets.build_rollup_buckets`
    (``kind == "rollup"``); the export never recomputes a value. It returns one
    self-contained JSON object per bucket (workspace) or per bucket per workspace
    (rollup), oldest-first, each serialised with
    ``json.dumps(..., ensure_ascii=False, sort_keys=True)`` and terminated by a
    ``"\\n"`` (including a trailing newline after the last line). An unknown
    ``kind`` raises :class:`ValueError`.
    """
    if kind not in KINDS:
        raise ValueError("unknown jsonl export kind: %r" % (kind,))
    if not isinstance(document, dict):
        document = {}
    buckets = document.get("buckets")
    if not isinstance(buckets, list):
        buckets = []

    lines = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        if kind == "workspace":
            lines.append(_line(bucket))
            continue
        breakdown = bucket.get("workspaces")
        if isinstance(breakdown, list) and breakdown:
            for entry in breakdown:
                if not isinstance(entry, dict):
                    continue
                workspace = entry.get("workspace")
                if not isinstance(workspace, str):
                    workspace = "" if workspace is None else str(workspace)
                lines.append(_line(bucket, entry, workspace))
        else:
            lines.append(_line(bucket, workspace=""))
    return "".join(
        json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines
    )
