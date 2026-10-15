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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import analyze

_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _valid_workspace(workspace: Any) -> bool:
    return bool(_WORKSPACE_RE.match(str(workspace or "")))


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._scans: Dict[str, Dict[str, Any]] = {}
        self._workspace_rules: Dict[str, List[Dict[str, str]]] = {}
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
        workspace_rules = data.get("workspace_rules")
        if isinstance(workspace_rules, dict):
            loaded: Dict[str, List[Dict[str, str]]] = {}
            for workspace, rules in workspace_rules.items():
                if not isinstance(workspace, str) or not _valid_workspace(workspace):
                    continue
                loaded[workspace] = analyze._normalise_rules(rules, origin="workspace")
            self._workspace_rules = loaded

    def _persist(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ddr-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"scans": self._scans, "workspace_rules": self._workspace_rules},
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

    def set_workspace_rules(self, workspace: str, rules: Any) -> List[Dict[str, str]]:
        name = self._check_workspace(workspace)
        normalised = analyze._normalise_rules(rules, origin="workspace")
        with self._lock:
            self._workspace_rules[name] = normalised
            self._persist()
            return copy.deepcopy(normalised)

    def delete_workspace_rules(self, workspace: str) -> bool:
        name = self._check_workspace(workspace)
        with self._lock:
            existed = name in self._workspace_rules
            if existed:
                del self._workspace_rules[name]
                self._persist()
            return existed

    def create_scan(
        self,
        name: str,
        source: str,
        findings: List[Dict[str, Any]],
        pages: Optional[List[Any]] = None,
        suppressed: int = 0,
        workspace: Optional[str] = None,
        suppressed_rules: Optional[List[Dict[str, Any]]] = None,
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
