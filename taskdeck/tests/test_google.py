import base64
import json
import urllib.parse

import pytest
import rsa
from pyasn1.codec.der import encoder
from pyasn1.type import univ

from taskdeck.shared import google
from taskdeck.shared.http import HttpError


@pytest.fixture(scope="module")
def keypair():
    return rsa.newkeys(1024)


def pkcs8_pem(private: rsa.PrivateKey) -> str:
    """Wrap a PKCS#1 key as PKCS#8, the format Google issues."""
    alg = univ.Sequence()
    alg.setComponentByPosition(0, univ.ObjectIdentifier("1.2.840.113549.1.1.1"))
    alg.setComponentByPosition(1, univ.Null(""))
    info = univ.Sequence()
    info.setComponentByPosition(0, univ.Integer(0))
    info.setComponentByPosition(1, alg)
    info.setComponentByPosition(2, univ.OctetString(private.save_pkcs1("DER")))
    b64 = base64.encodebytes(encoder.encode(info)).decode()
    return f"-----BEGIN PRIVATE KEY-----\n{b64}-----END PRIVATE KEY-----\n"


def key_json(private) -> str:
    return json.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
                       "private_key": pkcs8_pem(private), "private_key_id": "kid1",
                       "token_uri": "https://oauth2.googleapis.com/token"})


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_assertion_is_a_valid_rs256_jwt(keypair):
    public, private = keypair
    key = google.ServiceAccountKey.from_json(key_json(private))
    jwt = google.make_assertion(key, google.SHEETS_SCOPE, now=1000)
    head, claims, sig = jwt.split(".")
    assert json.loads(b64d(head)) == {"alg": "RS256", "typ": "JWT", "kid": "kid1"}
    assert json.loads(b64d(claims)) == {"iss": "bot@proj.iam.gserviceaccount.com", "scope": google.SHEETS_SCOPE,
                                        "aud": "https://oauth2.googleapis.com/token", "iat": 1000, "exp": 4600}
    assert rsa.verify(f"{head}.{claims}".encode(), b64d(sig), public) == "SHA-256"


@pytest.mark.parametrize("text,msg", [("not json", "isn.t JSON"), ('{"type": "authorized_user"}', "service account"),
                                      ('{"type": "service_account"}', "missing")])
def test_bad_key_files(text, msg):
    with pytest.raises(google.GoogleError, match=msg):
        google.ServiceAccountKey.from_json(text)


def test_token_cached_and_sheets_calls(keypair, monkeypatch):
    calls = []

    def fake_request(method, url, *, json_body=None, form=None, headers=None, timeout=30):
        calls.append((method, url, json_body, form, headers))
        if "oauth2" in url:
            return {"access_token": "tok", "expires_in": 3600}
        if url.endswith(":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"):
            return {}
        return {"values": [["", "Departure Time"]]}

    monkeypatch.setattr(google, "request", fake_request)
    monkeypatch.setattr(google, "get_json", lambda url, **kw: fake_request("GET", url, **kw))
    monkeypatch.setattr(google, "post_json", lambda url, body, **kw: fake_request("POST", url, json_body=body, **kw))

    creds = google.ServiceAccountCredentials(google.ServiceAccountKey.from_json(key_json(keypair[1])))
    client = google.SheetsClient(creds, "SHEET")
    assert client.get_values(google.a1_range("Sheet1", "A1:O1")) == [["", "Departure Time"]]
    client.append_row(google.a1_range("Sheet1", "A1"), ["10/16/2026", 52])

    token_calls = [c for c in calls if "oauth2" in c[1]]
    assert len(token_calls) == 1  # cached for the second call
    assert token_calls[0][3]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    get = calls[1]
    assert urllib.parse.unquote(get[1]).endswith("/SHEET/values/'Sheet1'!A1:O1")
    assert get[4] == {"Authorization": "Bearer tok"}
    assert calls[2][2] == {"values": [["10/16/2026", 52]]}


def test_sheets_errors_are_explained(keypair, monkeypatch):
    creds = google.ServiceAccountCredentials(google.ServiceAccountKey.from_json(key_json(keypair[1])))
    monkeypatch.setattr(creds, "token", lambda: "tok")
    client = google.SheetsClient(creds, "SHEET")

    def forbidden(url, **kw):
        raise HttpError("HTTP 403: The caller does not have permission", 403)

    monkeypatch.setattr(google, "get_json", forbidden)
    with pytest.raises(google.GoogleError, match="Share the sheet with bot@proj"):
        client.get_values("'Sheet1'!A1:O1")
