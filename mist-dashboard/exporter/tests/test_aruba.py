import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter import aruba  # noqa: E402
from mist_exporter.aruba import ArubaClient, ArubaError, ArubaPoller, summarize  # noqa: E402
from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.metrics import aruba_families  # noqa: E402

PN, MC = "PantherNet", "MiddleburyCollege"


def row(ssid, name="zz-a@example.test", mac="AA:BB:CC:00:00:01", role="BYOD Device", typ="macOS", phy="5GHz-VHT",
        auth="802.1x", user_type="WIRELESS"):
    return {"Essid/Bssid/Phy": "%s/11:22:33:44:55:66/%s" % (ssid, phy), "Name": name, "MAC": mac, "Role": role,
            "Type": typ, "Auth": auth, "User Type": user_type, "AP name": "ap-1"}


def value(fams, name, **labels):
    for f in fams:
        if f.name == name:
            for lab, v in f.samples:
                if lab == labels:
                    return v
    return None


class Summary(unittest.TestCase):
    def test_counts_by_ssid_role_device_and_band(self):
        out = summarize({"c1": [row(PN), row(PN, mac="AA:BB:CC:00:00:02", typ=None, phy="2.4GHz-HT"),
                                row(MC, mac="AA:BB:CC:00:00:03", role="STUDENT", name="zz-b")]}, PN, MC)
        self.assertEqual(out["clients"], {PN: 2, MC: 1})
        self.assertEqual(out["roles"][PN], {"BYOD Device": 2})
        self.assertEqual(out["devices"][PN], {"macOS": 1, "unknown": 1})
        self.assertEqual(out["bands"][(PN, "5GHz")], 1)
        self.assertEqual(out["bands"][(PN, "2.4GHz")], 1)
        self.assertEqual(out["auth"][(PN, "802.1x")], 2)

    def test_a_client_seen_on_two_controllers_counts_once(self):
        out = summarize({"c1": [row(PN)], "c2": [row(PN)]}, PN, MC)
        self.assertEqual(out["clients"], {PN: 1})
        self.assertEqual(out["by_controller"], {("c1", PN): 1})

    def test_wired_rows_are_ignored(self):
        out = summarize({"c1": [row(PN), row(PN, mac="AA:BB:CC:00:00:09", user_type="WIRED")]}, PN, MC)
        self.assertEqual(out["clients"], {PN: 1})

    def test_people_are_distinct_and_matched_across_name_forms(self):
        tables = {"c1": [row(PN, name="Zz-A@example.test", mac="a1"), row(PN, name="zz-a@example.test", mac="a2"),
                         row(PN, name="zz-c@example.test", mac="a3"),
                         row(MC, name="ZZ-A", mac="m1"), row(MC, name="zz-d", mac="m2"), row(MC, name="", mac="m3")]}
        out = summarize(tables, PN, MC)
        self.assertEqual(out["people"], {PN: 2, MC: 2})        # two devices of one person count once
        self.assertEqual(out["people_on_both"], 1)             # zz-a, whatever the case or domain

    def test_roles_are_bounded(self):
        rows = [row(MC, mac="m%d" % i, role="role-%d" % i) for i in range(40)]
        roles = summarize({"c1": rows}, PN, MC)["roles"][MC]
        self.assertEqual(len(roles), aruba.TOP_VALUES + 1)
        self.assertEqual(sum(roles.values()), 40)
        self.assertIn("other", roles)

    def test_no_username_or_mac_is_in_the_summary(self):
        text = repr(summarize({"c1": [row(PN, name="zz-secret@example.test", mac="AA:BB:CC:DD:EE:FF")]}, PN, MC))
        for secret in ("zz-secret", "example.test", "AA:BB:CC", "aabbcc"):
            self.assertNotIn(secret, text)

    def test_odd_rows_do_not_crash(self):
        out = summarize({"c1": [{}, {"Essid/Bssid/Phy": None, "MAC": None}, row(PN)]}, PN, MC)
        self.assertEqual(out["clients"][PN], 1)
        self.assertEqual(out["clients"]["unknown"], 2)


