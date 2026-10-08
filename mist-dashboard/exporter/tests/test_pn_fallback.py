import collections
import os
import sys
import traceback
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.metrics import render  # noqa: E402
from mist_exporter.mist_api import MistError  # noqa: E402
from mist_exporter.pn_fallback import (FAIL_EVENT, PARTIAL_WHILE_LOADING, Stream, Tracker,  # noqa: E402
                                       classify, normalize_username, run_slice)

NOW = 1_800_000_000
WINDOW = 1800
PN, MC = "PantherNet", "MiddleburyCollege"


def ev(mac, ts, reason=23, status=0, text=""):
    return {"mac": mac, "timestamp": ts, "reason_code": reason, "status_code": status, "text": text}


class Classify(unittest.TestCase):
    def test_buckets(self):
        cases = [(ev("a", 1, reason=23), "dot1x_failed"),
                 (ev("a", 1, reason=0, text="Client 802.1X Auth Fail (EAP)"), "dot1x_failed"),
                 (ev("a", 1, reason=15), "handshake_timeout"),
                 (ev("a", 1, reason=2), "previous_auth_invalid"),
                 (ev("a", 1, reason=3), "client_left"),
                 (ev("a", 1, reason=8), "client_left"),
                 (ev("a", 1, reason=0, status=79), "tx_failure"),
                 (ev("a", 1, reason=1), "other"),
                 ({"mac": "a", "timestamp": 1}, "other"),
                 (ev("a", 1, reason="23"), "other")]            # strings are not trusted as numbers
        for event, want in cases:
            self.assertEqual(classify(event), want, event)

    def test_first_match_wins(self):
        self.assertEqual(classify(ev("a", 1, reason=15, status=79, text="802.1x auth fail")), "dot1x_failed")
        self.assertEqual(classify(ev("a", 1, reason=2, status=79)), "previous_auth_invalid")

    def test_usernames(self):
        for raw, want in [("Alice@Example.edu", "alice"), ("EXAMPLE\\Bob", "bob"), ("  carol ", "carol"),
                          ("", ""), (None, ""), ("@x", "")]:
            self.assertEqual(normalize_username(raw), want, raw)


class PagedFeed:
    """Newest-first pages of at most `size` events inside [start, end], like Mist."""

    def __init__(self, events, size=3):
        self.events, self.size, self.calls = events, size, []

    def __call__(self, start, end):
        self.calls.append((start, end))
        rows = sorted((e for e in self.events if start <= e["timestamp"] <= end), key=lambda e: -e["timestamp"])
        return rows[:self.size], len(rows) > self.size


class WalkTests(unittest.TestCase):
    def test_walk_loads_everything_once_across_pages(self):
        events = [ev("m%d" % i, NOW - 100 * i) for i in range(10)]
        feed, seen = PagedFeed(events), []
        s = Stream()
        used = s.advance(feed, lambda e, ts: seen.append(e["mac"]), NOW, 86400, pages=50)
        self.assertEqual(sorted(set(seen)), sorted(e["mac"] for e in events))
        self.assertEqual(used, 5)       # each page re-reads its boundary second (cheap at 1000/page)
        self.assertEqual(s.covered_to, NOW)
        self.assertIsNone(s.walk)

    def test_budget_exhaustion_resumes_where_it_stopped(self):
        events = [ev("m%d" % i, NOW - 100 * i) for i in range(10)]
        feed, seen = PagedFeed(events), set()
        s = Stream()
        for _ in range(10):
            s.advance(feed, lambda e, ts: seen.add(e["mac"]), NOW, 86400, pages=1)
            if s.walk is None:
                break
        self.assertEqual(seen, {e["mac"] for e in events})
        self.assertEqual(s.covered_to, NOW)

    def test_incremental_walk_only_reads_new_events_plus_overlap(self):
        events = [ev("m%d" % i, NOW - 1000 - 100 * i) for i in range(6)]
        feed, s = PagedFeed(events, size=100), Stream()
        s.advance(feed, lambda e, ts: None, NOW, 86400, pages=5)
        later = NOW + 300
        s.advance(feed, lambda e, ts: None, later, 86400, pages=5)
        start, end = feed.calls[-1]
        self.assertEqual((start, end), (NOW - 60, later))

    def test_one_second_with_more_events_than_a_page_cannot_loop_forever(self):
        events = [ev("m%d" % i, NOW - 10) for i in range(9)]
        feed, s = PagedFeed(events, size=3), Stream()
        used = s.advance(feed, lambda e, ts: None, NOW, 86400, pages=20)
        self.assertLessEqual(used, 20)
        self.assertIsNone(s.walk)


