# Facilities alerts: LibreNMS -> Keep -> Slack + email

Facilities power and building-controls alerts (eGauge meter voltage, meter down) use their own lane so the
network alert flow is not touched.

```text
LibreNMS rule (Facilities eGauge group)
  -> template "Keep Facilities JSON"   (librenms/templates/keep-facilities-json.blade, source = facilities)
  -> Keep Production transport         (unchanged)
  -> Keep workflow middlebury-facilities-alerts   (keep/workflows/facilities-slack.yaml)
       -> Slack card in #network-sensor-monitors, updated in place on recovery
       -> email, one on firing and one on recovery, through the Microsoft 365 port-25 relay
```

The existing `middlebury-librenms-slack` workflow filters on `source: librenms`. Facilities alerts carry
`source: facilities`, so each workflow only sees its own alerts.

## Where things live

| Piece | Location |
|---|---|
| Keep workflow, template copy, condition tests | this repo |
| Check script that reads the meters, alert-rule and setup runbook, per-meter table | the `midd-fac-backnet` repo (see its `docs/facilities-alerting.md`) |
| Keep SMTP provider (no secrets, but runtime-only like the Slack one) | `/opt/stacks/keep/provider-config/middlebury-smtp-relay.yaml` on Gravitron; template copy in `midd-fac-backnet/keep/providers/` |

## Keep SMTP provider

Provider name `middlebury-smtp-relay`: server `relay.middlebury.edu`, port 25, no authentication, encryption
`"None"` (Keep accepts `SSL`, `TLS` or `None`). The relay's EHLO offers no STARTTLS. Verified from Gravitron that
the relay accepts its address and the sender `LibreNMS@middlebury.edu`. Same owner and mode as the Slack provider file.

## Deployment on Gravitron

1. The provider file above exists.
2. Merge this change, then:

   ```bash
   cd /opt/stacks/keep-config
   git pull --ff-only

   cd /opt/stacks/keep
   sudo docker compose up -d --force-recreate keep-backend
   ```

3. Keep logs should show both providers and `facilities-slack.yaml` loading.
4. **After any Keep restart, confirm a real card posts in the existing network channels**, then post a labeled
   test event into this lane and confirm the Slack card and the email (see the runbook in `midd-fac-backnet`).

## Test phase and go-live

`to_email` in both email actions is the tester's own address while the lane is being proved. At go-live change both
actions to the Facilities energy mailbox, in one small follow-up change. Recipient and channel are the only
Facilities-specific settings in the workflow.

## Tests

`python -m unittest keep/tests/test_workflow_conditions.py` includes `FacilitiesTests`: one card by colour plus
one email on firing, nothing on a repeat, a card update plus one email on recovery, and silence on a recovery that
was never notified.

## Hosting note

This repo and `midd-fac-backnet` are currently under a personal GitHub account. Moving all of it into the college's
GitHub organization is planned as a separate project. Nothing here depends on the personal account's name, but update
any clone URLs on Gravitron and in the runbooks when that happens.
