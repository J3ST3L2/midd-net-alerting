import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poller import AP1, alarm, bootstrapped, statuses  # noqa: E402
from mist_poller.refresh import refresh  # noqa: E402
from mist_poller.normalize import resolved_payload  # noqa: E402


class RefreshTests(unittest.TestCase):
    def _open_alert(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        p.run_cycle()
        self.assertEqual(statuses(keep), [("device_down", "firing")])
        self.assertEqual(keep.posted[0]["labels"]["mist_ip"], "10.1.2.3")
        return p, mist, keep, clock

    def test_refresh_resends_open_alert_with_marker_and_new_fields(self):
        p, mist, keep, _ = self._open_alert()
        mist.devices = {AP1: {"ip": "10.9.9.9", "name": "AP-ONE", "model": "AP45",
                              "version": "9.9", "last_seen": 1790000000, "status": "disconnected"}}
        lines = []
        self.assertEqual(refresh(p.cfg, mist, keep, p.state, out=lines.append), 1)
        sent = keep.posted[-1]
        self.assertEqual(sent["labels"]["mist_refresh"], "true")
        self.assertEqual(sent["labels"]["mist_ip"], "10.9.9.9")
        self.assertEqual(sent["fingerprint"], keep.posted[0]["fingerprint"])
        self.assertEqual(sent["status"], "firing")
        stored = json.loads(p.state.alert(sent["fingerprint"])["payload"])
        self.assertNotIn("mist_refresh", stored["labels"])
        self.assertEqual(stored["labels"]["mist_ip"], "10.9.9.9")
        self.assertNotIn("mist_refresh", resolved_payload(stored, 1)["labels"])

    def test_dry_run_posts_and_changes_nothing(self):
        p, mist, keep, _ = self._open_alert()
        before = p.state.alert(keep.posted[0]["fingerprint"])["payload"]
        n = len(keep.posted)
        refresh(p.cfg, mist, keep, p.state, dry_run=True, out=lambda s: None)
        self.assertEqual(len(keep.posted), n)
        self.assertEqual(p.state.alert(keep.posted[0]["fingerprint"])["payload"], before)

    def test_missing_alarm_is_skipped(self):
        p, mist, keep, _ = self._open_alert()
        mist.alarms = []
        n = len(keep.posted)
        self.assertEqual(refresh(p.cfg, mist, keep, p.state, out=lambda s: None), 0)
        self.assertEqual(len(keep.posted), n)

    def test_resolved_alerts_are_not_refreshed(self):
        p, mist, keep, clock = self._open_alert()
        mist.alarms.append(alarm("r1", "device_reconnected", ts=clock.t + 10, aps=[AP1]))
        clock.t += 60
        p.run_cycle()
        n = len(keep.posted)
        self.assertEqual(refresh(p.cfg, mist, keep, p.state, out=lambda s: None), 0)
        self.assertEqual(len(keep.posted), n)


if __name__ == "__main__":
    unittest.main()
