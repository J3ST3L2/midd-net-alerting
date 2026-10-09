# Slack alert card layout (all sources)

Every card Keep posts (LibreNMS, Mist wifi, Mist infrastructure, EfficientIP, and ClearPass when it
is built) uses the same layout, so people learn it once.

```text
<device> - <event>                       header: the same for the whole life of the alert
Status: Ongoing | Resolved    State/Severity: ...      one row, two short fields
Reason: ...                              full width (stacked lines where the reason is structured)
<detail pairs>                           Site | MAC, IP | Model, Firmware | Last seen, ...
Started / Recovered: ...                 time at the end
```

- **Header:** `<device> - <event>`. Never "DOWN" or "RECOVERED": many events are not outages
  (PEM alarms, VC master changes, port flaps). The colour (red, amber, green) carries the state.
- **Status:** `Ongoing` while firing, `Resolved` after. The same Slack message is edited in place.
- **Second field:** Mist shows its own `State` (open, auto_resolved); LibreNMS and EfficientIP show `Severity`.
- **Reason:** Mist reasons shaped like `CODE: TYPE: detail` are stacked as `Code:`, `Type:`, `Detail:`
  lines (the poller adds the `mist_reason_card` label; see `card_reason` in
  `mist-poller/mist_poller/normalize.py`). Plain-English reasons are shown as they are. Do not invent a
  split for free text.
- **Resolved cards keep the firing details** (host, object, relay, reason) saved with `enrich_alert`
  when the red card was posted, so the green card is not emptier than the red one.

## Adding a new source (for example ClearPass)

Copy a workflow from `keep/workflows/`, keep the header, the `Status` row and the field order above,
and use `enrich_alert` to save whatever the green card needs to show.
