"""Background event scheduler — fires CREATE EVENT jobs on their schedule."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database


class EventScheduler:
    """Daemon thread that fires scheduled events on a Database.

    Polls every second.  For each enabled event:
      - INTERVAL: fires when now - last_run >= interval_seconds
      - AT:       fires once when now >= at_time; auto-drops after firing

    SQL bodies are executed inside an auto-committed transaction so they use
    the normal write-lock path and never corrupt concurrent readers.
    """

    def __init__(self, db: "Database", poll_interval: float = 1.0) -> None:
        self._db           = db
        self._poll_interval = poll_interval
        self._stop         = threading.Event()
        self._thread       = threading.Thread(
            target=self._run, daemon=True, name="hyperion-event-scheduler"
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self._tick()
            except Exception:
                pass  # never let the scheduler thread crash

    def _tick(self) -> None:
        db = self._db
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        with db._lock.read():
            events = list(db._catalog.events.values())

        to_drop: list[str] = []

        for evt in events:
            if not evt.enabled:
                continue
            try:
                if evt.schedule_type == "INTERVAL":
                    if evt.last_run:
                        last = datetime.strptime(evt.last_run, "%Y-%m-%d %H:%M:%S")
                        elapsed = (now - last).total_seconds()
                        if elapsed < evt.interval_seconds:
                            continue
                    # first run or interval elapsed — fire
                    self._fire(evt.name, evt.sql, now_str)

                elif evt.schedule_type == "AT":
                    if evt.last_run:
                        continue  # already fired
                    at_dt = datetime.strptime(evt.at_time, "%Y-%m-%d %H:%M:%S")
                    if now >= at_dt:
                        self._fire(evt.name, evt.sql, now_str)
                        to_drop.append(evt.name)
            except Exception:
                pass  # bad schedule spec — skip silently

        for name in to_drop:
            try:
                db.drop_event(name, if_exists=True)
            except Exception:
                pass

    def _fire(self, name: str, sql: str, now_str: str) -> None:
        db = self._db
        try:
            db.execute(sql)
        except Exception:
            pass
        # Update last_run inside the write lock
        with db._lock.write():
            evt = db._catalog.events.get(name)
            if evt is not None:
                evt.last_run = now_str
                db._schema_flushed_bytes = b""
