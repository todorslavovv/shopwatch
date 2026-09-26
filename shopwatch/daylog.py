"""Today's checks, and only today's.

A single day of results: what was checked, what came back, when. Rows from any earlier
day are deleted on the next touch, so the file never accumulates a history. There is no
cron and no timer — the purge happens on read and on write, which means it also happens
correctly after the machine was asleep across midnight.

Deliberately stores NO client identifier: not the IP, not the forwarded header, nothing
about who asked. It records that a shop was checked, not who checked it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "day.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    host     TEXT PRIMARY KEY,
    day      TEXT NOT NULL,
    checked  TEXT NOT NULL,
    verdict  TEXT,
    score    INTEGER,
    entry    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_day ON checks(day);
"""


def _conn(path: Path | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or DB_PATH, timeout=5)
    c.executescript(_SCHEMA)
    return c


def _purge(c: sqlite3.Connection) -> int:
    """Drop everything that is not today. Returns rows removed."""
    cur = c.execute("DELETE FROM checks WHERE day <> ?", (date.today().isoformat(),))
    return cur.rowcount or 0


def record(host: str, entry: dict, path: Path | None = None) -> None:
    """Store one investigation result, replacing any earlier one for the same shop."""
    a = entry.get("assessment") or {}
    with _lock, _conn(path) as c:
        _purge(c)
        c.execute(
            "INSERT OR REPLACE INTO checks (host, day, checked, verdict, score, entry) "
            "VALUES (?,?,?,?,?,?)",
            (host, date.today().isoformat(), datetime.now().isoformat(timespec="seconds"),
             a.get("verdict"), a.get("score"), json.dumps(entry, ensure_ascii=False)))


def get(host: str, path: Path | None = None) -> dict | None:
    """Today's stored result for a shop, or None."""
    with _lock, _conn(path) as c:
        _purge(c)
        row = c.execute(
            "SELECT entry FROM checks WHERE host = ? AND day = ?",
            (host, date.today().isoformat())).fetchone()
    return json.loads(row[0]) if row else None


def today(limit: int = 50, path: Path | None = None) -> list[dict]:
    """Everything checked today, most recent first."""
    with _lock, _conn(path) as c:
        _purge(c)
        rows = c.execute(
            "SELECT host, checked, verdict, score FROM checks WHERE day = ? "
            "ORDER BY checked DESC LIMIT ?",
            (date.today().isoformat(), limit)).fetchall()
    return [{"host": h, "checked": t, "verdict": v, "score": s} for h, t, v, s in rows]


if __name__ == "__main__":       # self-check: yesterday's rows do not survive a touch
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        record("a.bg", {"assessment": {"verdict": "ok", "score": 0}}, path=p)
        with _conn(p) as c:      # backdate it, then touch the store
            c.execute("UPDATE checks SET day = '2000-01-01' WHERE host = 'a.bg'")
        assert get("a.bg", path=p) is None, "stale row survived"
        assert today(path=p) == [], "stale row listed"
        record("b.bg", {"assessment": {"verdict": "suspect", "score": 40}}, path=p)
        assert [r["host"] for r in today(path=p)] == ["b.bg"]
        print("daylog self-check ok")
