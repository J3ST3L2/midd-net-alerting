# mist-poller on Gravitron

Polls the Mist org alarms API (outbound HTTPS only) and posts normalized alerts to
Keep at `127.0.0.1:8088`. Slack routing stays in Keep. No listener, no inbound rule.

Runtime layout (nothing here is in Git):

```text
/opt/stacks/keep-config                      Git checkout (this repo)
/opt/stacks/mist-poller/secrets/mist-api-token   read-only Mist org token
/opt/stacks/mist-poller/state/poller/            SQLite state (cursor, dedupe, outbox)
```

## How alarms map to Keep (from live discovery)

| Mist alarm | Keep behavior |
|---|---|
| Aggregated alarm (`aps`/`switches`/`hostnames` arrays, `count` > 1) | one Keep alert per device |
| Down/up pairs (`device_down`/`device_reconnected`, `switch_*`, `gateway_*`, `sw_*_clear`, `*_down`/`*_up`, ...; table in `normalize.py`) | pair on `mist:alarms:<canonical>:<mac>`; `device_state` is unchanged from before |
| Marvis alarms with `status` open/resolved | `mist:alarm:<alarm_id>:<device>` (device from `impacted_entities`), resolved when Mist says so |
| Events with no recovery signal (restarts, `vc_*`, `rogue_ap`, ...) | posted, then auto-resolved after `AUTO_RESOLVE_MINUTES` |
| `infra_arp_*`, `infra_dhcp_*`, `infra_dns_*` | suppressed (`SUPPRESS_TYPES`) |

Severity: `critical`->critical, `warn`->warning, `info`->low.
Each alert carries `labels.mist_category` (`wifi` or `infra`); the Keep workflows route on it.

## Known limits

- Mist `start`/`end` filter on the alarm's own `timestamp` (verified live). Updates to an older
  alarm are therefore seen on the 15-minute wide re-query (72 h), not the 60 s poll. Open Marvis
  alarms older than that are looked up individually by timestamp.
- Alarm device arrays appear capped at 10 entries even when `count` is larger.
- A device that has been down for longer than the bootstrap window (72 h) when the poller first
  starts is not known; its later recovery is ignored rather than posted.
- `aps` and `hostnames` are paired by position only when equal length; otherwise the MAC is the name.

## Install

1. The Mist token file already exists from the discovery step. Keep on Gravitron runs in
   `NO_AUTH` mode (same as LibreNMS), so no Keep API key is needed.

2. The container runs as uid 10001, so give it the files and state dir:

```bash
sudo install -d -m 700 -o 10001 -g 10001 /opt/stacks/mist-poller/state/poller
sudo chown 10001:10001 /opt/stacks/mist-poller/secrets/mist-api-token
sudo chmod 400 /opt/stacks/mist-poller/secrets/mist-api-token
```

3. Config and start (dry-run by default):

```bash
cd /opt/stacks/keep-config/deploy/mist-poller
cp .env.example .env && nano .env      # set MIST_ORG_ID
sudo docker compose build
sudo docker compose up -d
sudo docker logs -f mist-poller
```

## Dry run, then go live

In dry-run the log shows `DRY_RUN would post: {...}` for each alert and nothing reaches Keep.
Review for a few hours: no repeated posts, sensible routing categories, small queue sizes.
Then set `DRY_RUN=false` in `.env` and:

```bash
sudo docker compose up -d --force-recreate
```

Dry-run records alarms as handled, so going live from the same state would never post the
alarms that were open during the dry run, and their later recovery would reach Keep with no
matching firing alert. Reset the state so the cold start posts what is still open, once:

```bash
cd /opt/stacks/keep-config/deploy/mist-poller
sudo docker compose down
sudo rm -rf /opt/stacks/mist-poller/state/poller/*
sudo sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' .env
sudo docker compose up -d
```

## Health

```bash
sudo docker inspect --format '{{.State.Health.Status}}' mist-poller
sudo docker exec mist-poller python -m mist_poller.health
```

## Tests (no dependencies)

```bash
cd mist-poller && python3 -m unittest discover -s tests
```
