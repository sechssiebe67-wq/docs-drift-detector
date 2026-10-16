"""Entry point: ``python -m docs_drift_detector``.

Environment variables:
    PORT                  HTTP port (default 8080)
    HOST                  bind address (default 0.0.0.0)
    DDR_DATA_DIR          directory for the JSON scan store (default ./data)
    DDR_SCHEDULER_TICK    scheduler tick in seconds (default 60)
    DDR_SCHEDULER_ENABLED set to 0 to disable the background scheduler
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from .scheduler import Scheduler
from .server import create_server
from .store import Store


def _scheduler_enabled() -> bool:
    raw = os.environ.get("DDR_SCHEDULER_ENABLED", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _scheduler_tick() -> int:
    try:
        value = int(os.environ.get("DDR_SCHEDULER_TICK", "60"))
    except (TypeError, ValueError):
        value = 60
    return value if value > 0 else 60


def main(argv=None) -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        port = 8080
    data_dir = os.environ.get("DDR_DATA_DIR", os.path.join(os.getcwd(), "data"))
    store = Store(os.path.join(data_dir, "scans.json"))

    server = create_server(host, port, store)

    scheduler = None
    if _scheduler_enabled():
        scheduler = Scheduler(store, tick_seconds=_scheduler_tick(), enabled=True)
        server.scheduler = scheduler
        scheduler.start()
        sys.stderr.write(
            "Docs Drift Detector scheduler enabled (tick=%ds)\n" % scheduler.tick_seconds
        )
    else:
        sys.stderr.write("Docs Drift Detector scheduler disabled\n")

    def shutdown(signum, frame):  # noqa: ANN001
        if scheduler is not None:
            scheduler.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    sys.stderr.write("Docs Drift Detector listening on http://%s:%d\n" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
