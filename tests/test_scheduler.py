import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docs_drift_detector import notify  # noqa: E402
from docs_drift_detector.scheduler import Scheduler, _advance  # noqa: E402
from docs_drift_detector.store import Store  # noqa: E402

PAST = "2020-01-01T00:00:00+00:00"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SCAN = {
    "name": "Nightly drift",
    "changelog": 'renamed "Save" to "Save changes"',
    "pages": [{"path": "help/a.md", "content": "Click Save."}],
}


class AdvanceTest(unittest.TestCase):
    def test_advance_adds_interval(self):
        self.assertEqual(
            _advance("2020-01-01T00:00:00+00:00", 2), "2020-01-01T02:00:00+00:00"
        )

    def test_advance_handles_z_suffix_and_bad_value(self):
        self.assertEqual(
            _advance("2020-01-01T00:00:00Z", 1), "2020-01-01T01:00:00+00:00"
        )
        self.assertTrue(_advance("not-a-time", 1))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "scans.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def _schedule(self, workspace, interval=1, enabled=True, next_run_at=PAST, scan=None):
        return self.store.set_workspace_schedule(
            workspace,
            {
                "interval_hours": interval,
                "enabled": enabled,
                "scan": scan if scan is not None else dict(SCAN),
                "next_run_at": next_run_at,
            },
        )

    def test_run_once_runs_due_schedule_and_advances(self):
        self._schedule("ws1", interval=2)
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        captured = {}
        original = notify.dispatch

        def fake_dispatch(url, payload, timeout=5):
            captured["url"] = url
            captured["payload"] = payload
            return {"ok": True, "status": 200, "error": ""}

        notify.dispatch = fake_dispatch
        try:
            results = Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ran"])
        self.assertTrue(results[0]["scan_id"])
        scans = self.store.list_scans()
        self.assertEqual(len(scans), 1)
        self.assertEqual(captured["url"], "https://hooks.example.com/x")
        digest = captured["payload"]
        self.assertEqual(digest["workspace"], "ws1")
        self.assertEqual(digest["total_findings"], 1)
        self.assertEqual(digest["by_severity"]["medium"], 1)
        self.assertEqual(digest["new_high"], [])
        advanced = self.store.get_workspace_schedule("ws1")
        self.assertEqual(advanced["next_run_at"], "2020-01-01T02:00:00+00:00")

    def test_run_once_skips_disabled_and_future(self):
        self._schedule("wsOff", enabled=False)
        self._schedule("wsFuture", next_run_at="2999-01-01T00:00:00+00:00")
        called = []
        scheduler = Scheduler(
            self.store,
            scan_runner=lambda store, schedule: called.append(schedule["workspace"]),
        )
        results = scheduler.run_once(NOW)
        self.assertEqual(results, [])
        self.assertEqual(called, [])

    def test_failing_webhook_keeps_scan_and_advances(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch

        def boom(url, payload, timeout=5):
            raise RuntimeError("webhook exploded")

        notify.dispatch = boom
        try:
            results = Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        self.assertEqual(len(self.store.list_scans()), 1)
        self.assertFalse(results[0]["digest_sent"])
        self.assertEqual(
            self.store.get_workspace_schedule("ws1")["next_run_at"],
            "2020-01-01T01:00:00+00:00",
        )

    def test_non_2xx_webhook_does_not_raise(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch

        notify.dispatch = lambda url, payload, timeout=5: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
        }
        try:
            Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        self.assertEqual(len(self.store.list_scans()), 1)

    def test_run_once_records_digest_entry(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch
        notify.dispatch = lambda url, payload, timeout=5: {
            "ok": True,
            "status": 200,
            "error": "",
        }
        try:
            Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        history = self.store.list_workspace_digests("ws1")
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertTrue(entry["scan_id"])
        self.assertEqual(entry["total_findings"], 1)
        self.assertEqual(entry["by_severity"], {"high": 0, "medium": 1, "low": 0})
        self.assertEqual(entry["new_high_count"], 0)
        self.assertTrue(entry["delivery_ok"])
        self.assertEqual(entry["delivery_error"], "")
        self.assertEqual(entry["attempts"], 1)

    def test_failing_webhook_records_attempts_after_retries(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch
        notify.dispatch = lambda url, payload, timeout=5: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
        }
        try:
            with mock.patch.dict(
                os.environ,
                {"DDR_WEBHOOK_RETRIES": "2", "DDR_WEBHOOK_BACKOFF": "0.001"},
            ):
                Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        history = self.store.list_workspace_digests("ws1")
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertFalse(entry["delivery_ok"])
        self.assertEqual(entry["delivery_error"], "HTTP 500")
        self.assertEqual(entry["attempts"], 2)

    def test_no_webhook_records_zero_attempts(self):
        self._schedule("ws1")
        Scheduler(self.store).run_once(NOW)
        entry = self.store.list_workspace_digests("ws1")[0]
        self.assertFalse(entry["delivery_ok"])
        self.assertEqual(entry["attempts"], 0)

    def test_failing_webhook_records_digest_and_continues(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch

        def boom(url, payload, timeout=5):
            raise RuntimeError("webhook exploded")

        notify.dispatch = boom
        try:
            results = Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        self.assertEqual(len(self.store.list_scans()), 1)
        self.assertFalse(results[0]["digest_sent"])
        history = self.store.list_workspace_digests("ws1")
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]["delivery_ok"])
        self.assertTrue(history[0]["delivery_error"])
        self.assertEqual(
            self.store.get_workspace_schedule("ws1")["next_run_at"],
            "2020-01-01T01:00:00+00:00",
        )

    def test_non_2xx_webhook_records_delivery_error(self):
        self._schedule("ws1")
        self.store.set_workspace_webhook("ws1", "https://hooks.example.com/x")
        original = notify.dispatch
        notify.dispatch = lambda url, payload, timeout=5: {
            "ok": False,
            "status": 500,
            "error": "HTTP 500",
        }
        try:
            Scheduler(self.store).run_once(NOW)
        finally:
            notify.dispatch = original

        entry = self.store.list_workspace_digests("ws1")[0]
        self.assertFalse(entry["delivery_ok"])
        self.assertEqual(entry["delivery_error"], "HTTP 500")

    def test_due_schedule_without_webhook_records_attempt(self):
        self._schedule("ws1")
        Scheduler(self.store).run_once(NOW)
        history = self.store.list_workspace_digests("ws1")
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]["delivery_ok"])
        self.assertTrue(history[0]["delivery_error"])

    def test_recording_failure_does_not_stop_loop(self):
        self._schedule("ws1")

        def boom(workspace, digest, delivery):
            raise RuntimeError("store exploded")

        self.store.record_workspace_digest = boom
        captured = io.StringIO()
        with redirect_stderr(captured):
            results = Scheduler(self.store).run_once(NOW)

        self.assertTrue(results[0]["ran"])
        self.assertEqual(len(self.store.list_scans()), 1)
        self.assertIn("failed to record digest", captured.getvalue())
        self.assertEqual(
            self.store.get_workspace_schedule("ws1")["next_run_at"],
            "2020-01-01T01:00:00+00:00",
        )

    def test_failing_scan_does_not_stop_other_schedules(self):
        self._schedule("wsFail")
        self._schedule("wsOk")
        advanced = []

        def runner(store, schedule):
            if schedule["workspace"] == "wsFail":
                raise RuntimeError("scan exploded")
            advanced.append(schedule["workspace"])
            return store.create_scan("S", "schedule", [], workspace=schedule["workspace"])

        results = Scheduler(self.store, scan_runner=runner).run_once(NOW)
        self.assertEqual(advanced, ["wsOk"])
        self.assertEqual(len(self.store.list_scans()), 1)
        by_workspace = {r["workspace"]: r for r in results}
        self.assertFalse(by_workspace["wsFail"]["ran"])
        self.assertIn("error", by_workspace["wsFail"])
        self.assertTrue(by_workspace["wsOk"]["ran"])
        for workspace in ("wsFail", "wsOk"):
            self.assertEqual(
                self.store.get_workspace_schedule(workspace)["next_run_at"],
                "2020-01-01T01:00:00+00:00",
            )

    def test_start_stop_daemon_thread(self):
        scheduler = Scheduler(self.store, tick_seconds=30)
        thread = scheduler.start()
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.is_alive())
        scheduler.stop(timeout=5)
        self.assertIsNone(scheduler._thread)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
