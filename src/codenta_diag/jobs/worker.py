"""Investigation workers.

`drain()` processes the queue with a bounded thread pool (used by tests, the demo, and the admin
endpoint). `run_forever()` is the long-running worker loop used by `python -m codenta_diag.worker`.
Independent investigations run concurrently; the concurrency limit is the pool size.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from ..service import InvestigationService
from .queue import JobQueue

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, queue: JobQueue, service: InvestigationService, worker_id: str | None = None) -> None:
        self.queue = queue
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:6]}"

    def process_one(self) -> bool:
        claimed = self.queue.claim(self.worker_id)
        if claimed is None:
            return False
        job_id, inv_id = claimed
        try:
            status = self.service.run(inv_id, heartbeat=lambda: self.queue.heartbeat(job_id, self.worker_id))
            self.queue.complete(job_id)
            log.info("job %s investigation %s -> %s", job_id, inv_id, status)
        except Exception as exc:  # noqa: BLE001 - every failure must reach retry/dead-letter handling
            new_status = self.queue.fail(job_id, f"{type(exc).__name__}: {exc}")
            log.warning("job %s failed (%s): %s", job_id, new_status, exc)
        return True


class WorkerPool:
    def __init__(self, queue: JobQueue, service: InvestigationService, concurrency: int = 4) -> None:
        self.queue = queue
        self.service = service
        self.concurrency = concurrency

    def drain(self, max_wait_seconds: float = 30.0) -> int:
        """Process until the queue is empty (waiting for scheduled retries up to max_wait_seconds)."""
        processed = 0
        lock = threading.Lock()
        deadline = time.monotonic() + max_wait_seconds

        def loop(i: int) -> None:
            nonlocal processed
            worker = Worker(self.queue, self.service, f"drain-{i}")
            while time.monotonic() < deadline:
                if worker.process_one():
                    with lock:
                        processed += 1
                    continue
                if self.queue.depth() == 0 and self.queue.counts().get("running", 0) == 0:
                    return
                self.queue.recover_expired_leases()
                time.sleep(min(0.2, self.queue.next_delayed_run() or 0.05))

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(loop, range(self.concurrency)))
        return processed

    def run_forever(self, stop: threading.Event, poll_seconds: float = 1.0) -> None:
        def loop(i: int) -> None:
            worker = Worker(self.queue, self.service, f"worker-{i}-{uuid.uuid4().hex[:4]}")
            while not stop.is_set():
                if not worker.process_one():
                    stop.wait(poll_seconds)

        threads = [threading.Thread(target=loop, args=(i,), daemon=True) for i in range(self.concurrency)]
        for t in threads:
            t.start()
        while not stop.is_set():
            self.queue.recover_expired_leases()
            stop.wait(5.0)
        for t in threads:
            t.join(timeout=5)
