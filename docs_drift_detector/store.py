"""Persistent scan storage backed by a single JSON file.

No database is required. Writes are atomic (write to a temporary file, then
``os.replace``) so a crash cannot leave a partially written store behind.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import analyze

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_TOKEN_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BASE_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

MAX_RULE_HISTORY = 20
MAX_DIGEST_HISTORY = 20
MAX_WEBHOOK_URL = 500
MIN_SCHEDULE_HOURS = 1
MAX_SCHEDULE_HOURS = 168
MAX_RETENTION_SCANS = 10000
DEFAULT_GITHUB_BASE_BRANCH = "main"
DEFAULT_GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
_RULE_HISTORY_ACTIONS = ("set", "import", "clear", "promote")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _valid_workspace(workspace: Any) -> bool:
    return bool(_WORKSPACE_RE.match(str(workspace or "")))


def _validate_webhook_url(url: Any) -> str:
    """Return the normalised webhook URL, or ``""`` when it should be cleared.

    A non-empty URL must be an ``http``/``https`` URL of at most
    ``MAX_WEBHOOK_URL`` characters; anything else raises :class:`ValueError`.
    """
    if url is None:
        raise ValueError("invalid webhook url")
    if not isinstance(url, str):
        raise ValueError("invalid webhook url")
    url = url.strip()
    if not url:
        return ""
    if len(url) > MAX_WEBHOOK_URL:
        raise ValueError("webhook url too long")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("webhook url must be http or https")
    return url


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a persisted timestamp into an aware UTC datetime, or return None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso_after_hours(hours: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.replace(microsecond=0).isoformat()


def _default_github(workspace: str) -> Dict[str, str]:
    return {
        "workspace": workspace,
        "repo": "",
        "base_branch": DEFAULT_GITHUB_BASE_BRANCH,
        "token_env": DEFAULT_GITHUB_TOKEN_ENV,
    }


def _normalise_github(workspace: str, config: Any) -> Dict[str, str]:
    """Validate and normalise a stored GitHub connector config.

    Raises :class:`ValueError` for a config that cannot be stored. An empty
    ``repo`` is allowed and clears the config (returning the default shape), so
    the connector can be disabled without a separate flag. A non-empty ``repo``
    must be ``owner/name``; ``base_branch`` must match the shared branch guard
    and ``token_env`` must be a valid environment-variable name.
    """
    if not isinstance(config, dict):
        raise ValueError("invalid github config")
    repo = str(config.get("repo") or "").strip()
    base_branch = str(config.get("base_branch") or DEFAULT_GITHUB_BASE_BRANCH).strip()
    token_env = str(config.get("token_env") or DEFAULT_GITHUB_TOKEN_ENV).strip()
    if not repo:
        return _default_github(workspace)
    if not _GITHUB_REPO_RE.match(repo):
        raise ValueError("invalid github repo")
    if not _BASE_BRANCH_RE.match(base_branch):
        raise ValueError("invalid base_branch")
    if not _GITHUB_TOKEN_ENV_RE.match(token_env):
        raise ValueError("invalid token_env")
    return {
        "workspace": workspace,
        "repo": repo,
        "base_branch": base_branch,
        "token_env": token_env,
    }


def _default_schedule(workspace: str) -> Dict[str, Any]:
    return {
        "workspace": workspace,
        "interval_hours": 0,
        "enabled": False,
        "scan": {},
        "next_run_at": "",
    }


def _normalise_schedule(workspace: str, config: Any) -> Dict[str, Any]:
    """Validate and normalise a stored schedule config.

    Raises :class:`ValueError` for a config that cannot be stored: it must
    carry an integer ``interval_hours`` within ``MIN..MAX`` and a ``scan``
    object. A missing or malformed ``next_run_at`` is recomputed for an enabled
    schedule; a disabled schedule keeps an empty ``next_run_at``.
    """
    if not isinstance(config, dict):
        raise ValueError("invalid schedule config")
    interval = config.get("interval_hours")
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise ValueError("invalid interval_hours")
    if interval < MIN_SCHEDULE_HOURS or interval > MAX_SCHEDULE_HOURS:
        raise ValueError("interval_hours out of range")
    scan = config.get("scan")
    if not isinstance(scan, dict):
        raise ValueError("invalid scan")
    enabled = bool(config.get("enabled", True))
    next_run_at = config.get("next_run_at")
    if _parse_time(next_run_at) is None:
        next_run_at = _iso_after_hours(interval) if enabled else ""
    return {
        "workspace": workspace,
        "interval_hours": interval,
        "enabled": enabled,
        "scan": copy.deepcopy(scan),
        "next_run_at": next_run_at,
    }


def _normalise_retention(value: Any) -> Optional[int]:
    """Return a valid positive retention limit, or None when malformed/unset."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > MAX_RETENTION_SCANS:
        return None
    return value


