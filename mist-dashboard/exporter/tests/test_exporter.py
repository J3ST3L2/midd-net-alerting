import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.main import make_handler  # noqa: E402
from mist_exporter.metrics import (TOP_APS, alarm_families, device_families, render,  # noqa: E402
                                   site_ap_families, sle_families, wireless_families)
from mist_exporter.mist_api import MistError  # noqa: E402

SITES = {"s1": "Davis Library", "s2": 'Atwater "Hall"'}

# Shapes follow what Middlebury's org returns: APs carry status/uptime/version only; switches carry
# cpu_stat.idle, memory_stat.usage and per-module PoE.
AP = {"mac": "5C:5B:35:00:00:01", "type": "ap", "site_id": "s1", "name": "ap-davis-1", "model": "AP45",
      "status": "connected", "version": "0.14.1", "ip": "10.0.0.5", "uptime": 86400, "last_seen": 1790000000,
      "cpu_stat": None, "memory_stat": None, "module_stat": None}
SWITCH = {"mac": "5c5b35aa0001", "type": "switch", "site_id": "s2", "name": "sw-atwater", "model": "EX4100",
          "status": "disconnected", "cpu_stat": {"idle": 80}, "memory_stat": {"usage": 41.5},
          "module_stat": [{"poe": {"power_draw": 120.5, "max_power": 740}},
                          {"poe": {"power_draw": 30, "max_power": 740}}]}


def samples(fams, name):
    return next(f for f in fams if f.name == name).samples


class DeviceMetrics(unittest.TestCase):
    def setUp(self):
        self.fams = device_families([AP, SWITCH], SITES)

    def test_up_and_labels(self):
        up = {l["name"]: v for l, v in samples(self.fams, "mist_device_up")}
        self.assertEqual(up, {"ap-davis-1": 1.0, "sw-atwater": 0.0})
        labels = samples(self.fams, "mist_device_up")[0][0]
        self.assertEqual((labels["site"], labels["mac"]), ("Davis Library", "5c5b35000001"))

    def test_switch_load(self):
        self.assertEqual({l["name"]: v for l, v in samples(self.fams, "mist_device_cpu_percent")},
                         {"sw-atwater": 20.0})
        self.assertEqual({l["name"]: v for l, v in samples(self.fams, "mist_device_memory_percent")},
                         {"sw-atwater": 41.5})

    def test_ap_with_null_stats_makes_no_load_samples(self):
        names = {l["name"] for l, _ in samples(self.fams, "mist_device_cpu_percent")}
        self.assertNotIn("ap-davis-1", names)

    def test_switch_poe_sums_modules(self):
        self.assertEqual(samples(self.fams, "mist_switch_poe_draw_watts")[0][1], 150.5)
        self.assertEqual(samples(self.fams, "mist_switch_poe_budget_watts")[0][1], 1480.0)

    def test_disconnected_ap_with_null_fields(self):
        gone = dict(AP, mac="aa", ip=None, last_seen=None, uptime=None, version=None)
        fams = device_families([gone], SITES)
        self.assertEqual(samples(fams, "mist_device_uptime_seconds"), [])
        self.assertEqual(samples(fams, "mist_device_info")[0][0]["version"], "")

    def test_rows_without_mac_skipped(self):
        self.assertEqual(samples(device_families([{"type": "ap"}], {}), "mist_device_up"), [])

    def test_bool_is_not_a_number(self):
        fams = device_families([dict(AP, uptime=True)], SITES)
        self.assertEqual(samples(fams, "mist_device_uptime_seconds"), [])


