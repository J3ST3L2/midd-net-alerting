# SOLIDserver REST discovery (read-only)

`solid_discover.py` makes a few GET requests to the SOLIDserver REST API to find out which
services exist for alerts and DHCP objects, and what fields they carry. It writes nothing to
SOLIDserver and never prints the password. Standard library only.

## 1. Create a read-only SOLIDserver user

In the SOLIDserver console: **Administration -> Authentication & Security -> Users** (and Groups).
Create a user such as `keep-readonly` in a group with read-only rights on the DHCP, DNS and
monitoring objects. Do not give it write rights.

## 2. Put its password in a root-only file on Gravitron

```bash
sudo install -d -m 700 /opt/stacks/efficientip-poller/secrets /opt/stacks/efficientip-poller/state/discovery
sudo sh -c 'umask 077; printf "SOLIDserver password: "; read -rs T; echo; printf %s "$T" > /opt/stacks/efficientip-poller/secrets/solid-password'
```

## 3. Run the probe

```bash
sudo SOLID_HOST=<solidserver-hostname> SOLID_USER=keep-readonly \
     SOLID_PASSWORD_FILE=/opt/stacks/efficientip-poller/secrets/solid-password \
     SOLID_DISCOVERY_OUT=/opt/stacks/efficientip-poller/state/discovery \
     python3 /opt/stacks/keep-config/efficientip-poller/discovery/solid_discover.py
```

If the appliance uses a private CA add `SOLID_CA_FILE=/path/to/ca.pem`. If you get a 401, try
`SOLID_AUTH=basic`. `SOLID_INSECURE=1` skips TLS verification for a quick test only.

## 4. Read the result

Each candidate service shows `http_status` and either `rows` and `fields`, or an `error`. A service
that returns rows with state/name fields is the one the poller will read. Review the output before
sharing it: alert rows are printed, which is the point, but may contain server names.

## If it stops with 401, 403 or 503

The probe makes ONE request first (`member_list`) and stops if the login is rejected, because
repeated failed logins can lock the account or make the appliance answer 503. Before running it again:

1. In the SOLIDserver console open **Administration -> Authentication & Security -> Users** and check
   that `keep-readonly` is enabled, not locked/expired, and in a group that is allowed to use the API.
2. Confirm the password by logging in to the console as `keep-readonly`.
3. Re-save the password file if in doubt (step 2 above), then run the probe once.
