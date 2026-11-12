"""ASGI entrypoint: `uvicorn codenta_diag.main:app`.

Set CODENTA_RUN_WORKERS=1 to run investigation workers in-process (single-container dev mode).
In docker-compose / AWS, workers run as a separate process (`python -m codenta_diag.worker`).
"""
from __future__ import annotations

import logging
import os

from .api import create_app

logging.basicConfig(level=os.getenv("CODENTA_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(run_workers=os.getenv("CODENTA_RUN_WORKERS") == "1")
