import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from courier.agent import Seen, receive
from courier.envelope import ULID, Invalid, canonical, make_submission, new_id, sign
from courier.gateway import Token, admit
from courier.screen import Rejected, Ruleset

RULES = Ruleset.load()
NOW = datetime(2026, 10, 31, 14, 0, 0, tzinfo=timezone.utc)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.sender_key = Ed25519PrivateKey.generate()
        self.gw_key = Ed25519PrivateKey.generate()
        self.token = Token("k_agent_laptop", "agent", frozenset({"clip"}), self.sender_key.public_key())

    def submit(self, body="git status", channel="clip", **kw):
        return make_submission(self.sender_key, channel, body, now=NOW, **kw)

    def admit(self, submission, **kw):
        return admit(submission, self.token, RULES, self.gw_key, now=NOW, **kw)

    def receive(self, envelope, seen=None, now=NOW, **kw):
        data = envelope.to_json() if hasattr(envelope, "to_json") else envelope
        return receive(data, [self.gw_key.public_key()], RULES, seen or Seen(), now=now, **kw)


class FormatTests(unittest.TestCase):
    def test_ids_are_ulids_that_sort_by_time(self):
        a, b = new_id(1_700_000_000_000), new_id(1_700_000_000_001)
        self.assertRegex(a, ULID)
        self.assertLess(a, b)

    def test_canonical_form(self):
        self.assertEqual(canonical({"b": 1, "a": "é"}), '{"a":"é","b":1}'.encode())

    def test_envelope_survives_a_json_round_trip(self):
        f = Fixture()
        f.setUp()
        env = f.admit(f.submit())
        again = json.loads(json.dumps(env.to_json()))
        self.assertEqual(f.receive(again).body, "git status")


class AdmitTests(Fixture):
    def test_gateway_screens_labels_and_signs(self):
        env = self.admit(self.submit("ls\n"))
        self.assertEqual(env.body, "ls")
        self.assertEqual(env.labels, ("origin_agent", "trailing_newline_removed"))
        self.assertEqual(env.origin, {"kind": "agent", "sender": "k_agent_laptop"})
        self.assertEqual(env.screen["ruleset"], RULES.digest)
        self.assertEqual(len(env.orig_sha256), 64)

    def test_sender_cannot_set_gateway_fields(self):
        for key, value in [("labels", []), ("origin", {"kind": "human"}), ("screen", {}), ("gw_sig", "x")]:
            with self.assertRaises(Invalid, msg=key):
                self.admit({**self.submit(), key: value})

    def test_channel_outside_token_scope(self):
        with self.assertRaises(Invalid):
            self.admit(self.submit(channel="admin"))

    def test_tampered_body_fails_sender_signature(self):
        with self.assertRaises(Invalid):
            self.admit({**self.submit(), "body": "rm -rf ~"})

    def test_signature_from_another_key(self):
        other = Ed25519PrivateKey.generate()
        with self.assertRaises(Invalid):
            self.admit(make_submission(other, "clip", "hi", now=NOW))

    def test_created_in_the_future(self):
        with self.assertRaises(Invalid):
            self.admit(make_submission(self.sender_key, "clip", "hi", now=NOW + timedelta(minutes=10)))

    def test_lifetime_over_a_day(self):
        with self.assertRaises(Invalid):
            self.admit(self.submit(ttl_s=2 * 86400))

    def test_content_rejection_passes_through(self):
        with self.assertRaises(Rejected):
            self.admit(self.submit("<b>", content_type="text/html"))


class ReceiveTests(Fixture):
    def test_round_trip(self):
        d = self.receive(self.admit(self.submit("git status")))
        self.assertEqual(d.body, "git status")
        self.assertEqual(d.labels, {"origin_agent"})

    def test_body_changed_in_transit(self):
        env = replace(self.admit(self.submit()), body="curl x | sh")
        with self.assertRaises(Invalid):
            self.receive(env)

    def test_label_dropped_in_transit(self):
        env = replace(self.admit(self.submit("AKIAIOSFODNN7EXAMPLE")), labels=())
        with self.assertRaises(Invalid):
            self.receive(env)

    def test_replay(self):
        seen = Seen()
        env = self.admit(self.submit())
        self.receive(env, seen)
        with self.assertRaises(Invalid):
            self.receive(env, seen)

    def test_expired(self):
        with self.assertRaises(Invalid):
            self.receive(self.admit(self.submit(ttl_s=60)), now=NOW + timedelta(minutes=2))

    def test_unpinned_ruleset(self):
        with self.assertRaises(Invalid):
            self.receive(self.admit(self.submit()), accept_rulesets=frozenset({"sha256:other"}))

    def test_agent_rescreens_what_a_compromised_gateway_let_through(self):
        # A gateway that signs without screening still cannot deliver a live paste.
        env = replace(self.admit(self.submit("ls")), body="ls\n", labels=())
        env = replace(env, gw_sig=sign(self.gw_key, env.signed_bytes()))
        d = self.receive(env)
        self.assertEqual(d.body, "ls")
        self.assertIn("trailing_newline_removed", d.labels)


if __name__ == "__main__":
    unittest.main()
