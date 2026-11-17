"""Process metrics: CPU, RAM, FDs, status."""

from __future__ import annotations

import sys
import time
from typing import Any

import psutil

# Windows reports bogus multi-hundred % for the idle pseudo-process.
_SKIP_NAMES = frozenset({"System Idle Process"})
_CPU_COUNT = psutil.cpu_count(logical=True) or 1


def _skip_process(pid: int, name: str | None) -> bool:
    if pid == 0:
        return True
    return (name or "") in _SKIP_NAMES


def collect_process_metrics(ts: float | None = None) -> list[dict[str, Any]]:
    ts = ts or time.time()
    out: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "status", "memory_info"]):
        try:
            info = proc.info
            name = info["name"] or f"pid{info['pid']}"
            pid = int(info["pid"])
            if _skip_process(pid, name):
                continue
            mem = info.get("memory_info")
            rss_mb = (mem.rss / 1e6) if mem else None
            num_fds = None
            # open_files() per process on Windows is very slow and can starve the agent under CPU load.
            if sys.platform != "win32":
                try:
                    num_fds = proc.num_fds() if hasattr(proc, "num_fds") else len(proc.open_files())
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
            try:
                cpu_raw = float(proc.cpu_percent(interval=None))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                cpu_raw = 0.0
            # psutil: process % can exceed 100 on multi-core; scale to whole-machine share.
            cpu_share = min(100.0, max(0.0, cpu_raw / _CPU_COUNT))
            out.append(
                {
                    "ts": ts,
                    "pid": pid,
                    "name": name,
                    "cpu_percent": cpu_share,
                    "cpu_percent_raw": cpu_raw,
                    "mem_rss_mb": rss_mb,
                    "status": info.get("status"),
                    "num_fds": num_fds,
                }
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return out