import json
import unittest

import vlan_inventory as v


def vlan_row(sid="1", number="10", name="Users", domain_id="2", domain="Campus", desc="User access"):
    return {"vlan_id": sid, "vlan_vlan_id": number, "vlan_name": name, "domain_id": domain_id,
            "domain_name": domain, "vlan_type": "vlan",
            "vlan_class_parameters": json.dumps([{"name": "middlebury_vlan_description", "value": desc}])}


def net_row(sid, addr, prefix, name="", params="", sources=""):
    return {"subnet_id": sid, "subnet_start_hostaddr": addr, "subnet_prefix": prefix, "subnet_name": name,
            "ip_class_parameters": params, "ip_class_parameters_inheritance_source": sources}


LINK = "vlmdomain_name=Campus&vlmvlan_vlan_id=10"
OWN = "vlmdomain_name=real_network,5&vlmvlan_vlan_id=real_network,5"


class VlanTest(unittest.TestCase):
    def test_normalize(self):
        n = v.normalize_vlan(vlan_row(desc="A &amp; B"))
        self.assertEqual((n["domain_id"], n["vlan_id"], n["name"], n["description"]), (2, 10, "Users", "A & B"))

    def test_bad_vlans_rejected(self):
        with self.assertRaises(ValueError):
            v.normalize_vlan(vlan_row(number="5000"))
        with self.assertRaises(ValueError):
            v.normalize_vlan(vlan_row(number=""))


class NetworkTest(unittest.TestCase):
    def test_only_own_parameters_count(self):
        own = v.normalize_network(net_row("5", "10.1.0.0", "24", "n", LINK, OWN))
        self.assertEqual((own["domain_name"], own["vlan_id"]), ("Campus", 10))
        inherited = v.normalize_network(net_row("5", "10.1.0.0", "24", "n", LINK, OWN.replace("5", "9")))
        self.assertEqual((inherited["domain_name"], inherited["vlan_id"]), (None, None))

    def test_placeholder_and_bad_networks_skipped(self):
        self.assertIsNone(v.normalize_network(net_row("1", "0.0.0.0", "0")))
        self.assertIsNone(v.normalize_network(net_row("2", "not-an-ip", "24")))

    def test_fetch_prefers_row_with_link(self):
        rows = [net_row("5", "10.1.0.0", "24", "n"), net_row("5", "10.1.0.0", "24", "n", LINK, OWN)]
        orig = v.eip_api.get_rows
        v.eip_api.get_rows = lambda *a, **k: rows
        try:
            nets = v.fetch_networks({})
        finally:
            v.eip_api.get_rows = orig
        self.assertEqual(len(nets), 1)
        self.assertEqual(nets[0]["vlan_id"], 10)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.vlans = [v.normalize_vlan(vlan_row("1", "20", "Voice")), v.normalize_vlan(vlan_row("2", "10", "Users"))]
        self.nets = [{"id": "5", "network": "10.1.0.0/24", "name": "n", "domain_name": "campus", "vlan_id": 10},
                     {"id": "6", "network": "10.9.0.0/24", "name": "", "domain_name": None, "vlan_id": None}]

    def test_join_is_case_insensitive_and_sorted(self):
        inv = v.build_inventory(self.vlans, self.nets)
        rows = inv[(2, "Campus")]
        self.assertEqual([r["vlan_id"] for r in rows], [10, 20])
        self.assertEqual(rows[0]["networks"], ["10.1.0.0/24 (n)"])
        self.assertEqual(rows[1]["networks"], [])

    def test_render_is_deterministic_and_escaped(self):
        self.vlans[0]["description"] = "<script>"
        inv = v.build_inventory(self.vlans, self.nets)
        page1, d1 = v.render_page(inv)
        page2, d2 = v.render_page(inv)
        self.assertEqual((page1, d1), (page2, d2))
        self.assertNotIn("<script>", page1)
        self.assertIn("content hash <code>%s</code>" % d1, page1)


class FakeConfluence:
    def __init__(self, body, title="VLAN Inventory", space="DOCS", version=3):
        self.page = {"title": title, "space": {"key": space}, "version": {"number": version},
                     "body": {"storage": {"value": body}}}
        self.updates = []

    def get_page(self, _id):
        return self.page

    def update_page(self, page_id, title, space_key, version, body):
        self.updates.append((version, body))


class PublishTest(unittest.TestCase):
    def setUp(self):
        inv = v.build_inventory([v.normalize_vlan(vlan_row())], [])
        self.page, self.digest = v.render_page(inv)

    def test_unchanged_page_is_left_alone(self):
        c = FakeConfluence(self.page)
        self.assertEqual(v.publish(c, "1", "VLAN Inventory", "DOCS", self.page, self.digest), "unchanged")
        self.assertEqual(c.updates, [])

    def test_changed_page_is_updated_with_next_version(self):
        c = FakeConfluence("<p>old</p><p><em>Publisher: MiddKiPS SOLIDserver VLAN inventory sync.</em></p>")
        self.assertEqual(v.publish(c, "1", "VLAN Inventory", "DOCS", self.page, self.digest), "updated")
        self.assertEqual(c.updates[0][0], 3)

    def test_refuses_foreign_page_and_wrong_target(self):
        with self.assertRaises(RuntimeError):
            v.publish(FakeConfluence("<p>someone's notes</p>"), "1", "VLAN Inventory", "DOCS", self.page, self.digest)
        with self.assertRaises(RuntimeError):
            v.publish(FakeConfluence(self.page, title="Other"), "1", "VLAN Inventory", "DOCS", self.page, self.digest)
        with self.assertRaises(RuntimeError):
            v.publish(FakeConfluence(self.page, space="X"), "1", "VLAN Inventory", "DOCS", self.page, self.digest)


if __name__ == "__main__":
    unittest.main()
