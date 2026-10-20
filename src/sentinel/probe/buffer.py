"""Local result buffer (SQLite). Results leave it only after the core accepted them."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_ROWS = 100_000  # beyond this the oldest results are dropped (the caller logs it)
MAX_AGE_S = 6 * 24 * 3600.0  # the core refuses results older than 7 days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created REAL NOT NULL
)
"""
_DROP_OLDEST = "DELETE FROM results WHERE id IN (SELECT id FROM results ORDER BY created LIMIT ?)"


class ResultBuffer:
    def __init__(
        self,
        path: str | Path,
        *,
        max_rows: int = MAX_ROWS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._lock = asyncio.Lock()
        self._max_rows = max_rows
        self._clock = clock

    async def _run(self, sql: str, params: Any = ()) -> list[Any]:
        async with self._lock:
            return await asyncio.to_thread(lambda: self._db.execute(sql, params).fetchall())

    async def add(self, results: list[dict[str, Any]]) -> int:
        """Store results; returns how many old rows were dropped to respect the cap."""
        rows = [(r["id"], json.dumps(r, default=str), self._clock()) for r in results]

        def write() -> int:
            self._db.executemany(
                "INSERT OR IGNORE INTO results (id, payload, created) VALUES (?, ?, ?)", rows
            )
            (count,) = self._db.execute("SELECT COUNT(*) FROM results").fetchone()
            excess = max(0, int(count) - self._max_rows)
            if excess:
                self._db.execute(_DROP_OLDEST, (excess,))
            return excess

        async with self._lock:
            return await asyncio.to_thread(write)

    async def take(self, limit: int, max_bytes: int | None = None) -> list[dict[str, Any]]:
        """The oldest results still young enough for the core: at most ``limit`` of them
        and about ``max_bytes`` of JSON (always at least one)."""
        await self._run("DELETE FROM results WHERE created < ?", (self._clock() - MAX_AGE_S,))
        rows = await self._run("SELECT payload FROM results ORDER BY created LIMIT ?", (limit,))
        taken: list[dict[str, Any]] = []
        size = 0
        for (payload,) in rows:
            size += len(payload)
            if taken and max_bytes is not None and size > max_bytes:
                break
            taken.append(json.loads(payload))
        return taken

    async def ack(self, ids: list[str]) -> None:
        if ids:
            marks = ",".join("?" for _ in ids)  # only "?" marks; the ids are bound parameters
            sql = f"DELETE FROM results WHERE id IN ({marks})"  # noqa: S608  # nosec B608
            await self._run(sql, ids)

    async def size(self) -> int:
        ((count,),) = await self._run("SELECT COUNT(*) FROM results")
        return int(count)

    def close(self) -> None:
        self._db.close()