def _normalise_history_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Validate a persisted history entry, returning None when malformed."""
    if not isinstance(entry, dict):
        return None
    revision = entry.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    action = entry.get("action")
    if action not in _RULE_HISTORY_ACTIONS:
        action = "set"
    created_at = entry.get("created_at")
    if not isinstance(created_at, str):
        created_at = ""
    rules = analyze._normalise_rules(entry.get("rules"), origin="workspace")
    rule_count = entry.get("rule_count")
    if not isinstance(rule_count, int) or isinstance(rule_count, bool) or rule_count < 0:
        rule_count = len(rules)
    return {
        "revision": revision,
        "created_at": created_at,
        "action": action,
        "rule_count": rule_count,
        "rules": rules,
    }


def _normalise_digest_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Validate a persisted digest-history entry, or return None when malformed.

    Missing or malformed scalar fields are replaced with safe defaults so a
    partially written store never discards the whole history. A non-dict entry
    is discarded outright.
    """
    if not isinstance(entry, dict):
        return None
    created_at = entry.get("created_at")
    if not isinstance(created_at, str):
        created_at = ""
    scan_id = entry.get("scan_id")
    if not isinstance(scan_id, str):
        scan_id = ""
    total_findings = entry.get("total_findings")
    if (
        isinstance(total_findings, bool)
        or not isinstance(total_findings, int)
        or total_findings < 0
    ):
        total_findings = 0
    by_severity = {"high": 0, "medium": 0, "low": 0}
    raw_severity = entry.get("by_severity")
    if isinstance(raw_severity, dict):
        for key in by_severity:
            value = raw_severity.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                by_severity[key] = value
    new_high_count = entry.get("new_high_count")
    if (
        isinstance(new_high_count, bool)
        or not isinstance(new_high_count, int)
        or new_high_count < 0
    ):
        new_high_count = 0
    delivery_error = entry.get("delivery_error")
    if not isinstance(delivery_error, str):
        delivery_error = ""
    attempts = entry.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        attempts = 0
    return {
        "created_at": created_at,
        "scan_id": scan_id,
        "total_findings": total_findings,
        "by_severity": by_severity,
        "new_high_count": new_high_count,
        "delivery_ok": bool(entry.get("delivery_ok", False)),
        "delivery_error": delivery_error,
        "attempts": attempts,
    }


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._scans: Dict[str, Dict[str, Any]] = {}
        self._workspace_rules: Dict[str, List[Dict[str, str]]] = {}
        self._workspace_rule_history: Dict[str, List[Dict[str, Any]]] = {}
        self._workspace_webhooks: Dict[str, str] = {}
        self._workspace_schedules: Dict[str, Dict[str, Any]] = {}
        self._workspace_github: Dict[str, Dict[str, str]] = {}
        self._workspace_retention: Dict[str, int] = {}
        self._workspace_digests: Dict[str, List[Dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        scans = data.get("scans")
        if isinstance(scans, dict):
            self._scans = scans
            for scan in self._scans.values():
                if isinstance(scan, dict):
                    details = scan.get("suppressed_details")
                    scan["suppressed_details"] = details if isinstance(details, list) else []
        workspace_rules = data.get("workspace_rules")
        if isinstance(workspace_rules, dict):
            loaded: Dict[str, List[Dict[str, str]]] = {}
            for workspace, rules in workspace_rules.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                loaded[workspace] = analyze._normalise_rules(rules, origin="workspace")
            self._workspace_rules = loaded
        history = data.get("workspace_rule_history")
        if isinstance(history, dict):
            loaded_history: Dict[str, List[Dict[str, Any]]] = {}
            for workspace, revisions in history.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                if not isinstance(revisions, list):
                    continue
                entries = []
                for entry in revisions:
                    normalised = _normalise_history_entry(entry)
                    if normalised is not None:
                        entries.append(normalised)
                entries.sort(key=lambda item: item["revision"])
                loaded_history[workspace] = entries
            self._workspace_rule_history = loaded_history
        webhooks = data.get("workspace_webhooks")
        if isinstance(webhooks, dict):
            loaded_webhooks: Dict[str, str] = {}
            for workspace, url in webhooks.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                try:
                    normalised = _validate_webhook_url(url)
                except ValueError:
                    continue
                if normalised:
                    loaded_webhooks[workspace] = normalised
            self._workspace_webhooks = loaded_webhooks
        schedules = data.get("workspace_schedules")
        if isinstance(schedules, dict):
            loaded_schedules: Dict[str, Dict[str, Any]] = {}
            for workspace, config in schedules.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                try:
                    normalised_schedule = _normalise_schedule(workspace, config)
                except ValueError:
                    continue
                loaded_schedules[workspace] = normalised_schedule
            self._workspace_schedules = loaded_schedules
        github = data.get("workspace_github")
        if isinstance(github, dict):
            loaded_github: Dict[str, Dict[str, str]] = {}
            for workspace, config in github.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                try:
                    normalised_github = _normalise_github(workspace, config)
                except ValueError:
                    continue
                if normalised_github.get("repo"):
                    loaded_github[workspace] = normalised_github
            self._workspace_github = loaded_github
        retention = data.get("workspace_retention")
        if isinstance(retention, dict):
            loaded_retention: Dict[str, int] = {}
            for workspace, limit in retention.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                normalised_limit = _normalise_retention(limit)
                if normalised_limit is not None:
                    loaded_retention[workspace] = normalised_limit
            self._workspace_retention = loaded_retention
        digests = data.get("workspace_digests")
        if isinstance(digests, dict):
            loaded_digests: Dict[str, List[Dict[str, Any]]] = {}
            for workspace, entries in digests.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                if not isinstance(entries, list):
                    continue
                normalised_entries = []
                for entry in entries:
                    normalised_entry = _normalise_digest_entry(entry)
                    if normalised_entry is not None:
                        normalised_entries.append(normalised_entry)
                if len(normalised_entries) > MAX_DIGEST_HISTORY:
                    normalised_entries = normalised_entries[-MAX_DIGEST_HISTORY:]
                loaded_digests[workspace] = normalised_entries
            self._workspace_digests = loaded_digests

    def _persist(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ddr-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "scans": self._scans,
                        "workspace_rules": self._workspace_rules,
                        "workspace_rule_history": self._workspace_rule_history,
                        "workspace_webhooks": self._workspace_webhooks,
                        "workspace_schedules": self._workspace_schedules,
                        "workspace_github": self._workspace_github,
                        "workspace_retention": self._workspace_retention,
                        "workspace_digests": self._workspace_digests,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _check_workspace(workspace: Any) -> str:
        if not _valid_workspace(workspace):
            raise ValueError("invalid workspace id")
        return str(workspace)

    def list_workspace_rules(self, workspace: str) -> List[Dict[str, str]]:
        name = self._check_workspace(workspace)
        with self._lock:
            rules = self._workspace_rules.get(name, [])
            return copy.deepcopy(rules)

    def _record_history(
        self, name: str, action: str, rules: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Append a revision for ``name`` and trim history to the cap."""
        entries = self._workspace_rule_history.setdefault(name, [])
        revision = entries[-1]["revision"] + 1 if entries else 1
        entry = {
            "revision": revision,
            "created_at": _utc_now(),
            "action": action if action in _RULE_HISTORY_ACTIONS else "set",
            "rule_count": len(rules),
            "rules": copy.deepcopy(rules),
        }
        entries.append(entry)
        if len(entries) > MAX_RULE_HISTORY:
            del entries[: len(entries) - MAX_RULE_HISTORY]
        return entry

    def set_workspace_rules(
        self, workspace: str, rules: Any, action: str = "set"
    ) -> List[Dict[str, str]]:
        name = self._check_workspace(workspace)
        normalised = analyze._normalise_rules(rules, origin="workspace")
        with self._lock:
            self._workspace_rules[name] = normalised
            self._record_history(name, action, normalised)
            self._persist()
            return copy.deepcopy(normalised)

    def delete_workspace_rules(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_rules
            self._workspace_rules.pop(name, None)
            self._record_history(name, "clear", [])
            self._persist()
            return existed

    def list_workspace_rule_history(self, workspace: str) -> List[Dict[str, Any]]:
        name = self._check_workspace(workspace)
        with self._lock:
            entries = self._workspace_rule_history.get(name, [])
            newest_first = sorted(
                entries, key=lambda item: item["revision"], reverse=True
            )
            return copy.deepcopy(newest_first)

    def promote_workspace_rule_revision(
        self, workspace: str, revision: int
    ) -> Optional[List[Dict[str, str]]]:
        name = self._check_workspace(workspace)
        with self._lock:
            for entry in self._workspace_rule_history.get(name, []):
                if entry["revision"] == revision:
                    normalised = analyze._normalise_rules(
                        entry["rules"], origin="workspace"
                    )
                    self._workspace_rules[name] = normalised
                    self._record_history(name, "promote", normalised)
                    self._persist()
                    return copy.deepcopy(normalised)
            return None

    def get_workspace_webhook(self, workspace: str) -> Dict[str, str]:
        name = self._check_workspace(workspace)
        with self._lock:
            return {"url": self._workspace_webhooks.get(name, "")}

    def set_workspace_webhook(self, workspace: str, url: Any) -> Dict[str, str]:
        name = self._check_workspace(workspace)
        normalised = _validate_webhook_url(url)
        with self._lock:
            if normalised:
                self._workspace_webhooks[name] = normalised
            else:
                self._workspace_webhooks.pop(name, None)
            self._persist()
            return {"url": normalised}

    def delete_workspace_webhook(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_webhooks
            self._workspace_webhooks.pop(name, None)
            self._persist()
            return existed

    def get_workspace_github(self, workspace: str) -> Dict[str, str]:
        name = self._check_workspace(workspace)
        with self._lock:
            config = self._workspace_github.get(name)
            if not config:
                return _default_github(name)
            return copy.deepcopy(config)

    def set_workspace_github(self, workspace: str, config: Any) -> Dict[str, str]:
        name = self._check_workspace(workspace)
        normalised = _normalise_github(name, config)
        with self._lock:
            if normalised.get("repo"):
                self._workspace_github[name] = normalised
            else:
                self._workspace_github.pop(name, None)
            self._persist()
            return copy.deepcopy(normalised)

    def delete_workspace_github(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_github
            self._workspace_github.pop(name, None)
            self._persist()
            return existed

    def get_workspace_retention(self, workspace: str) -> int:
        """Return the stored per-workspace scan limit, or ``0`` when unset."""
        name = self._check_workspace(workspace)
        with self._lock:
            return int(self._workspace_retention.get(name, 0))

    def set_workspace_retention(self, workspace: str, max_scans: Any) -> Dict[str, Any]:
        """Store the per-workspace retention limit; ``0`` clears it.

        Raises :class:`ValueError` for a non-integer or out-of-range value.
        """
        name = self._check_workspace(workspace)
        if isinstance(max_scans, bool) or not isinstance(max_scans, int):
            raise ValueError("invalid retention limit")
        if max_scans < 0 or max_scans > MAX_RETENTION_SCANS:
            raise ValueError("invalid retention limit")
        with self._lock:
            if max_scans == 0:
                self._workspace_retention.pop(name, None)
            else:
                self._workspace_retention[name] = max_scans
            self._persist()
            return {"workspace": name, "max_scans": max_scans}

    def delete_workspace_retention(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_retention
            self._workspace_retention.pop(name, None)
            self._persist()
            return existed

    def prune_workspace_scans(self, workspace: str, max_scans: Any) -> int:
        """Delete the oldest scans of ``workspace`` beyond ``max_scans``.

        Scans of other workspaces and scans without a workspace are never
        touched. A limit of ``0`` (the unset policy) is a no-op. Returns the
        number of deleted scans, so calling it again with nothing to prune
        returns ``0``.
        """
        name = self._check_workspace(workspace)
        if isinstance(max_scans, bool) or not isinstance(max_scans, int):
            raise ValueError("invalid retention limit")
        if max_scans < 0 or max_scans > MAX_RETENTION_SCANS:
            raise ValueError("invalid retention limit")
        if max_scans == 0:
            return 0
        with self._lock:
            relevant = [
                (str(scan.get("created_at", "")), index, scan_id)
                for index, (scan_id, scan) in enumerate(self._scans.items())
                if scan.get("workspace") == name
            ]
            if len(relevant) <= max_scans:
                return 0
            relevant.sort()
            to_delete = relevant[: len(relevant) - max_scans]
            for _, _, scan_id in to_delete:
                del self._scans[scan_id]
            self._persist()
            return len(to_delete)

    def get_workspace_schedule(self, workspace: str) -> Dict[str, Any]:
        """Return the stored schedule for ``workspace`` or a disabled default."""
        name = self._check_workspace(workspace)
        with self._lock:
            config = self._workspace_schedules.get(name)
            if not config:
                return _default_schedule(name)
            return copy.deepcopy(config)

    def set_workspace_schedule(self, workspace: str, config: Any) -> Dict[str, Any]:
        name = self._check_workspace(workspace)
        normalised = _normalise_schedule(name, config)
        with self._lock:
            self._workspace_schedules[name] = normalised
            self._persist()
            return copy.deepcopy(normalised)

    def delete_workspace_schedule(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_schedules
            self._workspace_schedules.pop(name, None)
            self._persist()
            return existed

    def list_workspace_schedules(self) -> List[Dict[str, Any]]:
        with self._lock:
            configs = [copy.deepcopy(config) for config in self._workspace_schedules.values()]
        configs.sort(key=lambda config: config.get("workspace", ""))
        return configs

    def record_workspace_digest(
        self, workspace: str, digest: Any, delivery: Any
    ) -> Dict[str, Any]:
        """Append a scheduled-scan digest to the workspace's bounded history.

        ``digest`` is the payload built by :func:`notify.build_digest` and
        ``delivery`` is the result of the webhook dispatch (or a synthetic
        failure when no webhook is configured). The stored entry keeps the
        audit-relevant summary: ``created_at``, ``scan_id``, ``total_findings``,
        ``by_severity``, ``new_high_count``, ``delivery_ok``,
        ``delivery_error`` and ``attempts`` (the number of webhook POSTs made).
        History is capped at :data:`MAX_DIGEST_HISTORY`
        entries per workspace, dropping the oldest first.
        """
        name = self._check_workspace(workspace)
        digest = digest if isinstance(digest, dict) else {}
        delivery = delivery if isinstance(delivery, dict) else {}
        raw_severity = digest.get("by_severity")
        by_severity = {"high": 0, "medium": 0, "low": 0}
        if isinstance(raw_severity, dict):
            for key in by_severity:
                value = raw_severity.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    by_severity[key] = value
        total_findings = digest.get("total_findings")
        if (
            isinstance(total_findings, bool)
            or not isinstance(total_findings, int)
            or total_findings < 0
        ):
            total_findings = 0
        new_high = digest.get("new_high")
        new_high_count = len(new_high) if isinstance(new_high, list) else 0
        delivery_error = delivery.get("error")
        if not isinstance(delivery_error, str):
            delivery_error = ""
        attempts = delivery.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        entry = {
            "created_at": _utc_now(),
            "scan_id": str(digest.get("scan_id") or ""),
            "total_findings": total_findings,
            "by_severity": by_severity,
            "new_high_count": new_high_count,
            "delivery_ok": bool(delivery.get("ok")),
            "delivery_error": delivery_error,
            "attempts": attempts,
        }
        with self._lock:
            entries = self._workspace_digests.setdefault(name, [])
            entries.append(entry)
            if len(entries) > MAX_DIGEST_HISTORY:
                del entries[: len(entries) - MAX_DIGEST_HISTORY]
            self._persist()
            return copy.deepcopy(entry)

    def list_workspace_digests(
        self, workspace: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Return the workspace's digest history, newest-first.

        ``limit`` bounds the number of returned entries; a non-positive or
        non-integer value returns the full (already bounded) history.
        """
        name = self._check_workspace(workspace)
        with self._lock:
            entries = self._workspace_digests.get(name, [])
            newest_first = list(reversed(entries))
            if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                newest_first = newest_first[:limit]
            return copy.deepcopy(newest_first)

    def list_due_schedules(self, now: Any) -> List[Dict[str, Any]]:
        """Return enabled schedules whose ``next_run_at`` is at or before ``now``."""
        moment = _parse_time(now)
        if moment is None:
            moment = datetime.now(timezone.utc)
        due: List[Dict[str, Any]] = []
        with self._lock:
            for config in self._workspace_schedules.values():
                if not config.get("enabled"):
                    continue
                when = _parse_time(config.get("next_run_at"))
                if when is None or when > moment:
                    continue
                due.append(copy.deepcopy(config))
        due.sort(key=lambda config: (config.get("next_run_at", ""), config.get("workspace", "")))
        return due

    def latest_workspace_scan(self, workspace: str) -> Optional[Dict[str, Any]]:
        """Return the most recent stored scan for ``workspace``, or None."""
        name = self._check_workspace(workspace)
        with self._lock:
            latest = None
            for scan in self._scans.values():
                if scan.get("workspace") != name:
                    continue
                if latest is None or scan.get("created_at", "") >= latest.get("created_at", ""):
                    latest = scan
            return copy.deepcopy(latest) if latest is not None else None

    def list_all_scans(self) -> List[Dict[str, Any]]:
        """Return every stored scan as a full deep copy, newest-first.

        Unlike :meth:`list_scans` (a summary used by ``GET /api/scans``) this
        keeps each scan's full document, so the cross-workspace roll-up can
        summarise findings exactly like the per-workspace trend. Ordering uses
        ``(created_at, insertion index)`` descending so scans created in the
        same second keep a deterministic newest-first order.
        """
        with self._lock:
            relevant = [
                (str(scan.get("created_at", "")), index, scan)
                for index, scan in enumerate(self._scans.values())
                if isinstance(scan, dict)
            ]
        relevant.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [copy.deepcopy(scan) for _, _, scan in relevant]

    def list_workspace_scans(self, workspace: str) -> List[Dict[str, Any]]:
        """Return the workspace's stored scans, newest-first.

        Only scans that carry a matching ``workspace`` are returned. Ordering
        uses ``(created_at, insertion index)`` descending so scans created in
        the same second keep a deterministic newest-first order.
        """
        name = self._check_workspace(workspace)
        return [
            scan
            for scan in self.list_all_scans()
            if scan.get("workspace") == name
        ]

    def create_scan(
        self,
        name: str,
        source: str,
        findings: List[Dict[str, Any]],
        pages: Optional[List[Any]] = None,
        suppressed: int = 0,
        workspace: Optional[str] = None,
        suppressed_rules: Optional[List[Dict[str, Any]]] = None,
        suppressed_details: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            scan_id = uuid.uuid4().hex[:12]
            scan = {
                "id": scan_id,
                "name": (name or "Untitled release").strip()[:200] or "Untitled release",
                "source": (source or "release").strip()[:200] or "release",
                "created_at": _utc_now(),
                "findings": findings,
                "stats": analyze.summarise(findings, pages, suppressed, suppressed_rules),
                "suppressed_details": copy.deepcopy(suppressed_details)
                if suppressed_details
                else [],
            }
            if workspace:
                scan["workspace"] = self._check_workspace(workspace)
            self._scans[scan_id] = scan
            self._persist()
            return copy.deepcopy(scan)

    def list_scans(self) -> List[Dict[str, Any]]:
        with self._lock:
            scans = sorted(self._scans.values(), key=lambda s: s.get("created_at", ""), reverse=True)
            return [
                {
                    "id": s["id"],
                    "name": s.get("name"),
                    "source": s.get("source"),
                    "created_at": s.get("created_at"),
                    "stats": s.get("stats", {}),
                }
                for s in scans
            ]

    def get_scan(self, scan_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            scan = self._scans.get(scan_id)
            return copy.deepcopy(scan) if scan else None

    def resolve_finding(self, scan_id: str, finding_id: str, resolved: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            scan = self._scans.get(scan_id)
            if not scan:
                return None
            updated = None
            for finding in scan.get("findings", []):
                if finding.get("id") == finding_id:
                    finding["resolved"] = bool(resolved)
                    updated = finding
                    break
            if updated is None:
                return None
            stats = scan.get("stats", {}) or {}
            scan["stats"] = analyze.summarise(
                scan.get("findings", []),
                suppressed=stats.get("suppressed", 0),
                suppressed_rules=stats.get("suppressed_rules", []),
            )
            self._persist()
            return copy.deepcopy(scan)

    def delete_scan(self, scan_id: str) -> bool:
        with self._lock:
            if scan_id not in self._scans:
                return False
            del self._scans[scan_id]
            self._persist()
            return True
