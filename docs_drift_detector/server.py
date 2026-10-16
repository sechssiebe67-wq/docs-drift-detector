"""Dependency-free HTTP server and JSON API for Docs Drift Detector."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import (
    analyze,
    buckets,
    csv_export,
    export,
    github,
    pagination,
    pullrequest,
    rollup,
    runner,
    timewindow,
    trend,
)
from .store import (
    DEFAULT_GITHUB_TOKEN_ENV,
    MAX_DIGEST_HISTORY,
    MAX_RETENTION_SCANS,
    MAX_SCHEDULE_HOURS,
    MIN_SCHEDULE_HOURS,
    Store,
)

MAX_BODY = 8 * 1024 * 1024
MAX_PAGES = 2000
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _scheduler_enabled_from_env() -> bool:
    raw = os.environ.get("DDR_SCHEDULER_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _scheduler_tick_from_env() -> int:
    try:
        value = int(os.environ.get("DDR_SCHEDULER_TICK", "60"))
    except (TypeError, ValueError):
        value = 60
    return value if value > 0 else 60

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


def _valid_branch(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9._/-]{1,200}$", value or ""))


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

    def _csv(self, filename: str, text: str) -> None:
        """Stream a UTF-8 CSV attachment with the given download filename."""
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", 'attachment; filename="%s"' % filename
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _jsonl(self, filename: str, text: str) -> None:
        """Stream a UTF-8 newline-delimited JSON attachment as a download."""
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header(
            "Content-Disposition", 'attachment; filename="%s"' % filename
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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

    def _branch_options(self) -> Tuple[Optional[str], Optional[str]]:
        """Parse and validate the optional ``base``/``prefix`` query values.

        Returns ``(None, None)`` when either value fails the same
        ``[A-Za-z0-9._/-]`` guard used for other path segments.
        """
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        base_branch = query.get("base", [pullrequest.DEFAULT_BASE_BRANCH])[0]
        branch_prefix = query.get("prefix", [pullrequest.DEFAULT_BRANCH_PREFIX])[0]
        if not _valid_branch(base_branch) or not _valid_branch(branch_prefix):
            return None, None
        return base_branch, branch_prefix

    def _bucket_options(self) -> Tuple[Optional[str], Optional[int]]:
        """Parse and validate the optional ``bucket``/``limit`` query values.

        Returns ``(None, None)`` when ``bucket`` is not ``day``/``week`` or
        ``limit`` is present but not an integer, so the caller can answer HTTP
        400. A missing ``bucket`` defaults to ``week`` and a missing ``limit``
        defaults to :data:`docs_drift_detector.trend.DEFAULT_LIMIT`.
        """
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        bucket = query.get("bucket", [buckets.DEFAULT_BUCKET])[0]
        if not buckets.is_valid_bucket(bucket):
            return None, None
        limit = trend.DEFAULT_LIMIT
        if "limit" in query:
            try:
                limit = int(query["limit"][0])
            except (TypeError, ValueError):
                return None, None
        return bucket, limit

    def _cursor_option(self) -> Optional[str]:
        """Parse the optional opaque ``cursor`` query value.

        Returns the cursor string, or ``None`` when it is absent or blank (a
        blank cursor is treated as absent, matching the ``from``/``to``
        handling). Whether the cursor is *well-formed* is validated by the
        pagination helper, so a malformed value surfaces as HTTP 400 at the
        call site.
        """
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        if "cursor" not in query:
            return None
        value = query["cursor"][0]
        if not isinstance(value, str) or not value.strip():
            return None
        return value

    def _window_options(
        self,
    ) -> Optional[Tuple[Optional[datetime], Optional[datetime]]]:
        """Parse the optional ``from``/``to`` query values.

        Returns ``(start, end)`` where either side may be ``None`` (unbounded),
        or ``None`` when a present value is malformed so the caller can answer
        HTTP 400. A plain date is midnight UTC, a full ISO-8601 timestamp keeps
        its offset, and the window is inclusive ``[start, end]``.
        """
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        try:
            return timewindow.parse_window(
                query.get("from", [None])[0], query.get("to", [None])[0]
            )
        except timewindow.WindowError:
            return None

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
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "webhook":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            config = self.server.store.get_workspace_webhook(parts[2])
            return self._json(200, {"workspace": parts[2], "url": config.get("url", "")})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "schedule":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            return self._json(200, self.server.store.get_workspace_schedule(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "github":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            return self._json(200, self.server.store.get_workspace_github(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "retention":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            limit = self.server.store.get_workspace_retention(parts[2])
            return self._json(200, {"workspace": parts[2], "max_scans": limit})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "digests":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query, keep_blank_values=True
            )
            limit = 10
            if "limit" in query:
                try:
                    limit = int(query["limit"][0])
                except (TypeError, ValueError):
                    return self._error(400, "invalid limit")
            if limit < 1:
                limit = 1
            elif limit > MAX_DIGEST_HISTORY:
                limit = MAX_DIGEST_HISTORY
            digests = self.server.store.list_workspace_digests(parts[2], limit)
            return self._json(200, {"workspace": parts[2], "digests": digests})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "trend":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query, keep_blank_values=True
            )
            limit = trend.DEFAULT_LIMIT
            if "limit" in query:
                try:
                    limit = int(query["limit"][0])
                except (TypeError, ValueError):
                    return self._error(400, "invalid limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            try:
                document = trend.build_trend(
                    self.server.store,
                    parts[2],
                    limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._json(200, document)
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "trend"
            and parts[4] == "buckets"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_workspace_scans(parts[2])
            try:
                document = buckets.build_workspace_buckets(
                    scans,
                    parts[2],
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._json(200, document)
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "trend"
            and parts[4] == "buckets.csv"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_workspace_scans(parts[2])
            try:
                document = buckets.build_workspace_buckets(
                    scans,
                    parts[2],
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._csv(
                "docs-drift-%s-trend-buckets.csv" % parts[2],
                csv_export.to_csv(document, "workspace"),
            )
        if (
            len(parts) == 5
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "trend"
            and parts[4] == "buckets.jsonl"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_workspace_scans(parts[2])
            try:
                document = buckets.build_workspace_buckets(
                    scans,
                    parts[2],
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._jsonl(
                "docs-drift-%s-trend-buckets.jsonl" % parts[2],
                csv_export.to_jsonl(document, "workspace"),
            )
        if parts == ["api", "schedule", "status"]:
            return self._schedule_status()
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
        if parts == ["api", "trend", "buckets.csv"]:
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_all_scans()
            try:
                document = buckets.build_rollup_buckets(
                    scans,
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._csv(
                "docs-drift-trend-buckets.csv",
                csv_export.to_csv(document, "rollup"),
            )
        if parts == ["api", "trend", "buckets.jsonl"]:
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_all_scans()
            try:
                document = buckets.build_rollup_buckets(
                    scans,
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._jsonl(
                "docs-drift-trend-buckets.jsonl",
                csv_export.to_jsonl(document, "rollup"),
            )
        if parts == ["api", "trend", "buckets"]:
            bucket, limit = self._bucket_options()
            if bucket is None or limit is None:
                return self._error(400, "invalid bucket or limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_all_scans()
            try:
                document = buckets.build_rollup_buckets(
                    scans,
                    bucket=bucket,
                    limit=limit,
                    from_time=start,
                    to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._json(200, document)
        if parts == ["api", "trend"]:
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query, keep_blank_values=True
            )
            limit = rollup.DEFAULT_LIMIT
            if "limit" in query:
                try:
                    limit = int(query["limit"][0])
                except (TypeError, ValueError):
                    return self._error(400, "invalid limit")
            window = self._window_options()
            if window is None:
                return self._error(400, "invalid from or to")
            start, end = window
            scans = self.server.store.list_all_scans()
            try:
                document = rollup.build_rollup(
                    scans, limit, from_time=start, to_time=end,
                    cursor=self._cursor_option(),
                )
            except pagination.CursorError:
                return self._error(400, "invalid cursor")
            return self._json(200, document)
        if parts == ["api", "scans"]:
            return self._json(200, {"scans": self.server.store.list_scans()})
        if len(parts) == 4 and parts[:2] == ["api", "scans"] and parts[3] == "suppressed":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid scan id")
            scan = self.server.store.get_scan(parts[2])
            if scan is None:
                return self._error(404, "scan not found")
            details = scan.get("suppressed_details") or []
            rules = []
            for entry in details:
                if not isinstance(entry, dict):
                    continue
                findings = entry.get("findings")
                if not isinstance(findings, list):
                    findings = []
                rules.append(
                    {
                        "rule_id": entry.get("rule_id"),
                        "origin": entry.get("origin"),
                        "kind": entry.get("kind"),
                        "page": entry.get("page"),
                        "value": entry.get("value"),
                        "count": len(findings),
                        "findings": findings,
                    }
                )
            stats = scan.get("stats") or {}
            return self._json(
                200,
                {
                    "scan": scan["id"],
                    "suppressed": stats.get("suppressed", 0),
                    "rules": rules,
                },
            )
        if len(parts) == 4 and parts[:2] == ["api", "scans"] and parts[3] == "pull-request":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid scan id")
            scan = self.server.store.get_scan(parts[2])
            if scan is None:
                return self._error(404, "scan not found")
            base_branch, branch_prefix = self._branch_options()
            if base_branch is None or branch_prefix is None:
                return self._error(400, "invalid base or prefix")
            document = pullrequest.build_suggestion(
                scan, base_branch=base_branch, branch_prefix=branch_prefix
            )
            return self._json(200, document)
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
        if len(parts) > 4 and parts[:2] == ["api", "scans"] and parts[-1] == "suppressed":
            return self._error(400, "invalid scan id")
        if len(parts) > 4 and parts[:2] == ["api", "scans"] and parts[-1] == "pull-request":
            return self._error(400, "invalid scan id")
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

        if (
            len(parts) == 5
            and parts[:2] == ["api", "scans"]
            and parts[3] == "pull-request"
            and parts[4] == "open"
        ):
            if not _valid_id(parts[2]):
                return self._error(400, "invalid scan id")
            scan = self.server.store.get_scan(parts[2])
            if scan is None:
                return self._error(404, "scan not found")
            return self._open_pull_request(scan)

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
        schedule_route = (
            len(parts) == 4
            and parts[:2] == ["api", "workspaces"]
            and parts[3] == "schedule"
        )
        if data is None:
            if schedule_route:
                data = {}
            else:
                return self._error(400, "invalid or missing JSON body")
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "rules":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            rules = self.server.store.set_workspace_rules(parts[2], data.get("rules"))
            return self._json(200, {"rules": rules})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "webhook":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            if "url" not in data:
                return self._error(400, "invalid webhook url")
            try:
                stored = self.server.store.set_workspace_webhook(parts[2], data.get("url"))
            except ValueError:
                return self._error(400, "invalid webhook url")
            return self._json(200, {"workspace": parts[2], "url": stored.get("url", "")})
        if schedule_route:
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            return self._put_schedule(parts[2], data)
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "github":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            if "repo" not in data:
                return self._error(400, "invalid github config")
            try:
                stored = self.server.store.set_workspace_github(parts[2], data)
            except ValueError:
                return self._error(400, "invalid github config")
            return self._json(200, stored)
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "retention":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            if "max_scans" not in data:
                return self._error(400, "invalid max_scans")
            try:
                stored = self.server.store.set_workspace_retention(
                    parts[2], data.get("max_scans")
                )
            except ValueError:
                return self._error(
                    400, "max_scans must be an integer in 0..%d" % MAX_RETENTION_SCANS
                )
            return self._json(200, stored)
        return self._error(404, "not found")

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        parts = _split(path)
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "rules":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_rules(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "webhook":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_webhook(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "schedule":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_schedule(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "github":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_github(parts[2])
            return self._json(200, {"deleted": parts[2]})
        if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "retention":
            if not _valid_id(parts[2]):
                return self._error(400, "invalid workspace id")
            self.server.store.delete_workspace_retention(parts[2])
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
        return runner.prepare_scan(self.server.store, data, workspace)

    def _merge_rules(self, request_rules: Any, workspace: Optional[str]) -> List[Dict[str, str]]:
        """Combine per-scan rules with the workspace library."""
        return runner.merge_rules(self.server.store, request_rules, workspace)

    @staticmethod
    def _validate_pages(raw: Any) -> List[Dict[str, str]]:
        return runner.validate_pages(raw)

    def _notify_webhook(self, scan: Dict[str, Any]) -> None:
        runner.notify_webhook(self.server.store, scan)

    def _analyze(self, data: Dict[str, Any], store: bool) -> None:
        workspace_raw = data.get("workspace")
        workspace: Optional[str] = None
        if workspace_raw:
            workspace = str(workspace_raw).strip()
            if not _valid_id(workspace):
                return self._error(400, "invalid workspace id")
        if not store:
            changes, findings, extra = runner.prepare_scan(
                self.server.store, data, workspace
            )
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
        scan = runner.run_scan(self.server.store, data, workspace)
        return self._json(201, scan)

    def _put_schedule(self, workspace: str, data: Dict[str, Any]) -> None:
        keys = set(data.keys()) if isinstance(data, dict) else set()
        existing = self.server.store.get_workspace_schedule(workspace)
        if not keys or keys <= {"enabled"}:
            enabled = bool(data.get("enabled", False)) if isinstance(data, dict) else False
            if existing.get("interval_hours"):
                existing["enabled"] = enabled
                stored = self.server.store.set_workspace_schedule(workspace, existing)
            else:
                stored = existing
                stored["enabled"] = enabled
            return self._json(200, stored)

        interval = data.get("interval_hours")
        if isinstance(interval, bool) or not isinstance(interval, int):
            return self._error(400, "invalid interval_hours")
        if interval < MIN_SCHEDULE_HOURS or interval > MAX_SCHEDULE_HOURS:
            return self._error(400, "invalid interval_hours")
        scan = data.get("scan")
        if not isinstance(scan, dict):
            return self._error(400, "invalid scan")
        config = {
            "interval_hours": interval,
            "enabled": bool(data.get("enabled", True)),
            "scan": scan,
        }
        try:
            stored = self.server.store.set_workspace_schedule(workspace, config)
        except ValueError:
            return self._error(400, "invalid schedule")
        return self._json(200, stored)

    def _schedule_status(self) -> None:
        scheduler = getattr(self.server, "scheduler", None)
        enabled = (
            bool(scheduler.enabled) if scheduler is not None
            else _scheduler_enabled_from_env()
        )
        tick = (
            scheduler.tick_seconds if scheduler is not None
            else _scheduler_tick_from_env()
        )
        now = datetime.now(timezone.utc)
        due = self.server.store.list_due_schedules(now)
        due_summary = [
            {
                "workspace": item.get("workspace"),
                "next_run_at": item.get("next_run_at"),
                "interval_hours": item.get("interval_hours"),
            }
            for item in due
        ]
        return self._json(
            200,
            {
                "enabled": bool(enabled),
                "tick_seconds": tick,
                "due": due_summary,
                "schedules": self.server.store.list_workspace_schedules(),
            },
        )

    def _resolve(self, scan_id: str, finding_id: str, data: Dict[str, Any]) -> None:
        resolved = bool(data.get("resolved", True))
        scan = self.server.store.resolve_finding(scan_id, finding_id, resolved)
        if scan is None:
            return self._error(404, "scan or finding not found")
        return self._json(200, scan)

    def _open_pull_request(self, scan: Dict[str, Any]) -> None:
        """Open a real GitHub pull request for ``scan`` via the stored config.

        The scan must belong to a workspace with a stored GitHub config whose
        ``repo`` is set; the configured ``token_env`` must be present in the
        process environment. Those local prerequisites are client errors (HTTP
        400). Once the connector runs, any upstream failure is reported as HTTP
        200 with ``{"ok": false, ...}`` so the connector result is always
        returned and a scan is never mutated.
        """
        workspace = scan.get("workspace")
        if not workspace:
            return self._error(400, "github not configured")
        config = self.server.store.get_workspace_github(workspace)
        repo = str(config.get("repo") or "")
        if not repo:
            return self._error(400, "github not configured")
        token_env = str(config.get("token_env") or DEFAULT_GITHUB_TOKEN_ENV)
        if not os.environ.get(token_env):
            return self._error(400, "token env %s is not set" % token_env)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True
        )
        branch_prefix = query.get("prefix", [pullrequest.DEFAULT_BRANCH_PREFIX])[0]
        if not _valid_branch(branch_prefix):
            return self._error(400, "invalid prefix")
        base_branch = str(
            config.get("base_branch") or pullrequest.DEFAULT_BASE_BRANCH
        )
        suggestion = pullrequest.build_suggestion(
            scan, base_branch=base_branch, branch_prefix=branch_prefix
        )
        return self._json(200, github.open_pull_request(config, suggestion))


def create_server(host: str, port: int, store: Store) -> DriftHTTPServer:
    return DriftHTTPServer((host, port), Handler, store)
