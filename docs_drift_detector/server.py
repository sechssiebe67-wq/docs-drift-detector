"""Dependency-free HTTP server and JSON API for Docs Drift Detector."""

from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import analyze, export
from .store import Store

MAX_BODY = 8 * 1024 * 1024
MAX_PAGES = 2000
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class DriftHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store: Store):
        super().__init__(address, handler)
        self.store = store


def _split(path: str) -> List[str]:
    return [part for part in path.split("/") if part]


def _valid_id(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_-]{1,64}$", value or ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "DocsDriftDetector/0.1"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # -- static files -----------------------------------------------------
    def _serve_static(self, relative: str) -> None:
        safe = os.path.normpath("/" + relative).lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, safe))
        root = os.path.normpath(STATIC_DIR)
        if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
            return self._error(404, "not found")
        ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as handle:
            self._send(200, handle.read(), ctype)

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        parts = _split(path)
        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if parts and parts[0] == "static":
            return self._serve_static("/".join(parts[1:]) or "index.html")
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "rules":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            return self._json(200, {"rules": self.server.store.list_workspace_rules(parts[2])})
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "rules"
            and parts[4] == "export"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            document = {
                "version": 1,
                "workspace": parts[2],
                "rules": self.server.store.list_workspace_rules(parts[2]),
            }
            body = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition", 'attachment; filename="workspace-%s-rules.json"' % parts[2]
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "rules"
            and parts[4] == "history"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            history = self.server.store.list_workspace_rule_history(parts[2])
            return self._json(200, {"workspace": parts[2], "history": history})
        if parts == ["api", "scans"]:
            return self._json(200, {"scans": self.server.store.list_scans()})
        if len(parts) == 3 and parts[:2] == ["api", "scans"] and _valid_id(parts[2]):
            scan = self.server.store.get_scan(parts[2])
            if scan is None:
                return self._error(404, "scan not found")
            return self._json(200, scan)
        if (
            len(parts) == 4
            and parts[:2] == ["api", "scans"]
            and _valid_id(parts[2])
            and parts[3] == "export.md"
        ):
            scan = self.server.store.get_scan(parts[2])
            if scan is None:
                return self._error(404, "scan not found")
            body = export.to_markdown(scan).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="docs-drift-%s.md"' % scan["id"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return self._error(404, "not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        parts = _split(path)

        if (
            len(parts) == 7
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "rules"
            and parts[4] == "history"
            and parts[6] == "promote"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            try:
                revision = int(parts[5])
            except (TypeError, ValueError):
                return self._error(400, "invalid revision")
            if revision < 1:
                return self._error(400, "invalid revision")
            restored = self.server.store.promote_workspace_rule_revision(parts[2], revision)
            if restored is None:
                return self._error(404, "revision not found")
            return self._json(200, {"rules": restored, "promoted": revision})

        data = self._read_json()
        if data is None:
            return self._error(400, "invalid or missing JSON body")

        if parts == ["api", "analyze"]:
            return self._analyze(data, store=False)
        if parts == ["api", "scans"]:
            return self._analyze(data, store=True)
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "rules"
            and parts[4] == "import"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            rules = data.get("rules")
            if not isinstance(rules, list):
                return self._error(400, "invalid rules document")
            stored = self.server.store.set_workspace_rules(parts[2], rules, action="import")
            return self._json(200, {"rules": stored, "imported": len(stored)})
        if (
            len(parts) == 6
            and parts[:2] == ["api", "scans"]
            and _valid_id(parts[2])
            and parts[3] == "findings"
            and _valid_id(parts[4])
            and parts[5] == "resolve"
        ):
            return self._resolve(parts[2], parts[4], data)
        return self._error(404, "not found")

    def do_PATCH(self) -> None:  # noqa: N802
        self.do_POST()

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        parts = _split(path)
        data = self._read_json()
        if data is None:
            return self._error(400, "invalid or missing JSON body")
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "rules":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            rules = self.server.store.set_workspace_rules(parts[2], data.get("rules"))
            return self._json(200, {"rules": rules})
        return self._error(404, "not found")

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        parts = _split(path)
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "rules":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_rules(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if len(parts) == 3 and parts[:2] == ["api", "scans"] and _valid_id(parts[2]):
            if self.server.store.delete_scan(parts[2]):
                return self._json(200, {"deleted": parts[2]})
            return self._error(404, "scan not found")
        return self._error(404, "not found")

    # -- handlers ---------------------------------------------------------
    def _prepare(
        self, data: Dict[str, Any], workspace: Optional[str] = None
    ) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, Any]], Dict[str, Any]]:
        pages = self._validate_pages(data.get("pages") or data.get("docs") or [])
        changelog = data.get("changelog") or ""
        diff = data.get("diff") or ""
        text = changelog + "\n" + diff
        changes = analyze.extract_changes(
            text, labels=data.get("labels"), routes=data.get("routes")
        )
        screenshots = data.get("screenshots") or []
        metrics = data.get("metrics") or {}
        report: Dict[str, Any] = {}
        findings = analyze.detect_drift(
            changes,
            pages=pages,
            screenshots=screenshots,
            metrics=metrics,
            rules=self._merge_rules(data.get("rules"), workspace),
            stats=report,
        )
        suppressed = report.get("suppressed", 0)
        suppressed_rules = report.get("suppressed_rules", [])
        return changes, findings, {
            "pages": pages,
            "metrics": metrics,
            "suppressed": suppressed,
            "suppressed_rules": suppressed_rules,
        }

    def _merge_rules(self, request_rules: Any, workspace: Optional[str]) -> List[Dict[str, str]]:
        """Combine per-scan rules with the workspace library.

        Per-scan rules are normalised first and tagged with ``origin=request``;
        stored workspace rules are tagged with ``origin=workspace``. Both sets
        are applied so that their suppressions are additive and the provenance
        of each suppression is preserved.
        """
        requested = analyze._normalise_rules(request_rules, origin="request")
        stored = analyze._normalise_rules(
            self.server.store.list_workspace_rules(workspace) if workspace else [],
            origin="workspace",
        )
        return requested + stored

    @staticmethod
    def _validate_pages(raw: Any) -> List[Dict[str, str]]:
        if not isinstance(raw, list):
            return []
        pages = []
        for item in raw[:MAX_PAGES]:
            if isinstance(item, dict):
                path = str(item.get("path") or item.get("url") or "(unnamed)")
                content = item.get("content")
                if content is None:
                    content = item.get("text") or ""
                pages.append({"path": path, "content": str(content)})
            elif isinstance(item, str):
                pages.append({"path": "(unnamed)", "content": item})
        return pages

    def _analyze(self, data: Dict[str, Any], store: bool) -> None:
        workspace_raw = data.get("workspace")
        workspace: Optional[str] = None
        if workspace_raw:
            workspace = str(workspace_raw).strip()
            if not _valid_id(workspace):
                return self._error(400, "invalid workspace id")
        changes, findings, extra = self._prepare(data, workspace)
        if not store:
            return self._json(
                200,
                {
                    "findings": findings,
                    "stats": analyze.summarise(
                        findings,
                        extra["pages"],
                        extra["suppressed"],
                        extra["suppressed_rules"],
                    ),
                    "changes": changes,
                },
            )
        scan = self.server.store.create_scan(
            name=data.get("name") or "Release scan",
            source=data.get("source") or "release",
            findings=findings,
            pages=extra["pages"],
            suppressed=extra["suppressed"],
            workspace=workspace,
            suppressed_rules=extra["suppressed_rules"],
        )
        return self._json(201, scan)

    def _resolve(self, scan_id: str, finding_id: str, data: Dict[str, Any]) -> None:
        resolved = bool(data.get("resolved", True))
        scan = self.server.store.resolve_finding(scan_id, finding_id, resolved)
        if scan is None:
            return self._error(404, "scan or finding not found")
        return self._json(200, scan)


def create_server(host: str, port: int, store: Store) -> DriftHTTPServer:
    return DriftHTTPServer((host, port), Handler, store)
