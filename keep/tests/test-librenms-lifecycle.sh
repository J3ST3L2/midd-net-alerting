#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEP_URL="${KEEP_URL:-http://127.0.0.1:8088}"
KEEP_API_KEY="${KEEP_API_KEY:-}"

if [[ -z "$KEEP_API_KEY" ]]; then
  echo "KEEP_API_KEY is required in the environment."
  echo "Example: read -s -p 'Keep API key: ' KEEP_API_KEY; export KEEP_API_KEY"
  exit 1
fi

AUTH=(-H "X-API-KEY: $KEEP_API_KEY")
JSON=(-H "Content-Type: application/json")
FIRING="$REPO_ROOT/keep/templates/librenms-firing.json"
RESOLVED="$REPO_ROOT/keep/templates/librenms-resolved.json"

post_event() {
  local label="$1"
  local body="$2"

  echo
  echo "=== $label ==="
  curl -fsS \
    "${AUTH[@]}" \
    "${JSON[@]}" \
    -H "X-Service-Name: librenms-pilot" \
    -X POST "$KEEP_URL/alerts/event" \
    --data-binary "@$body"
  echo
}

post_event "FIRING" "$FIRING"

echo "Sending the same fingerprint again to simulate a LibreNMS repeat/update."
post_event "UPDATE / SAME FINGERPRINT" "$FIRING"

echo "Resolving the same fingerprint."
post_event "RESOLVED" "$RESOLVED"

echo
echo "Lifecycle test submitted."
echo "Expected result:"
echo "  - one Keep alert lifecycle"
echo "  - one Slack message total"
echo "  - initial card is red/DOWN"
echo "  - repeated firing creates no duplicate Slack message"
echo "  - recovery updates that same card to green/RECOVERED"
