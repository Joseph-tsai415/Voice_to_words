"""Which of this machine's addresses a phone on the same network can reach.

The QR code in the 新增會議 dialog carries one address, so it has to be the
right one. A laptop here typically holds several: the Wi-Fi, WSL's
`vEthernet (WSL) 172.24.80.1`, maybe a VPN, and - when the Windows Mobile
Hotspot is on - `192.168.137.1`.

The hotspot wins whenever it is up. It shares an existing connection, so a
default route through the office Wi-Fi normally still exists while it is
on, and choosing by default route would print the office address - the one
the user turned the hotspot on to get away from, because that Wi-Fi keeps
devices from seeing each other.

Adapter names would make this tidier but are not available without psutil
or parsing `ipconfig`, whose output is localised. Ranking by address range
covers the same cases: WSL and Hyper-V sit in 172.16/12, so that range goes
last. Only the three RFC 1918 ranges are eligible at all - not
`ipaddress.is_private`, which on some Python versions admits CGNAT
(100.64/10, where Tailscale lives) and a phone cannot reach that.
"""
from __future__ import annotations

import ipaddress
import socket

_HOTSPOT = ipaddress.ip_network("192.168.137.0/24")     # Windows ICS default
_RANKED = [ipaddress.ip_network("192.168.0.0/16"),
           ipaddress.ip_network("10.0.0.0/8"),
           ipaddress.ip_network("172.16.0.0/12")]


def _rank(addr: str) -> int | None:
    """Position in _RANKED, or None when a phone could not reach it."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    for i, net in enumerate(_RANKED):
        if ip in net:
            return i
    return None


def pick(candidates: list[str], default_route: str | None) -> str | None:
    """The address to put in the QR code, or None when there is no usable one."""
    eligible = [a for a in candidates if _rank(a) is not None]
    for addr in eligible:
        if ipaddress.ip_address(addr) in _HOTSPOT:
            return addr
    if default_route and _rank(default_route) is not None:
        return default_route
    eligible.sort(key=_rank)             # stable: keeps the OS order within a range
    return eligible[0] if eligible else None


def candidates() -> list[str]:
    """IPv4 addresses of the adapters that are up."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


def default_route_ip() -> str | None:
    """The local address the OS would use to leave the machine.

    connect() on a UDP socket only picks a route; nothing is sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ip() -> str | None:
    return pick(candidates(), default_route_ip())


__all__ = ["candidates", "default_route_ip", "lan_ip", "pick"]
