from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from .service import Orchestrator


class Scheduler:
    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log_path = orchestrator.settings.home / "logs" / "scheduler.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="xianyu-auto-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _log(self, message: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")

    def _run(self) -> None:
        interval = max(5, self.orchestrator.settings.scheduler_tick_seconds)
        while not self._stop.is_set():
            try:
                self.orchestrator.send_pending_reminders()
                self.orchestrator.db.expire_stale_approvals()
                self.orchestrator.enqueue_daily_tasks()
                self.orchestrator.process_jobs(max_jobs=5)
            except Exception:
                self._log(traceback.format_exc())
            self._stop.wait(interval)

