"""Resolve default gateway from config or OS routing table."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any


def _norm(v: str) -> str:
    return v.strip()


def _ipv4(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s))


def _gateway_windows_ipconfig() -> str | None:
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        cp = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        for line in (cp.stdout or "").splitlines():
            if "Default Gateway" not in line:
                continue
            m = re.search(r":\s*([\d.]+)\s*$", line.strip())
            if m and _ipv4(m.group(1)) and m.group(1) != "0.0.0.0":
                return m.group(1)
    except Exception:
        pass
    return None


def _gateway_windows_route() -> str | None:
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        cp = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=flags,
        )
        for line in (cp.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                if _ipv4(gw) and gw != "0.0.0.0":
                    return gw
    except Exception:
        pass
    return None


def _gateway_linux() -> str | None:
    try:
        cp = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        m = re.search(r"default via (\S+)", cp.stdout or "")
        if m and _ipv4(m.group(1)):
            return m.group(1)
    except Exception:
        pass
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            next(f, None)
            for line in f:
                cols = line.split()
                if len(cols) >= 3 and cols[1] == "00000000":
                    raw = int(cols[2])
                    gw = ".".join(str((raw >> (8 * i)) & 0xFF) for i in range(4))
                    if _ipv4(gw) and gw != "0.0.0.0":
                        return gw
    except Exception:
        pass
    return None


def _gateway_fallback_guess() -> str | None:
    try:
        import socket

        import psutil

        addrs = psutil.net_if_addrs()
        for iface, stats in psutil.net_if_stats().items():
            if not stats.isup:
                continue
            for addr in addrs.get(iface, []):
                if addr.family == socket.AF_INET and addr.broadcast:
                    parts = addr.address.split(".")
                    if len(parts) == 4:
                        return ".".join(parts[:3] + ["1"])
    except Exception:
        pass
    return None


def detect_default_gateway() -> tuple[str | None, str]:
    """Return (gateway_ip, source_label)."""
    if sys.platform == "win32":
        gw = _gateway_windows_ipconfig() or _gateway_windows_route()
        if gw:
            return gw, "windows_route"
    else:
        gw = _gateway_linux()
        if gw:
            return gw, "linux_route"
    guess = _gateway_fallback_guess()
    if guess:
        return guess, "guess"
    return None, "none"


def resolve_gateway(cfg: dict[str, Any]) -> tuple[str | None, str]:
    np = cfg.get("network_probes") or {}
    explicit = np.get("gateway_host")
    if explicit is not None and _norm(str(explicit)):
        return _norm(str(explicit)), "config"
    return detect_default_gateway()