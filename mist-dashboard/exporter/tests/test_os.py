import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter.aruba import summarize  # noqa: E402
from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.metrics import mist_os_families  # noqa: E402
from mist_exporter.mist_api import MistError  # noqa: E402
from mist_exporter.osfamily import FAMILIES, os_family  # noqa: E402

from test_aruba import MC, PN, row  # noqa: E402
from test_exporter import Clock, FakeClient  # noqa: E402


class Families(unittest.TestCase):
    def test_mist_and_aruba_labels_land_in_the_same_family(self):
        cases = {"macOS": ("macOS Catalina", "macOS", "Mac OS X 10.15", "OS X"),
                 "iOS": ("iOS 16", "iPhone", "iPad", "iPadOS 17"),
                 "Windows": ("Windows 10", "Win 11", "Windows", "Windows Phone"),
                 "Android": ("Android 13", "android"),
                 "Linux": ("Linux", "Ubuntu 22.04"),
                 "ChromeOS": ("Chrome OS", "ChromeOS"),
                 "Unknown": ("", None, "Unknown", "None"),
                 "Other": ("Roku", "PlayStation", "Tizen")}
        for family, names in cases.items():
            for name in names:
                self.assertEqual(os_family(name), family, name)

    def test_every_result_is_a_listed_family(self):
        for name in ("x", "Win", "mac", None, 5):
            self.assertIn(os_family(name), FAMILIES)


class MistSide(unittest.TestCase):
    def test_counts_are_summed_into_families_per_ssid(self):
        fams = mist_os_families({PN: [{"last_os": "macOS Catalina", "count": 400}, {"last_os": "macOS Ventura", "count": 100},
                                      {"last_os": "iOS 16", "count": 50}, {"last_os": "Unknown", "count": 7}, {"count": 1}]})
        got = {(l["ssid"], l["os"]): v for l, v in fams[0].samples}
        self.assertEqual(got, {(PN, "macOS"): 500.0, (PN, "iOS"): 50.0, (PN, "Unknown"): 8.0})

    def test_collector_serves_them(self):
        c = Collector(Config(devices_interval=60), FakeClient(), Clock())
        c.run_due()
        text = c.render()
        self.assertIn('mist_ssid_os_clients{os="macOS",ssid="PantherNet"} 6', text)
        self.assertIn('mist_ssid_os_clients{os="macOS",ssid="MiddleburyCollege"} 6', text)

    def test_a_failed_os_call_does_not_lose_the_other_counts(self):
        client = FakeClient()
        original = client.client_counts

        def counts(distinct, duration, site_id=None, ssid=None):
            if distinct == "os":
                raise MistError("Mist HTTP 400", status=400)
            return original(distinct, duration, site_id, ssid)
        client.client_counts = counts
        c = Collector(Config(devices_interval=60), client, Clock())
        c.run_due()
        text = c.render()
        self.assertIn('mist_ap_clients{', text)
        self.assertNotIn("mist_ssid_os_clients{", text)


class ArubaSide(unittest.TestCase):
    def test_device_types_fold_into_families(self):
        rows = [row(PN, mac="a1", typ="Win 11"), row(PN, mac="a2", typ="iPhone"), row(PN, mac="a3", typ="iPad"),
                row(PN, mac="a4", typ=None), row(MC, mac="m1", typ="macOS")]
        out = summarize({"c1": rows}, PN, MC)
        self.assertEqual(out["os"][PN], {"Windows": 1, "iOS": 2, "Unknown": 1})
        self.assertEqual(out["os"][MC], {"macOS": 1})


if __name__ == "__main__":
    unittest.main()
