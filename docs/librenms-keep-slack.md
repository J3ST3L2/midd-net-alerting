# LibreNMS -> Keep -> Slack pilot

## Objective

Validate a stateful local alert path before expanding to additional sources.

```text
LibreNMS -> Keep on Gravitron -> Slack test channel
```

GitHub is the configuration source of truth. Gravitron pulls this repository and runs the test/configuration artifacts locally.

## Acceptance criteria

1. A firing event creates one Keep alert.
2. A repeated firing event with the same fingerprint updates that alert rather than creating another Keep incident.
3. The repeated firing event does not create another Slack DOWN card.
4. Recovery resolves the same Keep alert.
5. Slack receives a clean DOWN notification and a clean recovery notification.
6. Mobile notification text is useful without opening Slack.
7. Opening Slack shows the detailed operational card.

## Slack provider

Create a Slack provider in Keep named exactly:

```text
middlebury-slack-test
```

For the pilot, an incoming webhook pointed at a dedicated Slack test channel is the simplest setup. The webhook URL remains inside Keep and is never committed to GitHub.

The workflow references the provider by name:

```yaml
config: "{{ providers.middlebury-slack-test }}"
```

## Card design

Severity remains available internally to Keep, but is intentionally omitted from the visible Slack card.

Firing card:

- Device
- Status: DOWN
- IP
- Fired
- Rule
- Site
- Summary
- Event ID
- LibreNMS link

Recovery card:

- Device
- Status: UP
- IP
- Fired
- Resolved
- Duration
- Rule
- Site
- Summary
- Event ID
- LibreNMS link

## Mobile notification text

Firing:

```text
🔴 carr hall DOWN
192.168.31.14 • Device Down
```

Resolved:

```text
🟢 carr hall recovered
192.168.31.14 • 5m 19s
```

## Stateful behavior

The test firing and recovery payloads use the same fingerprint:

```text
librenms:device:carr-hall:rule:device-down
```

Keep therefore owns one alert lifecycle. The workflow writes the Slack timestamp back onto the Keep alert after the first firing notification. Repeated firing events with the same fingerprint see that field and skip creating another Slack DOWN card.

Recovery deliberately creates a new Slack recovery message instead of only editing the old message. That preserves a useful phone notification for service restoration while Keep itself remains stateful.

## Test from Gravitron

After cloning/pulling the repository:

```bash
cd /opt/stacks/keep-config
git pull --ff-only
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
```

Expected Slack behavior:

```text
DOWN card
(no duplicate DOWN card from update)
RECOVERED card
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
