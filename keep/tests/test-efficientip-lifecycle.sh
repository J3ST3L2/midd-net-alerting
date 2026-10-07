#!/usr/bin/env bash
# Synthetic EfficientIP lifecycle: firing, repeat (must not duplicate the card), resolved.
# WARNING: this posts a clearly marked TEST alert into the real #efficientip-alerts channel.
# Run it on Gravitron: it posts straight to Keep on 127.0.0.1, bypassing the Nginx allowlist.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEP_URL="${KEEP_URL:-http://127.0.0.1:8088}"
KEEP_API_KEY="${KEEP_API_KEY:-}"   # optional: Keep currently runs NOAUTH

HEADERS=(-H "Content-Type: application/json" -H "X-Service-Name: efficientip-pilot")
if [[ -n "$KEEP_API_KEY" ]]; then HEADERS+=(-H "X-API-KEY: $KEEP_API_KEY"); fi

post_event() {
  echo
  echo "=== $1 ==="
  curl -fsS "${HEADERS[@]}" -X POST "$KEEP_URL/alerts/event" --data-binary "@$2"
  echo
}

post_event "FIRING"  "$REPO_ROOT/keep/templates/efficientip-firing.json"
echo "Sending the same fingerprint again to simulate a repeat."
post_event "REPEAT (same fingerprint)" "$REPO_ROOT/keep/templates/efficientip-firing.json"
echo "Waiting 10s so you can see the red card, then resolving."
sleep 10
post_event "RESOLVED" "$REPO_ROOT/keep/templates/efficientip-resolved.json"

echo
echo "Expected in #efficientip-alerts:"
echo "  - exactly one red 'TEST-SOLIDserver ALERT' card (the repeat adds nothing)"
echo "  - the same card turns green 'RECOVERED' in place"
