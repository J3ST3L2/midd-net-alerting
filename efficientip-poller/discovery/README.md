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

## API tokens (what this appliance expects)

Password logins to the REST API were refused here (401) even for a full admin, and SSO users such as
your own account cannot use a password for the API at all. SOLIDserver's API tokens are the intended
route. A token has an **Access Key** and a **Secret** (the secret is shown only once).

1. In the console open **Administration -> Authentication & Security -> Users -> `keep-readonly`**
   and use **All API tokens** to create one. Copy the Access Key and the Secret immediately.
2. Save them into root-only files on Gravitron (silent prompts, nothing is echoed):

```bash
sudo sh -c 'umask 077; printf "Access Key: "; read -rs K; echo; printf %s "$K" > /opt/stacks/efficientip-poller/secrets/solid-token-id'
sudo sh -c 'umask 077; printf "Secret: "; read -rs S; echo; printf %s "$S" > /opt/stacks/efficientip-poller/secrets/solid-token-secret'
```

3. Run the probe with token auth:

```bash
sudo SOLID_AUTH=token SOLID_HOST=juno-eip.middlebury.edu      SOLID_TOKEN_ID_FILE=/opt/stacks/efficientip-poller/secrets/solid-token-id      SOLID_TOKEN_SECRET_FILE=/opt/stacks/efficientip-poller/secrets/solid-token-secret      SOLID_DISCOVERY_OUT=/opt/stacks/efficientip-poller/state/discovery      python3 /opt/stacks/keep-config/efficientip-poller/discovery/solid_discover.py
```

A token acts with the rights of its user, so after testing put `keep-readonly` back in a read-only group.
