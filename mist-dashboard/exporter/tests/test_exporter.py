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
from mist_exporter.metrics import alarm_families, device_families, render  # noqa: E402
from mist_exporter.main import make_handler  # noqa: E402
from mist_exporter.mist_api import MistError  # noqa: E402

SITES = {"s1": "Davis Library", "s2": 'Atwater "Hall"'}

AP = {"mac": "5C:5B:35:00:00:01", "type": "ap", "site_id": "s1", "name": "ap-davis-1", "model": "AP45",
      "status": "connected", "version": "0.14.1", "ip": "10.0.0.5", "uptime": 86400, "last_seen": 1790000000,
      "cpu_util": 12, "mem_used_kb": 250, "mem_total_kb": 1000, "num_clients": 14,
      "radio_stat": {"band_24": {"num_clients": 4}, "band_5": {"num_clients": 10}, "band_6": None},
      "power_constrained": False}
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

    def test_ap_load_and_clients(self):
        self.assertEqual(samples(self.fams, "mist_ap_clients")[0][1], 14.0)
        self.assertEqual({l["band"]: v for l, v in samples(self.fams, "mist_ap_band_clients")},
                         {"2.4": 4.0, "5": 10.0})          # null 6 GHz block yields no sample
        cpu = {l["name"]: v for l, v in samples(self.fams, "mist_device_cpu_percent")}
        mem = {l["name"]: v for l, v in samples(self.fams, "mist_device_memory_percent")}
        self.assertEqual(cpu, {"ap-davis-1": 12.0, "sw-atwater": 20.0})
        self.assertEqual(mem, {"ap-davis-1": 25.0, "sw-atwater": 41.5})

    def test_switch_poe_sums_modules(self):
        self.assertEqual(samples(self.fams, "mist_switch_poe_draw_watts")[0][1], 150.5)
        self.assertEqual(samples(self.fams, "mist_switch_poe_budget_watts")[0][1], 1480.0)

    def test_missing_stats_make_no_samples(self):
        fams = device_families([{"mac": "aa", "type": "gateway", "status": "connected"}], {})
        self.assertEqual(samples(fams, "mist_device_cpu_percent"), [])
        self.assertEqual(samples(fams, "mist_device_up")[0][0]["site"], "unassigned")

    def test_rows_without_mac_skipped(self):
        self.assertEqual(samples(device_families([{"type": "ap"}], {}), "mist_device_up"), [])

    def test_bool_is_not_a_number(self):
        fams = device_families([dict(AP, uptime=True, cpu_util=True)], SITES)
        self.assertEqual(samples(fams, "mist_device_uptime_seconds"), [])


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
        self.assertNotIn("1.0\n", text.replace("25.0", ""))   # whole numbers render as ints


class FakeClient:
    def __init__(self):
        self.fail = None
        self.devices, self.alarms, self.calls = [AP], [], 0

    def list_sites(self):
        return SITES

    def list_devices(self):
        self.calls += 1
        if self.fail:
            raise self.fail
        return list(self.devices)

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
        self.assertIn('mist_ap_clients{', self.c.render())

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
