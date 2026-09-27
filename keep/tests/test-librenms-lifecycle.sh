#!/usr/bin/env bash
set -euo pipefail

KEEP_URL="${KEEP_URL:-http://127.0.0.1:8088}"
KEEP_API_KEY="${KEEP_API_KEY:-}"

if [[ -z "$KEEP_API_KEY" ]]; then
  echo "KEEP_API_KEY is required in the environment."
  exit 1
fi

AUTH=(-H "Authorization: Bearer $KEEP_API_KEY")
JSON=(-H "Content-Type: application/json")

echo "Sending FIRING test..."
curl -fsS "${AUTH[@]}" "${JSON[@]}" \
  -X POST "$KEEP_URL/alerts/event" \
  --data @keep/templates/librenms-firing.json

echo
echo "Sending UPDATE test with the same fingerprint..."
curl -fsS "${AUTH[@]}" "${JSON[@]}" \
  -X POST "$KEEP_URL/alerts/event" \
  --data @keep/templates/librenms-firing.json

echo
echo "Sending RESOLVED test with the same fingerprint..."
curl -fsS "${AUTH[@]}" "${JSON[@]}" \
  -X POST "$KEEP_URL/alerts/event" \
  --data @keep/templates/librenms-resolved.json

echo
echo "Lifecycle test submitted."
