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

## Colors

- **Red (`#ff3131`)**: the thing is down or unreachable.
- **Amber (`#ffb000`)**: needs a look, but nothing is out (PEM alarms, reboots, port flaps, capacity, licences).
- **Green (`#39ff14`)**: resolved.

The source decides red or amber and sends it with the alert, so the workflows only render it:
Mist poller `card_color()` in `mist-poller/mist_poller/normalize.py` (label `mist_card_color`; red for
`device_down`, `switch_down`, `gateway_down`, `mist_edge_disconnected`), LibreNMS Blade template
(`card_color`; red when the rule name says down, unreachable or offline), EfficientIP (`card_color` in
the OpenObserve firing template and `eip_trap_handler.py`; red for lease exhaustion and DHCP cluster
failure).

## Adding a new source (for example ClearPass)

Copy a workflow from `keep/workflows/`, keep the header, the `Status` row and the field order above,
and use `enrich_alert` to save whatever the green card needs to show.
