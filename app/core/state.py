"""SQLite state: idempotency keys, last-run times, manual-intervention queue, kv."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class StateStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS intervention(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, carrier TEXT,
            bill_to TEXT, reference TEXT, reason TEXT, resolved INTEGER DEFAULT 0);
        """)

    def was_sent(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM sent WHERE key=?", (key,)).fetchone() is not None

    def mark_sent(self, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO sent VALUES(?,?)", (key, time.time()))
        self.db.commit()

    def kv_get(self, k: str) -> str | None:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
        self.db.commit()

    def enqueue(self, run_id, carrier, bill_to, refs: list[str], reason: str) -> None:
        self.db.executemany(
            "INSERT INTO intervention(ts,run_id,carrier,bill_to,reference,reason) VALUES(?,?,?,?,?,?)",
            [(time.time(), run_id, carrier, bill_to, r, reason) for r in refs])
        self.db.commit()

    def open_interventions(self) -> list[tuple]:
        return self.db.execute(
            "SELECT carrier,bill_to,reference,reason,ts FROM intervention WHERE resolved=0").fetchall()

    def resolve_interventions(self, carrier: str) -> None:
        self.db.execute("UPDATE intervention SET resolved=1 WHERE carrier=?", (carrier,))
        self.db.commit()
