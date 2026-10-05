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
    from mist_exporter.metrics import alarm_families, device_families, wireless_families
    names = {f.name for f in device_families([], {}) + alarm_families([], {})
             + wireless_families([], {}, [], [], {})}
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
            exprs = [t["expr"] for p in d["panels"] for t in p["targets"]]
            exprs += [v["query"]["query"] for v in d["templating"]["list"]]
            for e in exprs:
                for metric in re.findall(r"\bmist_[a-z_]+\b", e):
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
