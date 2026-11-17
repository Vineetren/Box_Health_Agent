"""Deterministic alert rules with open/close semantics (no flapping)."""

from __future__ import annotations

import fnmatch
import time
from collections import defaultdict, deque
from typing import Any

from box_health_agent.db.storage import AlertRecord, Database


class AlertEngine:
    RULE_CPU_HIGH = "cpu_high"
    RULE_RAM_HIGH = "ram_high"
    RULE_DISK_HIGH = "disk_high"
    RULE_NETWORK_LOST = "network_lost"
    RULE_MEMORY_CLIMBING = "memory_climbing"
    RULE_PROCESS_DIED = "process_died"
    RULE_ZOMBIE = "zombie"

    def __init__(self, db: Database, cfg: dict[str, Any]) -> None:
        self.db = db
        self.cfg = cfg.get("alerts", {})
        self.watch_list = cfg.get("process_watch", {}).get("watch_list", [])
        self._cpu_high_since: float | None = None
        self._net_fail_streak = 0
        self._zombie_since: dict[int, float] = {}
        self._active_watch_pids: dict[str, set[int]] = defaultdict(set)
        self._last_minute_bucket: int | None = None
        raw_ex = self.cfg.get("disk_high_exclude_mounts") or []
        self._disk_exclude = {self._norm_mount(m) for m in raw_ex}

        climb_cfg = cfg.get("alerts", {})
        self._climb_samples = int(climb_cfg.get("memory_climb_samples", 10))
        self._climb_rising = int(climb_cfg.get("memory_climb_rising_count", 9))
        self._climb_min_mb = float(climb_cfg.get("memory_climb_min_mb", 50))

    def evaluate(
        self,
        ts: float,
        system_rows: list[tuple[str, str, float | None, dict | None]],
        processes: list[dict[str, Any]],
        probe_results: list[dict[str, Any]] | None,
    ) -> None:
        self._eval_cpu(ts, system_rows)
        self._eval_ram(ts, system_rows)
        self._eval_disk(ts, system_rows)
        self._eval_processes(ts, processes)
        self._rollup_process_minutes(ts, processes)
        self._eval_memory_climbing(ts)
        if probe_results is not None:
            self._eval_network(ts, probe_results)

    def evaluate_probes(self, ts: float, probe_results: list[dict[str, Any]]) -> None:
        self._eval_network(ts, probe_results)

    def _eval_cpu(self, ts: float, rows: list[tuple]) -> None:
        threshold = float(self.cfg.get("cpu_high_percent", 85))
        duration = float(self.cfg.get("cpu_high_duration_sec", 60))
        total = next((v for c, n, v, _ in rows if n == "cpu.total_percent"), None)
        if total is None:
            return
        if total > threshold:
            if self._cpu_high_since is None:
                self._cpu_high_since = ts
            elif ts - self._cpu_high_since >= duration:
                self._ensure_open(
                    self.RULE_CPU_HIGH,
                    "cpu",
                    ts,
                    total,
                    {"duration_sec": ts - self._cpu_high_since},
                )
                open_a = self.db.get_open_alert(self.RULE_CPU_HIGH, "cpu")
                if open_a:
                    self.db.update_alert_peak(open_a.id, total)
        else:
            self._cpu_high_since = None
            self._close_if_open(self.RULE_CPU_HIGH, "cpu", ts)

    def _eval_ram(self, ts: float, rows: list[tuple]) -> None:
        threshold = float(self.cfg.get("ram_high_percent", 90))
        ram = next((v for c, n, v, _ in rows if n == "mem.ram_percent"), None)
        if ram is None:
            return
        subject = "ram"
        if ram > threshold:
            self._ensure_open(self.RULE_RAM_HIGH, subject, ts, ram)
            open_a = self.db.get_open_alert(self.RULE_RAM_HIGH, subject)
            if open_a:
                self.db.update_alert_peak(open_a.id, ram)
        else:
            self._close_if_open(self.RULE_RAM_HIGH, subject, ts)

    def _eval_disk(self, ts: float, rows: list[tuple]) -> None:
        threshold = float(self.cfg.get("disk_high_percent", 90))
        for cat, name, val, extra in rows:
            if not name.startswith("disk.usage_percent.") or val is None:
                continue
            mount = (extra or {}).get("mount") or name.split(".", 2)[-1]
            subject = mount
            if self._norm_mount(mount) in self._disk_exclude:
                self._close_if_open(self.RULE_DISK_HIGH, subject, ts)
                continue
            if val > threshold:
                self._ensure_open(self.RULE_DISK_HIGH, subject, ts, val, {"mount": mount})
                open_a = self.db.get_open_alert(self.RULE_DISK_HIGH, subject)
                if open_a:
                    self.db.update_alert_peak(open_a.id, val)
            else:
                self._close_if_open(self.RULE_DISK_HIGH, subject, ts)

    def _eval_network(self, ts: float, probes: list[dict]) -> None:
        need = int(self.cfg.get("network_failures_before_alert", 3))
        gw = next((p for p in probes if p["probe"] == "gateway_reachable"), None)
        inet = next((p for p in probes if p["probe"] == "internet_ping"), None)
        failed = (gw is not None and not gw["success"]) or (inet is not None and not inet["success"])
        if failed:
            self._net_fail_streak += 1
        else:
            self._net_fail_streak = 0
            self._close_if_open(self.RULE_NETWORK_LOST, "uplink", ts)

        if self._net_fail_streak >= need:
            detail = {"streak": self._net_fail_streak, "gw": gw, "internet": inet}
            self._ensure_open(self.RULE_NETWORK_LOST, "uplink", ts, float(self._net_fail_streak), detail)

    @staticmethod
    def _norm_mount(mount: str) -> str:
        return mount.replace("\\", "/").rstrip("/").lower()

    def _match_watch(self, name: str) -> bool:
        for pattern in self.watch_list:
            if fnmatch.fnmatch(name, pattern) or pattern in name:
                return True
        return False

    def _eval_processes(self, ts: float, processes: list[dict]) -> None:
        zombie_dur = float(self.cfg.get("zombie_min_duration_sec", 30))
        live_watch: dict[str, set[int]] = defaultdict(set)

        by_pid = {p["pid"]: p for p in processes}
        for p in processes:
            name = p["name"]
            pid = p["pid"]
            if self._match_watch(name):
                for pattern in self.watch_list:
                    if fnmatch.fnmatch(name, pattern) or pattern in name:
                        live_watch[pattern].add(pid)

            status = (p.get("status") or "").upper()
            if status == "Z" or status == "ZOMBIE":
                if pid not in self._zombie_since:
                    self._zombie_since[pid] = ts
                elif ts - self._zombie_since[pid] >= zombie_dur:
                    subject = f"pid:{pid}"
                    self._ensure_open(
                        self.RULE_ZOMBIE,
                        subject,
                        ts,
                        None,
                        {"name": name, "pid": pid},
                    )
            else:
                self._zombie_since.pop(pid, None)
                self._close_if_open(self.RULE_ZOMBIE, f"pid:{pid}", ts)

        for pattern in self.watch_list:
            live = live_watch.get(pattern, set())
            prev = self._active_watch_pids.get(pattern, set())
            # Condition is "this watch name is not running". A new PID clears it.
            if live:
                self._close_process_died(pattern, ts)
                self._active_watch_pids[pattern] = set(live)
                continue
            for pid in list(prev):
                subject = f"{pattern}:pid{pid}"
                self._ensure_open(
                    self.RULE_PROCESS_DIED,
                    subject,
                    ts,
                    None,
                    {"watch_pattern": pattern, "pid": pid, "name": pattern},
                )
                self._active_watch_pids[pattern].discard(pid)

    def _rollup_process_minutes(self, ts: float, processes: list[dict]) -> None:
        bucket = int(ts // 60)
        if self._last_minute_bucket is not None and bucket != self._last_minute_bucket:
            pass
        self._last_minute_bucket = bucket
        ts_minute = float(bucket * 60)
        for p in processes:
            if p.get("mem_rss_mb") is None:
                continue
            self.db.upsert_process_minute(ts_minute, p["pid"], p["name"], float(p["mem_rss_mb"]))

    def _eval_memory_climbing(self, ts: float) -> None:
        # Documented rule: 9 of last 10 one-minute samples strictly rise; total delta >= 50 MB
        for pid in self.db.get_process_minute_pids():
            hist = self.db.get_process_minute_history(pid, self._climb_samples)
            if len(hist) < self._climb_samples:
                continue
            mems = [h[1] for h in hist]
            rises = sum(1 for i in range(1, len(mems)) if mems[i] > mems[i - 1])
            total_delta = mems[-1] - mems[0]
            if rises >= self._climb_rising and total_delta >= self._climb_min_mb:
                name = next((h for h in self.db.get_processes_latest() if h["pid"] == pid), {}).get(
                    "name", f"pid{pid}"
                )
                subject = f"pid:{pid}"
                open_a = self.db.get_open_alert(self.RULE_MEMORY_CLIMBING, subject)
                if not open_a:
                    self._ensure_open(
                        self.RULE_MEMORY_CLIMBING,
                        subject,
                        ts,
                        total_delta,
                        {"pid": pid, "name": name, "delta_mb": total_delta, "rising_samples": rises},
                    )
            else:
                self._close_if_open(self.RULE_MEMORY_CLIMBING, f"pid:{pid}", ts)

    def _ensure_open(
        self,
        rule: str,
        subject: str,
        ts: float,
        peak: float | None,
        details: dict | None = None,
    ) -> None:
        if self.db.get_open_alert(rule, subject):
            return
        self.db.open_alert(rule, subject, ts, peak, details)

    def _close_if_open(self, rule: str, subject: str, ts: float) -> None:
        open_a = self.db.get_open_alert(rule, subject)
        if open_a:
            self.db.close_alert(open_a.id, ts)

    def _close_process_died(self, pattern: str, ts: float) -> None:
        prefix = f"{pattern}:pid"
        for alert in self.db.list_alerts(open_only=True):
            if alert.rule != self.RULE_PROCESS_DIED:
                continue
            watched = (alert.details or {}).get("watch_pattern")
            if watched == pattern or alert.subject.startswith(prefix):
                self.db.close_alert(alert.id, ts)

    def health_status(self) -> tuple[str, list[AlertRecord]]:
        open_alerts = self.db.list_alerts(open_only=True)
        critical_rules = {
            self.RULE_NETWORK_LOST,
            self.RULE_PROCESS_DIED,
            self.RULE_DISK_HIGH,
        }
        warn_rules = {
            self.RULE_CPU_HIGH,
            self.RULE_RAM_HIGH,
            self.RULE_MEMORY_CLIMBING,
            self.RULE_ZOMBIE,
        }
        if any(a.rule in critical_rules for a in open_alerts):
            return "critical", open_alerts
        if any(a.rule in warn_rules for a in open_alerts):
            return "warn", open_alerts
        return "ok", open_alerts