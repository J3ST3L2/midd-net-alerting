# midd-net-alerting

Configuration and documentation for Middlebury network alerting.

## Source of truth

GitHub is the configuration source of truth. Gravitron pulls this repository for Keep workflows, LibreNMS payload templates, tests, deployment wiring, and documentation. Runtime credentials and state stay outside Git.

## Production architecture

```text
LibreNMS -> Keep on Gravitron -> NMS-Alert-Bot -> #nms-alerts + #net-alerts
```

Current goals:

- Normalize LibreNMS events before they enter Keep.
- Use a stable per-device/per-rule fingerprint for firing and recovery.
- Keep one alert lifecycle in Keep.
- Create one Slack card per destination channel.
- Update each original Slack card in place on recovery.
- Keep presentation logic in Keep rather than LibreNMS.
- Store no credentials, tokens, webhook URLs, or runtime databases in Git.
- Retire the legacy alert broker only after a real LibreNMS firing/recovery test passes.

## Repository layout

```text
librenms/
  templates/
keep/
  workflows/
  templates/
  tests/
deploy/
docs/
```

Production details are documented in `docs/librenms-keep-slack.md`.
