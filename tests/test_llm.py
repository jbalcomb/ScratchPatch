import pytest

from taskdeck.core import StepError
from taskdeck.shared import llm
from taskdeck.shared.http import HttpError

LOCAL = llm.ModelEndpoint("AI Workstation", "http://ws:8080/v1/chat/completions", "local")
CLOUD = llm.ModelEndpoint("Gemini", llm.GEMINI_URL, "gemini-flash-latest", api_key="KEY", timeout=60)


def reply(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def net(monkeypatch):
    """Fake network: `up` hosts are reachable; `answers` maps URL -> reply or exception."""
    state = {"up": {"ws", "generativelanguage.googleapis.com"}, "answers": {}, "sent": []}

    def fake_reachable(url, timeout=3):
        return any(h in url for h in state["up"])

    def fake_post(url, body, headers=None, timeout=30):
        state["sent"].append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        ans = state["answers"][url]
        if isinstance(ans, Exception):
            raise ans
        return ans

    monkeypatch.setattr(llm, "reachable", fake_reachable)
    monkeypatch.setattr(llm, "post_json", fake_post)
    return state


def test_parse_json_with_stray_text():
    assert llm.parse_json_object('Sure!\n```json\n{"day": 16}\n```') == {"day": 16}


@pytest.mark.parametrize("content", ["no json here", "{not: valid}", ""])
def test_garbage_reply_is_a_clear_error(content):
    with pytest.raises(StepError, match="AI model"):
        llm.parse_json_object(content)


def test_uses_workstation_when_up(net):
    net["answers"][LOCAL.url] = reply('{"day": 16}')
    data, used = llm.chat_json_with_fallback([LOCAL, CLOUD], "SYS", "log")
    assert data == {"day": 16} and used.label == "AI Workstation"
    sent = net["sent"][0]
    assert sent["body"]["temperature"] == 0 and sent["body"]["response_format"] == {"type": "json_object"}
    assert sent["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert sent["headers"] is None and sent["timeout"] == 120


def test_falls_back_when_workstation_offline(net):
    net["up"] = {"generativelanguage.googleapis.com"}
    net["answers"][CLOUD.url] = reply('{"day": 16}')
    data, used = llm.chat_json_with_fallback([LOCAL, CLOUD], "SYS", "log")
    assert used.label == "Gemini" and len(net["sent"]) == 1  # workstation was never sent the log
    assert net["sent"][0]["headers"] == {"Authorization": "Bearer KEY"}
    assert net["sent"][0]["body"]["model"] == "gemini-flash-latest"


def test_falls_back_when_workstation_errors(net):
    net["answers"][LOCAL.url] = HttpError("HTTP 500: model crashed", 500)
    net["answers"][CLOUD.url] = reply('{"day": 16}')
    assert llm.chat_json_with_fallback([LOCAL, CLOUD], "SYS", "log")[1].label == "Gemini"


def test_both_down_reports_each(net):
    net["up"] = {"generativelanguage.googleapis.com"}
    net["answers"][CLOUD.url] = HttpError("HTTP 429: quota exceeded", 429)
    with pytest.raises(StepError) as exc:
        llm.chat_json_with_fallback([LOCAL, CLOUD], "SYS", "log")
    assert "AI Workstation: not reachable" in str(exc.value) and "Gemini: HTTP 429" in str(exc.value)


def test_nothing_configured():
    with pytest.raises(StepError, match="No AI model"):
        llm.chat_json_with_fallback([llm.ModelEndpoint("x", "", "m")], "SYS", "log")


def test_reachable_real_socket():
    assert llm.reachable("http://127.0.0.1:1/v1", timeout=0.5) is False
    assert llm.reachable("not a url") is False
