import json
import unittest

from discord_capture.capture import capture_channel
from discord_capture.client import AuthError, Client
from discord_capture.store import Store


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def make_client(responses, clock=None):
    """responses: list of (status, headers, body) returned in order."""
    clock = clock or FakeClock()
    calls = []

    def transport(method, url, headers):
        calls.append(url)
        status, hdrs, body = responses.pop(0)
        return status, hdrs, json.dumps(body).encode()

    client = Client("token", min_interval=1.0, transport=transport, sleep=clock.sleep, clock=clock)
    return client, calls, clock


def msg(i, channel=1):
    return {"id": str(i), "channel_id": str(channel), "content": f"m{i}", "type": 0,
            "timestamp": "2026-01-01T00:00:00+00:00", "author": {"id": "9", "username": "u"}}


class ClientTests(unittest.TestCase):
    def test_429_waits_longer_than_retry_after_then_retries(self):
        client, calls, clock = make_client([
            (429, {}, {"retry_after": 2.0, "global": False}),
            (200, {}, {"ok": True}),
        ])
        self.assertEqual(client.get("/x"), {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(sum(clock.slept), 2.0 * 1.5 + 1)

    def test_exhausted_bucket_waits_for_reset(self):
        client, _, clock = make_client([
            (200, {"x-ratelimit-remaining": "1", "x-ratelimit-reset-after": "10", "x-ratelimit-bucket": "b"}, []),
            (200, {}, []),
        ])
        client.get("/channels/1/messages")
        client.get("/channels/1/messages")
        self.assertGreaterEqual(sum(clock.slept), 10)

    def test_min_interval_between_requests(self):
        client, _, clock = make_client([(200, {}, []), (200, {}, [])])
        client.get("/a")
        client.get("/b")
        self.assertEqual(clock.slept, [1.0])

    def test_401_stops(self):
        client, _, _ = make_client([(401, {}, {"message": "401: Unauthorized"})])
        with self.assertRaises(AuthError):
            client.get("/users/@me/guilds")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_backfill_pages_back_to_the_start_then_sync_picks_up_new(self):
        newest = [msg(i) for i in range(300, 200, -1)]   # Discord returns newest first
        older = [msg(i) for i in range(200, 150, -1)]    # short page: start of channel
        client, calls, _ = make_client([(200, {}, newest), (200, {}, older)])
        self.assertEqual(capture_channel(client, self.store, "5", "1"), 150)
        self.assertIn("before=201", calls[1])
        state = self.store.state(1)
        self.assertEqual((state["newest_id"], state["oldest_id"], state["backfill_done"]), (300, 151, 1))

        client, calls, _ = make_client([(200, {}, [msg(302), msg(301)])])
        self.assertEqual(capture_channel(client, self.store, "5", "1", backfill=False), 2)
        self.assertIn("after=300", calls[0])
        self.assertEqual(self.store.state(1)["newest_id"], 302)
        count = self.store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 152)

    def test_forbidden_channel_is_recorded_and_skipped(self):
        client, _, _ = make_client([(403, {}, {"message": "Missing Access"})])
        self.assertEqual(capture_channel(client, self.store, "5", "1"), 0)
        self.assertIn("Missing Access", self.store.state(1)["last_error"])

    def test_max_pages_leaves_backfill_resumable(self):
        client, _, _ = make_client([
            (200, {}, [msg(i) for i in range(400, 300, -1)]),
            (200, {}, [msg(i) for i in range(300, 200, -1)]),
        ])
        capture_channel(client, self.store, "5", "1", max_pages=1)
        state = self.store.state(1)
        self.assertEqual((state["oldest_id"], state["backfill_done"]), (201, 0))


class StatusTests(unittest.TestCase):
    def test_status_json_lists_channels_with_progress(self):
        import io, os, tempfile
        from contextlib import redirect_stdout
        from discord_capture.cli import main

        path = os.path.join(tempfile.mkdtemp(), "d.sqlite3")
        store = Store(path)
        store.upsert_guild({"id": "5", "name": "Home"})
        store.upsert_channel({"id": "1", "guild_id": "5", "type": 0, "name": "general"})
        store.save_messages("5", [msg(10), msg(11)])
        store.set_state(1, newest_id=11, oldest_id=10, backfill_done=0)
        store.commit()
        store.close()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["status", "--json", "--db", path]), 0)
        [entry] = json.loads(out.getvalue())
        self.assertEqual((entry["id"], entry["name"], entry["guild"], entry["messages"], entry["backfill"]),
                         ("1", "general", "Home", 2, "partial"))


if __name__ == "__main__":
    unittest.main()
