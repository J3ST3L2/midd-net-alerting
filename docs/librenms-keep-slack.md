# LibreNMS -> Keep -> Slack pilot

## Objective

Validate a stateful local alert path before expanding to additional sources.

```text
LibreNMS -> Keep on Gravitron -> Slack test channel
```

## Acceptance criteria

1. A firing alert creates one Keep incident.
2. Repeated/update events with the same fingerprint do not create duplicate incidents.
3. Recovery resolves the same Keep incident.
4. Slack receives a clean firing notification.
5. Slack receives a clean recovery notification.
6. Mobile notification text is useful without opening Slack.
7. Opening Slack shows operational details.

## Card fields

The detailed Slack card should include:

- Device
- Status
- IP
- Fired
- Resolved
- Duration
- Rule
- Site
- Summary
- Event ID
- Open in LibreNMS link

Severity is intentionally omitted from the visible card.

## Mobile notification examples

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

## Secrets

Do not commit:

- Keep API keys
- Slack bot tokens
- Slack webhook URLs
- .env files
- Keep SQLite state
- TLS private keys
