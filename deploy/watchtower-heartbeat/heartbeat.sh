#!/usr/bin/env bash
# Push a heartbeat from Gravitron to MiddWatchtower (Gatus external endpoint).
# Runs every minute from watchtower-heartbeat.timer. See README.md in this directory.
#
# Pushes success=true when the local Keep backend is healthy, success=false when it is not.
# If Gravitron or this timer dies, no push arrives and Gatus raises the alert on its own.
set -uo pipefail

ENV_FILE="${ENV_FILE:-/etc/watchtower-heartbeat.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

: "${WATCHTOWER_URL:?set WATCHTOWER_URL in $ENV_FILE}"          # https://watchtower.middlebury.edu
: "${HEARTBEAT_TOKEN:?set HEARTBEAT_TOKEN in $ENV_FILE}"        # same as GRAVITRON_HEARTBEAT_TOKEN on Parachute
KEEP_HEALTH_URL="${KEEP_HEALTH_URL:-http://127.0.0.1:8088/healthcheck}"
ENDPOINT_KEY="${ENDPOINT_KEY:-10-monitoring_gravitron-heartbeat}"

if curl -fsS -m 5 -o /dev/null "$KEEP_HEALTH_URL"; then
  success=true; error=""
else
  success=false; error="Keep backend health check failed on Gravitron"
fi

# Headers go through a curl config on stdin so the secrets never appear in `ps`.
{
  printf 'header = "Authorization: Bearer %s"\n' "$HEARTBEAT_TOKEN"
  if [ -n "${CF_ACCESS_CLIENT_ID:-}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
    printf 'header = "CF-Access-Client-Id: %s"\n' "$CF_ACCESS_CLIENT_ID"
    printf 'header = "CF-Access-Client-Secret: %s"\n' "$CF_ACCESS_CLIENT_SECRET"
  fi
} | curl -sS -m 15 -K - -G -X POST -o /dev/null -w "pushed success=$success HTTP %{http_code}\n" \
    --fail \
    --data-urlencode "success=$success" \
    --data-urlencode "error=$error" \
    "$WATCHTOWER_URL/api/v1/endpoints/$ENDPOINT_KEY/external" || {
  echo "heartbeat push FAILED (success=$success)" >&2
  exit 1
}