class FakeController:
    """Stands in for ArubaClient: a table per host, or a failure."""

    def __init__(self, tables):
        self.tables, self.calls = tables, []

    def user_table(self, host):
        self.calls.append(host)
        t = self.tables[host]
        if isinstance(t, Exception):
            raise t
        return t


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Poller(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tables = {"c1": [row(PN)], "c2": [row(MC, mac="m1", name="zz-b")]}
        self.client = FakeController(self.tables)
        self.poller = ArubaPoller(self.client, ["c1", "c2"], PN, MC, interval=120, clock=self.clock)

    def test_reports_each_controller_and_the_totals(self):
        out = self.poller.poll()
        self.assertEqual(out["clients"], {PN: 1, MC: 1})
        self.assertEqual(out["controllers"]["c1"]["up"], 1.0)
        self.assertEqual(out["controllers"]["c2"]["clients"], 1.0)

    def test_a_failed_controller_keeps_its_last_table_for_a_while(self):
        self.poller.poll()
        self.tables["c2"] = ArubaError("c2: URLError")
        self.clock.t += 120
        out = self.poller.poll()
        self.assertEqual(out["controllers"]["c2"]["up"], 0.0)
        self.assertEqual(out["clients"][MC], 1)                    # still counted, so the total does not dip
        self.assertEqual(out["controllers"]["c2"]["age"], 120)

    def test_a_controller_down_too_long_stops_counting(self):
        self.poller.poll()
        self.tables["c2"] = ArubaError("c2: URLError")
        self.clock.t += 400                                        # > 3 intervals
        out = self.poller.poll()
        self.assertNotIn(MC, out["clients"])
        self.assertIsNone(out["controllers"]["c2"]["age"])

    def test_no_answer_and_nothing_cached_is_an_error(self):
        self.tables["c1"] = self.tables["c2"] = ArubaError("down")
        with self.assertRaises(ArubaError):
            self.poller.poll()


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Client(unittest.TestCase):
    def opener(self, responses):
        calls = []

        class Opener:
            def open(self, req, timeout=None):
                url = getattr(req, "full_url", req)
                calls.append(url)
                nxt = responses.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return FakeResponse(nxt)
        return Opener(), calls

    def test_logs_in_reads_the_table_and_logs_out(self):
        opener, calls = self.opener([json.dumps({"_global_result": {"UIDARUBA": "tok"}}),
                                     json.dumps({"Users": [row(PN)]}), "{}"])
        with mock.patch.object(aruba.urllib.request, "build_opener", return_value=opener):
            rows = ArubaClient("u", "p").user_table("c1.example.test")
        self.assertEqual(len(rows), 1)
        self.assertTrue(calls[0].endswith("/v1/api/login"))
        self.assertIn("command=show+user-table", calls[1])
        self.assertIn("/v1/api/logout", calls[2])

    def test_an_empty_reply_is_an_empty_table(self):
        opener, _ = self.opener([json.dumps({"_global_result": {"UIDARUBA": "tok"}}), "", "{}"])
        with mock.patch.object(aruba.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(ArubaClient("u", "p").user_table("c1.example.test"), [])

    def test_failures_name_the_host_and_error_type_only(self):
        bad = '{"Users": [{"Name": "zz-secret@example.test" '            # cut off mid-row: a decode error
        opener, _ = self.opener([json.dumps({"_global_result": {"UIDARUBA": "tok"}}), bad, "{}"])
        with mock.patch.object(aruba.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(ArubaError) as cm:
                ArubaClient("u", "hunter2").user_table("c1.example.test")
        self.assertEqual(str(cm.exception), "c1.example.test: JSONDecodeError")
        self.assertIsNone(cm.exception.__cause__)
        self.assertNotIn("hunter2", str(cm.exception))


class InCollector(unittest.TestCase):
    def collector(self, **cfg):
        c = Collector(Config(devices_interval=60, **cfg), mock.Mock(), Clock())
        return c

    def test_off_without_controllers_and_a_password(self):
        c = self.collector(aruba_controllers=("c1",))
        self.assertEqual(c._due["aruba"], float("inf"))
        self.assertNotIn("aruba_clients{", c.render())

    def test_on_serves_the_counts(self):
        c = self.collector(aruba_controllers=("c1", "c2"), aruba_password="pw")
        c._aruba = ArubaPoller(FakeController({"c1": [row(PN)], "c2": [row(MC, mac="m1", name="zz-a")]}),
                               ["c1", "c2"], PN, MC, 120, clock=c.clock)
        c.refresh("aruba")
        text = c.render()
        self.assertIn('aruba_clients{ssid="PantherNet"} 1', text)
        self.assertIn('aruba_controller_up{controller="c1"} 1', text)
        self.assertIn("aruba_people_on_both_ssids 1", text)       # zz-a is on both, under two name forms
        self.assertIn('mist_exporter_last_success_timestamp_seconds{source="aruba"}', text)

    def test_a_failed_poll_is_counted_and_keeps_serving(self):
        c = self.collector(aruba_controllers=("c1",), aruba_password="pw")
        c._aruba = ArubaPoller(FakeController({"c1": [row(PN)]}), ["c1"], PN, MC, 120, clock=c.clock)
        c.refresh("aruba")
        c._aruba.client.tables["c1"] = ArubaError("c1: URLError")
        c._aruba._last.clear()
        c.refresh("aruba")
        text = c.render()
        self.assertIn('mist_exporter_errors_total{source="aruba"} 1', text)
        self.assertIn('aruba_clients{ssid="PantherNet"} 1', text)


class Metrics(unittest.TestCase):
    def test_empty_data_renders_no_samples(self):
        self.assertTrue(all(not f.samples for f in aruba_families({})))

    def test_families(self):
        data = summarize({"c1": [row(PN), row(MC, mac="m1", name="zz-a", role="STUDENT", typ="iPhone")]}, PN, MC)
        data["controllers"] = {"c1": {"up": 1.0, "seconds": 4.2, "clients": 2.0, "age": 0.0}}
        fams = aruba_families(data)
        self.assertEqual(value(fams, "aruba_clients", ssid=PN), 1.0)
        self.assertEqual(value(fams, "aruba_controller_clients", controller="c1", ssid=MC), 1.0)
        self.assertEqual(value(fams, "aruba_ssid_role_clients", ssid=MC, role="STUDENT"), 1.0)
        self.assertEqual(value(fams, "aruba_ssid_device_clients", ssid=MC, device_type="iPhone"), 1.0)
        self.assertEqual(value(fams, "aruba_ssid_band_clients", ssid=PN, band="5GHz"), 1.0)
        self.assertEqual(value(fams, "aruba_people_on_both_ssids"), 1.0)
        self.assertEqual(value(fams, "aruba_controller_poll_seconds", controller="c1"), 4.2)


if __name__ == "__main__":
    unittest.main()
