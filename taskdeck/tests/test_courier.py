"""Send to Devices (Courier), against a fake gateway; and the pure-Python Ed25519 it signs with."""

import base64
import json
import os
from datetime import datetime, timezone

import pytest

from taskdeck.core import StepError
from taskdeck.jobs import courier as courier_job
from taskdeck.jobs.courier import client
from taskdeck.shared import ed25519
from taskdeck.shared.http import HttpError

# RFC 8032 section 7.1, tests 1 and 2.
RFC_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
]

SEED = bytes(range(32))
CREDS_JSON = json.dumps({"sender": "phone", "secret": "s3cret", "private_key": base64.b64encode(SEED).decode(),
                         "gateway": "https://ws.tail.ts.net:8766"}, indent=2)
SETTINGS = {"credentials": CREDS_JSON, "gateway_url": ""}
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("seed,public,message,signature", RFC_VECTORS)
def test_ed25519_matches_rfc_8032(seed, public, message, signature):
    seed, message = bytes.fromhex(seed), bytes.fromhex(message)
    assert ed25519.public_key(seed).hex() == public
    assert ed25519.sign(seed, message).hex() == signature
    assert ed25519.verify(bytes.fromhex(public), message, bytes.fromhex(signature))


def test_ed25519_rejects_tampering():
    sig = ed25519.sign(SEED, b"hello")
    public = ed25519.public_key(SEED)
    assert ed25519.verify(public, b"hello", sig)
    assert not ed25519.verify(public, b"hellp", sig)
    assert not ed25519.verify(public, b"hello", sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not ed25519.verify(ed25519.public_key(bytes(32)), b"hello", sig)


def test_ed25519_agrees_with_the_cryptography_package():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    for _ in range(5):
        seed, message = os.urandom(32), os.urandom(50)
        key = crypto.Ed25519PrivateKey.from_private_bytes(seed)
        assert ed25519.sign(seed, message) == key.sign(message)
        assert ed25519.public_key(seed) == key.public_key().public_bytes_raw()


def test_credentials_as_json_or_paste_string():
    creds = client.parse_credentials(CREDS_JSON)
    assert creds["sender"] == "phone"
    paste = client.PASTE_PREFIX + base64.urlsafe_b64encode(CREDS_JSON.encode()).decode().rstrip("=")
    assert client.parse_credentials(paste) == creds
    for bad in ["", "{}", "not json", client.PASTE_PREFIX + "!!!", json.dumps({"sender": "p", "secret": "s",
                                                                                "private_key": "c2hvcnQ="})]:
        with pytest.raises(StepError):
            client.parse_credentials(bad)


def test_submission_is_signed_over_courier_canonical_json():
    creds = client.parse_credentials(CREDS_JSON)
    sub = client.build_submission(creds, "clip", "git status", now=NOW, randbytes=lambda n: b"\x01" * n)
    fields = {k: v for k, v in sub.items() if k != "sender_sig"}
    assert fields["created"] == "2026-11-07T12:00:00Z" and fields["expires"] == "2026-11-07T12:10:00Z"
    assert fields["mode"] == "fanout" and fields["content_type"] == "text/plain" and fields["v"] == 1
    assert len(fields["id"]) == 26 and set(fields["id"]) <= set(client.CROCKFORD)
    assert len(base64.b64decode(fields["nonce"])) == 16
    signed = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert ed25519.verify(ed25519.public_key(SEED), signed, base64.b64decode(sub["sender_sig"]))


@pytest.fixture
def gateway(monkeypatch):
    calls = []

    def fake_request(method, url, json_body=None, headers=None, timeout=30):
        calls.append((method, url, json_body, headers))
        if json_body == {}:
            raise HttpError("HTTP 400", 400, json.dumps({"error": "missing fields: v, id"}))
        if json_body["body"] == "<b>":
            raise HttpError("HTTP 422", 422, json.dumps({"error": "content type 'text/html' is not allowed"}))
        if json_body["body"] == "flood":
            raise HttpError("HTTP 429", 429, "{}")
        return {"id": json_body["id"], "channel": json_body["channel"],
                "labels": ["origin_human", "shell_chars"]}

    monkeypatch.setattr(client, "request", fake_request)
    return calls


def test_send_runs_the_pipeline(gateway):
    report = courier_job.PLUGIN.run({"channel": "clip", "message": "ls | head"}, settings=SETTINGS)
    assert report.ok, report.error
    method, url, body, headers = gateway[0]
    assert (method, url, headers["Authorization"]) == ("POST", "https://ws.tail.ts.net:8766/messages", "Bearer s3cret")
    assert body["body"] == "ls | head" and body["channel"] == "clip"
    assert report.result.title == "Sent to clip"
    assert dict(report.result.rows)["Screening"] == "shell characters"


def test_gateway_url_setting_overrides_the_credentials(gateway):
    courier_job.PLUGIN.run({"channel": "clip", "message": "hi"},
                           settings={**SETTINGS, "gateway_url": "https://other.ts.net:8766/"})
    assert gateway[0][1] == "https://other.ts.net:8766/messages"


@pytest.mark.parametrize("message,channel,error", [
    ("<b>", "clip", "Screening rejected it"),
    ("flood", "clip", "30 messages a minute"),
    ("hi", "Clip Board", "lowercase letters"),
])
def test_errors_are_explained(gateway, message, channel, error):
    report = courier_job.PLUGIN.run({"channel": channel, "message": message}, settings=SETTINGS)
    assert not report.ok and error in report.error


def test_test_connection(gateway):
    assert client.test_connection(SETTINGS) == "Connected as phone. The gateway accepts these credentials."
    assert gateway[0][2] == {}, "the check sends an empty submission, never a message"


def test_test_connection_with_an_unknown_sender(monkeypatch):
    def refuse(*args, **kw):
        raise HttpError("HTTP 401", 401, json.dumps({"error": "unknown sender"}))

    monkeypatch.setattr(client, "request", refuse)
    assert "doesn't know this sender" in client.test_connection(SETTINGS)


def test_message_is_never_kept_in_history():
    kept = courier_job.PLUGIN.history_inputs({"channel": "clip", "message": "my password"})
    assert kept == {"channel": "clip"}


@pytest.mark.parametrize("settings,problem", [
    ({}, "Sender credentials"),
    ({"credentials": "junk"}, "aren't readable"),
    ({**SETTINGS, "gateway_url": "ws.tail.ts.net"}, "must start with https://"),
    ({**SETTINGS, "gateway_url": "http://127.0.0.1:8766"}, "points at the phone"),
])
def test_check_ready(settings, problem):
    problems = courier_job.PLUGIN.check_ready(settings)
    assert problems and problem in problems[0].message


def test_ready_with_good_settings():
    assert courier_job.PLUGIN.check_ready(SETTINGS) == []
