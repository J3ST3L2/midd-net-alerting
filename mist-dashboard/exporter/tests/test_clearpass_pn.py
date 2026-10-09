import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter import clearpass_pn  # noqa: E402
from mist_exporter.clearpass_pn import ClearPassTracker, extract, parse_fields, shape  # noqa: E402
from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.metrics import render  # noqa: E402
from mist_exporter.syslog_listener import Listener, parse_allow  # noqa: E402

PN, MC = "PantherNet", "MiddleburyCollege"
WINDOW = 1800
# Fake identities: they must never appear in exported text or in a log line.
NAMES = ["zz-a", "zz-b", "zz-c", "zz-d", "zz-e"]
MACS = ["aabbcc000001", "aabbcc000002", "aabbcc000003", "aabbcc000004", "aabbcc000005", "aabbcc00000f"]


def cef(user, mac, outcome, ssid, code="", pri="<134>Oct  8 10:00:00 cppm ", nas=""):
    ext = "suser=%s smac=%s outcome=%s Aruba-Essid-Name=%s" % (user, mac, outcome, ssid)
    if nas:
        ext += " NAS-IP-Address=" + nas
    if code:
        ext += " Error-Code=%s msg=RADIUS authentication failed" % code
    return pri + "CEF:0|Aruba|ClearPass|6.11|1|Radius Auth|3|" + ext


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def tracker(clock, **kw):
    args = dict(pn_ssid=PN, mc_ssid=MC, window_s=WINDOW, lookback_s=86400, min_history_s=0, clock=clock)
    args.update(kw)
    return ClearPassTracker(**args)


def value(fams, name, **labels):
    for f in fams:
        if f.name == name:
            for l, v in f.samples:
                if all(l.get(k) == want for k, want in labels.items()):
                    return v
    return None


class Parsing(unittest.TestCase):
    def test_cef_with_syslog_header(self):
        f = parse_fields(cef("zz-a@example.test", "AA-BB-CC-00-00-01", "Accept", PN))
        self.assertEqual((f["suser"], f["outcome"], f["Aruba-Essid-Name"]), ("zz-a@example.test", "Accept", PN))
        self.assertEqual(f["cefname"], "Radius Auth")

    def test_plain_key_value_with_dotted_names_and_spaces_in_values(self):
        f = parse_fields("Common.Username=zz-a Common.Calling-Station-Id=AA-BB-CC-00-00-01 "
                         "Common.Login-Status=REJECT RADIUS.Aruba-Essid-Name=Panther Net Error-Code=9002")
        rec = extract(f)
        self.assertEqual((rec["user"], rec["mac"], rec["status"], rec["ssid"], rec["reason"]),
                         ("zz-a", "aabbcc000001", "reject", "Panther Net", "9002"))

    def test_extract_normalises_and_buckets(self):
        r = extract(parse_fields(cef("EXAMPLE\\ZZ-B", "AA:BB:CC:00:00:02", "REJECT", PN, code="9005")))
        self.assertEqual((r["user"], r["mac"], r["reason"]), ("zz-b", "aabbcc000002", "9005"))
        r = extract(parse_fields("username=zz-a callingstationid=aabbcc000001 status=Timeout essid=PantherNet "
                                 "msg=request timeout"))
        self.assertEqual((r["status"], r["reason"]), ("reject", "timeout"))
        r = extract(parse_fields("username=zz-a status=Failed essid=PantherNet msg=something odd"))
        self.assertEqual(r["reason"], "other")

    def test_ssid_from_called_station_id_when_no_ssid_field(self):
        r = extract(parse_fields("suser=zz-a smac=aabbcc000001 outcome=Accept Called-Station-Id=11-22-33-44-55-66:PantherNet"))
        self.assertEqual(r["ssid"], "PantherNet")

    def test_hostile_input_is_parsed_in_linear_time(self):
        started = time.time()
        for junk in ["x" * 200000, "a=" * 100000, "a b " * 50000, ("k=v " * 5000) + "y" * 100000,
                     "CEF:" + "|" * 100000]:
            parse_fields(junk)
        self.assertLess(time.time() - started, 2.0)

    def test_records_that_are_not_authentication_results(self):
        self.assertIsNone(extract(parse_fields("hello world")))
        self.assertIsNone(extract(parse_fields("suser=zz-a outcome=Pending")))
        self.assertEqual(parse_fields(""), {})