class WirelessMetrics(unittest.TestCase):
    def test_site_ap_and_breakdowns(self):
        fams = wireless_families(
            [AP, SWITCH], SITES,
            site_stats=[{"name": "Davis Library", "num_clients": 214}, {"num_clients": 5}],
            ap_counts=[{"last_ap": "5c5b35000001", "count": 14}, {"last_ap": "ffffffffffff", "count": 3}],
            site_breakdowns={"s1": {"band": [{"band": "5", "count": 900}, {"band": "24", "count": 300}],
                                    "ssid": [{"last_ssid": "eduroam", "count": 1000},
                                             {"last_ssid": "", "count": 2}]},
                             "s2": {"band": [{"band": "5", "count": 7}], "ssid": []}})
        self.assertEqual(samples(fams, "mist_site_clients"), [({"site": "Davis Library"}, 214.0)])
        ap = samples(fams, "mist_ap_clients")                      # unknown AP and non-AP rows dropped
        self.assertEqual([(l["name"], v) for l, v in ap], [("ap-davis-1", 14.0)])
        band = {(l["site"], l["band"]): v for l, v in samples(fams, "mist_site_clients_by_band")}
        self.assertEqual(band, {("Davis Library", "5"): 900.0, ("Davis Library", "24"): 300.0,
                                ('Atwater "Hall"', "5"): 7.0})
        self.assertEqual(samples(fams, "mist_site_clients_by_ssid"),
                         [({"site": "Davis Library", "ssid": "eduroam"}, 1000.0)])  # blank SSID dropped

    def test_empty_inputs(self):
        fams = wireless_families([], {}, [], [], {})
        self.assertEqual([f.samples for f in fams], [[], [], [], []])


class SiteApMetrics(unittest.TestCase):
    def test_positions_become_labels_and_bad_ones_are_skipped(self):
        fams = site_ap_families([
            {"name": "Davis Library", "num_ap": 40, "num_ap_connected": 38, "latlng": {"lat": 44.0087, "lng": -73.1768}},
            {"name": "No GPS", "num_ap": 3},
            {"name": "Null Island", "num_ap": 2, "latlng": {"lat": 0, "lng": 0}},
            {"num_ap": 1, "latlng": {"lat": 1, "lng": 1}}])
        pos = {"site": "Davis Library", "latitude": "44.0087", "longitude": "-73.1768"}
        self.assertEqual(samples(fams, "mist_site_aps"), [(pos, 40.0)])
        self.assertEqual(samples(fams, "mist_site_aps_connected"), [(pos, 38.0)])

    def test_empty_inputs(self):
        self.assertEqual([f.samples for f in site_ap_families([])], [[], []])


class SleMetrics(unittest.TestCase):
    def body(self, num_users, total_users, num_aps=3, total_aps=40):
        return {"impact": {"num_users": num_users, "total_users": total_users,
                           "num_aps": num_aps, "total_aps": total_aps}}

    def test_site_scores_and_worst_aps(self):
        sle = {"s1": {"summary": {"coverage": self.body(120, 600), "roaming": {"impact": {}}},
                      "aps": {"coverage": [{"ap_mac": "5C:5B:35:00:00:01", "name": "ap-a", "degraded": 94, "total": 165},
                                           {"ap_mac": "5c5b35000002", "name": "ap-b", "degraded": 5, "total": 100},
                                           {"ap_mac": "", "name": "no-mac", "degraded": 1, "total": 2},
                                           {"ap_mac": "5c5b35000003", "name": "idle", "degraded": 0, "total": 0}]}}}
        users, aps, worst = sle_families(SITES, sle)
        got = {(l["metric"], l["state"]): v for l, v in users.samples}
        self.assertEqual(got, {("coverage", "impacted"): 120.0, ("coverage", "total"): 600.0})
        self.assertEqual(len(aps.samples), 2)                           # impacted + total APs
        self.assertEqual([(l["name"], round(v, 3)) for l, v in worst.samples],
                         [("ap-a", 0.57), ("ap-b", 0.05)])               # sorted worst-first, bad rows dropped
        self.assertEqual(worst.samples[0][0]["mac"], "5c5b35000001")

    def test_worst_aps_are_capped(self):
        rows = [{"ap_mac": "%012x" % i, "name": "ap%d" % i, "degraded": i, "total": 1000} for i in range(100)]
        _, _, worst = sle_families(SITES, {"s1": {"summary": {}, "aps": {"capacity": rows}}})
        self.assertEqual(len(worst.samples), TOP_APS)
        self.assertEqual(worst.samples[0][0]["name"], "ap99")

    def test_empty(self):
        self.assertEqual([f.samples for f in sle_families(SITES, {})], [[], [], []])


