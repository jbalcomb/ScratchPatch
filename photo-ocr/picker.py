"""Google Photos Picker: the person picks photos in Google Photos; the service downloads them into Immich.

The Library API can no longer read photos an app didn't create (PRD.md), so this is the only way in from
Google Photos. It needs a Google Cloud project with the Photos Picker API enabled and an OAuth client of
type "Web application" whose redirect URI is <RECEIPTS_PUBLIC_URL>/picker/callback (README.md). Written
from Google's Picker API reference; not yet run against Google.
"""
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://photospicker.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"


class PickerError(Exception):
    pass


class Picker:
    def __init__(self, store, client_id, client_secret, public_url, api=API, token_url=TOKEN_URL):
        self.store, self.client_id, self.client_secret = store, client_id, client_secret
        self.redirect_uri = (public_url or "").rstrip("/") + "/picker/callback"
        self.api, self.token_url = api, token_url

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)

    @property
    def signed_in(self):
        return bool((self.store.get("google_token") or {}).get("refresh_token"))

    # ---- OAuth (authorization code, offline access) ----

    def auth_url(self):
        state = secrets.token_urlsafe(16)
        self.store.put("google_oauth_state", state)
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state})

    def finish_auth(self, code, state):
        if not state or state != self.store.get("google_oauth_state"):
            raise PickerError("The sign-in link expired or didn't come from this service; start again.")
        tok = self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri})
        self._save(tok)

    def _token(self, form):
        body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret, **form}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.token_url, body), timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise PickerError(f"Google sign-in failed: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from None

    def _save(self, tok):
        old = self.store.get("google_token") or {}
        self.store.put("google_token", {"access_token": tok["access_token"],
                                        "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
                                        "expires_at": time.time() + int(tok.get("expires_in", 3600)) - 60})

    def access_token(self):
        tok = self.store.get("google_token") or {}
        if not tok.get("refresh_token"):
            raise PickerError("Not signed in to Google yet: open /picker and sign in.")
        if tok.get("expires_at", 0) < time.time():
            self._save(self._token({"refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}))
            tok = self.store.get("google_token")
        return tok["access_token"]

    # ---- Picker API ----

    def _call(self, method, path_or_url, body=None, raw=False):
        url = path_or_url if path_or_url.startswith("http") else self.api + path_or_url
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data, method=method, headers={
            "Authorization": f"Bearer {self.access_token()}", **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise PickerError(f"Google Photos {method} {url.split('?')[0]}: HTTP {e.code} "
                              f"{e.read()[:300].decode(errors='replace')}") from None
        return payload if raw else (json.loads(payload) if payload else {})

    def create_session(self):
        """-> {id, pickerUri, pollingConfig, ...}. Open pickerUri on the phone to choose photos."""
        return self._call("POST", "/sessions", {})

    def session(self, session_id):
        return self._call("GET", f"/sessions/{urllib.parse.quote(session_id)}")

    def media_items(self, session_id):
        items, token = [], None
        while True:
            q = {"sessionId": session_id, "pageSize": 100, **({"pageToken": token} if token else {})}
            res = self._call("GET", "/mediaItems?" + urllib.parse.urlencode(q))
            items += res.get("mediaItems", [])
            token = res.get("nextPageToken")
            if not token:
                return items

    def download(self, item):
        """The original bytes of a picked photo (baseUrl + "=d")."""
        return self._call("GET", item["mediaFile"]["baseUrl"] + "=d", raw=True)

    def delete_session(self, session_id):
        try:
            self._call("DELETE", f"/sessions/{urllib.parse.quote(session_id)}")
        except PickerError:
            pass

    def wait_and_import(self, session_id, on_photo, poll=5, timeout=1800, sleep=time.sleep):
        """Poll until the person has finished picking, then hand each photo to on_photo(bytes, filename).
        Returns the number imported."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.session(session_id)
            if s.get("mediaItemsSet"):
                break
            if time.monotonic() > deadline:
                raise PickerError("No photos were picked in time; start again.")
            interval = s.get("pollingConfig", {}).get("pollInterval", f"{poll}s")
            sleep(min(float(str(interval).rstrip("s") or poll), 30))
        n = 0
        for item in self.media_items(session_id):
            if item.get("type", "PHOTO") != "PHOTO":
                continue
            on_photo(self.download(item), item["mediaFile"].get("filename") or f"{item['id']}.jpg")
            n += 1
        self.delete_session(session_id)
        return n
