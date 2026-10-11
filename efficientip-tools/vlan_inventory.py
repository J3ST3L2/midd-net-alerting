#!/usr/bin/env python3
"""Publish the VLAN inventory from SOLIDserver to a Confluence page. No database.

SOLIDserver is the source of truth. Each run reads the VLANs (VLAN manager macro
`/rest/vlmvlanv2_list`) and the subnets with their VLAN links (`/rest/ip_address_list`, one row per
address, deduplicated by `subnet_id`), renders one table per VLAN domain, and updates the Confluence
page only if the content changed. A hidden marker in the page body holds a hash of the content, so a
run that finds nothing new makes no new page version.

VLAN <-> subnet links are the network-level class parameters `vlmdomain_name` and `vlmvlan_vlan_id`.
Only values whose inheritance source is the subnet itself are trusted (values inherited from sites,
parent networks or single addresses are ignored). 0.0.0.0/0 placeholder networks are skipped.

Environment (see eip-tools.env.example): EIP_* (see eip_api.py), EIP_VLAN_DOMAIN_IDS (default 2),
CONFLUENCE_BASE_URL, CONFLUENCE_SPACE_KEY, CONFLUENCE_PAGE_ID, CONFLUENCE_PAGE_TITLE,
CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN.
"""
import argparse
import base64
import hashlib
import html
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import parse_qs

import eip_api

PUBLISHER = "efficientip-tools/vlan_inventory.py"
OLD_PUBLISHER = "MiddKiPS SOLIDserver VLAN inventory sync"  # the page was first published by MiddKiPS
# Confluence may drop HTML comments, so the content hash is carried in the visible footer.
HASH_RE = re.compile(r"content hash <code>([0-9a-f]{16})</code>")


def clean(value):
    return str(value or "").strip()


# ---- VLANs ---------------------------------------------------------------------------------------

def class_parameter(row, name):
    raw = row.get("vlan_class_parameters")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and str(item.get("name", "")) == name:
            return html.unescape(str(item.get("value") or "")).strip() or None
    return None


def normalize_vlan(row):
    sid = clean(row.get("vlan_id"))
    number_text = clean(row.get("vlan_vlan_id"))
    if not sid or not number_text:
        raise ValueError("VLAN row without vlan_id / vlan_vlan_id")
    number = int(number_text)
    if not 1 <= number <= 4094:
        raise ValueError("VLAN %s out of range: %d" % (sid, number))
    domain_name = html.unescape(clean(row.get("domain_name")))
    if not clean(row.get("domain_id")) or not domain_name:
        raise ValueError("VLAN %s has no domain" % sid)
    return {"domain_id": int(clean(row.get("domain_id"))), "domain_name": domain_name,
            "vlan_id": number, "name": html.unescape(clean(row.get("vlan_name"))) or "vlan%d" % number,
            "description": class_parameter(row, "middlebury_vlan_description") or ""}


def fetch_vlans(cfg, domain_ids):
    vlans = []
    for domain_id in domain_ids:
        rows = eip_api.get_rows("/rest/vlmvlanv2_list", where="domain_id=%s" % domain_id,
                                orderby="vlan_vlan_id ASC", max_rows=20000, cfg=cfg)
        for row in rows:
            if clean(row.get("vlan_type", "vlan")) == "vlan" and clean(row.get("domain_id")) == str(domain_id):
                vlans.append(normalize_vlan(row))
    return vlans


# ---- subnets -------------------------------------------------------------------------------------

def network_parameters(row, subnet_id):
    """Class parameters that originate on this subnet object."""
    values = parse_qs(clean(row.get("ip_class_parameters")), keep_blank_values=True)
    sources = parse_qs(clean(row.get("ip_class_parameters_inheritance_source")), keep_blank_values=True)
    expected = "real_network,%s" % subnet_id
    out = {}
    for key, vals in values.items():
        value = clean(vals[0]) if vals else ""
        source = clean(sources.get(key, [""])[0])
        if value and source == expected:
            out[key] = value
    return out


def normalize_network(row):
    sid = clean(row.get("subnet_id"))
    address, prefix = clean(row.get("subnet_start_hostaddr")), clean(row.get("subnet_prefix"))
    if not (sid and address and prefix):
        return None
    try:
        net = ipaddress.ip_network("%s/%s" % (address, prefix), strict=False)
    except ValueError:
        return None
    if str(net) == "0.0.0.0/0":
        return None
    params = network_parameters(row, sid)
    vlan = None
    try:
        n = int(params.get("vlmvlan_vlan_id", ""))
        vlan = n if 1 <= n <= 4094 else None
    except ValueError:
        pass
    return {"id": sid, "network": str(net), "name": clean(row.get("subnet_name")),
            "domain_name": params.get("vlmdomain_name") or None, "vlan_id": vlan}


def fetch_networks(cfg, max_rows=100000):
    """Address rows -> one network per subnet_id, preferring the row that carries the VLAN link."""
    best = {}
    for row in eip_api.get_rows("/rest/ip_address_list", where="subnet_name IS NOT NULL",
                                max_rows=max_rows, cfg=cfg):
        net = normalize_network(row)
        if net is None:
            continue
        old = best.get(net["id"])
        linked = bool(net["domain_name"] and net["vlan_id"])
        if old is None or (linked and not (old["domain_name"] and old["vlan_id"])):
            best[net["id"]] = net
    return list(best.values())


# ---- inventory and page --------------------------------------------------------------------------

