# midd-net-alerting

Configuration and documentation for Middlebury network alerting.

## Current pilot

The first integration being built is:

```text
LibreNMS -> Keep -> Slack
```

Goals for the pilot:

- Preserve alert lifecycle state in Keep.
- Use a stable fingerprint so firing, update, and recovery events belong to the same incident.
- Send clean Slack cards that work well on desktop and mobile notifications.
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
