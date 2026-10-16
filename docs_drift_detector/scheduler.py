"""Single-process background scheduler for recurring drift scans.

The scheduler is deliberately small and dependency-free: it ticks on a fixed
interval, finds enabled schedules that are due, runs their stored scan payload
through the ordinary analysis path, stores the resulting scan and POSTs a
digest to the workspace webhook. A failing scan or delivery is logged to stderr
and never stops the loop or the other due schedules.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from . import notify, runner

DEFAULT_TICK_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _advance(next_run_at: Any, interval_hours: Any) -> str:
    """Return ``next_run_at`` moved forward by ``interval_hours``."""
    parsed = None
    if isinstance(next_run_at, str):
        text = next_run_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = _utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        hours = int(interval_hours)
    except (TypeError, ValueError):
        hours = 1
    return (parsed + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def default_scan_runner(store: Any, schedule: Dict[str, Any]) -> Dict[str, Any]:
    """Run the scan payload stored on ``schedule`` without the normal webhook.

    The scheduler sends the digest instead, so the ordinary per-scan webhook is
    suppressed to avoid two notifications for one scheduled scan.
    """
    data = dict(schedule.get("scan") or {})
    workspace = schedule.get("workspace")
    if workspace:
        data["workspace"] = workspace
    return runner.run_scan(store, data, workspace=workspace, dispatch=False)


class Scheduler:
    def __init__(
        self,
        store: Any,
        scan_runner: Optional[Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = None,
        tick_seconds: int = DEFAULT_TICK_SECONDS,
        enabled: bool = True,
    ):
        self.store = store
        self.scan_runner = scan_runner or default_scan_runner
        try:
            tick = int(tick_seconds)
        except (TypeError, ValueError):
            tick = DEFAULT_TICK_SECONDS
        self.tick_seconds = max(tick, 1)
        self.enabled = bool(enabled)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def run_once(self, now: Any = None) -> List[Dict[str, Any]]:
        """Run every due schedule once and return a per-schedule result report."""
        moment = now if now is not None else _utc_now()
        results: List[Dict[str, Any]] = []
        for schedule in self.store.list_due_schedules(moment):
            workspace = schedule.get("workspace")
            result: Dict[str, Any] = {"workspace": workspace, "ran": False}
            previous = None
            if workspace:
                try:
                    previous = self.store.latest_workspace_scan(workspace)
                except Exception:  # pragma: no cover - defensive
                    previous = None
            try:
                scan = self.scan_runner(self.store, schedule)
            except Exception as err:
                sys.stderr.write(
                    "docs-drift: scheduled scan failed (workspace=%s): %s\n"
                    % (workspace, err)
                )
                result["error"] = str(err) or err.__class__.__name__
                self._advance_schedule(schedule)
                results.append(result)
                continue
            result["ran"] = True
            result["scan_id"] = scan.get("id") if isinstance(scan, dict) else None
            result["digest_sent"] = self._dispatch_digest(workspace, scan, previous)
            self._advance_schedule(schedule)
            results.append(result)
        return results

    def _advance_schedule(self, schedule: Dict[str, Any]) -> None:
        workspace = schedule.get("workspace")
        if not workspace:
            return
        try:
            schedule["next_run_at"] = _advance(
                schedule.get("next_run_at"), schedule.get("interval_hours")
            )
            self.store.set_workspace_schedule(workspace, schedule)
        except Exception as err:
            sys.stderr.write(
                "docs-drift: failed to advance schedule (workspace=%s): %s\n"
                % (workspace, err)
            )

    def _dispatch_digest(
        self, workspace: Optional[str], scan: Any, previous: Any
    ) -> bool:
        if not workspace or not isinstance(scan, dict):
            return False
        try:
            digest = notify.build_digest(scan, previous)
        except Exception as err:  # never let digest building break the loop
            sys.stderr.write("docs-drift: schedule digest error: %s\n" % err)
            return False
        delivery: Dict[str, Any] = {
            "ok": False,
            "status": None,
            "error": "no webhook configured",
            "attempts": 0,
        }
        sent = False
        try:
            config = self.store.get_workspace_webhook(workspace)
            url = config.get("url") or ""
            if url:
                delivery = notify.dispatch_with_retry(url, digest)
                sent = bool(delivery.get("ok"))
                if not delivery.get("ok"):
                    sys.stderr.write(
                        "docs-drift: schedule digest delivery failed "
                        "(workspace=%s, url=%s): %s\n"
                        % (workspace, url, delivery.get("error") or delivery.get("status"))
                    )
        except Exception as err:  # never let delivery break the loop
            sys.stderr.write("docs-drift: schedule digest error: %s\n" % err)
            delivery = {
                "ok": False,
                "status": None,
                "error": str(err) or err.__class__.__name__,
                "attempts": 0,
            }
        self._record_digest(workspace, digest, delivery)
        return sent

    def _record_digest(
        self, workspace: str, digest: Dict[str, Any], delivery: Dict[str, Any]
    ) -> None:
        """Best-effort persistence of a digest attempt for later auditing."""
        try:
            self.store.record_workspace_digest(workspace, digest, delivery)
        except Exception as err:  # recording must never break the loop
            sys.stderr.write(
                "docs-drift: failed to record digest (workspace=%s): %s\n"
                % (workspace, err)
            )

    def start(self) -> threading.Thread:
        if self._thread is not None:
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ddr-scheduler", daemon=True
        )
        self._thread.start()
        return self._thread

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            if not self.enabled:
                continue
            try:
                self.run_once()
            except Exception as err:  # pragma: no cover - defensive
                sys.stderr.write("docs-drift: scheduler tick failed: %s\n" % err)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