def build_inventory(vlans, networks):
    """{(domain_id, domain_name): [vlan dict with sorted 'networks']} joined on (domain name, VLAN id)."""
    by_vlan = defaultdict(set)
    for n in networks:
        if n["domain_name"] and n["vlan_id"]:
            label = n["network"] + (" (%s)" % n["name"] if n["name"] else "")
            by_vlan[(n["domain_name"].lower(), n["vlan_id"])].add(label)
    domains = defaultdict(list)
    for v in vlans:
        domains[(v["domain_id"], v["domain_name"])].append(
            dict(v, networks=sorted(by_vlan.get((v["domain_name"].lower(), v["vlan_id"]), ()))))
    for rows in domains.values():
        rows.sort(key=lambda r: (r["vlan_id"], r["name"].lower()))
    return dict(sorted(domains.items(), key=lambda kv: (kv[0][0], kv[0][1].lower())))


def esc(value):
    return html.escape(str(value or ""), quote=True)


def render_body(domains, total):
    parts = ["<p><strong>Managed automatically.</strong> The source of truth is SOLIDserver; "
             "manual edits to these tables are overwritten.</p>",
             "<p>Total VLANs: <strong>%d</strong></p>" % total]
    for (domain_id, domain_name), vlans in domains.items():
        parts.append("<h2>%s VLANs</h2><p>Domain ID: <code>%s</code> | VLAN count: <strong>%d</strong></p>"
                     % (esc(domain_name), domain_id, len(vlans)))
        parts.append('<table data-layout="wide" style="width:100%; table-layout:auto;"><colgroup>'
                     '<col style="width:10%;" /><col style="width:20%;" /><col style="width:45%;" />'
                     '<col style="width:25%;" /></colgroup><thead><tr>'
                     '<th style="white-space:nowrap;">VLAN ID</th><th style="white-space:nowrap;">Name</th>'
                     '<th>Description</th><th style="white-space:nowrap;">Networks</th></tr></thead><tbody>')
        for v in vlans:
            nets = "<br />".join("<code>%s</code>" % esc(n) for n in v["networks"])
            parts.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                         % (v["vlan_id"], esc(v["name"]), esc(v["description"]), nets))
        parts.append("</tbody></table>")
    return "\n".join(parts)


def render_page(domains):
    """Returns (page body, 16-hex content hash). The hash covers the tables, not the footer."""
    total = sum(len(v) for v in domains.values())
    body = render_body(domains, total)
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    footer = "<p><em>Publisher: %s | content hash <code>%s</code></em></p>" % (PUBLISHER, digest)
    return body + "\n" + footer, digest


# ---- Confluence ----------------------------------------------------------------------------------

class Confluence:
    def __init__(self, base_url, email, token):
        self.base = base_url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (email, token)).encode()).decode()

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "Authorization": self.auth}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Confluence HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:500]))

    def get_page(self, page_id):
        return self.call("GET", "/wiki/rest/api/content/%s?expand=body.storage,version,space" % page_id)

    def update_page(self, page_id, title, space_key, version, body):
        return self.call("PUT", "/wiki/rest/api/content/%s" % page_id, {
            "id": page_id, "type": "page", "title": title, "space": {"key": space_key},
            "version": {"number": version + 1, "message": "Automated VLAN inventory update"},
            "body": {"storage": {"value": body, "representation": "storage"}}})


def publish(client, page_id, title, space_key, body, digest):
    """Returns 'unchanged' or 'updated'. Refuses to touch a page that is not ours."""
    page = client.get_page(page_id)
    if page.get("title") != title or page.get("space", {}).get("key") != space_key:
        raise RuntimeError("target page mismatch (title/space): refusing to overwrite")
    current = page.get("body", {}).get("storage", {}).get("value", "")
    m = HASH_RE.search(current)
    if m and m.group(1) == digest:
        return "unchanged"
    if PUBLISHER not in current and OLD_PUBLISHER not in current:
        raise RuntimeError("target page is not a managed inventory page: refusing to overwrite")
    client.update_page(page_id, title, space_key, int(page.get("version", {}).get("number", 0)), body)
    return "updated"


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError("missing environment variable: " + name)
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="build the page and print a summary; change nothing")
    args = ap.parse_args(argv)
    cfg = eip_api.config()
    domain_ids = [d.strip() for d in os.environ.get("EIP_VLAN_DOMAIN_IDS", "2").split(",") if d.strip()]

    vlans = fetch_vlans(cfg, domain_ids)
    if not vlans:
        raise RuntimeError("no VLANs returned; refusing to replace the page with an empty inventory")
    networks = fetch_networks(cfg)
    if not networks:
        raise RuntimeError("no networks returned; refusing to publish VLANs without their networks")
    domains = build_inventory(vlans, networks)
    page, digest = render_page(domains)
    linked = sum(1 for rows in domains.values() for v in rows if v["networks"])
    print("vlans=%d networks=%d vlans_with_networks=%d content_hash=%s" % (len(vlans), len(networks), linked, digest))
    if args.dry_run:
        return 0

    client = Confluence(require_env("CONFLUENCE_BASE_URL"), require_env("CONFLUENCE_EMAIL"),
                        require_env("CONFLUENCE_API_TOKEN"))
    print(publish(client, require_env("CONFLUENCE_PAGE_ID"), require_env("CONFLUENCE_PAGE_TITLE"),
                  require_env("CONFLUENCE_SPACE_KEY"), page, digest))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        sys.exit(1)
