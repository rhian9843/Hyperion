import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProfileStep:
    status:      str
    duration_ms: float


@dataclass
class ProfileEntry:
    id:       int
    sql:      str
    total_ms: float
    steps:    list[ProfileStep] = field(default_factory=list)


class QueryProfiler:
    MAX_ENTRIES = 100

    def __init__(self) -> None:
        self._enabled      = False
        self._profiles:    list[ProfileEntry] = []
        self._next_id      = 1
        self._in_query     = False
        self._current_sql  = ""
        self._query_start  = 0.0
        self._step_start   = 0.0
        self._current_steps: list[ProfileStep] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self)  -> None: self._enabled = True
    def disable(self) -> None: self._enabled = False

    def start_query(self, sql: str) -> None:
        if not self._enabled:
            return
        self._in_query       = True
        self._current_sql    = sql
        self._query_start    = time.perf_counter()
        self._step_start     = self._query_start
        self._current_steps  = []

    def add_step(self, status: str) -> None:
        if not self._enabled or not self._in_query:
            return
        now = time.perf_counter()
        ms  = (now - self._step_start) * 1000.0
        self._current_steps.append(ProfileStep(status, round(ms, 3)))
        self._step_start = now

    def end_query(self) -> None:
        if not self._enabled or not self._in_query:
            return
        self._in_query = False
        total_ms = (time.perf_counter() - self._query_start) * 1000.0
        entry = ProfileEntry(
            id=self._next_id,
            sql=self._current_sql,
            total_ms=round(total_ms, 3),
            steps=list(self._current_steps),
        )
        self._next_id += 1
        if len(self._profiles) >= self.MAX_ENTRIES:
            self._profiles.pop(0)
        self._profiles.append(entry)

    def get_profiles(self) -> list[ProfileEntry]:
        return list(self._profiles)

    def get_profile(self, query_id: int) -> Optional[ProfileEntry]:
        for p in self._profiles:
            if p.id == query_id:
                return p
        return None

    def clear(self) -> None:
        self._profiles.clear()
        self._next_id = 1
