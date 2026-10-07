# EfficientIP SOLIDserver -> Keep -> Slack

EfficientIP alerts go straight to Keep over HTTP and are posted to `#efficientip-alerts`
(`C0BG31RLY1H`) by `keep/workflows/efficientip-slack.yaml`. Nothing runs on raccoon.

```text
SOLIDserver  --HTTP POST-->  https://keep.middlebury.edu/backend/alerts/event  -->  Keep  -->  #efficientip-alerts
```

## What Keep expects

A JSON array of alerts. One fingerprint per thing that can alert, the same fingerprint on the
firing and the resolved event, so the Slack card turns green in place instead of posting twice.

```json
[{
  "name": "<appliance>: <event>",
  "status": "firing",
  "severity": "critical",
  "source": ["efficientip"],
  "fingerprint": "efficientip:<appliance>:<event>:<object>",
  "hostname": "<appliance name>",
  "ip": "<appliance ip>",
  "event": "dhcp_shared_network_utilization",
  "object": "<scope or shared network name>",
  "message": "human readable detail",
  "lastReceived": "2026-10-07T20:00:00Z"
}]
```

Required: `status` (`firing` or `resolved`), `source` containing `efficientip`, and a stable
`fingerprint`. `hostname`, `ip`, `event`, `object`, `message` fill the Slack card. Samples are in
`keep/templates/efficientip-firing.json` and `efficientip-resolved.json`.

## Events we want

| Event | Notes |
|---|---|
| DHCP lease exhaustion | **Shared networks.** Alert on the shared network's combined free addresses, not on each scope: one full scope inside a shared network is normal while the others still have free addresses. |
| DNS / DHCP service down | per appliance and service |
| HA / replication failure | failover or sync between servers |
| Appliance health | CPU, disk, memory, unreachable. Also covered by LibreNMS over SNMP. |

## Allow SOLIDserver through Nginx

Keep does not authenticate `/alerts/event`, so Nginx only lets listed hosts post to it. Add each
SOLIDserver appliance IP in `/etc/nginx/conf.d/keep.conf`, in both server blocks, next to raccoon:

```nginx
    location /backend/alerts/event {
        allow 140.233.37.50;      # raccoon.middlebury.edu (LibreNMS)
        allow <solidserver-ip>;   # EfficientIP appliance
        deny  all;
```

Then `sudo nginx -t && sudo systemctl reload nginx`.

## Test the Keep side without SOLIDserver

On Gravitron (posts a clearly marked TEST alert into the real channel, then resolves it):

```bash
cd /opt/stacks/keep-config && git pull --ff-only
cd /opt/stacks/keep && sudo docker compose up -d --force-recreate keep-backend
bash /opt/stacks/keep-config/keep/tests/test-efficientip-lifecycle.sh
```

Expected: one red `TEST-SOLIDserver ALERT` card that turns green in place, with no duplicate.

## Open items

- What SOLIDserver can actually send (HTTP webhook body/format, or only syslog/SNMP). If it cannot
  post this JSON itself, the options are a Keep extraction/mapping rule, or LibreNMS.
- Appliance IPs for the Nginx allowlist.
