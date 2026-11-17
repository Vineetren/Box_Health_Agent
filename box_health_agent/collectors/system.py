"""System metrics: CPU, RAM, disk, load, uptime."""

from __future__ import annotations

import os
import platform
import time
from typing import Any

import psutil

from .rate import RateCounterStore

_disk_io_rates = RateCounterStore()
# Short blocking sample — interval=None often returns 0 if two collectors run too close together.
_CPU_SAMPLE_SEC = 0.15


def collect_system_metrics(ts: float | None = None) -> list[tuple[str, str, float | None, dict | None]]:
    ts = ts or time.time()
    rows: list[tuple[str, str, float | None, dict | None]] = []

    cpu_per = psutil.cpu_percent(interval=_CPU_SAMPLE_SEC, percpu=True)
    total_cpu = sum(cpu_per) / len(cpu_per) if cpu_per else 0.0
    rows.append(("system", "cpu.total_percent", total_cpu, {"per_core": cpu_per}))

    mem = psutil.virtual_memory()
    rows.append(("system", "mem.ram_percent", mem.percent, {"used_mb": mem.used / 1e6, "total_mb": mem.total / 1e6}))
    swap = psutil.swap_memory()
    rows.append(
        ("system", "mem.swap_percent", swap.percent, {"used_mb": swap.used / 1e6, "total_mb": swap.total / 1e6})
    )

    load_avg: tuple[float, float, float] | None = None
    if hasattr(os, "getloadavg"):
        load_avg = os.getloadavg()
        rows.append(
            ("system", "load.1", load_avg[0], {"5": load_avg[1], "15": load_avg[2]})
        )

    boot = psutil.boot_time()
    rows.append(("system", "uptime_sec", ts - boot, None))

    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        subject = part.mountpoint.replace("\\", "/")
        rows.append(
            (
                "system",
                f"disk.usage_percent.{subject}",
                usage.percent,
                {"mount": part.mountpoint, "device": part.device},
            )
        )

    dio = psutil.disk_io_counters(perdisk=True)
    if dio:
        for name, counters in dio.items():
            read_bps = _disk_io_rates.rate(f"disk.{name}.read", ts, float(counters.read_bytes))
            write_bps = _disk_io_rates.rate(f"disk.{name}.write", ts, float(counters.write_bytes))
            if read_bps is not None:
                rows.append(("system", f"disk.io.read_bps.{name}", read_bps, None))
            if write_bps is not None:
                rows.append(("system", f"disk.io.write_bps.{name}", write_bps, None))

    rows.append(("system", "host.platform", 0.0, {"platform": platform.platform()}))
    return rows