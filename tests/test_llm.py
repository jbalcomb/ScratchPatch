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
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
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


CURL = """curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent" \\
  -H 'Content-Type: application/json' \\
  -H 'X-goog-api-key: AQ.Ab8-example_KEY.value1234567890' \\
  -X POST -d '{"contents": []}'"""


@pytest.mark.parametrize("pasted", [
    "AQ.Ab8-example_KEY.value1234567890",
    "  'AQ.Ab8-example_KEY.value1234567890'\n",
    CURL,
    "X-goog-api-key: AQ.Ab8-example_KEY.value1234567890",
    "Authorization: Bearer AQ.Ab8-example_KEY.value1234567890",
    "https://generativelanguage.googleapis.com/v1beta/models?key=AQ.Ab8-example_KEY.value1234567890",
])
def test_api_key_extracted_from_whatever_was_pasted(pasted):
    assert llm.extract_api_key(pasted) == "AQ.Ab8-example_KEY.value1234567890"


def test_curl_paste_is_sent_as_bearer(net):
    net["up"] = {"generativelanguage.googleapis.com"}
    net["answers"][CLOUD.url] = reply('{"day": 16}')
    endpoint = llm.ModelEndpoint("Gemini", llm.GEMINI_URL, "gemini-flash-latest", api_key=CURL)
    llm.chat_json_with_fallback([endpoint], "SYS", "log")
    assert net["sent"][0]["headers"] == {"Authorization": "Bearer AQ.Ab8-example_KEY.value1234567890"}


def test_busy_model_is_retried(net, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls = iter([HttpError("HTTP 503: high demand", 503), reply('{"day": 16}')])

    def flaky(url, body, headers=None, timeout=30):
        ans = next(calls)
        if isinstance(ans, Exception):
            raise ans
        return ans

    monkeypatch.setattr(llm, "post_json", flaky)
    assert llm.chat_json(CLOUD, "SYS", "log") == {"day": 16}


def test_gives_up_after_retries(net, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    net["answers"][CLOUD.url] = HttpError("HTTP 503: high demand", 503)
    with pytest.raises(StepError, match="Gemini: HTTP 503"):
        llm.chat_json(CLOUD, "SYS", "log")
    assert len(net["sent"]) == 3


def test_list_shaped_error_body():
    from taskdeck.shared.http import _error_detail
    body = '[{"error": {"code": 503, "message": "This model is currently experiencing high demand."}}]'
    assert _error_detail(body) == "This model is currently experiencing high demand."
