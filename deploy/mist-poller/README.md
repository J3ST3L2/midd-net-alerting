# mist-poller on Gravitron

Polls the Mist org alarms API (outbound HTTPS only) and posts normalized alerts to
Keep at `127.0.0.1:8088`. Slack routing stays in Keep. No listener, no inbound rule.

Runtime layout (nothing here is in Git):

```text
/opt/stacks/keep-config                      Git checkout (this repo)
/opt/stacks/mist-poller/secrets/mist-api-token   read-only Mist org token
/opt/stacks/mist-poller/secrets/keep-api-key     Keep API key (X-API-KEY)
/opt/stacks/mist-poller/state/poller/            SQLite state (cursor, dedupe, outbox)
```

## How alarms map to Keep (from live discovery)

| Mist alarm | Keep behavior |
|---|---|
| Aggregated alarm (`aps`/`switches`/`hostnames` arrays, `count` > 1) | one Keep alert per device |
| `device_down` / `device_reconnected` | pair on `mist:alarms:device_state:<mac>` (unchanged scheme) |
| Marvis alarms with `status` open/resolved | `mist:alarm:<alarm_id>:<device>`, resolved when Mist says so |
| Events with no recovery signal (restarts, `vc_*`, `rogue_ap`, ...) | posted, then auto-resolved after `AUTO_RESOLVE_MINUTES` |
| `infra_arp_failure` / `infra_arp_success` | suppressed (`SUPPRESS_TYPES`) |

Severity: `critical`->critical, `warn`->warning, `info`->low.
Each alert carries `labels.mist_category` (`wifi` or `infra`); the Keep workflows route on it.

## Install

1. Tokens (token file already exists from the discovery step; add the Keep key silently):

```bash
sudo sh -c 'umask 077; printf "Keep API key: "; read -rs T; echo; printf %s "$T" > /opt/stacks/mist-poller/secrets/keep-api-key'
```

2. The container runs as uid 10001, so give it the files and state dir:

```bash
sudo install -d -m 700 -o 10001 -g 10001 /opt/stacks/mist-poller/state/poller
sudo chown 10001:10001 /opt/stacks/mist-poller/secrets/mist-api-token /opt/stacks/mist-poller/secrets/keep-api-key
sudo chmod 400 /opt/stacks/mist-poller/secrets/mist-api-token /opt/stacks/mist-poller/secrets/keep-api-key
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

Dry-run marks alarms as seen, so going live does not replay them; only new events post.
To rehearse the cold start again, stop the container and delete
`/opt/stacks/mist-poller/state/poller/`.

## Health

```bash
sudo docker inspect --format '{{.State.Health.Status}}' mist-poller
sudo docker exec mist-poller python -m mist_poller.health
```

## Tests (no dependencies)

```bash
cd mist-poller && python3 -m unittest discover -s tests
```
