import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "exporter"))

import build  # noqa: E402
from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402


class FakeClient:
    def list_sites(self):
        return {}

    def list_devices(self):
        return []

    def search_alarms(self, start, end):
        return []


def exported_metric_names():
    """Every family name the exporter can emit, taken from its own # TYPE lines."""
    text = Collector(Config(), FakeClient()).render()
    from mist_exporter.metrics import (alarm_families, aruba_families, device_families, mist_os_families,
                                       sle_families, wireless_families)
    names = {f.name for f in device_families([], {}) + alarm_families([], {})
             + wireless_families([], {}, [], [], {}) + sle_families({}, {})
             + aruba_families({}) + mist_os_families({})}
    return names | set(re.findall(r"^# TYPE (\S+)", text, re.M))


class Dashboards(unittest.TestCase):
    def test_committed_json_matches_generator(self):
        for name, d in build.build().items():
            path = os.path.join(build.OUT, name)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), build.render(d), "%s is stale; run build.py" % name)

    def test_every_queried_metric_is_exported(self):
        known = exported_metric_names()
        for name, d in build.build().items():
            exprs = [t["expr"] for p in d["panels"] for t in p.get("targets", [])]
            exprs += [v["query"]["query"] for v in d["templating"]["list"]]
            for e in exprs:
                for metric in re.findall(r"\b(?:mist|aruba)_[a-z_]+\b", e):
                    self.assertIn(metric, known, "%s queries unknown metric %s" % (name, metric))

    def test_structure(self):
        uids = set()
        for name, d in build.build().items():
            self.assertNotIn(d["uid"], uids)
            uids.add(d["uid"])
            ids = [p["id"] for p in d["panels"]]
            self.assertEqual(len(ids), len(set(ids)))
            for p in d["panels"]:
                g = p["gridPos"]
                self.assertLessEqual(g["x"] + g["w"], 24, "%s/%s overflows the grid" % (name, p["title"]))
            json.dumps(d)

    def test_drilldown_links_point_at_real_dashboards(self):
        dashboards = build.build()
        uids = {d["uid"] for d in dashboards.values()}
        found = 0
        for name, d in dashboards.items():
            for p in d["panels"]:
                if p["type"] == "row":
                    continue
                fc = p["fieldConfig"]
                links = list(fc["defaults"].get("links", []))
                for o in fc["overrides"]:
                    for prop in o["properties"]:
                        if prop["id"] == "links":
                            links += prop["value"]
                for link in links:
                    m = re.match(r"/d/([a-z-]+)\?var-(\w+)=", link["url"])
                    self.assertTrue(m, "%s/%s: unexpected link %s" % (name, p["title"], link["url"]))
                    self.assertIn(m.group(1), uids)
                    target_vars = {v["name"] for v in dashboards[m.group(1) + ".json"]["templating"]["list"]}
                    self.assertIn(m.group(2), target_vars)
                    found += 1
        self.assertGreater(found, 5)

    def test_wireless_opens_on_seven_days_and_charts_follow_the_time_picker(self):
        dash = build.build()["mist-wireless.json"]
        self.assertEqual(dash["time"]["from"], "now-7d")
        for p in dash["panels"]:
            self.assertNotIn("timeFrom", p, p["title"])         # no panel pins its own range
        row_y = next(p["gridPos"]["y"] for p in dash["panels"] if p["title"] == "PantherNet adoption")
        charts = [p for p in dash["panels"] if p["type"] == "timeseries" and p["gridPos"]["y"] > row_y]
        self.assertGreaterEqual(len(charts), 3)

    def test_pantherNet_row_is_plain_language(self):
        dash = build.build()["mist-wireless.json"]
        row_y = next(p["gridPos"]["y"] for p in dash["panels"] if p["title"] == "PantherNet adoption")
        panels = [p for p in dash["panels"] if p["gridPos"]["y"] > row_y]
        text = " ".join(p["title"] + " " + p.get("description", "") for p in panels)
        for jargon in ("match", "dot1x_failed", "eligible", "judged clients"):
            self.assertNotIn(jargon, text.lower(), jargon)
        # reasons are shown with readable names, and every chart explains itself
        reasons = next(p for p in panels if p["title"].startswith("Why PantherNet"))
        self.assertIn("Couldn't complete 802.1X sign-in", reasons["targets"][0]["expr"])
        for p in panels:
            if p["type"] == "timeseries":
                self.assertGreater(len(p["description"]), 40, p["title"])
        # the old dual-axis chart and the always-zero device series are gone
        self.assertFalse(any('match="device"' in t["expr"] for p in panels for t in p["targets"]))
        # the other dashboards keep the usual 6 hour default
        others = [d["time"]["from"] for n, d in build.build().items()
                  if n not in ("mist-wireless.json", "aruba-wireless.json", "pn-adoption.json", "orb-sensors.json")]
        self.assertEqual(set(others), {"now-6h"})

    def test_history_dependent_boxes_say_loading_instead_of_no_data(self):
        dashboards = build.build()
        wanted = {"mist-wireless.json": {"Users who failed to sign in", "...who then used MiddleburyCollege",
                                         "Users judged"},
                  "pn-adoption.json": {"Users who failed to sign in", "...who then used MiddleburyCollege"}}
        for name, titles in wanted.items():
            found = {p["title"] for p in dashboards[name]["panels"]
                     if p.get("fieldConfig", {}).get("defaults", {}).get("noValue") == "Loading history"}
            self.assertEqual(found, titles, name)

    def test_panels_that_can_be_legitimately_empty_say_so(self):
        dashboards = build.build()
        wanted = {"mist-overview.json": {"Open alarms (24 h)", "Alarms by severity (24 h window)"},
                  "mist-site.json": {"Open alarms (24 h)"},
                  "mist-device.json": {"Clients", "CPU", "Memory", "PoE draw"},
                  "mist-wifi.json": {"APs that dropped offline (24 h)"}}
        for name, titles in wanted.items():
            got = {p["title"] for p in dashboards[name]["panels"]
                   if p.get("fieldConfig", {}).get("defaults", {}).get("noValue")}
            self.assertTrue(titles <= got, (name, titles - got))

    # Fields Orb documents for each dataset (https://orb.net/docs/deploy-and-configure/datasets). Telegraf names the
    # metrics <dataset>_<field>, so a typo here would be an empty panel.
    ORB_FIELDS = {
        "scores_1m": {"orb_score", "responsiveness_score", "reliability_score", "speed_score", "lag_avg_us",
                      "download_avg_kbps", "upload_avg_kbps"},
        "responsiveness_1s": {"latency_avg_us", "jitter_avg_us", "packet_loss_pct", "lag_avg_us",
                              "router_latency_avg_us", "router_jitter_avg_us", "router_packet_loss_pct"},
        "speed_results": {"download_kbps", "upload_kbps"},
        "web_responsiveness_results": {"ttfb_us", "dns_us"},
        "wifi_link_1s": {"rssi_avg", "snr_avg", "noise_avg", "tx_rate_mbps", "rx_rate_mbps", "channel_number"},
        "system_telemetry_1m": {"cpu_avg_pct", "mem_used_avg_bytes", "disk_used_avg_bytes"},
    }

    def test_orb_dashboard_uses_only_documented_fields_and_labels(self):
        dash = build.build()["orb-sensors.json"]
        exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
        exprs += [v["query"]["query"] for v in dash["templating"]["list"]]
        pattern = (r"\b(scores_1m|responsiveness_1s|speed_results|web_responsiveness_results|wifi_link_1s|"
                   r"system_telemetry_1m)_([a-z0-9_]+)\b")
        found = 0
        for e in exprs:
            for dataset, field in re.findall(pattern, e):
                self.assertIn(field, self.ORB_FIELDS[dataset], "%s_%s is not a documented Orb field" % (dataset, field))
                found += 1
            for label in re.findall(r"\b(public_ip|private_ip|mac_address|latitude|longitude)\b", e):
                self.fail("query uses an identifiable label: " + label)
        self.assertGreater(found, 15)

    def test_orb_panels_say_what_they_are_waiting_for(self):
        for p in build.build()["orb-sensors.json"]["panels"]:
            if "fieldConfig" in p:
                self.assertEqual(p["fieldConfig"]["defaults"].get("noValue"), build.ORB_WAITING, p["title"])
                self.assertLess(len(build.ORB_WAITING), 30)               # must fit a small stat box

    def test_no_panel_overlap(self):
        for name, d in build.build().items():
            boxes = [(p["gridPos"], p["title"]) for p in d["panels"]]
            for i, (a, ta) in enumerate(boxes):
                for b, tb in boxes[i + 1:]:
                    apart = (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or
                             a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
                    self.assertTrue(apart, "%s: '%s' overlaps '%s'" % (name, ta, tb))


if __name__ == "__main__":
    unittest.main()