class Tracking(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t = tracker(self.clock)
        t0 = self.clock.t

        def at(offset, line):
            self.clock.t = t0 + offset
            self.t.handle_line(line)

        # A: fails on PantherNet, then succeeds on MiddleburyCollege 10 minutes later -> fallback
        at(0, cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        at(600, cef("zz-a", "AA-BB-CC-00-00-0F", "Accept", MC))
        # B: fails, MiddleburyCollege only 40 minutes later (outside the window) -> eligible, not fallback
        at(10, cef("zz-b", "AA-BB-CC-00-00-02", "Reject", PN, "9002"))
        at(2410, cef("zz-b", "AA-BB-CC-00-00-02", "Accept", MC))
        # C: fails right at the end, not old enough to judge
        at(5000, cef("zz-c", "AA-BB-CC-00-00-03", "Reject", PN, "9005"))
        # D: no username on the record -> unresolved, never in the fallback maths
        at(20, "suser= smac=AA-BB-CC-00-00-04 outcome=Reject Aruba-Essid-Name=PantherNet Error-Code=9002")
        # E: succeeds on PantherNet (adoption) and on MiddleburyCollege
        at(30, cef("zz-e", "AA-BB-CC-00-00-05", "Accept", PN))
        at(40, cef("zz-e", "AA-BB-CC-00-00-05", "Accept", MC))
        self.clock.t = t0 + 5100                                   # judge from here
        self.fams = self.t.families(ttl=0)

    def test_counts(self):
        self.assertEqual(value(self.fams, "clearpass_pn_auth_failure_events"), 4.0)        # A, B, C, D
        self.assertEqual(value(self.fams, "clearpass_pn_auth_failure_clients"), 3.0)        # A, B, C
        self.assertEqual(value(self.fams, "clearpass_pn_auth_failure_clients_unresolved"), 1.0)
        self.assertEqual(value(self.fams, "clearpass_pn_failure_reason_events", reason="9002"), 3.0)
        self.assertEqual(value(self.fams, "clearpass_pn_failure_reason_events", reason="9005"), 1.0)

    def test_fallback_judges_only_matured_failures(self):
        self.assertEqual(value(self.fams, "clearpass_pn_fallback_eligible_clients"), 2.0)   # A, B
        self.assertEqual(value(self.fams, "clearpass_pn_fallback_clients"), 1.0)            # A
        self.assertAlmostEqual(value(self.fams, "clearpass_pn_fallback_rate"), 0.5)

    def test_unique_clients_count_devices_with_a_successful_auth(self):
        self.assertEqual(value(self.fams, "clearpass_pn_unique_clients", ssid=PN), 1.0)   # only E succeeded on PN
        self.assertEqual(value(self.fams, "clearpass_pn_unique_clients", ssid=MC), 3.0)   # A's second MAC, B, E

    def test_failure_that_matures_later_becomes_eligible(self):
        self.clock.t += WINDOW
        fams = self.t.families(ttl=0)
        self.assertEqual(value(fams, "clearpass_pn_fallback_eligible_clients"), 3.0)

    def test_nothing_eligible_means_no_rate_sample(self):
        t = tracker(self.clock)
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        self.assertIsNone(value(t.families(ttl=0), "clearpass_pn_fallback_rate"))

    def test_events_age_out_of_the_window(self):
        self.clock.t += 90000
        fams = self.t.families(ttl=0)
        self.assertEqual(value(fams, "clearpass_pn_auth_failure_events"), 0.0)
        self.assertEqual(value(fams, "clearpass_pn_unique_clients", ssid=MC), 0.0)

    def test_noise_codes_are_not_counted_but_still_charted(self):
        t = tracker(self.clock, noise=["9005"])
        t.handle_line(cef("zz-c", "AA-BB-CC-00-00-03", "Reject", PN, "9005"))
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        fams = t.families(ttl=0)
        self.assertEqual(value(fams, "clearpass_pn_auth_failure_events"), 1.0)
        self.assertEqual(value(fams, "clearpass_pn_failure_reason_events", reason="9005"), 1.0)

    def test_reason_labels_are_bounded(self):
        t = tracker(self.clock)
        for code in range(1000, 1040):
            t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, str(code)))
        reasons = [f for f in t.families(ttl=0) if f.name == "clearpass_pn_failure_reason_events"][0].samples
        self.assertLessEqual(len(reasons), 13)
        self.assertEqual(sum(v for _, v in reasons), 40.0)

    def test_counts_are_withheld_until_enough_history_then_released(self):
        t = tracker(self.clock, min_history_s=3600)
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        fams = t.families(ttl=0)
        self.assertIsNone(value(fams, "clearpass_pn_auth_failure_events"))             # withheld
        self.assertEqual(value(fams, "clearpass_pn_history_complete"), 0.0)             # status still visible
        self.assertEqual(value(fams, "clearpass_pn_events_received_total"), 1.0)
        self.clock.t += 3601
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        fams = t.families(ttl=0)
        self.assertEqual(value(fams, "clearpass_pn_history_complete"), 1.0)
        self.assertEqual(value(fams, "clearpass_pn_auth_failure_events"), 2.0)

    def test_garbage_never_raises_and_is_counted(self):
        for junk in ["", "\x00\x01", "CEF:0|only|two", "=====", "a=b c=d", "x" * 100000]:
            self.assertFalse(self.t.handle_line(junk))
        self.assertGreaterEqual(value(self.t.families(ttl=0), "clearpass_pn_unmapped_events_total"), 6.0)

    def test_memory_is_bounded_under_a_flood(self):
        old = (clearpass_pn.MAX_EVENTS, clearpass_pn.MAX_CLIENTS, clearpass_pn.MAX_PER_USER)
        clearpass_pn.MAX_EVENTS, clearpass_pn.MAX_CLIENTS, clearpass_pn.MAX_PER_USER = 50, 40, 3
        self.addCleanup(lambda: setattr(clearpass_pn, "MAX_EVENTS", old[0]) or
                        setattr(clearpass_pn, "MAX_CLIENTS", old[1]) or setattr(clearpass_pn, "MAX_PER_USER", old[2]))
        t = tracker(self.clock)                          # built after the caps were lowered
        for i in range(500):
            t.handle_line(cef("zz-%d" % i, "%012x" % i, "Reject", PN, "9002"))
            t.handle_line(cef("zz-%d" % i, "%012x" % i, "Accept", MC))
        for _ in range(10):
            t.handle_line(cef("zz-0", "%012x" % 0, "Accept", MC))
        fams = t.families(ttl=0)
        self.assertLessEqual(value(fams, "clearpass_pn_auth_failure_events"), 50.0)
        self.assertLessEqual(value(fams, "clearpass_pn_unique_clients", ssid=MC), 40.0)
        self.assertLessEqual(len(t._mc_ok["zz-0"]), 3)

    def test_other_ssids_are_ignored(self):
        self.t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Accept", "eduroam"))
        self.assertEqual(value(self.t.families(ttl=0), "clearpass_pn_unique_clients", ssid=PN), 1.0)


