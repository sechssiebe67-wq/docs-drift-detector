"""Heuristic change extraction and documentation drift detection.

The module is deliberately dependency-free and works on plain data structures so
that it can be tested and reused independently of the HTTP layer.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

LABEL_BASE = 4
ROUTE_BASE = 5
SCREENSHOT_BASE = 3

_MAX_VALUE = 200

_RULE_KINDS = ("label", "route", "screenshot", "*")

_RULE_ORIGINS = ("request", "workspace")

_QUOTED = r"\"([^\"\n]{1,160})\"|'([^'\n]{1,160})'|`([^`\n]{1,160})`"
_ROUTE = r"[\"'`](/[A-Za-z0-9_\-.:{}\[\]*]+(?:/[A-Za-z0-9_\-.:{}\[\]*]*)*)[\"'`]"
_RENAME = (
    r"(?:rename[sd]?|change[sd]?|update[sd]?|replace[sd]?|moved?|"
    r"label(?:d)?|button)\s+[\"'`]([^\"'`\n]+)[\"'`]\s+"
    r"(?:to|with|into|->|=>|\u2192)\s+[\"'`]([^\"'`\n]+)[\"'`]"
)
_ARROW = (
    r"[\"'`]([^\"'`\n]{1,120})[\"'`]\s*(?:->|=>|\u2192)\s*[\"'`]([^\"'`\n]{1,120})[\"'`]"
)

_QUOTED_RE = re.compile(_QUOTED)
_ROUTE_RE = re.compile(_ROUTE)
_RENAME_RE = re.compile(_RENAME, re.IGNORECASE)
_ARROW_RE = re.compile(_ARROW)


def _quoted_strings(line: str) -> List[str]:
    values = []
    for match in _QUOTED_RE.finditer(line):
        for group in match.groups():
            if group is not None:
                values.append(group)
                break
    return values


def _routes(line: str) -> List[str]:
    return [m.group(1) for m in _ROUTE_RE.finditer(line)]


def _structured(explicit: Optional[Iterable[Any]]) -> List[Tuple[str, str]]:
    """Normalise an explicit label/route change input into (old, new) tuples."""
    result = []
    if not explicit:
        return result
    for item in explicit:
        if isinstance(item, dict):
            old = item.get("old") or item.get("from") or ""
            new = item.get("new") or item.get("to") or ""
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            old, new = item[0], item[1]
        else:
            continue
        old = str(old).strip()
        new = str(new).strip()
        if old or new:
            result.append((old, new))
    return result


def _iter_diff(text: str) -> Iterable[Tuple[List[str], List[str]]]:
    """Yield (removed_lines, added_lines) blocks for a unified diff or changelog."""
    removed: List[str] = []
    added: List[str] = []

    def flush():
        nonlocal removed, added
        if removed or added:
            block = (removed, added)
            removed, added = [], []
            return block
        return None

    for raw in text.splitlines():
        if (
            raw.startswith("@@")
            or raw.startswith("diff ")
            or raw.startswith("index ")
            or raw.startswith("--- ")
            or raw.startswith("+++ ")
        ):
            block = flush()
            if block:
                yield block
            continue
        if raw.startswith("-"):
            removed.append(raw[1:])
        elif raw.startswith("+"):
            added.append(raw[1:])
        elif raw.startswith(" ") or raw == "":
            block = flush()
            if block:
                yield block
    block = flush()
    if block:
        yield block


def _is_route(value: str) -> bool:
    return value.startswith("/")


def _dedupe_pairs(pairs: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    result = []
    for pair in pairs:
        key = (pair.get("old", "").lower(), pair.get("new", "").lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(pair)
    return result


def _rule_id(kind: str, page: str, value: str) -> str:
    raw = "|".join([kind, page, value]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _normalise_origin(entry: Any, origin: Optional[str]) -> str:
    """Resolve the rule origin, preserving an explicit tag when not overridden."""
    if origin is not None:
        return origin if origin in _RULE_ORIGINS else "request"
    tagged = entry.get("origin") if isinstance(entry, dict) else None
    return tagged if tagged in _RULE_ORIGINS else "request"


def _normalise_rule(
    entry: Any, origin: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """Normalise a single ignore rule, returning None for malformed entries."""
    resolved_origin = _normalise_origin(entry, origin)
    if isinstance(entry, str):
        value = entry.strip()
        if not value:
            return None
        rule = {"kind": "*", "page": "", "value": value, "reason": ""}
    elif isinstance(entry, dict):
        kind = entry.get("kind", "*")
        if not isinstance(kind, str) or kind.strip().lower() not in _RULE_KINDS:
            kind = "*"
        else:
            kind = kind.strip().lower()

        page = entry.get("page", entry.get("path", ""))
        page = page.strip() if isinstance(page, str) else ""

        value = entry.get("value", entry.get("old_value", entry.get("old", "")))
        value = value.strip() if isinstance(value, str) else ""

        reason = entry.get("reason", "")
        reason = reason.strip() if isinstance(reason, str) else ""

        if not page and not value and kind == "*":
            return None
        rule = {"kind": kind, "page": page, "value": value, "reason": reason}
    else:
        return None

    rule["id"] = _rule_id(rule["kind"], rule["page"], rule["value"])
    rule["origin"] = resolved_origin
    return rule


def _normalise_rules(rules: Any, origin: Optional[str] = None) -> List[Dict[str, str]]:
    """Normalise a rules input into a list of rule dicts.

    Accepts a list of dicts or strings (a bare dict is treated as a single
    rule). Each rule is tagged with a stable ``id`` and an ``origin``
    (``request`` or ``workspace``). When ``origin`` is omitted an existing
    ``origin`` on a rule is preserved and anything else defaults to
    ``request``. Malformed entries are ignored silently so a bad rules value
    can never raise into the caller.
    """
    if isinstance(rules, dict):
        rules = [rules]
    if not isinstance(rules, list):
        return []
    result = []
    for entry in rules:
        rule = _normalise_rule(entry, origin)
        if rule is not None:
            result.append(rule)
    return result


def _match_rule(
    finding: Dict[str, Any], rules: List[Dict[str, str]]
) -> Optional[Dict[str, str]]:
    """Return the first rule that suppresses ``finding``, or None."""
    kind = str(finding.get("kind") or "").lower()
    page = str(finding.get("page") or "")
    old = str(finding.get("old_value") or "")
    for rule in rules:
        if rule["kind"] != "*" and rule["kind"] != kind:
            continue
        if rule["page"] and not fnmatch.fnmatch(page, rule["page"]):
            continue
        if rule["value"] and rule["value"].lower() not in old.lower():
            continue
        return rule
    return None


def extract_label_changes(
    text: str = "", explicit: Optional[Iterable[Any]] = None
) -> List[Dict[str, str]]:
    changes: List[Dict[str, str]] = []
    for old, new in _structured(explicit):
        if not _is_route(old) and not _is_route(new):
            changes.append({"old": old, "new": new, "source": "input"})

    for line in (text or "").splitlines():
        for match in _RENAME_RE.finditer(line):
            changes.append(
                {"old": match.group(1).strip(), "new": match.group(2).strip(), "source": "changelog"}
            )
        for match in _ARROW_RE.finditer(line):
            old, new = match.group(1).strip(), match.group(2).strip()
            if not _is_route(old) and not _is_route(new):
                changes.append({"old": old, "new": new, "source": "changelog"})

    for removed, added in _iter_diff(text or ""):
        rem = [s for line in removed for s in _quoted_strings(line) if not _is_route(s)]
        add = [s for line in added for s in _quoted_strings(line) if not _is_route(s)]
        for old, new in zip(rem, add):
            if old != new:
                changes.append({"old": old, "new": new, "source": "diff"})

    return _dedupe_pairs(changes)


def extract_route_changes(
    text: str = "", explicit: Optional[Iterable[Any]] = None
) -> List[Dict[str, str]]:
    changes: List[Dict[str, str]] = []
    for old, new in _structured(explicit):
        if _is_route(old) or _is_route(new):
            changes.append({"old": old, "new": new, "source": "input"})

    for line in (text or "").splitlines():
        for match in _ARROW_RE.finditer(line):
            old, new = match.group(1).strip(), match.group(2).strip()
            if _is_route(old) and _is_route(new):
                changes.append({"old": old, "new": new, "source": "changelog"})

    for removed, added in _iter_diff(text or ""):
        rem = [s for line in removed for s in _routes(line)]
        add = [s for line in added for s in _routes(line)]
        for old, new in zip(rem, add):
            if old != new:
                changes.append({"old": old, "new": new, "source": "diff"})

    return _dedupe_pairs(changes)


def extract_changes(
    text: str = "",
    labels: Optional[Iterable[Any]] = None,
    routes: Optional[Iterable[Any]] = None,
) -> Dict[str, List[Dict[str, str]]]:
    return {
        "labels": extract_label_changes(text, labels),
        "routes": extract_route_changes(text, routes),
    }


def _normalise_pages(pages: Optional[Iterable[Any]]) -> List[Dict[str, str]]:
    result = []
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        path = str(page.get("path") or page.get("url") or "").strip()
        content = page.get("content")
        if content is None:
            content = page.get("text") or ""
        if not path and not content:
            continue
        result.append({"path": path or "(unnamed)", "content": str(content)})
    return result


def _snippet(content: str, needle: str) -> str:
    for line in content.splitlines():
        if needle in line:
            stripped = line.strip()
            return stripped[:_MAX_VALUE]
    return ""


def _finding_id(kind: str, page: str, old: str, new: str) -> str:
    raw = "|".join([kind, page, old, new]).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return max(number, 0.0)


def _metric_for(metrics: Dict[str, Any], page: str) -> Dict[str, float]:
    if not isinstance(metrics, dict):
        return {"views": 0.0, "tickets": 0.0}
    entry = metrics.get(page) or {}
    if not isinstance(entry, dict):
        entry = {}
    return {"views": _num(entry.get("views")), "tickets": _num(entry.get("tickets"))}


def _drift_for_change(
    change: Dict[str, str],
    pages: List[Dict[str, str]],
    metrics: Dict[str, Any],
    kind: str,
    base: int,
) -> List[Dict[str, Any]]:
    old = (change.get("old") or "").strip()
    new = (change.get("new") or "").strip()
    findings: List[Dict[str, Any]] = []
    if not old:
        return findings

    for page in pages:
        content = page["content"]
        if old not in content:
            continue
        occurrences = content.count(old)
        new_present = bool(new) and new in content
        reference = _snippet(content, old)

        if kind == "route":
            title = 'Route "%s" changed in the release' % old
        else:
            title = 'UI label "%s" changed in the release' % old

        if new_present:
            detail = (
                'Documentation already mentions the new value "%s" but still '
                "references the old one." % new
            )
        elif new:
            detail = 'Documentation still references the old value; update it to "%s".' % new
        else:
            detail = "Documentation references a value that changed or was removed."

        findings.append(
            {
                "id": _finding_id(kind, page["path"], old, new),
                "kind": kind,
                "page": page["path"],
                "title": title,
                "detail": detail,
                "old_value": old,
                "new_value": new,
                "snippet": reference,
                "occurrences": occurrences,
                "resolved": False,
                "metrics": _metric_for(metrics, page["path"]),
                "base": base,
            }
        )
    return findings


def _screenshot_changed(shot: Dict[str, Any]) -> bool:
    if shot.get("changed") is not None:
        return bool(shot["changed"])
    baseline = shot.get("baseline_hash")
    current = shot.get("current_hash")
    if baseline and current:
        return baseline != current
    changed_pixels = shot.get("changed_pixels")
    if changed_pixels is not None:
        return _num(changed_pixels) > 0
    return False


def _screenshot_drift(
    shot: Dict[str, Any], pages: List[Dict[str, str]], metrics: Dict[str, Any]
) -> List[Dict[str, Any]]:
    image = str(shot.get("image") or shot.get("path") or "").strip()
    if not image or not _screenshot_changed(shot):
        return []

    page_hint = str(shot.get("page") or "").strip()
    basename = image.rsplit("/", 1)[-1]
    targets = []
    for page in pages:
        if page_hint and page["path"] != page_hint:
            continue
        if image in page["content"] or basename in page["content"]:
            targets.append(page)
    if not targets and page_hint:
        targets.append({"path": page_hint, "content": ""})

    findings = []
    for page in targets:
        description = str(shot.get("description") or "").strip()
        detail = "Screenshot %s changed in the release." % image
        if description:
            detail += " " + description
        findings.append(
            {
                "id": _finding_id("screenshot", page["path"], image, ""),
                "kind": "screenshot",
                "page": page["path"],
                "title": 'Screenshot "%s" changed in the release' % image,
                "detail": detail,
                "old_value": image,
                "new_value": "",
                "snippet": _snippet(page["content"], basename),
                "occurrences": page["content"].count(basename) if page["content"] else 0,
                "resolved": False,
                "metrics": _metric_for(metrics, page["path"]),
                "base": SCREENSHOT_BASE,
            }
        )
    return findings


def _dedupe_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for finding in findings:
        key = (finding["kind"], finding["page"], finding["old_value"], finding["new_value"])
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def score_finding(base: int, views: float, tickets: float) -> float:
    """Transparent priority score.

    base * 10 dominates, page views add at most 100 points, and each support
    ticket adds 5 points. The result is used both for ordering and for the
    severity band.
    """
    return round(base * 10 + min(max(views, 0.0), 100000.0) / 1000.0 + max(tickets, 0.0) * 5, 1)


def severity_for(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def detect_drift(
    changes: Dict[str, List[Dict[str, str]]],
    pages: Optional[Iterable[Any]] = None,
    screenshots: Optional[Iterable[Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    rules: Any = None,
    stats: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    normalised_pages = _normalise_pages(pages)
    metrics = metrics or {}
    normalised_rules = _normalise_rules(rules)

    findings: List[Dict[str, Any]] = []
    for change in (changes or {}).get("labels", []):
        findings.extend(_drift_for_change(change, normalised_pages, metrics, "label", LABEL_BASE))
    for change in (changes or {}).get("routes", []):
        findings.extend(_drift_for_change(change, normalised_pages, metrics, "route", ROUTE_BASE))
    for shot in screenshots or []:
        if isinstance(shot, dict):
            findings.extend(_screenshot_drift(shot, normalised_pages, metrics))

    findings = _dedupe_findings(findings)

    suppressed = 0
    suppressed_counts: Dict[Tuple[str, str], int] = {}
    rule_lookup: Dict[Tuple[str, str], Dict[str, str]] = {}
    if normalised_rules:
        for rule in normalised_rules:
            rule_lookup[(rule["id"], rule["origin"])] = rule
        kept: List[Dict[str, Any]] = []
        for finding in findings:
            matched = _match_rule(finding, normalised_rules)
            if matched is not None:
                suppressed += 1
                key = (matched["id"], matched["origin"])
                suppressed_counts[key] = suppressed_counts.get(key, 0) + 1
            else:
                kept.append(finding)
        findings = kept

    suppressed_rules = []
    for key, count in suppressed_counts.items():
        rule = rule_lookup.get(key)
        if rule is None:
            continue
        suppressed_rules.append(
            {
                "id": rule["id"],
                "origin": rule["origin"],
                "kind": rule["kind"],
                "page": rule["page"],
                "value": rule["value"],
                "count": count,
            }
        )
    suppressed_rules.sort(key=lambda r: (-r["count"], r["id"]))

    for finding in findings:
        base = finding.pop("base")
        signals = finding.get("metrics") or {}
        finding["score"] = score_finding(base, signals.get("views", 0.0), signals.get("tickets", 0.0))
        finding["severity"] = severity_for(finding["score"])

    findings.sort(key=lambda f: (-f["score"], f["page"], f["id"]))
    if stats is not None:
        stats["suppressed"] = suppressed
        stats["suppressed_rules"] = suppressed_rules
    return findings


def summarise(
    findings: List[Dict[str, Any]],
    pages: Optional[Iterable[Any]] = None,
    suppressed: Any = 0,
    suppressed_rules: Any = None,
) -> Dict[str, Any]:
    resolved = sum(1 for f in findings if f.get("resolved"))
    try:
        suppressed_count = max(int(suppressed), 0)
    except (TypeError, ValueError):
        suppressed_count = 0
    if not isinstance(suppressed_rules, list):
        suppressed_rules = []
    return {
        "findings": len(findings),
        "unresolved": len(findings) - resolved,
        "resolved": resolved,
        "high": sum(1 for f in findings if f.get("severity") == "high"),
        "medium": sum(1 for f in findings if f.get("severity") == "medium"),
        "low": sum(1 for f in findings if f.get("severity") == "low"),
        "pages": len(_normalise_pages(pages)) if pages is not None else 0,
        "suppressed": suppressed_count,
        "suppressed_rules": suppressed_rules,
    }
