# Watchtower heartbeat (Gravitron to Parachute)

Gravitron pushes a heartbeat to MiddWatchtower (Gatus on Parachute, Azure) every minute. Gatus alerts when the pushes stop, so a dead Keep or a dead Gravitron cannot stay silent. This is the dead-man's switch for the Keep alerting platform.

```text
watchtower-heartbeat.timer (every 1 min)
  -> heartbeat.sh
       1. GET http://127.0.0.1:8088/healthcheck           (Keep backend, on this host)
       2. POST $WATCHTOWER_URL/api/v1/endpoints/10-monitoring_gravitron-heartbeat/external
              success=true|false, Bearer token, optional Cloudflare Access service token
  -> Gatus "Gravitron Heartbeat"  -> Alerta -> #watchtower-alerts
```

The receiving side, Cloudflare setup, timings, verification and the failure test are documented in the Parachute repo: `J3ST3L2/midd-azure-monitoring`, `docs/gravitron-heartbeat.md`. Read that first.

## Install on Gravitron

Prerequisites: the Parachute side is deployed with `GRAVITRON_HEARTBEAT_TOKEN` set, and a Cloudflare Access service token exists.

```bash
cd /opt/stacks/keep-config && git pull --ff-only
cd deploy/watchtower-heartbeat

sudo install -m 0755 heartbeat.sh /usr/local/bin/watchtower-heartbeat
sudo install -m 0644 watchtower-heartbeat.service watchtower-heartbeat.timer /etc/systemd/system/

sudo install -m 0600 -o root -g root watchtower-heartbeat.env.example /etc/watchtower-heartbeat.env
sudo nano /etc/watchtower-heartbeat.env      # set HEARTBEAT_TOKEN and the CF_ACCESS_* values

sudo systemctl daemon-reload
sudo systemctl start watchtower-heartbeat.service       # one manual push first
sudo journalctl -u watchtower-heartbeat.service -n 20 --no-pager
```

Expect `pushed success=true HTTP 200`. If it prints `HTTP 403`, Cloudflare is challenging the request: fix the Access policy or WAF skip rule before enabling the timer. If it prints `HTTP 401` or `404`, the token or endpoint key is wrong. Only then:

```bash
sudo systemctl enable --now watchtower-heartbeat.timer
systemctl list-timers watchtower-heartbeat.timer
```

## Behaviour

- Keep healthy: pushes `success=true`.
- Keep health check fails: pushes `success=false` immediately (faster than waiting for silence).
- Gravitron down, timer stopped, or the path to Parachute broken: no push, Gatus alerts after about 4-5 minutes.
- Push fails (Parachute or Cloudflare unreachable): the unit exits non-zero, shows `failed` in `systemctl`, and logs `heartbeat push FAILED`. Nothing posts to Slack from this side yet. That follow-up is listed in the Parachute doc.

## Files

| File | Purpose |
| --- | --- |
| `heartbeat.sh` | The push, installed as `/usr/local/bin/watchtower-heartbeat` |
| `watchtower-heartbeat.service` | systemd oneshot, sandboxed (`DynamicUser`, read-only filesystem) |
| `watchtower-heartbeat.timer` | Every minute |
| `watchtower-heartbeat.env.example` | Template for `/etc/watchtower-heartbeat.env` (real file stays out of Git) |

## Removing it

```bash
sudo systemctl disable --now watchtower-heartbeat.timer
sudo rm /etc/systemd/system/watchtower-heartbeat.{service,timer} /usr/local/bin/watchtower-heartbeat /etc/watchtower-heartbeat.env
sudo systemctl daemon-reload
```

Gatus will alert within minutes of the last push, so remove the `Gravitron Heartbeat` endpoint from Parachute's `gatus/config.yaml` first.
