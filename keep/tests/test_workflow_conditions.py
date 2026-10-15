"""Check the Slack workflow `if:` conditions the way Keep evaluates them (render {{ alert.x }}, then eval).

For every sample alert exactly one card action must run, and red/amber must follow the rule.
No YAML library needed. Run: python -m unittest keep/tests/test_workflow_conditions.py
"""
import pathlib
import re
import unittest

WF = pathlib.Path(__file__).resolve().parent.parent / "workflows"
NAME = re.compile(r"^    - name: (\S+)\s*$", re.M)
IF = re.compile(r'^      if: "(.*)"\s*$', re.M)
VAR = re.compile(r"\{\{\s*alert\.([A-Za-z0-9_.]+)\s*\}\}")


def actions(fname):
    text = (WF / fname).read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^(?=    - name: )", text)
    out = {}
    for b in blocks:
        n, c = NAME.search(b), IF.search(b)
        if n and c:
            out[n.group(1)] = c.group(1)
    return out


def runs(cond, ctx):
    rendered = VAR.sub(lambda m: str(ctx.get(m.group(1), "")), cond)
    return bool(eval(rendered))      # same idea as Keep's own evaluation of the rendered string


def which(fname, ctx):
    return sorted(n for n, c in actions(fname).items() if runs(c, ctx))


class MistTests(unittest.TestCase):
    def check(self, fname, ts_key, event, expect_color, refresh=False, **extra):
        ctx = {"status": "firing", "labels.mist_event_type": event,
               "labels.mist_refresh": "true" if refresh else "false"}
        ctx.update(extra)
        if refresh:
            ctx[ts_key] = "1700000000.1"
        got = which(fname, ctx)
        self.assertEqual(len(got), 1, "%s %s -> %s" % (fname, event, got))
        self.assertTrue(got[0].endswith(expect_color) or "port-flap" in got[0] and expect_color == "amber",
                        "%s %s -> %s" % (fname, event, got))

    def test_infra_down_red_other_amber(self):
        f = "mist-infra-slack.yaml"
        for ev in ("device_down", "switch_down", "gateway_down"):
            self.check(f, "slack_timestamp_mist_infra_v1", ev, "red")
        for ev in ("vc_master_changed", "sw_alarm_chassis_pem", "bad_cable", "port_flap"):
            self.check(f, "slack_timestamp_mist_infra_v1", ev, "amber")
        self.check(f, "slack_timestamp_mist_infra_v1", "device_down", "red", refresh=True)
        self.check(f, "slack_timestamp_mist_infra_v1", "vc_master_changed", "amber", refresh=True)

    def test_wifi_down_red_other_amber(self):
        f = "mist-wifi-slack.yaml"
        self.check(f, "slack_timestamp_mist_wifi_v1", "device_down", "red")
        self.check(f, "slack_timestamp_mist_wifi_v1", "rogue_ap", "amber")

    def test_resolved_runs_only_the_resolved_action(self):
        for f, key in (("mist-infra-slack.yaml", "slack_timestamp_mist_infra_v1"),
                       ("mist-wifi-slack.yaml", "slack_timestamp_mist_wifi_v1")):
            got = which(f, {"status": "resolved", key: "1700000000.1", "labels.mist_event_type": "device_down"})
            self.assertEqual(len(got), 1)
            self.assertIn("resolved", got[0])
            self.assertEqual(which(f, {"status": "resolved", "labels.mist_event_type": "device_down"}), [])


class LibreNMSTests(unittest.TestCase):
    def colors(self, rule):
        got = which("librenms-slack.yaml", {"status": "firing", "rule": rule})
        return sorted(g.rsplit("-", 1)[1] for g in got), got

    def test_down_rules_red_others_amber(self):
        for rule in ("Devices up/down", "Devices up / down", "Device Down! Due to no ICMP response",
                     "Device Down (SNMP unreachable)"):
            colors, got = self.colors(rule)
            self.assertEqual(colors, ["red", "red"], "%s -> %s" % (rule, got))
        for rule in ("Device rebooted", "Processor usage over 85%", "Sensor over limit"):
            colors, got = self.colors(rule)
            self.assertEqual(colors, ["amber", "amber"], "%s -> %s" % (rule, got))   # one per channel

    def test_resolved_only_updates(self):
        got = which("librenms-slack.yaml", {"status": "resolved", "rule": "Device rebooted",
                                            "slack_timestamp_net_v2": "1.1", "slack_timestamp_nms_v2": "1.2"})
        self.assertEqual(len(got), 2)
        self.assertTrue(all("resolved" in g for g in got))


