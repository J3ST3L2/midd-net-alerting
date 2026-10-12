# efficientip-tools

Two small, database-free tools for EfficientIP SOLIDserver. They replace the parts of the old MiddKiPS
portal that did real work (the change-log collector and the VLAN inventory publisher) and nothing else.
Python 3 standard library only.

```text
SOLIDserver /rest/syslog_full_list --> audit_feed.py (every minute) --+--> OpenObserve  stream eip_audit (every event)
                                                                      +--> Keep --> #dns-change-log (changes worth a card)
SOLIDserver /rest/vlmvlanv2_list    --> vlan_inventory.py (every 6 hours)  ----> Confluence page "VLAN Inventory - EIP Dynamic"
SOLIDserver /rest/ip_address_list   -->
```

| File | What it does |
|---|---|
| `eip_api.py` | Tiny REST client: Basic auth, paging, error handling. |
| `audit_feed.py` | Reads new events from SOLIDserver's own event log (who did what: people, API accounts, automation). |
| `vlan_inventory.py` | Builds the VLAN to subnet inventory and updates one Confluence page, only when it changed. |
| `systemd/` | Service and timer units for both (they run as a throwaway `DynamicUser`). |
| `eip-tools.env.example` | Every setting. Copy to `/etc/eip-tools.env` (root, mode 0600). |
| `test_*.py` | Unit tests (no network): `python -m unittest discover -s efficientip-tools`. |

The matching Keep workflow is `keep/workflows/efficientip-audit-slack.yaml` (source `solidserver-audit`).

## The change log (`audit_feed.py`)

Each run reads the events newer than the last id it saw (one number kept in
`/var/lib/eip-tools/audit.state.json`), then:

1. **Ships every event** to the OpenObserve stream `eip_audit` (user, service, object, sanitised
   parameters). That stream is the searchable change log. Secrets in the parameters (passwords, SNMP
   communities, key values, tokens) are replaced by `[REDACTED]` before anything leaves the script.
2. **Drops noise:** ACME challenge records (`_acme-challenge.*`), anything in `in-addr.arpa` /
   `ip6.arpa`, and any account in `EIP_AUDIT_IGNORE_USERS`.
3. **Collapses bursts:** more than `EIP_AUDIT_BURST` (default 10) identical changes by one account in a
   run become one summary card ("1,976 x Add: DHCP statics (v4) between 14:02 and 14:06").
4. **Posts the rest to Keep** as one card each: `<user> - <service>`, the object, the details, the time.
   Changes are one-shot events, so cards stay blue and are never "resolved".

First run: it records the newest event id and posts nothing, so turning it on never floods the channel.
Delivery is at-least-once: if Keep is down the state does not move past the failed card and the next
minute retries (a rare duplicate card is possible, a lost one is not).

Try it without side effects: `python3 audit_feed.py --dry-run` (prints the cards, posts and saves nothing).

What it cannot tell you: the before/after value of a record. The event log says who ran which operation
on which object with which parameters, not what the old value was.

## The VLAN inventory (`vlan_inventory.py`)

Reads the VLANs of the configured VLAN-manager domain(s) and the subnets that point at them, builds one
table per domain (VLAN id, name, description, networks) and updates the Confluence page. VLAN to subnet
links are the subnet's own class parameters `vlmdomain_name` and `vlmvlan_vlan_id`; values inherited from
a site, a parent network or a single address are ignored, and `0.0.0.0/0` placeholders are skipped. The
VLAN description comes from the class parameter `middlebury_vlan_description`.

Safety rails: it refuses to publish if SOLIDserver returns no VLANs or no networks, refuses to overwrite
a page whose title or space differs from the configured one, and refuses to overwrite a page that is not a
managed inventory page. A content hash in the page footer means an unchanged inventory creates no new
page version. `python3 vlan_inventory.py --dry-run` builds everything and changes nothing.

## Install (on the host that can reach SOLIDserver, Keep and OpenObserve)

```bash
sudo git clone https://github.com/<owner>/midd-net-alerting /opt/midd-net-alerting   # read-only checkout
sudo cp /opt/midd-net-alerting/efficientip-tools/eip-tools.env.example /etc/eip-tools.env
sudo chmod 600 /etc/eip-tools.env && sudoedit /etc/eip-tools.env                      # fill in the values
cd /opt/midd-net-alerting/efficientip-tools
sudo env $(grep -v '^#' /etc/eip-tools.env | xargs) python3 audit_feed.py --dry-run     # first look
sudo cp systemd/eip-*.service systemd/eip-*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now eip-audit-feed.timer eip-vlan-inventory.timer
```

Create the SOLIDserver account first: a dedicated read-only API user, not a person's login. Updating is
`git pull` in `/opt/midd-net-alerting`; there is nothing to build.

Check it works:

```bash
systemctl list-timers 'eip-*'                      # both timers scheduled
journalctl -u eip-audit-feed -n 5 --no-pager       # "fetched=… bad=… noise=… cards=…"
journalctl -u eip-vlan-inventory -n 5 --no-pager   # "vlans=… networks=… content_hash=…" then updated/unchanged
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `SOLIDserver HTTP 401` | Wrong `EIP_USER` / `EIP_PASS`, or the account lacks read access to the event log. |
| Audit feed logs `cards=0` for hours | Quiet period (check the newest event in SOLIDserver) or every event matched the noise rules. |
| `Keep post failed` | Keep unreachable or the host is not on Keep's nginx allow list; the state is not advanced, so it catches up. |
| `target page mismatch` / `not a managed inventory page` | Wrong `CONFLUENCE_PAGE_ID` / title / space. The tool never overwrites an unrelated page. |
| `no networks returned` | `ip_address_list` returned nothing for `subnet_name IS NOT NULL`; check the account's rights. |

## Why it is this small

The old MiddKiPS portal stored every API response in PostgreSQL (45 GB, mostly unused) and had its own
accounts, pages and sync jobs. The two useful jobs only ever needed "ask SOLIDserver, decide, tell
someone", so state is one number and the archive is OpenObserve, which already holds the syslog.
