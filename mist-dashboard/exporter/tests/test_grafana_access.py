"""Anonymous viewing of Grafana must stay read-only, off by default, and limited by nginx to listed networks."""
import os
import re
import unittest

DEPLOY = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy", "mist-dashboard")


def read(*parts):
    with open(os.path.join(DEPLOY, *parts), encoding="utf-8") as f:
        return f.read()


class AnonymousViewing(unittest.TestCase):
    def setUp(self):
        compose = read("compose.yaml")
        self.grafana = compose[compose.index("  grafana:"):compose.index("\nnetworks:")]

    def test_it_is_off_unless_asked_for(self):
        self.assertIn("GF_AUTH_ANONYMOUS_ENABLED: ${GRAFANA_ANONYMOUS:-false}", self.grafana)

    def test_anonymous_visitors_can_only_view(self):
        self.assertIn("GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer", self.grafana)
        self.assertNotRegex(self.grafana, r"ANONYMOUS_ORG_ROLE:\s*(Editor|Admin)")

    def test_signing_up_stays_off_and_embedding_is_off_unless_asked_for(self):
        self.assertIn('GF_USERS_ALLOW_SIGN_UP: "false"', self.grafana)
        self.assertIn("GF_SECURITY_ALLOW_EMBEDDING: ${GRAFANA_ALLOW_EMBEDDING:-false}", self.grafana)


class NetworkLimit(unittest.TestCase):
    def test_nginx_will_not_start_without_the_allow_list(self):
        conf = read("nginx", "gravitron-dashboards.conf")
        self.assertIn("include /etc/nginx/grafana-allow.conf;", conf)       # no wildcard: a missing file is an error
        server = conf[conf.index("server {"):]
        self.assertLess(server.index("include /etc/nginx/grafana-allow.conf;"), server.index("location /"))

    def test_the_example_denies_everyone_else_and_leaves_out_private_ranges(self):
        example = read("nginx", "grafana-allow.conf.example")
        active = [l.strip() for l in example.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(active[-1], "deny all;")
        self.assertNotIn("allow 10.0.0.0/8;", active)
        self.assertNotIn("allow 0.0.0.0/0;", active)
        self.assertTrue(any(re.fullmatch(r"allow 140\.233\.0\.0/16;\s*", l) or l.startswith("allow 140.233.0.0/16;") for l in active))

    def test_the_orb_push_keeps_its_own_list_so_the_sensors_still_get_in(self):
        example = read("nginx", "orb-ingest.conf.example")
        self.assertIn("deny all;", example)
        self.assertIn("allow ", example)


if __name__ == "__main__":
    unittest.main()
