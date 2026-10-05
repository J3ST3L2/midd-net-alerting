# LibreNMS campus map

Grafana Geomap (Network layer) of every LibreNMS location that has GPS coordinates, with
building-to-building uplinks drawn from LLDP/CDP neighbours. Nothing is hard-coded: add a switch or
fix coordinates in LibreNMS and the map follows on the next refresh.

```text
LibreNMS MariaDB  <--(read-only SELECT)--  Grafana (127.0.0.1:3001)  --(PromQL)-->  Mist Prometheus
```

The Mist layers need `deploy/mist-dashboard` running first: it creates the `mist-dashboard-metrics`
Docker network that this stack joins. Redeploy the Mist exporter (`up -d --build exporter`) to get the
new site metrics, and run `docker compose up -d` in `deploy/mist-dashboard` once so Prometheus joins the
network.

- **Nodes:** one per location with lat/lng and active `network` devices. Sized by switch count, red if
  any switch is down; the tooltip lists the down ones.
- **Edges:** from the `links` table. Each physical link is seen from both ends, so they are collapsed,
  then rolled up per building pair. Thickness is total capacity (a 2x10G LAG is thicker than 1G); red if
  any member port is down.
- Links between devices in the same building do not draw (nodes are per building).
- Topology refreshes on LibreNMS discovery (about every 6 h); link state follows port polling.
- **Mist APs:** a blue marker per Mist site, sized by AP count, at the site's own GPS position in Mist;
  a red marker on top when any AP there is offline. Data comes from the Mist stack's Prometheus
  (`mist_site_aps`, `mist_site_aps_connected`), so sites are matched by coordinates, not by name. A Mist
  site with no coordinates (or 0/0) is left off the map.
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

- Mist site coordinates come from Mist's site stats `latlng`, which I could not probe live; if no blue
  markers appear, check `curl .../metrics | grep mist_site_aps` on the exporter.
- Click a building name and confirm `/devices/location=<id>` lands on the right LibreNMS page; the
  link format is unverified.
- The queries were tested only against a mock of the LibreNMS schema. Compare node and edge counts with
  LibreNMS before relying on the map.
- A commented `ifTrunk` filter in the edges query keeps only 802.1Q trunks; vendors populate it
  inconsistently (especially on LAG members), so confirm edges survive before enabling it.
- If a location has missing or default coordinates, it is absent or misplaced here; fix it in LibreNMS.
