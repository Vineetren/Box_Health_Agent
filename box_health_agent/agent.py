"""Background sampler loops (5s metrics, 15s probes)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import yaml

from box_health_agent.alerts.engine import AlertEngine
from box_health_agent.collectors import collect_network_metrics, collect_process_metrics, collect_system_metrics
from box_health_agent.db.storage import Database
from box_health_agent.probes import run_network_probes


class HealthAgent:
    def __init__(self, config_path: str | Path) -> None:
        with open(config_path, encoding="utf-8") as f:
            self.cfg: dict[str, Any] = yaml.safe_load(f)
        db_path = self.cfg.get("database", {}).get("path", "data/health_agent.db")
        retention = int(self.cfg.get("database", {}).get("retention_days", 7))
        self.db = Database(db_path, retention_days=retention)
        self.alerts = AlertEngine(self.db, self.cfg)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_probe: list[dict[str, Any]] = []

    def start(self) -> None:
        if self._threads:
            return
        mi = float(self.cfg.get("sampling", {}).get("metrics_interval_sec", 5))
        pi = float(self.cfg.get("sampling", {}).get("probe_interval_sec", 15))
        self._threads = [
            threading.Thread(target=self._metrics_loop, args=(mi,), daemon=True, name="metrics"),
            threading.Thread(target=self._probe_loop, args=(pi,), daemon=True, name="probes"),
            threading.Thread(target=self._maintenance_loop, daemon=True, name="maint"),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _metrics_loop(self, interval: float) -> None:
        purge_counter = 0
        while not self._stop.is_set():
            ts = time.time()
            sys_rows = collect_system_metrics(ts)
            net_rows = collect_network_metrics(ts)
            rows = sys_rows + net_rows
            self.db.insert_metrics(ts, rows)
            procs = collect_process_metrics(ts)
            self.db.insert_processes(ts, procs)
            self.alerts.evaluate(ts, sys_rows, procs, self._last_probe or None)
            purge_counter += 1
            if purge_counter >= 720:  # ~1h at 5s
                self.db.purge_old_data()
                purge_counter = 0
            if self._stop.wait(interval):
                break

    def _probe_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            ts = time.time()
            results = run_network_probes(self.cfg)
            self._last_probe = results
            for r in results:
                self.db.insert_probe(ts, r["probe"], r["success"], r.get("latency_ms"), r.get("detail", ""))
            # Network rules only — avoid a second CPU/process sample (caused 0% glitches).
            self.alerts.evaluate_probes(ts, results)
            if self._stop.wait(interval):
                break

    def _maintenance_loop(self) -> None:
        while not self._stop.wait(3600):
            self.db.purge_old_data()