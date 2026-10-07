# EfficientIP log alerts: OpenObserve -> Keep -> Slack

SOLIDserver sends all its logs to raccoon (Administration -> Monitoring -> Syslog -> Configuration).
rsyslog on raccoon forwards them to OpenObserve (`/etc/rsyslog.d/20-openobserve.conf`). The old
LibreNMS syslog piping is disabled (`30-librenms.conf.disabled`) and Graylog is no longer in the path.

```text
SOLIDserver --syslog--> raccoon rsyslog --> OpenObserve
                                              |  scheduled alert (SQL over the log stream)
                                              v
                            webhook POST  https://keep.middlebury.edu/backend/alerts/event
                                              v
                                   Keep  -->  #efficientip-alerts
```

OpenObserve runs on raccoon, so its webhook arrives from raccoon's IP, which is already on the
Nginx allowlist for `/backend/alerts/event`. Nothing to open.

The Keep side already exists: `keep/workflows/efficientip-slack.yaml` and the payload contract in
`docs/efficientip-keep-slack.md`. This page is the OpenObserve side.

## Card lifecycle: a firing alert plus a paired "cleared" alert

OpenObserve alerts say "this matched"; they do not send an all-clear on their own. So each alert is
created **twice**. Both must produce the same Keep fingerprint, `efficientip:openobserve:<alert name>`:

| OpenObserve alert | Condition | Template | Effect in Keep |
|---|---|---|---|
| `<name>` | matches >= 1 | `keep-efficientip-firing` (shared by all alerts) | red card |
| `<name> (cleared)` | matches < 1 | `keep-efficientip-cleared-<name>` (one per alert) | same card turns green |

Why the cleared template is per alert: the cleared alert has a different name (OpenObserve will not
allow two alerts with one name) and, with zero matching rows, it cannot read any row column such as
`{host}`. So its template hard-codes the base name. Copy `openobserve/efficientip-cleared.template.json`
once per alert and replace both `<ALERT NAME>` placeholders with the exact firing-alert name, for
example `DHCP Lease Exhaustion`. The firing template is shared because it builds the fingerprint from
`{alert_name}`, which is the firing alert's own name.

## 1. Webhook destination (one-time)

OpenObserve -> Alerts -> Destinations -> Add:
- Name: `keep-efficientip-firing`; for each cleared alert create a second destination with the same URL/headers that points at that alert's own cleared template
- URL: `https://keep.middlebury.edu/backend/alerts/event`
- Method: POST
- Headers: `Content-Type: application/json`, `X-Service-Name: openobserve`
- Template: see below

## 2. Templates (one-time)

OpenObserve -> Alerts -> Templates -> Add. Paste the bodies from:
- `openobserve/efficientip-firing.template.json`  -> name it `keep-efficientip-firing`
- `openobserve/efficientip-cleared.template.json` -> one copy per alert, named `keep-efficientip-cleared-<alert name>`, with `<ALERT NAME>` replaced

The `{...}` placeholders are OpenObserve template variables (`{alert_name}`, `{alert_count}`,
`{alert_period}`, `{alert_url}`, `{stream_name}`) plus columns returned by the alert's SQL (`{host}`,
`{message}`). **Verify the variable names against your OpenObserve version's template help** before
relying on them. An unresolved placeholder shows up literally in the card.

## 3. The alerts

`openobserve/efficientip-alert-catalog.json` lists the alerts (name, SQL, period, frequency,
threshold). Replace `<STREAM>` with the stream holding the SOLIDserver logs. Start with
**DHCP Lease Exhaustion**; enable the others once their queries are checked against real log lines.

DHCP and shared networks: `dhcpd` logs `network <name>: no free leases` per network, so a match means a
client really could not get an address on that network. That is better than a percent-used threshold,
which fires per scope. The card shows the most recent matching line.

## 4. Test the whole chain

1. Check the Keep side first: `bash keep/tests/test-efficientip-lifecycle.sh` on Gravitron.
2. In OpenObserve, use the alert's **test/preview** to confirm the SQL returns the expected row.
3. Send a synthetic DHCP line from a host allowed to log to raccoon, for example from raccoon:
   `logger -n 127.0.0.1 -P 514 -d -t 'dhcpd[99999]:' 'DHCPDISCOVER from 02:00:00:00:00:01 via hn1: network test-netv4: no free leases'`
4. Expect: a red `EfficientIP: DHCP Lease Exhaustion` card within a minute or two, and a green one
   after the period passes with no new matches.

## Known gaps

- Alerts that exist only inside SOLIDserver (Raised/Released: licence expiry, HA cluster, certificate
  validity, clock drift, scopes above 90%) are not in the logs reliably. They can only leave the
  appliance by REST, SNMP trap or email. The REST login is currently refused (see
  `efficientip-poller/discovery/`); parked until someone confirms how `slack_api` authenticates.
- The OpenObserve template variable names and the "less than" threshold for the cleared alert are
  unverified against your version.