class EfficientIPTests(unittest.TestCase):
    def test_colors(self):
        f = "efficientip-slack.yaml"
        for ev, color in (("DHCP_Lease_Exhaustion", "red"), ("DHCP CLUSTER failures", "red"),
                          ("DNS_Zone_Transfers_Stalled", "red"), ("DNS_Zone_Transfer_Failed", "amber"),
                          ("DNS_Notify_Failed_Unknown_Target", "amber"), ("DNS_Zone_Transfer_Denied", "amber"),
                          ("Member clock drift", "amber"), ("LICENSES: subscription expiration", "amber")):
            got = which(f, {"status": "firing", "event": ev})
            self.assertEqual(len(got), 1, ev)
            self.assertTrue(got[0].endswith(color), "%s -> %s" % (ev, got))


class EfficientIPAuditTests(unittest.TestCase):
    def test_change_card_posts_once_and_never_on_resolved(self):
        f = "efficientip-audit-slack.yaml"
        self.assertEqual(len(which(f, {"status": "firing", "user": "alice", "event": "Add: DNS RRs"})), 1)
        self.assertEqual(which(f, {"status": "resolved"}), [])


class FacilitiesTests(unittest.TestCase):
    F = "facilities-slack.yaml"

    def test_firing_one_card_by_color_and_one_email(self):
        for rule, color in (("Facilities Meter Down", "red"), ("Facilities Power Voltage", "amber")):
            got = which(self.F, {"status": "firing", "rule": rule})
            cards = [g for g in got if g.startswith("slack-firing")]
            self.assertEqual(len(cards), 1, "%s -> %s" % (rule, got))
            self.assertTrue(cards[0].endswith(color), "%s -> %s" % (rule, got))
            self.assertEqual([g for g in got if g.startswith("email")], ["email-firing-facilities"])

    def test_repeat_firing_does_nothing_more(self):
        got = which(self.F, {"status": "firing", "rule": "Facilities Power Voltage",
                             "slack_timestamp_facilities_v1": "1.1", "emailed_facilities_v1": "1"})
        self.assertEqual(got, [])

    def test_resolved_updates_card_and_emails_once(self):
        got = which(self.F, {"status": "resolved", "rule": "Facilities Power Voltage",
                             "slack_timestamp_facilities_v1": "1.1", "emailed_facilities_v1": "1"})
        self.assertEqual(got, ["email-resolved-facilities", "slack-resolved-update-facilities"])

    def test_resolved_without_prior_notification_is_silent(self):
        self.assertEqual(which(self.F, {"status": "resolved", "rule": "Facilities Power Voltage"}), [])

    # ---- actionable gate: firing alerts with severity info (nothing anyone can act on) are skipped ----
    def test_non_actionable_firing_alert_posts_and_emails_nothing(self):
        for sev in ("info", "INFO", "AlertSeverity.INFO"):    # tolerate how Keep may render the severity
            got = which(self.F, {"status": "firing", "rule": "Facilities Power Voltage", "severity": sev})
            self.assertEqual(got, [], "severity %r -> %s" % (sev, got))

    def test_actionable_firing_alert_still_posts_one_card_and_one_email(self):
        for sev in ("critical", "warning", "AlertSeverity.CRITICAL"):
            got = which(self.F, {"status": "firing", "rule": "Facilities Power Voltage", "severity": sev})
            self.assertEqual(sorted(g.split("-")[0] for g in got), ["email", "slack"], "severity %r -> %s" % (sev, got))

    def test_meter_down_stays_red_and_actionable(self):
        got = which(self.F, {"status": "firing", "rule": "Facilities Meter Down", "severity": "critical"})
        self.assertIn("slack-firing-facilities-red", got)
        self.assertIn("email-firing-facilities", got)

    def test_recovery_still_runs_even_though_recovery_severity_is_info(self):
        got = which(self.F, {"status": "resolved", "rule": "Facilities Power Voltage", "severity": "info",
                             "slack_timestamp_facilities_v1": "1.1", "emailed_facilities_v1": "1"})
        self.assertEqual(got, ["email-resolved-facilities", "slack-resolved-update-facilities"])


if __name__ == "__main__":
    unittest.main()
