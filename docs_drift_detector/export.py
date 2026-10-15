"""Markdown export of a drift scan, ready to commit into a docs repository."""

from __future__ import annotations

from typing import Any, Dict, List


def _escape(text: str) -> str:
    return str(text).replace("\n", " ").strip()


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
