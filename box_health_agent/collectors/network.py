"""Network metrics: bandwidth rates, errors, TCP, listening ports."""

from __future__ import annotations

import socket
import time

import psutil

from .rate import RateCounterStore

_net_rates = RateCounterStore()


def _count_listening_ports() -> int:
    count = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.status == psutil.CONN_LISTEN:
            count += 1
    return count


def _count_tcp_connections() -> int:
    n = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.type == socket.SOCK_STREAM and conn.status != psutil.CONN_LISTEN:
            n += 1
    return n


def collect_network_metrics(ts: float | None = None) -> list[tuple[str, str, float | None, dict | None]]:
    ts = ts or time.time()
    rows: list[tuple[str, str, float | None, dict | None]] = []

    try:
        tcp_count = _count_tcp_connections()
        listen_count = _count_listening_ports()
        rows.append(("network", "tcp.connections", float(tcp_count), None))
        rows.append(("network", "tcp.listening_ports", float(listen_count), None))
    except (psutil.AccessDenied, PermissionError):
        rows.append(("network", "tcp.connections", None, {"error": "access_denied"}))

    stats = psutil.net_io_counters(pernic=True)
    for iface, c in stats.items():
        up_bps = _net_rates.rate(f"net.{iface}.bytes_sent", ts, float(c.bytes_sent))
        down_bps = _net_rates.rate(f"net.{iface}.bytes_recv", ts, float(c.bytes_recv))
        if up_bps is not None:
            rows.append(("network", f"net.upload_bps.{iface}", up_bps, {"iface": iface}))
        if down_bps is not None:
            rows.append(("network", f"net.download_bps.{iface}", down_bps, {"iface": iface}))

        drop_rate = _net_rates.rate(f"net.{iface}.dropin", ts, float(c.dropin + c.dropout))
        err_rate = _net_rates.rate(f"net.{iface}.errin", ts, float(c.errin + c.errout))
        if drop_rate is not None:
            rows.append(("network", f"net.drops_per_sec.{iface}", drop_rate, None))
        if err_rate is not None:
            rows.append(("network", f"net.errors_per_sec.{iface}", err_rate, None))

    return rows