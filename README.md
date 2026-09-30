# midd-net-alerting

Configuration and documentation for Middlebury network alerting.

## Source of truth

GitHub is the configuration source of truth. Gravitron pulls this repository for Keep workflows, payload templates, tests, and documentation. Runtime credentials and state stay outside Git.

## Current pilot

The first integration being built is:

```text
LibreNMS -> Keep on Gravitron -> Slack #net-alerts
```

Goals for the pilot:

- Preserve alert lifecycle state in Keep.
- Use a stable fingerprint so firing, update, and recovery events belong to the same incident.
- Create one concise Slack card per incident.
- Update that same Slack card in place when the incident recovers.
- Keep severity and other low-value metadata out of the visible card.
- Keep production alerting in place until the pilot is verified.
- Store no credentials, tokens, webhook URLs, or runtime databases in Git.

## Repository layout

```text
keep/
  workflows/
  templates/
  tests/
docs/
```

The live Keep runtime on Gravitron remains separate from this repository.
