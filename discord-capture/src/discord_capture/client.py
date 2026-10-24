"""Discord REST client that stays well inside the rate limits.

Discord's limits (https://discord.com/developers/docs/topics/rate-limits):
  * per-route buckets, reported in X-RateLimit-* headers on every response;
  * a global limit of 50 requests/second per bot;
  * 10,000 invalid requests (401, 403, 429) per 10 minutes per IP, after which
    Cloudflare bans the IP for a while.

This client is far more conservative than any of those: it never sends more than
one request per `min_interval` seconds, waits out a bucket as soon as it is
exhausted (rather than running into the 429), pads every wait, backs off
exponentially with jitter on server and network errors, stops the whole run on
401, and gives up once it has seen a handful of invalid requests.
"""

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/jbalcomb/ScratchPatch, 0.1)"

log = logging.getLogger(__name__)


class DiscordError(Exception):
    def __init__(self, status, message, path):
        super().__init__(f"{status} on {path}: {message}")
        self.status = status
        self.path = path


class AuthError(DiscordError):
    """401: the token is wrong or revoked. Nothing else will work either."""


class Forbidden(DiscordError):
    """403: the bot can't see this resource (missing permission)."""


class NotFound(DiscordError):
    pass


class TooManyInvalidRequests(Exception):
    """We've hit our own invalid-request ceiling, far below Discord's ban threshold."""


def urllib_transport(method, url, headers, timeout=30):
    """Send one request; return (status, lowercased headers, body bytes)."""
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _lower(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, _lower(error.headers), error.read()


def _lower(headers):
    return {k.lower(): v for k, v in headers.items()}


class Client:
    def __init__(
        self,
        token,
        *,
        min_interval=1.0,
        margin=0.5,
        max_retries=6,
        max_invalid=50,
        transport=urllib_transport,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        self.min_interval = min_interval
        self.margin = margin
        self.max_retries = max_retries
        self.max_invalid = max_invalid
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self._route_bucket = {}  # path -> bucket hash Discord told us
        self._blocked_until = {}  # bucket hash (or path) -> clock time
        self._global_until = 0.0
        self.requests = 0
        self.invalid = 0

    def get(self, path, params=None):
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        attempt = 0
        while True:
            self._wait_for(path)
            try:
                status, headers, body = self._transport("GET", url, self._headers)
            except OSError as error:  # DNS, connection reset, timeout
                attempt += 1
                if attempt > self.max_retries:
                    raise
                self._backoff(attempt, f"network error on {path}: {error}")
                continue
            finally:
                self._last_request = self._clock()
                self.requests += 1

            self._note_limits(path, headers)

            if status == 429:
                self._count_invalid()
                data = _json(body)
                retry_after = float(data.get("retry_after") or headers.get("retry-after") or 5)
                wait = retry_after * 1.5 + 1  # Discord's figure, plus a wide pad
                until = self._clock() + wait
                if data.get("global") or headers.get("x-ratelimit-global"):
                    self._global_until = until
                else:
                    self._blocked_until[self._bucket(path)] = until
                log.warning("429 on %s (scope %s); waiting %.1fs", path, headers.get("x-ratelimit-scope"), wait)
                attempt += 1
                if attempt > self.max_retries:
                    raise DiscordError(429, "still rate limited after retries", path)
                continue
            if status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise DiscordError(status, "server error after retries", path)
                self._backoff(attempt, f"{status} on {path}")
                continue
            if status == 401:
                self._count_invalid()
                raise AuthError(status, _json(body).get("message", "unauthorized"), path)
            if status == 403:
                self._count_invalid()
                raise Forbidden(status, _json(body).get("message", "forbidden"), path)
            if status == 404:
                raise NotFound(status, _json(body).get("message", "not found"), path)
            if status >= 400:
                raise DiscordError(status, _json(body).get("message", body[:200]), path)
            return _json(body)

    def _bucket(self, path):
        return self._route_bucket.get(path, path)

    def _wait_for(self, path):
        now = self._clock()
        ready = max(self._global_until, self._blocked_until.get(self._bucket(path), 0.0))
        if self._last_request is not None:
            ready = max(ready, self._last_request + self.min_interval)
        if ready > now:
            self._sleep(ready - now)

    def _note_limits(self, path, headers):
        bucket = headers.get("x-ratelimit-bucket")
        if bucket:
            self._route_bucket[path] = bucket
        remaining = headers.get("x-ratelimit-remaining")
        reset_after = headers.get("x-ratelimit-reset-after")
        # Leave the last request of every bucket unused, so a clock skew or a
        # second process can't push us into a 429.
        if remaining is not None and reset_after is not None and int(remaining) <= 1:
            self._blocked_until[self._bucket(path)] = self._clock() + float(reset_after) + self.margin

    def _backoff(self, attempt, reason):
        wait = min(300, 2 ** attempt) + random.uniform(0, 1)
        log.warning("%s; retry %d in %.1fs", reason, attempt, wait)
        self._sleep(wait)

    def _count_invalid(self):
        self.invalid += 1
        if self.invalid >= self.max_invalid:
            raise TooManyInvalidRequests(
                f"{self.invalid} invalid requests this run; stopping to stay clear of a Cloudflare ban"
            )


def _json(body):
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}