class AlarmMetrics(unittest.TestCase):
    def test_counts_by_state(self):
        alarms = [{"site_id": "s1", "severity": "critical", "type": "device_down", "group": "infrastructure"},
                  {"site_id": "s1", "severity": "critical", "type": "device_down", "group": "infrastructure"},
                  {"site_id": "s1", "severity": "critical", "type": "device_down", "group": "infrastructure",
                   "resolved_time": 5},
                  {"site_id": "s2", "severity": "warn", "type": "x", "group": "marvis", "status": "resolved"}]
        got = {(l["site"], l["state"]): v for l, v in samples(alarm_families(alarms, SITES), "mist_alarms")}
        self.assertEqual(got, {("Davis Library", "open"): 2.0, ("Davis Library", "resolved"): 1.0,
                               ('Atwater "Hall"', "resolved"): 1.0})


class Rendering(unittest.TestCase):
    def test_exposition_format_and_escaping(self):
        text = render(device_families([AP, SWITCH], SITES))
        self.assertIn("# TYPE mist_device_up gauge", text)
        self.assertIn('mist_device_up{mac="5c5b35000001",model="AP45",name="ap-davis-1",'
                      'site="Davis Library",type="ap"} 1', text)
        self.assertIn('site="Atwater \\"Hall\\""', text)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("} 1\n", text)                           # whole numbers render without ".0"


class FakeClient:
    def __init__(self):
        self.fail = None
        self.devices, self.alarms, self.calls, self.client_calls = [AP], [], 0, []
        self.sle_calls, self.sle_unsupported = [], set()

    def list_sites(self):
        return SITES

    def list_devices(self):
        self.calls += 1
        if self.fail:
            raise self.fail
        return list(self.devices)

    def list_site_stats(self):
        return [{"id": "s1", "name": "Davis Library", "num_clients": 214,
                 "num_ap": 40, "num_ap_connected": 38, "latlng": {"lat": 44.0087, "lng": -73.1768}},
                {"id": "s2", "name": "Quiet Site", "num_clients": 0}]

    def client_counts(self, distinct, duration, site_id=None, ssid=None):
        self.client_calls.append((distinct, duration, site_id))
        return {"ap": [{"last_ap": "5c5b35000001", "count": 14}],
                "band": [{"band": "5", "count": 9}],
                "ssid": [{"last_ssid": "eduroam", "count": 9}],
                "os": [{"last_os": "macOS Catalina", "count": 6}, {"last_os": "iOS 16", "count": 3},
                       {"last_os": "Unknown", "count": 2}]}[distinct]

    def sle_summary(self, site_id, metric, duration):
        self.sle_calls.append(("summary", site_id, metric, duration))
        if metric in self.sle_unsupported:
            raise MistError("Mist HTTP 404", status=404)
        return {"impact": {"num_users": 10, "total_users": 100, "num_aps": 2, "total_aps": 20}}

    def sle_impacted_aps(self, site_id, metric, duration):
        self.sle_calls.append(("aps", site_id, metric, duration))
        return [{"ap_mac": "5c5b35000001", "name": "ap-davis-1", "degraded": 50, "total": 100}]

    def search_client_events(self, event_type, ssid, start, end, limit=1000):
        return [], False

    def search_clients(self, start, end, limit=1000, **filters):
        return [], 0

    def search_alarms(self, start, end):
        return list(self.alarms)


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def __call__(self):
        return self.t


