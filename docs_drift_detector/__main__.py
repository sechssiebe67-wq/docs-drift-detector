"""Entry point: ``python -m docs_drift_detector``.

Environment variables:
    PORT          HTTP port (default 8080)
    HOST          bind address (default 0.0.0.0)
    DDR_DATA_DIR  directory for the JSON scan store (default ./data)
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from .server import create_server
from .store import Store


def main(argv=None) -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        port = 8080
    data_dir = os.environ.get("DDR_DATA_DIR", os.path.join(os.getcwd(), "data"))
    store = Store(os.path.join(data_dir, "scans.json"))

    server = create_server(host, port, store)

    def shutdown(signum, frame):  # noqa: ANN001
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
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
