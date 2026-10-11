import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

import audit_feed as a

CFG = {"base_url": "https://eip.example.org", "user": "u", "password": "p", "verify_tls": False}


def row(syslog_id, user="alice@example.org", service="Add: DNS RRs", params="", stime=1791667787):
    return {"syslog_id": str(syslog_id), "syslog_stime": str(stime), "usr_login": user,
            "service_name": "dns_rr_add", "translated_service_name": service, "service_param": params}


ACME = ("dns_name=ns.example.org&dnszone_name=example.com&rr_type=TXT&rr_ttl=300&value1=abc"
        "&dnsview_name=External&rr_full_name=_acme-challenge.example.com")
REAL = ("dns_name=ns.example.org&dnszone_name=example.com&rr_type=A&rr_ttl=3600&value1=192.0.2.10"
        "&dnsview_name=External&rr_full_name=www.example.com")


class NormalizeTest(unittest.TestCase):
    def test_fields_are_derived(self):
        ev = a.normalize_event(row(10, service="Delete: DNS RRs", params=REAL))
        self.assertEqual((ev["id"], ev["user"], ev["action"], ev["object_type"]),
                         (10, "alice@example.org", "delete", "dns_rrs"))
        self.assertEqual(ev["object_name"], "www.example.com")

    def test_secrets_are_redacted(self):
        ev = a.normalize_event(row(11, service="Add: SNMP profiles",
                                   params="name=prof&snmp_community=hunter2&password=pw&dns_key_value=zzz"))
        self.assertNotIn("hunter2", ev["param_string"])
        self.assertNotIn("pw&", ev["param_string"] + "&")
        self.assertNotIn("zzz", ev["param_string"])
        self.assertEqual(ev["object_name"], "prof")

    def test_unusable_row_raises(self):
        with self.assertRaises(ValueError):
            a.normalize_event({"syslog_id": "", "syslog_stime": "0"})


class NoiseTest(unittest.TestCase):
    def test_acme_and_reverse_are_noise_real_changes_are_not(self):
        self.assertTrue(a.is_noise(a.normalize_event(row(1, params=ACME))))
        self.assertTrue(a.is_noise(a.normalize_event(row(2, params="rr_full_name=4.3.2.1.in-addr.arpa&dnszone_name=2.1.in-addr.arpa"))))
        self.assertFalse(a.is_noise(a.normalize_event(row(3, params=REAL))))

    def test_ignored_users(self):
        ev = a.normalize_event(row(4, user="Svc-Backup", params=REAL))
        self.assertTrue(a.is_noise(ev, ["svc-backup"]))


class CollapseTest(unittest.TestCase):
    def events(self, n, user="alice@example.org"):
        return [a.normalize_event(row(100 + i, user=user, params=REAL)) for i in range(n)]

    def test_small_groups_stay_individual(self):
        cards = a.collapse(self.events(3), burst_min=10)
        self.assertEqual([c["count"] for c in cards], [1, 1, 1])

    def test_burst_becomes_one_summary(self):
        cards = a.collapse(self.events(12), burst_min=10)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["count"], 12)
        self.assertIn("12 x", cards[0]["details"])

    def test_different_users_do_not_merge(self):
        cards = a.collapse(self.events(12, "a@x") + self.events(12, "b@x"), burst_min=10)
        self.assertEqual(sorted(c["user"] for c in cards), ["a@x", "b@x"])


class KeepEventTest(unittest.TestCase):
    def test_event_shape(self):
        card = a.collapse([a.normalize_event(row(7, params=REAL))])[0]
        ev = a.build_keep_event(card, "https://eip.example.org")
        self.assertEqual(ev["source"], ["solidserver-audit"])
        self.assertEqual(ev["status"], "firing")
        self.assertEqual(ev["name"], "alice@example.org - Add: DNS RRs")
        self.assertEqual(ev["fingerprint"], "solidserver-audit:7:1")
        for key in ("user", "event", "object", "details", "when", "message"):
            self.assertTrue(ev[key], key)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "s.json")
        self.env = mock.patch.dict(os.environ, {"EIP_AUDIT_STATE": self.state, "EIP_AUDIT_BURST": "10",
                                                "EIP_AUDIT_IGNORE_USERS": "", "OO_URL": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.args = argparse.Namespace(dry_run=False, max_events=5000)

    def test_first_run_posts_nothing_and_records_newest(self):
        with mock.patch.object(a.eip_api, "get_rows", return_value=[row(500)]), \
                mock.patch.object(a, "post_json") as post:
            self.assertEqual(a.run(self.args, CFG), 0)
        post.assert_not_called()
        self.assertEqual(a.load_state(self.state), 500)

    def test_normal_run_posts_cards_and_advances(self):
        a.save_state(self.state, 500)
        rows = [row(501, params=REAL), row(502, params=ACME), row(503, params=REAL.replace("www", "mail"))]
        with mock.patch.object(a.eip_api, "get_rows", return_value=rows), \
                mock.patch.object(a, "post_json") as post:
            self.assertEqual(a.run(self.args, CFG), 0)
        self.assertEqual(post.call_count, 2)  # the ACME row is noise
        self.assertEqual(a.load_state(self.state), 503)

    def test_keep_failure_does_not_skip_the_failed_card(self):
        a.save_state(self.state, 500)
        rows = [row(501, params=REAL), row(502, params=REAL.replace("www", "mail"))]
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("keep down")
            return 200
        with mock.patch.object(a.eip_api, "get_rows", return_value=rows), \
                mock.patch.object(a, "post_json", side_effect=flaky):
            self.assertEqual(a.run(self.args, CFG), 1)
        self.assertEqual(a.load_state(self.state), 501)  # retry starts at 502


if __name__ == "__main__":
    unittest.main()