class CollectorBehavior(unittest.TestCase):
    def setUp(self):
        self.clock, self.client = Clock(), FakeClient()
        self.c = Collector(Config(devices_interval=60), self.client, self.clock)

    def test_unhealthy_until_first_device_refresh(self):
        self.assertFalse(self.c.healthy())
        self.c.run_due()
        self.assertTrue(self.c.healthy())
        text = self.c.render()
        self.assertIn('mist_site_clients{site="Davis Library"} 214', text)
        self.assertIn('mist_ap_clients{', text)
        self.assertIn('mist_site_clients_by_ssid{site="Davis Library",ssid="eduroam"} 9', text)

    def test_breakdowns_only_query_sites_that_have_clients(self):
        self.c.run_due()
        per_site = [call for call in self.client.client_calls if call[2]]
        self.assertEqual({call[2] for call in per_site}, {"s1"})        # s2 reports 0 clients: skipped
        self.assertEqual(sorted(call[0] for call in per_site), ["band", "ssid"])
        self.assertTrue(all(call[1] == "30m" for call in self.client.client_calls))

    def test_scrape_does_not_call_mist(self):
        self.c.run_due()
        before = self.client.calls
        for _ in range(5):
            self.c.render()
        self.assertEqual(self.client.calls, before)

    def test_failure_keeps_previous_data_and_counts_error(self):
        self.c.run_due()
        self.client.fail = MistError("boom", status=500)
        self.clock.t += 61
        self.c.run_due()
        text = self.c.render()
        self.assertIn('mist_device_up{', text)                       # old data still served
        self.assertIn('mist_exporter_errors_total{source="devices"} 1', text)

    def test_sle_only_for_active_sites_and_ap_detail_only_for_coverage_capacity(self):
        self.c.run_due()
        self.assertEqual({c[1] for c in self.client.sle_calls}, {"s1"})             # s2 has no clients
        self.assertEqual(sorted(c[2] for c in self.client.sle_calls if c[0] == "aps"), ["capacity", "coverage"])
        self.assertEqual(len([c for c in self.client.sle_calls if c[0] == "summary"]), 5)
        self.assertTrue(all(c[3] == "1d" for c in self.client.sle_calls))
        text = self.c.render()
        self.assertIn('mist_site_sle_users{metric="coverage",site="Davis Library",state="impacted"} 10', text)
        self.assertIn('mist_ap_sle_degraded_ratio{', text)

    def test_unsupported_sle_metric_is_skipped_not_fatal(self):
        self.client.sle_unsupported = {"roaming"}
        self.c.run_due()
        text = self.c.render()
        self.assertIn('metric="coverage"', text)
        self.assertNotIn('metric="roaming"', text)
        self.assertIn('mist_exporter_errors_total{source="sle"} 0', text)

    def test_one_failing_source_does_not_block_the_others(self):
        self.client.client_counts = lambda *a, **k: (_ for _ in ()).throw(MistError("bad", status=400))
        self.c.run_due()
        text = self.c.render()
        self.assertIn('mist_exporter_errors_total{source="clients"} 1', text)
        self.assertIn('mist_site_clients{site="Davis Library"} 214', text)
        self.assertTrue(self.c.healthy())

    def test_rate_limit_retry_after_is_honoured(self):
        self.c.run_due()
        self.client.fail = MistError("429", status=429, retry_after=300)
        self.clock.t += 61
        self.c.run_due()
        calls = self.client.calls
        self.clock.t += 120                                            # past the interval, before retry-after
        self.c.run_due()
        self.assertEqual(self.client.calls, calls)

    def test_goes_unhealthy_when_data_goes_stale(self):
        self.c.run_due()
        self.client.fail = MistError("down")
        self.clock.t += 600
        self.c.run_due()
        self.assertFalse(self.c.healthy())


class HttpEndpoints(unittest.TestCase):
    def test_metrics_healthz_and_404(self):
        collector = Collector(Config(devices_interval=60), FakeClient(), Clock())
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(collector))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:      # nothing fetched yet
                urllib.request.urlopen(base + "/healthz")
            self.assertEqual(cm.exception.code, 503)
            collector.run_due()
            self.assertEqual(urllib.request.urlopen(base + "/healthz").status, 200)
            body = urllib.request.urlopen(base + "/metrics").read().decode()
            self.assertIn("mist_device_up{", body)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/nope")
            self.assertEqual(cm.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