class Clients:
    """Stand-in for Mist: event feeds plus clients/search rows (usernames live only here)."""

    def __init__(self, fails, assocs, rows, page=3):
        self.fails, self.assocs, self.rows, self.page = fails, assocs, rows, page
        self.calls = collections.Counter()

    def search_client_events(self, event_type, ssid, start, end, limit=1000):
        self.calls["events"] += 1
        feed = self.fails if event_type == FAIL_EVENT else self.assocs
        assert ssid == (PN if event_type == FAIL_EVENT else MC)
        return PagedFeed(feed, self.page)(start, end)

    def search_clients(self, start, end, limit=1000, **f):
        self.calls["clients"] += 1
        rows = [r for r in self.rows
                if (not f.get("mac") or r["mac"] == f["mac"])
                and (not f.get("ssid") or r["last_ssid"] == f["ssid"])
                and (not f.get("username") or f["username"] in r["username"])]
        return rows[:limit], len(rows)


def row(mac, names, os_, model, ssid):
    return {"mac": mac, "username": names, "os": os_, "model": model, "last_ssid": ssid}


# Fake identities. They must never appear in any exported text or log line.
SECRETS = ["zz-a", "zz-b", "zz-c", "zz-e", "ZZ-A@example.test", "EXAMPLE\\ZZ-B"]
MACS = ["aa0000000001", "bb0000000002", "cc0000000003", "dd0000000004", "ee0000000005",
        "a10000000001", "b10000000002", "e10000000005"]


def scenario():
    fails = [ev("aa0000000001", NOW - 5000, 23), ev("aa0000000001", NOW - 4900, 15),      # user A, matured
             ev("bb0000000002", NOW - 5000, 23),                                          # user B, MC too late
             ev("cc0000000003", NOW - 100, 23),                                           # user C, not matured
             ev("dd0000000004", NOW - 5000, 23),                                          # no username
             ev("ee0000000005", NOW - 6000, 15),                                          # user E, other device
             ev("ff0000000006", NOW - 3000, 2),                                           # noise
             ev("ff0000000007", NOW - 3000, 0, status=79)]                                # noise
    assocs = [ev("a10000000001", NOW - 5000 + 600),        # A: within the window
              ev("b10000000002", NOW - 5000 + 2000),       # B: 2000 s later, outside 1800
              ev("e10000000005", NOW - 6000 + 300)]        # E: within the window
    rows = [row("aa0000000001", ["ZZ-A@example.test"], "iOS", "iPhone 15", PN),
            row("bb0000000002", ["EXAMPLE\\ZZ-B"], "Android", "Pixel", PN),
            row("cc0000000003", ["zz-c"], "iOS", "iPhone 13", PN),
            row("ee0000000005", ["zz-e"], "iOS", "iPhone 14", PN),
            row("a10000000001", ["ZZ-A@example.test"], "iOS", "iPhone 15", MC),
            row("b10000000002", ["EXAMPLE\\ZZ-B"], "Android", "Pixel", MC),
            row("e10000000005", ["zz-e"], "macOS", "MacBook", MC),
            row("c10000000003", ["zz-c"], "iOS", "iPhone 13", MC)]
    return fails, assocs, rows


def cfg(**kw):
    base = dict(fallback_window_s=WINDOW, fallback_lookback_h=24, fallback_pages_per_slice=4,
                fallback_lookups_per_slice=4, fallback_totals_interval=300)
    base.update(kw)
    return Config(**base)


def run_until_complete(tracker, client, config, limit=40):
    for i in range(limit):
        run_slice(tracker, client, config, NOW)
        if tracker.complete:
            return i + 1
    raise AssertionError("never completed")


