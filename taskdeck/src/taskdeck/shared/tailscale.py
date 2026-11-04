"""Is this phone on Tailscale? So "the AI Workstation isn't reachable" can say why.

The workstation is usually reached by its Tailscale address (100.x.y.z, or a MagicDNS name). If the
phone itself isn't connected to Tailscale, it can never reach it, and the fix is on the phone (open the
Tailscale app), not on the workstation. Signals, strongest first:

* Tailscale answers on 100.100.100.100 (its own DNS service) on every device where it is connected.
* Android reports whether a VPN is up (Flet's Connectivity, via taskdeck.ui.network); Tailscale is one.
  No VPN at all means Tailscale isn't connected.
* Which local address the phone would send from to reach the workstation: a 100.64.0.0/10 address
  while a VPN is up means Tailscale carries it. (Mobile carriers use 100.64/10 too, so this one only
  counts together with the VPN.)
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Callable

TAILNET = ipaddress.ip_network("100.64.0.0/10")
QUAD100 = ("100.100.100.100", 53)
ON, OFF, UNKNOWN = "on", "off", "unknown"

# Set by the app (NetworkMonitor): True / False when Android said whether a VPN is up, None when unknown.
vpn_active: Callable[[], bool | None] = lambda: None


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url.strip()).hostname or ""


def in_tailnet(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in TAILNET
    except ValueError:
        return False


def is_tailnet_host(host: str) -> bool:
    """A Tailscale address or name: 100.64.0.0/10, *.ts.net, or a bare MagicDNS name like "ai-box"."""
    if not host or host in ("localhost",):
        return False
    if in_tailnet(host) or host.endswith(".ts.net"):
        return True
    try:
        ipaddress.ip_address(host)
        return False  # some other IP address (e.g. 192.168.1.50 on the home Wi-Fi)
    except ValueError:
        return "." not in host


def _quad100(timeout: float) -> bool:
    try:
        socket.create_connection(QUAD100, timeout=timeout).close()
        return True
    except OSError:
        return False


def _route_source(host: str) -> str | None:
    """The local address the phone would send from to reach host (nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def phone_state(host: str = "", timeout: float = 1.5) -> str:
    """ON, OFF or UNKNOWN: is Tailscale connected on this phone?"""
    if _quad100(timeout):
        return ON
    vpn = vpn_active()
    if vpn is False:
        return OFF
    source = _route_source(host) if host and in_tailnet(host) else None
    if vpn and source and in_tailnet(source):
        return ON
    if vpn is None and source and not in_tailnet(source):
        return OFF  # no VPN information, but the route to the workstation doesn't go through Tailscale
    return UNKNOWN


def why_unreachable(url: str) -> str:
    """For a URL that didn't answer: a short reason when it's about Tailscale, else ""."""
    host = host_of(url)
    if not is_tailnet_host(host):
        return ""
    state = phone_state(host)
    if state == OFF:
        return ("this phone isn't connected to Tailscale, so it can't reach the AI Workstation. "
                "Open the Tailscale app on the phone and connect")
    if state == ON:
        return (f"Tailscale is on, but {host} isn't answering: the workstation may be asleep, or Tailscale or "
                "the AI server is off there")
    return ""


def _dns_name(msg: bytes, i: int) -> tuple[str, int]:
    """Read a (possibly compressed) DNS name at offset i: (name, offset after it)."""
    labels, end, jumps = [], None, 0
    while True:
        n = msg[i]
        if n & 0xC0 == 0xC0:  # pointer to an earlier name
            if end is None:
                end = i + 2
            i = ((n & 0x3F) << 8) | msg[i + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("DNS name loop")
            continue
        if n == 0:
            return ".".join(labels), (end if end is not None else i + 1)
        labels.append(msg[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n


def reverse_lookup(ip: str, timeout: float = 2) -> str | None:
    """The MagicDNS name of a tailnet address, asked of Tailscale's own DNS (100.100.100.100), or None."""
    if not in_tailnet(ip):
        return None
    question = b"".join(bytes([len(p)]) + p.encode() for p in reversed(ip.split("."))) + b"\x07in-addr\x04arpa\x00"
    query = b"\x7a\x11\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + question + b"\x00\x0c\x00\x01"  # PTR, IN
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query, QUAD100)
            msg = s.recv(1500)
        if msg[:2] != query[:2] or msg[3] & 0x0F or int.from_bytes(msg[6:8], "big") == 0:
            return None
        _, i = _dns_name(msg, 12)
        i += 4  # question type and class
        for _ in range(int.from_bytes(msg[6:8], "big")):
            _, i = _dns_name(msg, i)
            rtype, length = int.from_bytes(msg[i:i + 2], "big"), int.from_bytes(msg[i + 8:i + 10], "big")
            if rtype == 12:
                return _dns_name(msg, i + 10)[0].rstrip(".") or None
            i += 10 + length
    except (OSError, ValueError, IndexError):
        return None
    return None


def full_name(host: str, timeout: float = 2) -> str | None:
    """The *.ts.net name for a tailnet address or a bare MagicDNS name ("ai-workstation"), or None.
    Tailscale Serve's HTTPS certificate is only valid for that name."""
    if host.endswith(".ts.net"):
        return host
    if not is_tailnet_host(host):
        return None
    try:
        ip = host if in_tailnet(host) else socket.gethostbyname(host)
    except OSError:
        return None
    name = reverse_lookup(ip, timeout)
    return name if name and name.endswith(".ts.net") else None
