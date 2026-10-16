"""Outbound webhook notifications for high-severity drift findings.

The module is dependency-free: payload construction works on plain data and
delivery uses a single :mod:`urllib.request` POST. Delivery is best-effort and
never raises into the caller, so a dead webhook can never fail a scan.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_TIMEOUT = 5
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5

RETRIES_ENV = "DDR_WEBHOOK_RETRIES"
BACKOFF_ENV = "DDR_WEBHOOK_BACKOFF"

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


def _finding_key(finding: Dict[str, Any]) -> tuple:
    """Identity used to diff two scans: kind, page and the change values."""
    return (
        finding.get("kind", ""),
        finding.get("page", ""),
        finding.get("old_value", ""),
        finding.get("new_value", ""),
    )


def build_digest(
    scan: Dict[str, Any], previous_scan: Any = None
) -> Dict[str, Any]:
    """Build a JSON-serialisable digest payload for a scheduled scan.

    The digest summarises every finding in ``scan`` (``total_findings`` and
    ``by_severity``) and lists the high-severity findings that are new relative
    to ``previous_scan`` as ``new_high``. A finding is considered new when its
    ``(kind, page, old_value, new_value)`` key is absent from the previous
    scan's findings. The high-severity selection and ordering reuse
    :func:`build_payload` so webhook and digest scope stay identical.
    """
    scan = scan if isinstance(scan, dict) else {}
    scan_id = scan.get("id") or ""
    findings = [f for f in (scan.get("findings") or []) if isinstance(f, dict)]

    by_severity = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        severity = finding.get("severity")
        if severity in by_severity:
            by_severity[severity] += 1

    previous_keys = set()
    if isinstance(previous_scan, dict):
        for finding in previous_scan.get("findings") or []:
            if isinstance(finding, dict):
                previous_keys.add(_finding_key(finding))

    new_high = [
        finding
        for finding in build_payload(scan)["findings"]
        if _finding_key(finding) not in previous_keys
    ]

    return {
        "scan_id": scan_id,
        "workspace": scan.get("workspace", ""),
        "created_at": scan.get("created_at", ""),
        "total_findings": len(findings),
        "by_severity": by_severity,
        "new_high": new_high,
        "url": scan.get("url") or ("/api/scans/%s" % scan_id),
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


def dispatch(
    url: str,
    payload: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """POST ``payload`` as JSON to ``url`` once.

    Returns ``{"ok": bool, "status": int | None, "error": str}`` and never
    raises for network or HTTP errors, so callers can ignore the result. The
    optional ``opener`` overrides the module-level :data:`_opener` (used to
    inject a fake transport in tests).
    """
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        response = (opener or _opener)(request, timeout)
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


def default_attempts() -> int:
    """Total POST attempts, from ``DDR_WEBHOOK_RETRIES`` (default ``3``).

    A missing, non-integer or non-positive value falls back to the default.
    """
    try:
        value = int(os.environ.get(RETRIES_ENV))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES
    if isinstance(value, bool) or value < 1:
        return DEFAULT_RETRIES
    return value


def default_backoff() -> float:
    """Base backoff seconds, from ``DDR_WEBHOOK_BACKOFF`` (default ``0.5``).

    A missing, non-numeric, non-finite or non-positive value falls back to the
    default.
    """
    try:
        value = float(os.environ.get(BACKOFF_ENV))
    except (TypeError, ValueError):
        return DEFAULT_BACKOFF
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_BACKOFF
    return value


def dispatch_with_retry(
    url: str,
    payload: Dict[str, Any],
    attempts: Optional[int] = None,
    backoff: Optional[float] = None,
    sleep: Callable[[float], Any] = time.sleep,
    opener: Optional[Callable[..., Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """POST ``payload`` to ``url`` with bounded exponential-backoff retries.

    Performs at most ``attempts`` POSTs (default
    :func:`default_attempts`/``DDR_WEBHOOK_RETRIES``), sleeping ``backoff``
    (default :func:`default_backoff`/``DDR_WEBHOOK_BACKOFF``) seconds before the
    first retry and doubling the delay for each subsequent retry. ``sleep`` and
    ``opener`` are injectable so tests can avoid real waiting and networking.

    Returns ``{"ok", "attempts", "status", "error"}`` where ``attempts`` is the
    number of POSTs actually made and ``error`` is the last error (empty on
    success). Delivery is best-effort: the function never raises into its
    caller.
    """
    total = default_attempts() if attempts is None else _positive_int(attempts)
    delay = default_backoff() if backoff is None else _positive_float(backoff)

    made = 0
    error = ""
    status: Any = None
    while made < total:
        made += 1
        try:
            if opener is None:
                result = dispatch(url, payload, timeout=timeout)
            else:
                result = dispatch(url, payload, timeout=timeout, opener=opener)
        except Exception as err:  # defensive: dispatch should never raise
            result = {
                "ok": False,
                "status": None,
                "error": str(err) or err.__class__.__name__,
            }
        if not isinstance(result, dict):
            result = {"ok": False, "status": None, "error": "invalid dispatch result"}
        if result.get("ok"):
            return {
                "ok": True,
                "attempts": made,
                "status": result.get("status"),
                "error": "",
            }
        error = result.get("error") or ""
        status = result.get("status")
        if made < total:
            try:
                sleep(delay * (2 ** (made - 1)))
            except Exception as err:  # defensive: a broken sleep must not raise
                error = error or str(err) or err.__class__.__name__
                break
    return {
        "ok": False,
        "attempts": made,
        "status": status,
        "error": error or "request failed",
    }


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRIES
    if isinstance(value, bool) or number < 1:
        return DEFAULT_RETRIES
    return number


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BACKOFF
    if not math.isfinite(number) or number <= 0:
        return DEFAULT_BACKOFF
    return number
