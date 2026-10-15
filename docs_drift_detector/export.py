"""Markdown export and machine-readable summary of a drift scan."""

from __future__ import annotations

import math
from typing import Any, Dict, List

TOP_FINDINGS_LIMIT = 5
_KINDS = ("label", "route", "screenshot")
_SEVERITIES = ("high", "medium", "low")


def _escape(text: str) -> str:
    return str(text).replace("\n", " ").strip()


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def to_markdown(scan: Dict[str, Any]) -> str:
    findings: List[Dict[str, Any]] = scan.get("findings", []) or []
    stats = scan.get("stats", {}) or {}
    lines = []
    lines.append("# Docs drift report: %s" % _escape(scan.get("name") or scan.get("id", "scan")))
    lines.append("")
    lines.append("- **Scan:** `%s`" % scan.get("id", ""))
    lines.append("- **Source:** %s" % _escape(scan.get("source") or "release"))
    lines.append("- **Created:** %s" % _escape(scan.get("created_at") or ""))
    lines.append(
        "- **Findings:** %d (%d open, %d high)"
        % (
            stats.get("findings", len(findings)),
            stats.get("unresolved", 0),
            stats.get("high", 0),
        )
    )
    lines.append("")

    if not findings:
        lines.append("_No documentation drift detected for this release._")
        lines.append("")
        return "\n".join(lines)

    for finding in findings:
        box = "x" if finding.get("resolved") else " "
        severity = str(finding.get("severity", "")).upper()
        lines.append(
            "- [%s] **[%s] %s** (`%s`, page `%s`)"
            % (
                box,
                severity,
                _escape(finding.get("title", "")),
                _escape(finding.get("kind", "")),
                _escape(finding.get("page", "")),
            )
        )
        detail = finding.get("detail")
        if detail:
            lines.append("  - %s" % _escape(detail))
        snippet = finding.get("snippet")
        if snippet:
            lines.append("  - Context: `%s`" % _escape(snippet).replace("`", "'"))
    lines.append("")
    return "\n".join(lines)


def summarise_scan(scan: Dict[str, Any]) -> Dict[str, Any]:
    """Build a compact, deterministic JSON summary of a stored scan.

    The document is intended for downstream consumers such as an outbound
    webhook or pull-request suggestion. It contains the scan identity, the
    existing counters (plus the per-rule ``suppressed_rules`` passthrough), up
    to :data:`TOP_FINDINGS_LIMIT` highest-scoring unresolved findings and
    ``kinds``/``severities`` breakdowns. No timestamps are added at read time,
    so the same scan always renders to byte-identical JSON.
    """
    scan = scan or {}
    findings: List[Dict[str, Any]] = scan.get("findings") or []
    stats = scan.get("stats") or {}

    summary: Dict[str, Any] = {
        "id": scan.get("id"),
        "name": scan.get("name"),
        "source": scan.get("source"),
        "created_at": scan.get("created_at"),
    }
    workspace = scan.get("workspace")
    if workspace:
        summary["workspace"] = workspace

    unresolved = [finding for finding in findings if not finding.get("resolved")]
    unresolved.sort(
        key=lambda finding: (
            -_num(finding.get("score")),
            str(finding.get("page") or ""),
            str(finding.get("id") or ""),
        )
    )
    top_findings = [
        {
            "id": finding.get("id"),
            "kind": finding.get("kind"),
            "page": finding.get("page"),
            "old_value": finding.get("old_value"),
            "new_value": finding.get("new_value"),
            "score": _num(finding.get("score")),
            "severity": finding.get("severity"),
        }
        for finding in unresolved[:TOP_FINDINGS_LIMIT]
    ]

    kinds = {kind: 0 for kind in _KINDS}
    severities = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        kind = finding.get("kind")
        if kind in kinds:
            kinds[kind] += 1
        severity = finding.get("severity")
        if severity in severities:
            severities[severity] += 1

    summary["stats"] = {
        "findings": stats.get("findings", len(findings)),
        "unresolved": stats.get(
            "unresolved", sum(1 for finding in findings if not finding.get("resolved"))
        ),
        "high": stats.get("high", severities["high"]),
        "suppressed": stats.get("suppressed", 0),
        "suppressed_rules": stats.get("suppressed_rules", []),
    }
    summary["top_findings"] = top_findings
    summary["kinds"] = kinds
    summary["severities"] = severities
    return summary
