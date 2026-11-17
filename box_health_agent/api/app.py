"""HTTP API and dashboard."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from datetime import datetime

from box_health_agent.agent import HealthAgent

RULE_LABELS: dict[str, str] = {
    "cpu_high": "CPU high",
    "ram_high": "RAM high",
    "disk_high": "Disk almost full",
    "network_lost": "Network / uplink lost",
    "memory_climbing": "Process memory leak",
    "process_died": "Watched process died",
    "zombie": "Zombie process",
}

RULE_HINTS: dict[str, str] = {
    "cpu_high": "Total CPU stayed above 85% for 60+ seconds.",
    "ram_high": "System RAM usage is above 90%.",
    "disk_high": "A filesystem mount is above 90% capacity.",
    "network_lost": "Gateway or internet reachability failed 3 probes in a row.",
    "memory_climbing": "A process RSS rose in 9 of 10 minute samples (≥50 MB).",
    "process_died": "A name on your watch list is no longer running.",
    "zombie": "A process has been in zombie (Z) state for 30+ seconds.",
}


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _decorate_processes(procs: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    thresholds = cfg.get("process_flags", {"cpu_percent": 50, "mem_mb": 1024})
    cpu_lim = float(thresholds.get("cpu_percent", 50))
    mem_lim = float(thresholds.get("mem_mb", 1024))
    watch = cfg.get("process_watch", {}).get("watch_list", [])
    for p in procs:
        name = p.get("name") or ""
        p["flag_high_cpu"] = (p.get("cpu_percent") or 0) >= cpu_lim
        p["flag_high_mem"] = (p.get("mem_rss_mb") or 0) >= mem_lim
        status = (p.get("status") or "").upper()
        p["flag_zombie"] = status in ("Z", "ZOMBIE")
        p["watched"] = any(pat in name or name == pat for pat in watch)
    return procs


def _latest_probe_map(probes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for p in probes:
        name = p.get("probe") or ""
        if name not in latest or p["ts"] > latest[name]["ts"]:
            latest[name] = p
    return latest


def _alert_to_dict(a) -> dict[str, Any]:
    return {
        "id": a.id,
        "rule": a.rule,
        "subject": a.subject,
        "opened_at": a.opened_at,
        "closed_at": a.closed_at,
        "peak_value": a.peak_value,
        "details": a.details,
    }


def create_app(agent: HealthAgent) -> FastAPI:
    app = FastAPI(title="Box Health Agent", version="1.0.0")
    base = Path(__file__).resolve().parent.parent / "dashboard"
    templates = Jinja2Templates(directory=str(base / "templates"))
    if (base / "static").exists():
        app.mount("/static", StaticFiles(directory=str(base / "static")), name="static")

    @app.get("/health")
    def health():
        status, open_alerts = agent.alerts.health_status()
        cpu = (
            agent.db.latest_metric_value("cpu.total_percent")
            or agent.db.recent_metric_max("cpu.total_percent")
        )
        ram = agent.db.latest_metric_value("mem.ram_percent")
        disk = agent.db.latest_disk_usage("C:")
        disk_mount, disk_pct = disk if disk else (None, None)
        return {
            "status": status,
            "open_alerts": [_alert_to_dict(a) for a in open_alerts],
            "open_count": len(open_alerts),
            "ts": time.time(),
            "snapshot": {
                "cpu_percent": cpu,
                "ram_percent": ram,
                "disk_mount": disk_mount,
                "disk_percent": disk_pct,
            },
        }

    @app.get("/metrics")
    def metrics(name: str | None = None, since: float | None = None):
        data = agent.db.query_metrics(name=name, since=since)
        return {"metrics": data, "count": len(data)}

    @app.get("/alerts")
    def alerts(open: bool | None = Query(None, alias="open")):
        if open is True:
            items = agent.db.list_alerts(open_only=True)
        elif open is False:
            items = agent.db.list_alerts(open_only=False)
        else:
            items = agent.db.list_alerts(open_only=None)
        return {"alerts": [_alert_to_dict(a) for a in items]}

    @app.get("/processes")
    def processes(sort: str = "cpu"):
        procs = _decorate_processes(agent.db.get_processes_latest(), agent.cfg)
        if sort == "cpu":
            procs.sort(key=lambda p: (p.get("cpu_percent") or 0), reverse=True)
        elif sort == "ram" or sort == "mem":
            procs.sort(key=lambda p: (p.get("mem_rss_mb") or 0), reverse=True)
        return {"processes": procs, "ts": time.time()}

    @app.get("/probes")
    def probes(since: float | None = None):
        since = since or (time.time() - 3600)
        return {"probes": agent.db.recent_probes(since)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        now = time.time()
        status, open_alerts = agent.alerts.health_status()
        closed = agent.db.list_alerts(open_only=False, limit=50)
        metrics_1h = agent.db.query_metrics(name="cpu.total_percent", since=now - 3600)
        metrics_24h = agent.db.query_metrics(name="cpu.total_percent", since=now - 86400)
        ram_1h = agent.db.query_metrics(name="mem.ram_percent", since=now - 3600)
        ram_24h = agent.db.query_metrics(name="mem.ram_percent", since=now - 86400)
        probes = agent.db.recent_probes(now - 3600)
        probe_latest = _latest_probe_map(probes)
        uptime = agent.db.latest_metric_value("uptime_sec")
        cpu_now = (
            agent.db.latest_metric_value("cpu.total_percent")
            or agent.db.recent_metric_max("cpu.total_percent")
        )
        ram_now = agent.db.latest_metric_value("mem.ram_percent")
        disk_info = agent.db.latest_disk_usage("C:")
        disk_mount, disk_pct = disk_info if disk_info else (None, None)
        procs = _decorate_processes(agent.db.get_processes_latest(), agent.cfg)
        procs.sort(key=lambda p: (p.get("cpu_percent") or 0), reverse=True)
        flagged = [p for p in procs if p["flag_high_cpu"] or p["flag_high_mem"] or p["flag_zombie"] or p["watched"]]
        top_procs = procs[:30]
        watch_list = agent.cfg.get("process_watch", {}).get("watch_list", [])
        gw_probe = probe_latest.get("gateway_reachable", {})
        uplink_ok = gw_probe.get("success", True)
        internet_ok = probe_latest.get("internet_ping", {}).get("success", True)
        gateway_ip = gw_probe.get("subject") or ""
        status_blurb = {
            "ok": "All monitored checks are within limits. No open alerts.",
            "warn": "One or more warning conditions are active. Review open alerts below.",
            "critical": "Critical issues detected — machine or uplink may be impaired.",
        }.get(status, "")
        critical_rules = {"network_lost", "process_died", "disk_high"}
        alert_critical = sum(1 for a in open_alerts if a.rule in critical_rules)
        alert_warn = len(open_alerts) - alert_critical
        metrics_iv = int(agent.cfg.get("sampling", {}).get("metrics_interval_sec", 5))
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "poll_interval_sec": metrics_iv,
                "full_reload_sec": max(30, metrics_iv * 6),
                "health_status": status,
                "health_blurb": status_blurb,
                "server_time": _fmt_ts(now),
                "open_alerts": open_alerts,
                "closed_alerts": closed,
                "cpu_1h": metrics_1h,
                "cpu_24h": metrics_24h,
                "ram_1h": ram_1h,
                "ram_24h": ram_24h,
                "probes": probes[-200:],
                "probe_latest": probe_latest,
                "snapshot": {
                    "cpu_percent": cpu_now,
                    "ram_percent": ram_now,
                    "disk_mount": disk_mount,
                    "disk_percent": disk_pct,
                    "uptime_sec": uptime,
                    "uplink_ok": uplink_ok,
                    "internet_ok": internet_ok,
                },
                "top_procs": top_procs,
                "flagged_procs": flagged[:15],
                "watch_list": watch_list,
                "gateway_ip": gateway_ip,
                "alert_critical": alert_critical,
                "alert_warn": alert_warn,
                "rule_labels": RULE_LABELS,
                "rule_hints": RULE_HINTS,
                "fmt_ts": _fmt_ts,
            },
        )

    return app