import unittest

import eip_trap_handler as h

FIRING = """juno-eip.middlebury.edu
UDP: [10.1.15.10]:40000->[140.233.37.50]:162
DISMAN-EVENT-MIB::sysUpTimeInstance 12:00:00.00
SNMPv2-MIB::snmpTrapOID.0 EIP-MIB::alertRaised
EIP-MIB::alertName.0 "Member clock drift"
EIP-MIB::alertState.0 "Raised"
"""

CLEARED = FIRING.replace("alertRaised", "alertReleased").replace('"Raised"', '"Released"')


class TrapHandlerTest(unittest.TestCase):
    def test_parse_extracts_sender_trap_and_varbinds(self):
        host, ip, trap, vb = h.parse(FIRING)
        self.assertEqual((host, ip, trap), ("juno-eip.middlebury.edu", "10.1.15.10", "alertRaised"))
        self.assertEqual(vb, ["alertName.0=Member clock drift", "alertState.0=Raised"])

    def test_firing_event(self):
        ev = h.build_event(*h.parse(FIRING))[0]
        self.assertEqual(ev["status"], "firing")
        self.assertEqual(ev["fingerprint"], "efficientip:trap:10.1.15.10:Member clock drift")
        self.assertIn("Member clock drift", ev["message"])

    def test_released_trap_resolves(self):
        ev = h.build_event(*h.parse(CLEARED))[0]
        self.assertEqual(ev["status"], "resolved")
        raised = h.build_event(*h.parse(FIRING))[0]
        self.assertEqual(ev["fingerprint"], raised["fingerprint"])

    def test_known_oids_map_to_alert_and_state(self):
        raised = FIRING.replace("EIP-MIB::alertRaised", "SNMPv2-SMI::enterprises.99999.2.1.1")
        released = FIRING.replace("EIP-MIB::alertRaised", "SNMPv2-SMI::enterprises.99999.2.1.2")
        r = h.build_event(*h.parse(raised))[0]
        c = h.build_event(*h.parse(released))[0]
        self.assertEqual((r["status"], r["event"]), ("firing", "Member clock drift"))
        self.assertEqual(c["status"], "resolved")
        self.assertEqual(r["fingerprint"], c["fingerprint"])

    def test_two_digit_alert_number(self):
        raised = FIRING.replace("EIP-MIB::alertRaised", "SNMPv2-SMI::enterprises.99999.2.10.1")
        r = h.build_event(*h.parse(raised))[0]
        self.assertEqual((r["status"], r["event"]), ("firing", "DHCP: Ranges above 90% usage"))

    def test_empty_input_does_not_crash(self):
        ev = h.build_event(*h.parse(""))[0]
        self.assertEqual(ev["event"], "unknown-trap")


if __name__ == "__main__":
    unittest.main()
