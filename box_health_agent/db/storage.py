"""SQLite persistence for metrics, probes, and alerts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass
class AlertRecord:
    id: int
    rule: str
    subject: str
    opened_at: float
    closed_at: float | None
    peak_value: float | None
    details: dict[str, Any]


class Database:
    def __init__(self, path: str | Path, retention_days: int = 7) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metric_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value REAL,
                    extra_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_metric_ts ON metric_samples(ts);
                CREATE INDEX IF NOT EXISTS idx_metric_name ON metric_samples(name, ts);

                CREATE TABLE IF NOT EXISTS probe_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    probe TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms REAL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_probe_ts ON probe_samples(ts);

                CREATE TABLE IF NOT EXISTS process_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    pid INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    cpu_percent REAL,
                    mem_rss_mb REAL,
                    status TEXT,
                    num_fds INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_proc_ts ON process_samples(ts, pid);

                CREATE TABLE IF NOT EXISTS process_minute (
                    ts_minute REAL NOT NULL,
                    pid INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    mem_rss_mb REAL NOT NULL,
                    PRIMARY KEY (ts_minute, pid)
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    peak_value REAL,
                    details_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(rule, subject, closed_at);
                """
            )

    def insert_metrics(self, ts: float, rows: list[tuple[str, str, float | None, dict | None]]) -> None:
        with self._lock, self._conn() as conn:
            conn.executemany(
                "INSERT INTO metric_samples (ts, category, name, value, extra_json) VALUES (?, ?, ?, ?, ?)",
                [
                    (ts, cat, name, val, json.dumps(extra) if extra else None)
                    for cat, name, val, extra in rows
                ],
            )

    def insert_probe(
        self, ts: float, probe: str, success: bool, latency_ms: float | None, detail: str = ""
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO probe_samples (ts, probe, success, latency_ms, detail) VALUES (?, ?, ?, ?, ?)",
                (ts, probe, 1 if success else 0, latency_ms, detail),
            )

    def insert_processes(self, ts: float, processes: list[dict[str, Any]]) -> None:
        with self._lock, self._conn() as conn:
            conn.executemany(
                """INSERT INTO process_samples
                   (ts, pid, name, cpu_percent, mem_rss_mb, status, num_fds)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        ts,
                        p["pid"],
                        p["name"],
                        p.get("cpu_percent"),
                        p.get("mem_rss_mb"),
                        p.get("status"),
                        p.get("num_fds"),
                    )
                    for p in processes
                ],
            )

    def upsert_process_minute(self, ts_minute: float, pid: int, name: str, mem_rss_mb: float) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO process_minute (ts_minute, pid, name, mem_rss_mb)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ts_minute, pid) DO UPDATE SET mem_rss_mb=excluded.mem_rss_mb""",
                (ts_minute, pid, name, mem_rss_mb),
            )

    def get_process_minute_pids(self) -> list[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT pid FROM process_minute").fetchall()
        return [int(r["pid"]) for r in rows]

    def get_process_minute_history(self, pid: int, limit: int = 10) -> list[tuple[float, float]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT ts_minute, mem_rss_mb FROM process_minute
                   WHERE pid=? ORDER BY ts_minute DESC LIMIT ?""",
                (pid, limit),
            ).fetchall()
        return [(r["ts_minute"], r["mem_rss_mb"]) for r in reversed(rows)]

    def get_open_alert(self, rule: str, subject: str) -> AlertRecord | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM alerts WHERE rule=? AND subject=? AND closed_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (rule, subject),
            ).fetchone()
        if not row:
            return None
        return self._row_to_alert(row)

    def open_alert(
        self,
        rule: str,
        subject: str,
        ts: float,
        peak_value: float | None = None,
        details: dict | None = None,
    ) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO alerts (rule, subject, opened_at, peak_value, details_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (rule, subject, ts, peak_value, json.dumps(details) if details else None),
            )
            return int(cur.lastrowid)

    def update_alert_peak(self, alert_id: int, peak_value: float) -> None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT peak_value FROM alerts WHERE id=?", (alert_id,)).fetchone()
            current = float(row["peak_value"]) if row and row["peak_value"] is not None else None
            new_peak = peak_value if current is None else max(current, peak_value)
            conn.execute("UPDATE alerts SET peak_value=? WHERE id=?", (new_peak, alert_id))

    def close_alert(self, alert_id: int, ts: float) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE alerts SET closed_at=? WHERE id=?", (ts, alert_id))

    def list_alerts(self, open_only: bool | None = None, limit: int = 200) -> list[AlertRecord]:
        with self._lock, self._conn() as conn:
            if open_only is True:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            elif open_only is False:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM alerts ORDER BY opened_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def query_metrics(
        self, name: str | None = None, since: float | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            q = "SELECT ts, category, name, value, extra_json FROM metric_samples WHERE 1=1"
            params: list[Any] = []
            if name:
                q += " AND name=?"
                params.append(name)
            if since is not None:
                q += " AND ts>=?"
                params.append(since)
            q += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "ts": r["ts"],
                    "category": r["category"],
                    "name": r["name"],
                    "value": r["value"],
                    "extra": json.loads(r["extra_json"]) if r["extra_json"] else None,
                }
            )
        return list(reversed(out))

    def latest_metric_value(self, name: str) -> float | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM metric_samples WHERE name=? ORDER BY ts DESC LIMIT 1",
                (name,),
            ).fetchone()
        return float(row["value"]) if row and row["value"] is not None else None

    def recent_metric_average(self, name: str, limit: int = 6, min_value: float = 0.01) -> float | None:
        """Average of recent samples; skips near-zero glitches from bad psutil deltas."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT value FROM metric_samples
                   WHERE name=? AND value IS NOT NULL ORDER BY ts DESC LIMIT ?""",
                (name, limit),
            ).fetchall()
        if not rows:
            return None
        vals = [float(r["value"]) for r in rows]
        usable = [v for v in vals if v >= min_value]
        bucket = usable if usable else vals
        return sum(bucket) / len(bucket)

    def latest_disk_usage(self, prefer: str = "C:") -> tuple[str, float] | None:
        """Return (mount, percent) for the newest disk usage sample (prefers mount containing prefer)."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT name, value, extra_json FROM metric_samples
                   WHERE name LIKE 'disk.usage_percent.%'
                   ORDER BY ts DESC LIMIT 30"""
            ).fetchall()
        best: tuple[str, float] | None = None
        for r in rows:
            if r["value"] is None:
                continue
            extra = json.loads(r["extra_json"]) if r["extra_json"] else {}
            mount = extra.get("mount") or r["name"].replace("disk.usage_percent.", "").replace("/", "\\")
            val = float(r["value"])
            if prefer.lower() in mount.lower():
                return mount, val
            if best is None:
                best = (mount, val)
        return best

    def recent_metric_max(self, name: str, limit: int = 3) -> float | None:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """SELECT MAX(value) AS v FROM (
                     SELECT value FROM metric_samples
                     WHERE name=? AND value IS NOT NULL
                     ORDER BY ts DESC LIMIT ?
                   )""",
                (name, limit),
            ).fetchone()
        return float(row["v"]) if row and row["v"] is not None else None

    def recent_probes(self, since: float) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, probe, success, latency_ms, detail FROM probe_samples WHERE ts>=? ORDER BY ts",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_processes_latest(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT MAX(ts) AS m FROM process_samples").fetchone()
            if not row or row["m"] is None:
                return []
            ts = row["m"]
            rows = conn.execute(
                """SELECT pid, name, cpu_percent, mem_rss_mb, status, num_fds
                   FROM process_samples WHERE ts=? ORDER BY cpu_percent DESC""",
                (ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_old_data(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM probe_samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM process_samples WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM process_minute WHERE ts_minute < ?", (cutoff,))

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> AlertRecord:
        return AlertRecord(
            id=row["id"],
            rule=row["rule"],
            subject=row["subject"],
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            peak_value=row["peak_value"],
            details=json.loads(row["details_json"]) if row["details_json"] else {},
        )