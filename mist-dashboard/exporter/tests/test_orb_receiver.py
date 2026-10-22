"""The Orb receiver config must never turn identifiable fields into Prometheus labels."""
import os
import re
import unittest

DEPLOY = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy", "mist-dashboard")


def read(*parts):
    with open(os.path.join(DEPLOY, *parts), encoding="utf-8") as f:
        return f.read()


class TelegrafConfig(unittest.TestCase):
    def setUp(self):
        self.conf = read("telegraf", "telegraf.conf")

    def tag_keys(self):
        m = re.search(r"tag_keys\s*=\s*\[(.*?)\]", self.conf, re.S)
        self.assertTrue(m, "tag_keys missing")
        return set(re.findall(r'"([^"]+)"', m.group(1)))

    def test_strings_never_become_labels(self):
        self.assertRegex(self.conf, r"(?m)^\s*string_as_label\s*=\s*false")

    def test_identifiable_fields_are_not_labels(self):
        forbidden = {"mac_address", "public_ip", "private_ip", "latitude", "longitude", "city", "state",
                     "country_code", "isp_name", "device_name", "dns_resolver", "dns_failed_resolvers", "ssid"}
        self.assertFalse(self.tag_keys() & forbidden, self.tag_keys() & forbidden)

    def test_the_sensor_and_ap_labels_are_kept(self):
        self.assertTrue({"orb_name", "bssid"} <= self.tag_keys())

    def test_nothing_is_written_to_disk(self):
        self.assertNotIn("outputs.file", self.conf)
        self.assertNotIn("outputs.influxdb", self.conf)

    def test_listens_only_on_the_ingest_path_for_post(self):
        self.assertIn('paths = ["/ingest"]', self.conf)
        self.assertIn('methods = ["POST"]', self.conf)


class Deployment(unittest.TestCase):
    def test_telegraf_image_is_pinned_and_hardened(self):
        compose = read("compose.yaml")
        block = compose[compose.index("  telegraf:"):compose.index("  prometheus:")]
        self.assertRegex(block, r"image: telegraf:\d+\.\d+\.\d+")
        self.assertIn("read_only: true", block)
        self.assertIn("cap_drop:", block)
        self.assertIn('"127.0.0.1:7081:7081"', block)           # reachable from nginx on the host only

    def test_prometheus_scrapes_the_receiver(self):
        self.assertIn("telegraf:9273", read("prometheus", "prometheus.yml"))

    def test_the_nginx_example_has_no_real_secret_and_is_restricted(self):
        example = read("nginx", "orb-ingest.conf.example")
        self.assertIn("REPLACE_WITH_RANDOM_TOKEN", example)
        self.assertIn("deny all;", example)
        self.assertIn("limit_except POST", example)
        self.assertIn("orb-ingest*.conf", read("nginx", "gravitron-dashboards.conf"))


if __name__ == "__main__":
    unittest.main()
