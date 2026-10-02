# Mist alarms discovery (read-only)

`mist_discover.py` makes a handful of GET requests to the Mist API and writes a
redacted `report.json`. It never writes to Mist or Keep and never prints the token.
Standard library only (Python 3.8+).

## What it answers

- Pagination shape and how many alarms a window returns
- Field names/types; whether device fields (`aps`, `switches`, `gateways`,
  `hostnames`) are arrays and whether `count` is ever > 1 (aggregated alarms)
- Whether resolved alarms are returned and carry `resolved_time`
- Which timestamp `start`/`end` filters on (`start_end_filters_on`): a field with
  `not_returned_by_narrow_query > 0` is NOT what the filter uses
- Severity / type / group values in use, plus `/const/alarm_defs` summary

## Run on Gravitron

Put the token in a root-only file without it landing in shell history:

```bash
sudo install -d -m 700 /opt/stacks/mist-poller/secrets /opt/stacks/mist-poller/state/discovery
sudo sh -c 'umask 077; read -rs T; printf %s "$T" > /opt/stacks/mist-poller/secrets/mist-api-token'
```

Use a read-only org-level token. Then, from the repo checkout
(`/opt/stacks/keep-config`):

```bash
sudo MIST_API_HOST=api.mist.com \
     MIST_ORG_ID=<org-id> \
     MIST_API_TOKEN_FILE=/opt/stacks/mist-poller/secrets/mist-api-token \
     MIST_DISCOVERY_OUT=/opt/stacks/mist-poller/state/discovery \
     python3 mist-poller/discovery/mist_discover.py
```

Set `MIST_API_HOST` to your cloud region host if not `api.mist.com`
(e.g. `api.eu.mist.com`, `api.gc1.mist.com`).

Paste `report.json` back for review; strings other than enum fields are redacted.