def metric(fams, name, **labels):
    for f in fams:
        if f.name == name:
            for l, v in f.samples:
                if all(l.get(k) == want for k, want in labels.items()):
                    return v
    return None


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.config = cfg()
        self.client = Clients(*scenario())
        self.t = Tracker(WINDOW, 86400, PN, MC)
        self.slices = run_until_complete(self.t, self.client, self.config)
        self.fams = self.t.families()

    def test_counts_and_buckets(self):
        self.assertEqual(metric(self.fams, "mist_pn_auth_failure_events"), 6.0)     # A2 + B + C + D + E
        self.assertEqual(metric(self.fams, "mist_pn_auth_failure_clients"), 4.0)    # A, B, C, E (D has no username)
        self.assertEqual(metric(self.fams, "mist_pn_auth_failure_clients_unresolved"), 1.0)
        reasons = {r: metric(self.fams, "mist_pn_failure_reason_events", reason=r)
                   for r in ("dot1x_failed", "handshake_timeout", "previous_auth_invalid", "tx_failure", "other")}
        self.assertEqual(reasons, {"dot1x_failed": 4.0, "handshake_timeout": 2.0, "previous_auth_invalid": 1.0,
                                   "tx_failure": 1.0, "other": 0.0})

    def test_fallback_user_and_device_matching(self):
        # Eligible (matured, looked up): A, B, E. Fallback by user: A, E. By device: only A (E moved to a Mac).
        self.assertEqual(metric(self.fams, "mist_pn_fallback_eligible_clients", match="user"), 3.0)
        self.assertEqual(metric(self.fams, "mist_pn_fallback_clients", match="user"), 2.0)
        self.assertAlmostEqual(metric(self.fams, "mist_pn_fallback_rate", match="user"), 2 / 3)
        self.assertEqual(metric(self.fams, "mist_pn_fallback_eligible_clients", match="device"), 3.0)
        self.assertEqual(metric(self.fams, "mist_pn_fallback_clients", match="device"), 1.0)
        self.assertAlmostEqual(metric(self.fams, "mist_pn_fallback_rate", match="device"), 1 / 3)

    def test_unmatured_failures_are_not_eligible_until_the_window_passes(self):
        # User C failed 100 s ago: invisible to the fallback maths now, eligible once 1800 s have passed.
        later = NOW + WINDOW
        client = Clients(*scenario())
        client.fails = client.fails + []                    # same feed
        client.assocs.append(ev("c10000000003", NOW - 100 + 60))
        run_slice(self.t, client, self.config, later)
        run_slice(self.t, client, self.config, later)
        fams = self.t.families()
        self.assertEqual(metric(fams, "mist_pn_fallback_eligible_clients", match="user"), 4.0)
        self.assertEqual(metric(fams, "mist_pn_fallback_clients", match="user"), 3.0)

    def test_partial_counts_are_withheld_until_history_is_loaded(self):
        t = Tracker(WINDOW, 86400, PN, MC)
        run_slice(t, Clients(*scenario()), self.config, NOW)           # one slice: history not loaded yet
        self.assertFalse(t.complete)
        fams = t.families()
        for f in fams:
            if f.name in PARTIAL_WHILE_LOADING:
                self.assertEqual(f.samples, [], f.name)                 # no false dip in a long trend
        self.assertEqual(metric(fams, "mist_pn_backfill_complete"), 0.0)       # status stays visible
        self.assertIsNotNone(metric(fams, "mist_pn_lookups_pending", stage="username"))
        self.assertEqual(metric(fams, "mist_pn_unique_clients", ssid=PN), 4.0)  # API-backed total is exact
        # and once loaded they are all back
        run_until_complete(t, Clients(*scenario()), self.config)
        self.assertEqual(metric(t.families(), "mist_pn_auth_failure_events"), 6.0)

    def test_totals_and_status(self):
        self.assertEqual(metric(self.fams, "mist_pn_unique_clients", ssid=PN), 4.0)
        self.assertEqual(metric(self.fams, "mist_pn_unique_clients", ssid=MC), 4.0)
        self.assertEqual(metric(self.fams, "mist_pn_backfill_complete"), 1.0)
        self.assertEqual(metric(self.fams, "mist_pn_fallback_window_seconds"), float(WINDOW))
        self.assertEqual(metric(self.fams, "mist_pn_lookups_pending", stage="username"), 0.0)

    def test_nothing_eligible_means_no_rate_sample(self):
        t = Tracker(WINDOW, 86400, PN, MC)
        fresh = Clients([ev("cc0000000003", NOW - 10, 23)], [], scenario()[2])
        run_until_complete(t, fresh, self.config)
        self.assertIsNone(metric(t.families(), "mist_pn_fallback_rate"))
        self.assertEqual(metric(t.families(), "mist_pn_fallback_eligible_clients", match="user"), 0.0)

    def test_incremental_slices_do_not_double_count(self):
        before = metric(self.fams, "mist_pn_auth_failure_events")
        for _ in range(3):
            run_slice(self.t, self.client, self.config, NOW + 120)
        self.assertEqual(metric(self.t.families(), "mist_pn_auth_failure_events"), before)

    def test_history_loads_over_several_slices_and_lookups_are_capped(self):
        self.assertGreater(self.slices, 2)                 # 4 pages of 3 events per slice cannot finish at once
        slow = cfg(fallback_lookups_per_slice=1)
        client, t = Clients(*scenario()), Tracker(WINDOW, 86400, PN, MC)
        before = 0
        for _ in range(6):
            run_slice(t, client, slow, NOW)
            self.assertLessEqual(client.calls["clients"] - before, 1 + 2)      # 1 lookup + totals (2) at most
            before = client.calls["clients"]
        self.assertGreater(metric(t.families(), "mist_pn_lookups_pending", stage="username")
                           + metric(t.families(), "mist_pn_lookups_pending", stage="connections"), 0)


