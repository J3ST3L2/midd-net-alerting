# EfficientIP SNMP traps -> Keep

SOLIDserver's own alerts (HA cluster, licences, SSL certificate validity, member clock drift, scopes
above 90%, ...) cannot reach the logs, and its REST API refuses our logins. They can leave the
appliance as **SNMP traps**. This receives them on raccoon and posts each one to Keep, which renders the
card in `#efficientip-alerts` (workflow `keep/workflows/efficientip-slack.yaml`).

```text
SOLIDserver --SNMP trap (UDP 162)--> raccoon snmptrapd --> eip_trap_handler.py --> Keep /alerts/event --> Slack
```

Raccoon's address is already on the Nginx allow list for `/backend/alerts/event`.

## Honest limits

The handler does **not** know SOLIDserver's real trap names yet. Until we see real traps it:
- logs every raw trap to `/var/log/eip-traps.log` (use this to learn the real wording),
- posts a generic card keyed on sender + first varbind value (normally the alert name),
- treats a trap as **resolved** when its name or text contains released / cleared / normal / recovered / closed.

After the first few real traps, tighten the mapping (card names, severity, resolve words) in
`eip_trap_handler.py` and update the tests with the real samples.

## Install on raccoon

```bash
sudo install -m 0755 eip_trap_handler.py /usr/local/bin/eip_trap_handler.py
sudo touch /var/log/eip-traps.log && sudo chmod 640 /var/log/eip-traps.log
COMM=$(openssl rand -hex 12)            # note it; SOLIDserver needs the same value
sed "s/<COMMUNITY>/$COMM/" snmptrapd.conf.example | sudo tee /etc/snmp/snmptrapd.conf >/dev/null
sudo chmod 600 /etc/snmp/snmptrapd.conf
sudo firewall-cmd --permanent --add-port=162/udp && sudo firewall-cmd --reload   # if firewalld is on
sudo systemctl enable --now snmptrapd
ss -ulnp | grep ':162 '
```

The service runs as root by default on EL8, so the handler can write the log. Check it with:
`sudo systemctl status snmptrapd --no-pager | head -5`.

## Point SOLIDserver at raccoon

In SOLIDserver, set the SNMP trap destination to raccoon (`140.233.37.50`, UDP 162, the community
above) and attach it to the alert definitions you want (Administration -> Monitoring -> Alerts ->
Definition -> edit an alert). Start with **Member clock drift** and **LICENSES: subscription
expiration**, which are the ones that fire in practice. Do this on the appliance that owns the alerts
(juno is the manager).

## Test without SOLIDserver

On raccoon (replace nothing; the community comes from the file):

```bash
snmptrap -v2c -c "$COMM" 127.0.0.1:162 '' 1.3.6.1.4.1.2021.251.1 1.3.6.1.2.1.1.5.0 s "TEST ONLY Member clock drift"
tail -n 2 /var/log/eip-traps.log
```

Expect a line in the log and a card in `#efficientip-alerts`. Send the same trap with `Released` in the
text to see it turn green.

## Unit tests

```bash
python -m unittest -v test_eip_trap_handler
```

## OIDs to enter in each SOLIDserver alert definition

In the alert's edit form, tick **SNMP trap** and fill: version `v2c`, destination `140.233.37.50`,
port `162`, the community from raccoon, and these OIDs (we choose them; the handler decodes them):

| # | Alert | Raised OID | Released OID |
|---|---|---|---|
| 1 | Member clock drift | `1.3.6.1.4.1.99999.2.1.1` | `1.3.6.1.4.1.99999.2.1.2` |
| 2 | LICENSES: subscription expiration | `1.3.6.1.4.1.99999.2.2.1` | `1.3.6.1.4.1.99999.2.2.2` |
| 3 | LICENSES: license expiration | `1.3.6.1.4.1.99999.2.3.1` | `1.3.6.1.4.1.99999.2.3.2` |
| 4 | LICENSES: maintenance expiration | `1.3.6.1.4.1.99999.2.4.1` | `1.3.6.1.4.1.99999.2.4.2` |
| 5 | LICENSES: metrics usage | `1.3.6.1.4.1.99999.2.5.1` | `1.3.6.1.4.1.99999.2.5.2` |
| 6 | HA SSL Certificate validity | `1.3.6.1.4.1.99999.2.6.1` | `1.3.6.1.4.1.99999.2.6.2` |
| 7 | DHCP CLUSTER failures | `1.3.6.1.4.1.99999.2.7.1` | `1.3.6.1.4.1.99999.2.7.2` |
| 8 | DHCP: Scopes Above 90% | `1.3.6.1.4.1.99999.2.8.1` | `1.3.6.1.4.1.99999.2.8.2` |

`.1` = raised (red card), `.2` = released (the same card turns green). `99999` is an internal placeholder
enterprise number; it only needs to be consistent between SOLIDserver and `ALERTS` in the handler.
