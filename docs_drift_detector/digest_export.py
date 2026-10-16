"""Dependency-free CSV/JSONL export of the per-workspace digest history.

``GET /api/workspaces/{ws}/digests`` already answers "what did the scheduler
POST, and how many new high-severity findings did it carry?" as JSON, but a
documentation lead attaching that history to a release review or loading it into
a spreadsheet/warehouse still has to reshape the JSON by hand. This module
renders that same document as CSV (for a spreadsheet) or JSONL
(newline-delimited JSON, for a warehouse, log pipeline or notebook).

The module deliberately reuses the JSON endpoint's output — it takes the
``{"workspace", "digests"}`` document built from
:meth:`docs_drift_detector.store.Store.list_workspace_digests` and never
recomputes a value, so the exports and the JSON can never diverge. It is
standard-library only, never mutates state and, like the JSON sibling, emits the
entries **newest-first**. A non-dict entry (or a malformed/missing ``digests``
list) is skipped rather than raising.

CSV values are RFC 4180 quoted only when they contain ``,``, ``"`` or a newline,
so a plain value stays unquoted (mirroring ``csv_export.to_csv``). JSONL lines
are single JSON objects serialised with :func:`json.dumps` (``ensure_ascii=False``,
``sort_keys=True``) and each terminated by ``\\n``, including a trailing newline
after the last line (mirroring ``csv_export.to_jsonl``).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Tuple

COLUMNS: Tuple[str, ...] = (
    "created_at",
    "scan_id",
    "total_findings",
    "by_severity_high",
    "by_severity_medium",
    "by_severity_low",
    "new_high_count",
    "delivery_ok",
    "delivery_error",
    "attempts",
)


def columns() -> Tuple[str, ...]:
    """Return the CSV header columns for a digest-history export."""
    return COLUMNS


def _digests(document: Any) -> List[Dict[str, Any]]:
    """Return the well-formed digest entries from a history ``document``.

    ``document`` may be either the ``{"workspace", "digests"}`` object returned
    by ``GET /api/workspaces/{ws}/digests`` or a bare list of entries. Anything
    other than a list under ``digests`` is treated as empty, and a non-dict
    entry is dropped, so a partially written store never raises.
    """
    if isinstance(document, dict):
        document = document.get("digests")
    if not isinstance(document, list):
        return []
    return [entry for entry in document if isinstance(entry, dict)]


def _severity(entry: Dict[str, Any], name: str) -> int:
    by_severity = entry.get("by_severity")
    if not isinstance(by_severity, dict):
        return 0
    value = by_severity.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Render one CSV row as a column-keyed mapping."""
    return {
        "created_at": entry.get("created_at", ""),
        "scan_id": entry.get("scan_id", ""),
        "total_findings": entry.get("total_findings", 0),
        "by_severity_high": _severity(entry, "high"),
        "by_severity_medium": _severity(entry, "medium"),
        "by_severity_low": _severity(entry, "low"),
        "new_high_count": entry.get("new_high_count", 0),
        "delivery_ok": entry.get("delivery_ok", False),
        "delivery_error": entry.get("delivery_error", ""),
        "attempts": entry.get("attempts", 0),
    }


def to_csv(document: Any) -> str:
    """Render a digest-history ``document`` as RFC 4180 CSV text.

    ``document`` is the output of ``GET /api/workspaces/{ws}/digests`` (or a
    bare list of digest entries); the export never recomputes a value. It
    returns a header row plus one row per digest entry, newest-first, using CRLF
    line endings and RFC 4180 minimal quoting. A missing/empty history yields a
    header-only document.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(COLUMNS))
    for entry in _digests(document):
        row = _row(entry)
        writer.writerow([row.get(name, "") for name in COLUMNS])
    return buffer.getvalue()


def _line(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Render one JSONL line as a JSON-serialisable mapping.

    The shape mirrors the JSON ``digests`` entries (nested ``by_severity``)
    instead of the flattened CSV columns, so a line round-trips and matches its
    JSON counterpart.
    """
    return {
        "created_at": entry.get("created_at", ""),
        "scan_id": entry.get("scan_id", ""),
        "total_findings": entry.get("total_findings", 0),
        "by_severity": {
            name: _severity(entry, name) for name in ("high", "medium", "low")
        },
        "new_high_count": entry.get("new_high_count", 0),
        "delivery_ok": bool(entry.get("delivery_ok", False)),
        "delivery_error": entry.get("delivery_error", ""),
        "attempts": entry.get("attempts", 0),
    }


def to_jsonl(document: Any) -> str:
    """Render a digest-history ``document`` as newline-delimited JSON text.

    ``document`` is the output of ``GET /api/workspaces/{ws}/digests`` (or a
    bare list of digest entries); the export never recomputes a value. It
    returns one self-contained JSON object per digest entry, newest-first, each
    serialised with ``json.dumps(..., ensure_ascii=False, sort_keys=True)`` and
    terminated by a ``"\\n"`` (including a trailing newline after the last line).
    An empty history yields an empty string.
    """
    return "".join(
        json.dumps(_line(entry), ensure_ascii=False, sort_keys=True) + "\n"
        for entry in _digests(document)
    )