class Privacy(unittest.TestCase):
    def test_exported_text_contains_no_usernames_or_macs(self):
        t = Tracker(WINDOW, 86400, PN, MC)
        run_until_complete(t, Clients(*scenario()), cfg())
        text = render(t.families()).lower()
        for secret in SECRETS + MACS:
            self.assertNotIn(secret.lower(), text)
        self.assertNotIn("user=", text)

    def test_a_crash_cannot_leak_a_username_into_the_log(self):
        class Boom(Clients):
            def search_clients(self, *a, **k):
                raise KeyError("zz-a")                      # a real KeyError would quote the key

        c = Collector(cfg(), Boom(*scenario()), clock=lambda: float(NOW))
        with self.assertLogs("mist_exporter", level="ERROR") as cm:
            c.refresh("fallback")
        dump = "\n".join(cm.output) + "".join("".join(traceback.format_exception(*r.exc_info))
                                              for r in cm.records if r.exc_info)
        self.assertNotIn("zz-a", dump.lower())
        self.assertIn("KeyError", dump)                    # the type is still logged, for debugging
        self.assertIn('mist_exporter_errors_total{source="fallback"} 1', c.render())

    def test_a_crash_while_publishing_cannot_leak_either(self):
        c = Collector(cfg(), Clients(*scenario()), clock=lambda: float(NOW))

        def broken(now):
            raise KeyError("zz-a")

        c._fallback.publish = broken
        with self.assertLogs("mist_exporter", level="ERROR") as cm:
            c.refresh("fallback")
        dump = "\n".join(cm.output) + "".join("".join(traceback.format_exception(*r.exc_info))
                                              for r in cm.records if r.exc_info)
        self.assertNotIn("zz-a", dump.lower())
        self.assertIn("KeyError", dump)

    def test_a_mist_error_is_counted_and_keeps_progress(self):
        class Flaky(Clients):
            def search_clients(self, *a, **k):
                raise MistError("Mist HTTP 429", status=429, retry_after=120)

        client = Flaky(*scenario())
        c = Collector(cfg(), client, clock=lambda: float(NOW))
        c.refresh("fallback")
        self.assertGreater(c._fallback.fail.covered_to + len(c._fallback._fail_events), 0)   # events kept
        self.assertGreaterEqual(c._due["fallback"], NOW + 120)                                # Retry-After honoured


class CollectorIntegration(unittest.TestCase):
    def test_disabled_never_calls_mist_and_emits_empty_families(self):
        client = Clients(*scenario())
        c = Collector(cfg(fallback_enabled=False), client, clock=lambda: float(NOW))
        for source in ("sites", "devices", "site_stats", "clients", "sle", "alarms"):
            c._due[source] = float("inf")                   # only the fallback source is under test
        c.run_due()
        self.assertEqual(client.calls["events"], 0)
        self.assertIn("# TYPE mist_pn_fallback_clients gauge", c.render())

    def test_interval_is_fast_while_loading_then_steady(self):
        config = cfg(fallback_interval=300)
        c = Collector(config, Clients(*scenario()), clock=lambda: float(NOW))
        self.assertEqual(c._interval("fallback"), 60)
        for _ in range(40):
            c.refresh("fallback")
            if not c._fallback.busy:
                break
        self.assertEqual(c._interval("fallback"), 300)

    def test_cadence_stays_fast_while_lookups_are_queued_then_relaxes(self):
        config = cfg(fallback_interval=300, fallback_lookups_per_slice=2)
        client = Clients(*scenario())
        c = Collector(config, client, clock=lambda: float(NOW))
        for _ in range(60):
            c.refresh("fallback")
            if not c._fallback.busy:
                break
        self.assertEqual(c._interval("fallback"), 300)
        # A burst of new failing devices queues more lookups than one slice can clear.
        client.fails += [ev("%012x" % (0x990000000000 + i), NOW + 100 + i, 23) for i in range(10)]
        client.rows += [row("%012x" % (0x990000000000 + i), ["zz-x%d" % i], "iOS", "iPhone", PN) for i in range(10)]
        later = NOW + 300
        c.clock = lambda: float(later)
        c.refresh("fallback")
        self.assertTrue(c._fallback.busy)
        self.assertEqual(c._interval("fallback"), 60)
        for _ in range(60):
            c.refresh("fallback")
            if not c._fallback.busy:
                break
        self.assertFalse(c._fallback.busy)
        self.assertEqual(c._interval("fallback"), 300)

    def test_render_has_pn_metrics_and_source_status(self):
        c = Collector(cfg(), Clients(*scenario()), clock=lambda: float(NOW))
        for _ in range(40):
            c.refresh("fallback")
            if c._fallback.complete:
                break
        text = c.render()
        self.assertIn('mist_pn_fallback_clients{match="user"} 2', text)
        self.assertIn('mist_exporter_last_success_timestamp_seconds{source="fallback"}', text)


if __name__ == "__main__":
    unittest.main()
