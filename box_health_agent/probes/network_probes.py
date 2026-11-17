"""Async-safe network probes with hard timeouts (no stall on hung DNS)."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

import httpx

from box_health_agent.probes.gateway import resolve_gateway

try:
    import dns.resolver
except ImportError:
    dns = None  # type: ignore

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")


def _resolve_dns(host: str, timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    if dns is not None:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        try:
            resolver.resolve(host, "A")
            ms = (time.perf_counter() - start) * 1000
            return True, ms, "ok"
        except Exception as e:
            ms = (time.perf_counter() - start) * 1000
            return False, ms, str(e)
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(host, None)
        ms = (time.perf_counter() - start) * 1000
        return True, ms, "ok"
    except OSError as e:
        ms = (time.perf_counter() - start) * 1000
        return False, ms, str(e)
    finally:
        socket.setdefaulttimeout(old)


def _tcp_connect(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = (time.perf_counter() - start) * 1000
            return True, ms, "ok"
    except OSError as e:
        ms = (time.perf_counter() - start) * 1000
        return False, ms, str(e)


def _ping_once(host: str, timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        rc = subprocess.run(cmd, capture_output=True, timeout=timeout + 1)
        ms = (time.perf_counter() - start) * 1000
        return rc.returncode == 0, ms, "ok" if rc.returncode == 0 else "fail"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        ms = (time.perf_counter() - start) * 1000
        return False, ms, str(e)


def _http_probe(url: str, timeout: float) -> tuple[bool, float, str]:
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
        ms = (time.perf_counter() - start) * 1000
        ok = 200 <= r.status_code < 400
        return ok, ms, f"status={r.status_code}"
    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        return False, ms, str(e)


def _run_with_timeout(fn, timeout: float) -> tuple[bool, float, str]:
    fut = _executor.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        return False, timeout * 1000, "timeout"
    except Exception as e:
        return False, 0.0, str(e)


def run_network_probes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Run all probes; each has independent timeout."""
    np = cfg.get("network_probes", {})
    timeouts = np.get("timeouts", {})
    dns_t = float(timeouts.get("dns_sec", 3))
    tcp_t = float(timeouts.get("tcp_sec", 3))
    ping_t = float(timeouts.get("ping_sec", 2))

    host_dns = np.get("dns_host", "google.com")
    tcp_host = np.get("tcp_host", "1.1.1.1")
    tcp_port = int(np.get("tcp_port", 53))
    internet = np.get("internet_ping_host", "8.8.8.8")
    gateway, gw_source = resolve_gateway(cfg)

    results: list[dict[str, Any]] = []

    ok, ms, detail = _run_with_timeout(lambda: _resolve_dns(host_dns, dns_t), dns_t + 0.5)
    results.append({"probe": "dns", "success": ok, "latency_ms": ms, "detail": detail, "subject": host_dns})

    ok, ms, detail = _run_with_timeout(lambda: _tcp_connect(tcp_host, tcp_port, tcp_t), tcp_t + 0.5)
    results.append({"probe": "tcp_connect", "success": ok, "latency_ms": ms, "detail": detail, "subject": tcp_host})

    gw_ok = False
    if gateway:
        ok, ms, detail = _run_with_timeout(lambda: _ping_once(gateway, ping_t), ping_t + 1)
        gw_ok = ok
        results.append(
            {
                "probe": "gateway_ping",
                "success": ok,
                "latency_ms": ms,
                "detail": detail,
                "subject": gateway,
                "detail": f"{detail}; via {gw_source}",
            }
        )
        results.append(
            {
                "probe": "gateway_reachable",
                "success": ok,
                "latency_ms": ms,
                "detail": f"{detail}; via {gw_source}",
                "subject": gateway,
            }
        )

    ok, ms, detail = _run_with_timeout(lambda: _ping_once(internet, ping_t), ping_t + 1)
    results.append(
        {"probe": "internet_ping", "success": ok, "latency_ms": ms, "detail": detail, "subject": internet}
    )

    packet_loss = 0.0 if (gw_ok and ok) else (100.0 if not gw_ok else 50.0)
    results.append(
        {
            "probe": "packet_loss_estimate",
            "success": gw_ok and ok,
            "latency_ms": None,
            "detail": f"gw={'up' if gw_ok else 'down'}",
            "subject": "gateway+internet",
            "packet_loss_percent": packet_loss,
        }
    )

    return results