ARUBA = parse_allow("192.0.2.0/29")            # the Aruba controllers, in this test


class Platforms(unittest.TestCase):
    """ClearPass authenticates PantherNet for Mist and Aruba access points alike, so the feed is campus-wide;
    the NAS address on each record says which kind of access point it came from."""

    def setUp(self):
        self.clock = Clock()
        self.t = tracker(self.clock, aruba_nas=ARUBA)
        t0 = self.clock.t

        def at(offset, line):
            self.clock.t = t0 + offset
            self.t.handle_line(line)

        # A fails on an Aruba controller, then connects to MiddleburyCollege through a Mist AP: an Aruba failure
        # that fell back, even though the success was on the other platform
        at(0, cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002", nas="192.0.2.1"))
        at(300, cef("zz-a", "AA-BB-CC-00-00-0F", "Accept", MC, nas="198.51.100.7"))
        # B fails on a Mist AP and never falls back
        at(10, cef("zz-b", "AA-BB-CC-00-00-02", "Reject", PN, "9005", nas="198.51.100.8"))
        # C: no NAS on the record -> counted for the campus only
        at(20, cef("zz-c", "AA-BB-CC-00-00-03", "Reject", PN, "9002"))
        # successes on PantherNet: one Aruba device, two Mist devices
        at(30, cef("zz-d", "AA-BB-CC-00-00-04", "Accept", PN, nas="192.0.2.2"))
        at(31, cef("zz-e", "AA-BB-CC-00-00-05", "Accept", PN, nas="198.51.100.9"))
        at(32, cef("zz-f", "AA-BB-CC-00-00-06", "Accept", PN, nas="198.51.100.9"))
        self.clock.t = t0 + 5000
        self.fams = self.t.families(ttl=0)

    def v(self, name, platform, **labels):
        return value(self.fams, name, platform=platform, **labels)

    def test_failures_belong_to_the_platform_they_happened_on(self):
        self.assertEqual(self.v("clearpass_pn_auth_failure_events", "aruba"), 1.0)
        self.assertEqual(self.v("clearpass_pn_auth_failure_events", "other"), 1.0)
        self.assertEqual(self.v("clearpass_pn_auth_failure_events", "campus"), 3.0)     # includes the unclassified one

    def test_fallback_crosses_platforms(self):
        self.assertEqual(self.v("clearpass_pn_fallback_clients", "aruba"), 1.0)
        self.assertEqual(self.v("clearpass_pn_fallback_eligible_clients", "aruba"), 1.0)
        self.assertEqual(self.v("clearpass_pn_fallback_clients", "other"), 0.0)          # B never connected
        self.assertEqual(self.v("clearpass_pn_fallback_eligible_clients", "other"), 1.0)
        self.assertEqual(self.v("clearpass_pn_fallback_clients", "campus"), 1.0)
        self.assertEqual(self.v("clearpass_pn_fallback_eligible_clients", "campus"), 3.0)

    def test_unique_clients_split_by_the_platform_of_the_last_success(self):
        self.assertEqual(self.v("clearpass_pn_unique_clients", "aruba", ssid=PN), 1.0)
        self.assertEqual(self.v("clearpass_pn_unique_clients", "other", ssid=PN), 2.0)
        self.assertEqual(self.v("clearpass_pn_unique_clients", "campus", ssid=PN), 3.0)

    def test_unclassified_records_are_counted_and_still_in_the_campus_totals(self):
        self.assertEqual(value(self.fams, "clearpass_pn_unclassified_events_total"), 1.0)

    def test_reasons_are_per_platform(self):
        self.assertEqual(self.v("clearpass_pn_failure_reason_events", "aruba", reason="9002"), 1.0)
        self.assertEqual(self.v("clearpass_pn_failure_reason_events", "other", reason="9005"), 1.0)
        self.assertEqual(self.v("clearpass_pn_failure_reason_events", "campus", reason="9002"), 2.0)

    def test_without_controller_addresses_only_the_campus_view_exists(self):
        t = tracker(self.clock)
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002", nas="192.0.2.1"))
        fams = t.families(ttl=0)
        platforms = {l["platform"] for f in fams for l, _ in f.samples if "platform" in l}
        self.assertEqual(platforms, {"campus"})
        self.assertEqual(value(fams, "clearpass_pn_unclassified_events_total"), 0.0)

    def test_nas_address_is_found_under_several_field_names(self):
        for line in ("suser=zz-a smac=AA-BB-CC-00-00-01 outcome=Accept ssid=PantherNet NAS-IP-Address=192.0.2.1",
                     "username=zz-a smac=AA-BB-CC-00-00-01 outcome=Accept ssid=PantherNet Radius.IETF.NAS-IP-Address=192.0.2.1",
                     "username=zz-a callingstationid=AABBCC000001 status=Accept essid=PantherNet nasip=192.0.2.1"):
            self.assertEqual(extract(parse_fields(line))["nas"], "192.0.2.1", line)

    def test_a_cidr_network_matches_many_controllers(self):
        t = tracker(self.clock, aruba_nas=parse_allow("10.20.0.0/16"))
        self.assertEqual(t.platform_of("10.20.30.40"), "aruba")
        self.assertEqual(t.platform_of("10.21.0.1"), "other")
        self.assertEqual(t.platform_of(""), "unclassified")
        self.assertEqual(t.platform_of("not-an-ip"), "unclassified")


class Privacy(unittest.TestCase):
    def test_nothing_identifying_is_exported_or_logged(self):
        clock = Clock()
        t = tracker(clock)
        with self.assertNoLogs("mist_exporter"):
            for n, m in zip(NAMES, MACS):
                t.handle_line(cef(n + "@example.test", m, "Reject", PN, "9002"))
                t.handle_line(cef(n, m, "Accept", MC))
            t.handle_line("garbage " + NAMES[0])
        text = render(t.families(ttl=0)).lower()
        for secret in NAMES + MACS:
            self.assertNotIn(secret, text)
        self.assertNotIn("user=", text)
        self.assertNotIn("mac=", text)

    def test_field_names_are_remembered_but_values_are_not(self):
        t = tracker(Clock())
        t.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Reject", PN, "9002"))
        names = t.field_names()
        self.assertIn("suser", names)
        self.assertNotIn("zz-a", " ".join(names).lower())


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Sockets(unittest.TestCase):
    def make(self, allow):
        t = tracker(Clock())
        listener = Listener("127.0.0.1", 0, t, parse_allow(allow), describe_every=3600)
        listener.start()
        self.addCleanup(listener.stop)
        return t, listener

    def test_udp_and_tcp_records_are_ingested(self):
        t, l = self.make("127.0.0.0/8")
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.sendto(cef("zz-a", "AA-BB-CC-00-00-01", "Accept", PN).encode(), ("127.0.0.1", l.udp_port))
        u.close()
        self.assertTrue(wait_for(lambda: t.received >= 1))
        c = socket.create_connection(("127.0.0.1", l.tcp_port))
        line = cef("zz-b", "AA-BB-CC-00-00-02", "Accept", PN)
        c.sendall((line + "\n" + "%d %s\n" % (len(line), line)).encode())      # plain, then octet-counted
        c.close()
        self.assertTrue(wait_for(lambda: t.received >= 3))

    def test_sources_outside_the_allow_list_are_dropped_unparsed(self):
        t, l = self.make("10.0.0.0/8")
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.sendto(cef("zz-a", "AA-BB-CC-00-00-01", "Accept", PN).encode(), ("127.0.0.1", l.udp_port))
        u.close()
        self.assertTrue(wait_for(lambda: t.dropped >= 1))
        self.assertEqual(t.received, 0)

    def test_stranger_over_tcp_is_dropped_and_cannot_hold_a_slot(self):
        t, l = self.make("10.0.0.0/8")
        held = [socket.create_connection(("127.0.0.1", l.tcp_port)) for _ in range(40)]   # > 16 slots
        self.addCleanup(lambda: [c.close() for c in held])
        self.assertTrue(wait_for(lambda: t.dropped >= 40))
        self.assertEqual(t.received, 0)
        # the slots were never taken, so an allowed sender is still served
        l.allow = parse_allow("127.0.0.0/8")
        ok = socket.create_connection(("127.0.0.1", l.tcp_port))
        ok.sendall((cef("zz-a", "AA-BB-CC-00-00-01", "Accept", PN) + "\n").encode())
        ok.close()
        self.assertTrue(wait_for(lambda: t.received >= 1))

    def test_refuses_to_start_without_an_allow_list(self):
        with self.assertRaises(ValueError):
            Listener("127.0.0.1", 0, tracker(Clock()), [])

    def test_allow_list_parsing(self):
        nets = parse_allow("192.0.2.10, 198.51.100.0/24  203.0.113.5")
        self.assertEqual(len(nets), 3)
        self.assertEqual(parse_allow(""), [])
        with self.assertRaises(ValueError):
            parse_allow("not-an-address")


class Diagnostics(unittest.TestCase):
    """What the exporter logs to help map a ClearPass export or fix an allow-list, without any content."""

    CEF_LINE = ("<134>Oct  8 10:00:00 cppm CEF:0|Aruba|ClearPass|6.11|1|Radius Auth|3|cs1=zz-a@example.test "
                "cs1Label=Auth.Username cs2=AA-BB-CC-00-00-01 cs2Label=Auth.Host-MAC-Address cs3=PantherNet "
                "cs3Label=Radius.Called-Station-Id suser=zz-b outcome=Accept")

    def test_shape_keeps_keys_and_labels_but_never_a_value(self):
        out = shape(self.CEF_LINE)
        for key in ("cs1=", "cs1Label=Auth.Username", "cs2Label=Auth.Host-MAC-Address", "suser=", "outcome="):
            self.assertIn(key, out)
        for secret in ["zz-a", "zz-b", "example.test", "aa-bb-cc", "pantherNet".lower(), "cppm", "accept"]:
            self.assertNotIn(secret, out.lower().replace("auth.", "").replace("called-station-id", ""), secret)
        self.assertNotIn("Oct", out)

    def test_shape_of_unstructured_and_hostile_input(self):
        self.assertEqual(shape("zz-c|aabbcc000001|Reject|9002|PantherNet"), "x|x|x|x|x")
        started = time.time()
        for junk in ["x" * 200000, "a=" * 100000, ("k=v " * 5000) + "y" * 100000, "|" * 100000]:
            self.assertLessEqual(len(shape(junk)), 1500)
        self.assertLess(time.time() - started, 2.0)

    def test_dropped_sources_are_counted_by_address_only_and_bounded(self):
        t = tracker(Clock())
        for i in range(80):
            t.count_dropped("10.0.0.%d" % i)
        for _ in range(5):
            t.count_dropped("10.0.0.1")
        top = t.dropped_sources(top=3)
        self.assertIn(("10.0.0.1", 6), top)                             # the busiest real address is reported
        self.assertEqual(top[0], ("(others)", 30))                      # and the overflow is lumped together
        self.assertEqual(t.dropped, 85)
        self.assertLessEqual(len(t._dropped_by), 51)                    # 50 addresses + "(others)"
        self.assertIn("(others)", t._dropped_by)

    def listener(self, allow="10.0.0.0/8"):
        t = tracker(Clock())
        l = Listener("127.0.0.1", 0, t, parse_allow(allow), describe_every=3600, describe_first=3600)
        l.start()
        return t, l

    def test_describe_reports_names_layout_and_sources_without_any_content(self):
        t, l = self.listener()
        self.addCleanup(l.stop)
        t.handle_line(self.CEF_LINE)                                    # allowed format, but no ssid -> unusable
        l._ingest("192.0.2.77", self.CEF_LINE)                          # not allowed -> dropped, by address
        lines = l.describe()
        text = "\n".join(lines)
        self.assertIn("clearpass fields seen (names only)", text)
        self.assertIn("clearpass records not usable", text)
        self.assertIn("192.0.2.77=1", text)
        for secret in ["zz-a", "zz-b", "example.test", "AA-BB-CC-00-00-01", "PantherNet"]:
            self.assertNotIn(secret, text)
        self.assertEqual(l.describe(), [])                              # nothing new, nothing logged again

    def test_describe_logs_again_when_something_changes(self):
        t, l = self.listener()
        self.addCleanup(l.stop)
        l._ingest("192.0.2.77", "x")
        self.assertEqual(len(l.describe()), 1)
        l._ingest("192.0.2.78", "x")
        self.assertEqual(len(l.describe()), 1)

    def test_a_dropped_tcp_stranger_is_counted_by_address(self):
        t, l = self.listener()
        self.addCleanup(l.stop)
        c = socket.create_connection(("127.0.0.1", l.tcp_port))
        c.close()
        self.assertTrue(wait_for(lambda: t.dropped >= 1))
        self.assertEqual(t.dropped_sources()[0][0], "127.0.0.1")


class ClearPassCef(unittest.TestCase):
    """The record layout ClearPass actually sends (CEF, columns as csN with a csNLabel naming them)."""

    HEAD = "<134>Oct  9 10:00:00 cpauth1 CEF:0|Aruba Networks|ClearPass|6.11.4|Syslog|Authentication|3|"

    def record(self, user="zz-a@example.test", mac="AA-BB-CC-00-00-01", status="ACCEPT", nas="192.0.2.21",
               called="11-22-33-44-55-66:PantherNet", service="PantherNet 802.1X", code=None):
        ext = ("cat=Session Log dvc=140.233.1.113 duser=%s dmac=%s cs2=EAP-TLS cs2Label=Authentication Method "
               "src=%s cs4=%s cs4Label=Login Status destinationServiceName=%s cs3=eap "
               "cs3Label=Authentication Protocol" % (user, mac, nas, status, service))
        if called is not None:
            ext += " cs5=%s cs5Label=Called Station Id" % called
        if code is not None:
            ext += " ArubaClearpassCppmErrorCodeErrorCode=%s" % code
        return self.HEAD + ext

    def test_custom_columns_are_readable_under_their_labels(self):
        f = parse_fields(self.record())
        self.assertEqual(f["Login Status"], "ACCEPT")
        self.assertEqual(f["Called Station Id"], "11-22-33-44-55-66:PantherNet")
        self.assertEqual(f["cs4"], "ACCEPT")                       # the original key is still there

    def test_the_observed_record_is_fully_understood(self):
        rec = extract(parse_fields(self.record()))
        self.assertEqual(rec, {"status": "accept", "ssid": "PantherNet", "user": "zz-a", "mac": "aabbcc000001",
                               "nas": "192.0.2.21", "reason": ""})

    def test_a_rejected_record_carries_its_error_code(self):
        rec = extract(parse_fields(self.record(status="REJECT", code="9002")))
        self.assertEqual((rec["status"], rec["reason"]), ("reject", "9002"))
        timed_out = extract(parse_fields(self.record(status="TIMEOUT")))
        self.assertEqual(timed_out["status"], "reject")

    def test_a_record_without_an_ssid_column_is_unusable_but_its_service_is_noted(self):
        t = tracker(Clock())
        self.assertFalse(t.handle_line(self.record(called=None)))
        self.assertEqual(t.unmapped, 1)
        self.assertEqual(t.top_values()[0], [("PantherNet 802.1X", 1)])

    def test_end_to_end_through_the_tracker(self):
        clock = Clock()
        t = tracker(clock, aruba_nas=ARUBA)
        self.assertTrue(t.handle_line(self.record(nas="192.0.2.3")))                       # an Aruba controller
        self.assertTrue(t.handle_line(self.record(user="zz-b", mac="AA-BB-CC-00-00-02", nas="198.51.100.9")))
        self.assertTrue(t.handle_line(self.record(user="zz-c", mac="AA-BB-CC-00-00-03", status="REJECT",
                                                  code="9002", nas="192.0.2.3")))
        fams = t.families(ttl=0)
        self.assertEqual(value(fams, "clearpass_pn_events_received_total"), 3.0)
        self.assertEqual(value(fams, "clearpass_pn_unique_clients", platform="campus", ssid=PN), 2.0)
        self.assertEqual(value(fams, "clearpass_pn_unique_clients", platform="aruba", ssid=PN), 1.0)
        self.assertEqual(value(fams, "clearpass_pn_auth_failure_events", platform="aruba"), 1.0)
        self.assertEqual(value(fams, "clearpass_pn_failure_reason_events", platform="campus", reason="9002"), 1.0)

    def test_shape_keeps_whole_labels_and_masks_every_value(self):
        out = shape(self.record())
        for label in ("cs4Label=Login Status", "cs5Label=Called Station Id", "cs3Label=Authentication Protocol"):
            self.assertIn(label, out)
        for secret in ["zz-a", "example.test", "AA-BB-CC", "192.0.2.21", "11-22-33", "PantherNet", "ACCEPT", "EAP-TLS"]:
            self.assertNotIn(secret, out, secret)

    def test_service_and_ssid_names_are_bounded(self):
        t = tracker(Clock())
        for i in range(120):
            t.handle_line(self.record(service="svc-%d" % i, called="11-22-33-44-55-66:ssid-%d" % i))
        services, ssids = t.top_values(top=200)
        self.assertLessEqual(len(services), 50)
        self.assertLessEqual(len(ssids), 50)

    def test_describe_lists_network_names_but_never_a_person(self):
        t = tracker(Clock())
        listener = Listener("127.0.0.1", 0, t, parse_allow("127.0.0.0/8"), describe_every=3600, describe_first=3600)
        self.addCleanup(listener.stop)
        t.handle_line(self.record())
        text = chr(10).join(listener.describe())
        self.assertIn("service names seen: PantherNet 802.1X=1", text)
        self.assertIn("SSIDs seen: PantherNet=1", text)
        for secret in ["zz-a", "example.test", "AA-BB-CC-00-00-01", "aabbcc000001"]:
            self.assertNotIn(secret, text)


class CollectorIntegration(unittest.TestCase):
    def test_render_exposes_the_aruba_metrics_and_status(self):
        c = Collector(Config(clearpass_min_history_h=0), object(), clock=Clock())
        c.clearpass.handle_line(cef("zz-a", "AA-BB-CC-00-00-01", "Accept", PN))
        text = c.render()
        self.assertIn("# TYPE clearpass_pn_unique_clients gauge", text)
        self.assertIn('clearpass_pn_unique_clients{platform="campus",ssid="PantherNet"} 1', text)
        self.assertIn("clearpass_pn_events_received_total 1", text)
        self.assertNotIn("zz-a", text)


if __name__ == "__main__":
    unittest.main()
