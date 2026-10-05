# LibreNMS campus map

Grafana Geomap (Network layer) of every LibreNMS location that has GPS coordinates, with
building-to-building uplinks drawn from LLDP/CDP neighbours. Nothing is hard-coded: add a switch or
fix coordinates in LibreNMS and the map follows on the next refresh.

```text
LibreNMS MariaDB  <--(read-only SELECT)--  Grafana (127.0.0.1:3001)
```

- **Nodes:** one per location with lat/lng and active `network` devices. Sized by switch count, red if
  any switch is down; the tooltip lists the down ones.
- **Edges:** from the `links` table. Each physical link is seen from both ends, so they are collapsed,
  then rolled up per building pair. Thickness is total capacity (a 2x10G LAG is thicker than 1G); red if
  any member port is down.
- Links between devices in the same building do not draw (nodes are per building).
- Topology refreshes on LibreNMS discovery (about every 6 h); link state follows port polling.
- Basemap is Esri World Imagery. Clicking a building name opens it in LibreNMS.

## Install

1. Create a read-only user on the LibreNMS MariaDB, and allow the Grafana host through its bind
   address/firewall:

   ```sql
   CREATE USER 'grafana_ro'@'<grafana-host>' IDENTIFIED BY '<password>';
   GRANT SELECT ON librenms.* TO 'grafana_ro'@'<grafana-host>';
   ```

2. Configure and start:

   ```bash
   cd deploy/librenms-grafana
   cp .env.example .env && nano .env
   sudo install -d -m 700 secrets
   sudo sh -c 'umask 077; printf "DB password: "; read -rs T; echo; printf %s "$T" > secrets/librenms-db-password'
   sudo sh -c 'umask 077; printf "Grafana admin password: "; read -rs T; echo; printf %s "$T" > secrets/grafana-admin-password'
   sudo chmod 444 secrets/*
   sudo docker compose config >/dev/null
   sudo docker compose up -d
   ```

3. Confirm the LibreNMS address: in `grafana/dashboards/campus-netmap.json`, change the hidden
   `librenms_url` variable (`https://raccoon.middlebury.edu`), commit, and Grafana
   reloads within 60 s.

## Check on first render

- Click a building name and confirm `/devices/location=<id>` lands on the right LibreNMS page; the
  link format is unverified.
- The queries were tested only against a mock of the LibreNMS schema. Compare node and edge counts with
  LibreNMS before relying on the map.
- A commented `ifTrunk` filter in the edges query keeps only 802.1Q trunks; vendors populate it
  inconsistently (especially on LAG members), so confirm edges survive before enabling it.
- If a location has missing or default coordinates, it is absent or misplaced here; fix it in LibreNMS.

## Reverse proxy

`nginx/librenms-grafana.conf` serves Grafana at `https://keep.middlebury.edu/netmap/`. Include it in the
existing `server` block, then `sudo nginx -t && sudo systemctl reload nginx`. In `.env` set
`GRAFANA_ROOT_URL=https://keep.middlebury.edu/netmap/` and `GRAFANA_COOKIE_SECURE=true`, then
`sudo docker compose up -d`. The map is at `https://keep.middlebury.edu/netmap/d/midd-campus-netmap`.

If you keep being asked to log in, the cause is almost always a Secure cookie over plain HTTP or a
`GRAFANA_ROOT_URL` that does not match the address in the browser. Use the HTTPS URL above.
