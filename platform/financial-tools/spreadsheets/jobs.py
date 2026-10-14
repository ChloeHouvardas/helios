"""Async job registry — calls that can take longer than 10 seconds return
``{job_id, status: "running"}`` per the Developer Guide."""

from __future__ import annotations

import threading
import time
import uuid

from .errors import validation
from .filestore import redact_for_logs


class JobRunner:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, tool: str, fn) -> str:
        job_id = "job_" + uuid.uuid4().hex[:12]
        job = {"job_id": job_id, "tool": tool, "status": "running",
               "result": None, "error": None}
        with self._lock:
            self._jobs[job_id] = job

        def run() -> None:
            try:
                job["result"] = redact_for_logs(fn())
                job["status"] = "succeeded"
            except Exception as exc:
                job["error"] = {
                    "code": getattr(exc, "code", "internal_error"),
                    "message": str(exc),
                    "retryable": bool(getattr(exc, "retryable", False)),
                }
                job["status"] = "failed"

        threading.Thread(target=run, daemon=True, name=job_id).start()
        return job_id

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise validation(f"unknown job_id '{job_id}'")
        out = {k: v for k, v in job.items() if k != "result"}
        if job["status"] == "succeeded":
            out["result"] = job["result"]
        return out

    def join(self, job_id: str, timeout: float = 5.0) -> dict:
        """Block until the job finishes — used by tests and internals."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        return self.get(job_id)
