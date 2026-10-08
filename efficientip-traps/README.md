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
