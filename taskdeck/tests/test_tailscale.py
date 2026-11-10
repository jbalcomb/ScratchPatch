"""Is the phone on Tailscale? (taskdeck.shared.tailscale)"""

import pytest

from taskdeck.shared import tailscale

WS = "http://100.83.88.59:8080/v1/chat/completions"


@pytest.fixture
def phone(monkeypatch):
    """Control the three signals: Tailscale's own address, Android's VPN flag, the route's source address."""
    sig = {"quad100": False, "vpn": None, "source": None}
    monkeypatch.setattr(tailscale, "_quad100", lambda timeout: sig["quad100"])
    monkeypatch.setattr(tailscale, "vpn_active", lambda: sig["vpn"])
    monkeypatch.setattr(tailscale, "_route_source", lambda host: sig["source"])
    return sig


def test_tailnet_addresses():
    assert tailscale.is_tailnet_host("100.83.88.59") and tailscale.is_tailnet_host("ai-box.tail1234.ts.net")
    assert tailscale.is_tailnet_host("ai-box")  # MagicDNS short name
    assert not tailscale.is_tailnet_host("192.168.1.50") and not tailscale.is_tailnet_host("localhost")
    assert not tailscale.is_tailnet_host("generativelanguage.googleapis.com")
    assert tailscale.host_of(WS) == "100.83.88.59"


def test_phone_state(phone):
    phone.update(quad100=True)
    assert tailscale.phone_state("100.83.88.59") == tailscale.ON
    phone.update(quad100=False, vpn=False)  # Android: no VPN at all
    assert tailscale.phone_state("100.83.88.59") == tailscale.OFF
    phone.update(vpn=True, source="100.101.7.9")  # a VPN, and the route goes out from a tailnet address
    assert tailscale.phone_state("100.83.88.59") == tailscale.ON
    phone.update(vpn=True, source="10.0.0.5")  # some other VPN: can't tell
    assert tailscale.phone_state("100.83.88.59") == tailscale.UNKNOWN
    phone.update(vpn=None, source="192.168.1.20")  # no VPN information; route not via Tailscale
    assert tailscale.phone_state("100.83.88.59") == tailscale.OFF
    phone.update(vpn=None, source="100.70.1.1")  # mobile carriers use 100.64/10 too: don't guess
    assert tailscale.phone_state("100.83.88.59") == tailscale.UNKNOWN


def test_why_unreachable(phone):
    phone.update(vpn=False)
    assert "isn't connected to Tailscale" in tailscale.why_unreachable(WS)
    phone.update(quad100=True)
    assert "Tailscale is on, but 100.83.88.59 isn't answering" in tailscale.why_unreachable(WS)
    phone.update(quad100=False, vpn=True, source="10.0.0.5")
    assert tailscale.why_unreachable(WS) == ""  # unknown: no guess
    assert tailscale.why_unreachable("http://192.168.1.50:8080/v1") == ""  # not a Tailscale address


def test_daily_log_says_why(phone, monkeypatch):
    from taskdeck.jobs import daily_log

    monkeypatch.setattr(daily_log, "reachable", lambda url, timeout: False)
    monkeypatch.setattr(daily_log.sheets, "make_backend", lambda *a, **k: (_ for _ in ()).throw(
        daily_log.HttpError("Can't reach sheets.googleapis.com")))
    phone.update(vpn=False)
    problems = daily_log.PLUGIN.check_ready_online({"llm_url": WS, "fallback_api_key": "k"})
    text = [p.message for p in problems if "AI Workstation" in p.message][0]
    assert "this phone isn't connected to Tailscale" in text and "Gemini parses the log" in text
    assert not [p for p in problems if "AI Workstation" in p.message][0].blocking


def test_route_source_really_works():
    # nothing is sent: connect() on UDP only picks the route
    assert tailscale._route_source("127.0.0.1") == "127.0.0.1"
