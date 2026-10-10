"""SQLite state: idempotency keys, last-run times, intervention queue, small kv cache."""
from __future__ import annotations

import json
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, run_id TEXT, client TEXT, carrier TEXT,
            bill_to TEXT, reference TEXT, reason TEXT, resolved INTEGER DEFAULT 0);
        """)
        # Older local databases created this table before client was recorded.
        # Add the nullable column in place, preserving queued interventions and state.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(intervention)")}
        if "client" not in columns:
            self.db.execute("ALTER TABLE intervention ADD COLUMN client TEXT")
            self.db.commit()

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

    def kv_delete(self, k: str) -> None:
        self.db.execute("DELETE FROM kv WHERE k=?", (k,))
        self.db.commit()

    def enqueue(self, run_id, client, carrier, bill_to, refs: list[str], reason: str) -> int:
        """Adds only refs not already waiting with the same reason (no duplicate rows run after run)."""
        new = [r for r in refs if not self.db.execute(
            "SELECT 1 FROM intervention WHERE resolved=0 AND client=? AND carrier=? AND bill_to=? AND reference=? AND reason=?",
            (client, carrier, bill_to, r, reason)).fetchone()]
        self.db.executemany(
            "INSERT INTO intervention(ts,run_id,client,carrier,bill_to,reference,reason) VALUES(?,?,?,?,?,?,?)",
            [(time.time(), run_id, client, carrier, bill_to, r, reason) for r in new])
        self.db.commit()
        return len(new)

    def open_interventions(self) -> list[tuple]:
        return self.db.execute(
            "SELECT client,carrier,bill_to,reference,reason,ts FROM intervention WHERE resolved=0").fetchall()

    def resolve_interventions(self, client: str, carrier: str) -> None:
        self.db.execute("UPDATE intervention SET resolved=1 WHERE client=? AND carrier=?", (client, carrier))
        self.db.commit()


class KVCache:
    """Tiny per-client+carrier cache handed to adapters (so they never touch the store directly)."""

    def __init__(self, store: StateStore, client: str, code: str):
        self._s, self._p = store, f"cache:{client}:{code}:"

    def get(self, name: str) -> dict | None:
        v = self._s.kv_get(self._p + name)
        return json.loads(v) if v else None

    def set(self, name: str, value: dict) -> None:
        self._s.kv_set(self._p + name, json.dumps(value))

    def delete(self, name: str) -> None:
        self._s.kv_delete(self._p + name)
