"""Standalone worker process: `python -m codenta_diag.worker`."""
from __future__ import annotations

import logging
import os
import signal
import threading

from .api.app import build_container
from .settings import get_settings


def main() -> None:
    logging.basicConfig(level=os.getenv("CODENTA_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    container = build_container(get_settings())
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logging.getLogger(__name__).info("worker pool starting (concurrency=%s, reasoner=%s)",
                                     container.settings.worker_concurrency, container.settings.reasoner)
    container.pool.run_forever(stop)


if __name__ == "__main__":
    main()
