# LibreNMS -> Keep -> Slack pilot

## Objective

Validate a stateful local alert path before expanding to additional sources.

```text
LibreNMS -> Keep on Gravitron -> Slack #net-alerts
```

GitHub is the configuration source of truth. Gravitron pulls this repository and runs the workflow and test artifacts locally. Do not hand-edit the deployed workflow on Gravitron and leave GitHub behind.

## Acceptance criteria

1. A firing event creates one Keep alert and one Slack message.
2. A repeated firing event with the same fingerprint updates the Keep alert and does not create another Slack message.
3. Recovery resolves the same Keep alert.
4. Recovery updates the original Slack message in place from DOWN to RECOVERED.
5. The visible Slack card omits severity, duplicate status fields, location, and other low-value metadata.
6. Mobile notification text is useful without opening Slack.
7. Opening Slack shows a compact operational card with the issue, IP address, and timing.
8. GitHub remains the source of truth for the workflow, templates, tests, and documentation.

## Slack provider

Create a Slack provider in Keep named exactly:

```text
middlebury-slack-test
```

The provider must use a Slack bot/access token, not only an incoming webhook. Keep uses the Slack Web API `chat.update` operation when `slack_timestamp` is supplied, which is required for updating the original alert card on recovery.

The Slack destination is:

```text
#net-alerts
C0C4MSELS4D
```

The token remains inside Keep and is never committed to GitHub.

The workflow references the provider by name:

```yaml
config: "{{ providers.middlebury-slack-test }}"
```

## Card design

Severity remains available internally to Keep but is intentionally omitted from the visible Slack card.

Firing card:

```text
🔴 device DOWN

Issue
Device Down (SNMP unreachable)

IP                         Started
172.17.15.11               2026-09-29 11:52:47
```

The Issue field spans the full width. IP and Started use Slack short fields so they render as a compact two-column row.

Recovery updates the same Slack message:

```text
🟢 device RECOVERED

Issue
Device Down (SNMP unreachable)

IP                         Duration
172.17.15.11               5m 19s

Started                    Recovered
2026-09-29 11:52:47        2026-09-29 11:58:06
```

There is no separate recovery message.

## Mobile notification text

Firing:

```text
🔴 device DOWN • Device Down (SNMP unreachable)
```

Resolved:

```text
🟢 device RECOVERED • Device Down (SNMP unreachable)
```

## Stateful behavior

The test firing and recovery payloads use the same fingerprint:

```text
librenms:device:carr-hall:rule:device-down
```

Keep therefore owns one alert lifecycle. The workflow writes the Slack timestamp back onto the Keep alert after the first firing notification. Repeated firing events with the same fingerprint see that field and skip creating another Slack message.

When the alert resolves, the workflow passes the stored `slack_timestamp` back to the Slack provider. With a bot/access token provider, Keep updates the original Slack message in place.

## Deploy or test from Gravitron

Always start by pulling the source-of-truth repository:

```bash
cd /opt/stacks/keep-config
git pull --ff-only
git status -sb
git log -1 --oneline
```

Then make the lifecycle test executable:

```bash
chmod +x keep/tests/test-librenms-lifecycle.sh
```

Create a dedicated Keep webhook-role API key in Keep. Do not put it in a file in this repository.

Load it only into the shell:

```bash
read -s -p "Keep API key: " KEEP_API_KEY
echo
export KEEP_API_KEY
```

Then run:

```bash
./keep/tests/test-librenms-lifecycle.sh
unset KEEP_API_KEY
```

Expected Slack behavior:

```text
one red DOWN card
(no duplicate card from repeated firing)
same card updates to green RECOVERED
```

Expected Keep behavior:

```text
one fingerprint
firing -> update -> resolved
```

## Secrets

Do not commit:

- Keep API keys
- Slack bot tokens
- Slack webhook URLs
- .env files
- Keep SQLite state
- TLS private keys
