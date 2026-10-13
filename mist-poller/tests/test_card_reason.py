import unittest

from mist_poller.normalize import AMBER, RED, DEFAULT_REASONS, card_color, card_reason, to_eastern, with_card_labels


class CardReasonTest(unittest.TestCase):
    def test_structured_switch_reason_is_stacked(self):
        r = ("L2CPD_RECEIVE_BPDU_BLOCK_ENABLED: BPDU_PROTECT: Interface mge-0/0/18 is DOWN: "
             "BPDU error detected")
        self.assertEqual(card_reason(r), "Code: L2CPD_RECEIVE_BPDU_BLOCK_ENABLED\n"
                                         "Type: BPDU_PROTECT\n"
                                         "Detail: Interface mge-0/0/18 is DOWN: BPDU error detected")

    def test_code_and_detail_only(self):
        self.assertEqual(card_reason("LINK_DOWN: Interface ge-0/0/1 is DOWN"),
                         "Code: LINK_DOWN\nDetail: Interface ge-0/0/1 is DOWN")

    def test_plain_english_is_unchanged(self):
        for s in ("Cable fault detected on the port", "n/a", "Master switched; x",
                  "port flap on port ge-0/0/4"):
            self.assertEqual(card_reason(s), s)

    def test_empty_becomes_na_label(self):
        p = with_card_labels({"labels": {"mist_reason": ""}})
        self.assertEqual(p["labels"]["mist_reason_card"], "n/a")

    def test_original_reason_label_and_payload_untouched(self):
        src = {"name": "x", "labels": {"mist_reason": "LINK_DOWN: a"}}
        out = with_card_labels(src)
        self.assertNotIn("mist_reason_card", src["labels"])
        self.assertEqual(out["labels"]["mist_reason"], "LINK_DOWN: a")
        self.assertEqual(out["name"], "x")

    def test_capacity_and_coverage_alarms_have_plain_english_reasons(self):
        self.assertIn("capacity", DEFAULT_REASONS["insufficient_capacity"])
        self.assertIn("coverage", DEFAULT_REASONS["insufficient_coverage"])

    def test_eastern_time_follows_us_daylight_rules(self):
        self.assertEqual(to_eastern("2026-10-13T02:00:00Z"), "2026-10-12 22:00 EDT")
        self.assertEqual(to_eastern("2026-10-13T02:00:00.000Z"), "2026-10-12 22:00 EDT")
        self.assertEqual(to_eastern("2026-01-15T17:00:00Z"), "2026-01-15 12:00 EST")
        # 2026: DST starts Sunday March 8 at 07:00 UTC and ends Sunday Nov 1 at 06:00 UTC
        self.assertEqual(to_eastern("2026-03-08T06:59:59Z"), "2026-03-08 01:59 EST")
        self.assertEqual(to_eastern("2026-03-08T07:00:00Z"), "2026-03-08 03:00 EDT")
        self.assertEqual(to_eastern("2026-11-01T05:59:59Z"), "2026-11-01 01:59 EDT")
        self.assertEqual(to_eastern("2026-11-01T06:00:00Z"), "2026-11-01 01:00 EST")

    def test_unparseable_times_pass_through(self):
        self.assertEqual(to_eastern("n/a"), "n/a")
        self.assertEqual(to_eastern(""), "")

    def test_cards_get_local_times_and_original_payload_is_untouched(self):
        src = {"lastReceived": "2026-10-13T02:00:00Z",
               "labels": {"mist_last_seen": "2026-10-13T04:55:41Z", "mist_reason": ""}}
        out = with_card_labels(src)
        self.assertEqual(out["labels"]["mist_event_time_local"], "2026-10-12 22:00 EDT")
        self.assertEqual(out["labels"]["mist_last_seen"], "2026-10-13 00:55 EDT")
        self.assertEqual(out["lastReceived"], "2026-10-13T02:00:00Z")
        self.assertEqual(src["labels"]["mist_last_seen"], "2026-10-13T04:55:41Z")

    def test_missing_last_seen_stays_na_and_event_time_always_present(self):
        out = with_card_labels({"lastReceived": "2026-10-13T02:00:00Z", "labels": {"mist_last_seen": "n/a"}})
        self.assertEqual(out["labels"]["mist_last_seen"], "n/a")
        self.assertIn("mist_event_time_local", with_card_labels({"labels": {}})["labels"])

    def test_red_only_for_down_events(self):
        for typ in ("device_down", "switch_down", "gateway_down", "mist_edge_disconnected"):
            self.assertEqual(card_color(typ), RED)
        for typ in ("sw_alarm_chassis_pem", "vc_master_changed", "port_flap", "bad_cable", ""):
            self.assertEqual(card_color(typ), AMBER)

    def test_label_added_for_card(self):
        p = with_card_labels({"labels": {"mist_event_type": "switch_down", "mist_reason": ""}})
        self.assertEqual(p["labels"]["mist_card_color"], RED)


if __name__ == "__main__":
    unittest.main()
