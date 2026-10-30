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
