"""Outbound webhook notifications for high-severity drift findings.

The module is dependency-free: payload construction works on plain data and
delivery uses a single :mod:`urllib.request` POST. Delivery is best-effort and
never raises into the caller, so a dead webhook can never fail a scan.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any, Dict, List

DEFAULT_TIMEOUT = 5

_FINDING_FIELDS = (
    "id",
    "kind",
    "page",
    "title",
    "old_value",
    "new_value",
    "score",
    "severity",
)


def _score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def build_payload(scan: Dict[str, Any]) -> Dict[str, Any]:
    """Build a stable, JSON-serialisable notification payload for ``scan``.

    Only ``severity == "high"`` findings are included, sorted by descending
    ``score`` (then page, then id) so the most urgent work is first.
    """
    scan = scan if isinstance(scan, dict) else {}
    scan_id = scan.get("id") or ""
    findings: List[Dict[str, Any]] = []
    for finding in scan.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if finding.get("severity") != "high":
            continue
        findings.append(
            {
                "id": finding.get("id", ""),
                "kind": finding.get("kind", ""),
                "page": finding.get("page", ""),
                "title": finding.get("title", ""),
                "old_value": finding.get("old_value", ""),
                "new_value": finding.get("new_value", ""),
                "score": _score(finding.get("score")),
                "severity": "high",
            }
        )
    findings.sort(key=lambda item: (-item["score"], item["page"] or "", item["id"] or ""))
    return {
        "scan_id": scan_id,
        "name": scan.get("name", ""),
        "workspace": scan.get("workspace", ""),
        "created_at": scan.get("created_at", ""),
        "url": scan.get("url") or ("/api/scans/%s" % scan_id),
        "findings": findings,
    }


def _opener(request: urllib.request.Request, timeout: float):
    """Indirection around :func:`urllib.request.urlopen` for easy testing."""
    return urllib.request.urlopen(request, timeout=timeout)


def _status_of(response: Any) -> Any:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            try:
                status = getcode()
            except Exception:  # pragma: no cover - defensive
                status = None
    return status if isinstance(status, int) else None


def dispatch(url: str, payload: Dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """POST ``payload`` as JSON to ``url`` once.

    Returns ``{"ok": bool, "status": int | None, "error": str}`` and never
    raises for network or HTTP errors, so callers can ignore the result.
    """
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        response = _opener(request, timeout)
        status = _status_of(response)
        try:
            response.close()
        except Exception:  # pragma: no cover - defensive
            pass
    except urllib.error.HTTPError as err:
        return {"ok": False, "status": err.code, "error": str(err)}
    except Exception as err:  # urllib.error.URLError and anything else
        return {"ok": False, "status": None, "error": str(err) or err.__class__.__name__}

    ok = status is None or (200 <= status < 300)
    error = "" if ok else ("HTTP %s" % status if status is not None else "request failed")
    return {"ok": ok, "status": status, "error": error}
