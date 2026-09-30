# LibreNMS -> Keep -> Slack

## Production path

```text
LibreNMS alert rule
  -> Keep Production API transport
  -> Keep normalized alert
  -> Middlebury LibreNMS Slack workflow
  -> NMS-Alert-Bot
  -> #nms-alerts + #net-alerts
```

GitHub is the configuration source of truth. Runtime credentials and Keep state remain outside Git.

## LibreNMS transport

Transport name:

```text
Keep Production
```

Configuration:

```text
Type: API
Default Alert: OFF
Method: POST
Send as form: OFF
URL: https://keep.middlebury.edu/backend/alerts/event

Headers:
Content-Type=application/json
X-Service-Name=librenms-production

Body:
{{ $msg }}

Auth username: blank
Auth password: blank
```

The LibreNMS transport test button is not the acceptance test for this integration. A real alert template renders JSON; the generic API transport test can send a non-JSON test body, which Keep correctly rejects.

## LibreNMS template

Create an alert template named:

```text
Keep Production JSON
```

Use the exact contents of:

```text
librenms/templates/keep-production-json.blade
```

Attach that template only to the rule selected for the first production test. Expand to other rules after firing and recovery are verified.

### Stable lifecycle identity

The Keep fingerprint is deliberately based on the LibreNMS device and rule:

```text
librenms:device:<device_id>:rule:<rule_id>
```

Do not use `$alert->uid` as the lifecycle fingerprint. LibreNMS uses alert-log event identifiers during alert processing, while device ID and rule ID remain the stable identity of one alert state for a device.

The template also reads the firing alert-log row through `$alert->id`. On recovery LibreNMS maps `$alert->id` back to the original firing alert-log entry, allowing the payload to preserve the original Started timestamp while using the recovery event timestamp for Recovered.

## Keep provider

Slack provider name:

```text
middlebury-nms-alert-bot
```

The provider uses the existing NMS-Alert-Bot access token. The credential is provisioned from the runtime-only directory:

```text
/opt/stacks/keep/provider-config
```

The token is not stored in GitHub.

## Slack destinations

```text
#net-alerts  C0C4MSELS4D
#nms-alerts  C0AHUT40W0Z
```

NMS-Alert-Bot must be a member of both private channels.

## Workflow design

The production workflow is:

```text
keep/workflows/librenms-slack.yaml
```

The four explicit Slack actions are intentional. Slack message timestamps are channel-specific, so Keep stores two independent enrichment fields:

```text
slack_timestamp_net
slack_timestamp_nms
```

Each firing action creates one message in its channel and stores that channel's timestamp. Each recovery action updates the matching message in place.

Visible Slack cards intentionally omit internal severity and other low-value metadata. The attachment bars use explicit neon colors:

```text
Firing:   #ff3131
Resolved: #39ff14
```

## Acceptance test

For one real LibreNMS rule:

1. Attach `Keep Production JSON`.
2. Deliver through `Keep Production`.
3. Trigger a real firing condition.
4. Confirm one NMS-Alert-Bot card appears in each Slack channel.
5. Allow or force the rule to recover.
6. Confirm both original cards update in place to RECOVERED.
7. Confirm Started remains the original firing time and Recovered is the recovery time.
8. Confirm no duplicate firing or recovery cards are created.

Do not disable the legacy Slack transport or alert broker until this complete lifecycle passes from LibreNMS itself.

## Deployment

On Gravitron:

```bash
cd /opt/stacks/keep-config
git pull --ff-only

cd /opt/stacks/keep
sudo docker compose up -d --force-recreate keep-backend
```

Keep should log successful provisioning of both the NMS bot provider and `librenms-slack.yaml`.
