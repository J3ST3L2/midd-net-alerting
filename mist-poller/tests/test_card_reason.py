import unittest

from mist_poller.normalize import card_reason, with_card_labels


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


if __name__ == "__main__":
    unittest.main()
