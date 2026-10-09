"""
UTILITIES/stack_discovery.py
==============================================================================
Shared Portal deployment topology discovery: enumerate every machine in an
ArcGIS Enterprise Portal deployment (Portal, hosting server, Data Store,
federated servers) and their FQDNs.

CONFIRMED REST PATHS
---------------------------------------------------------------------------
  * Portal machines  : <portal_url>/portaladmin/machines            (GET)
  * Server machines  : <server_admin_url>/admin/machines            (GET)
  * Data Store items : <hosting_admin_url>/admin/data/findItems     (POST)
                        with ancestorPath=/enterpriseDatabases

Public API
----------
    discover_stack(gis, session, logger) -> dict
        {
          "portal":            {"name", "url", "fqdns", "primary", "standby"},
          "hosting_server":    {"url", "fqdns", "site_name"},
          "data_store":        {"host", "fqdns", "items"},
          "federated_servers": [ {"name","admin_url","fqdns","site_name",
                                  "server_id"}, ... ],
        }
        Each federated server entry also carries "server_id" -- the raw
        federation GUID (gis.admin.federation.servers'/'id' field) for that
        server, e.g. "43ce7891ac37448e8560c02601446cc6". This is the SAME
        GUID Portal embeds in its "sharing/servers/<guid>/rest/services/..."
        proxy URL pattern (used for services published through Portal's own
        item-sharing proxy, e.g. Survey123 result layers, rather than a
        direct site path like ".../east/rest/services/..."). Consumers that
        need to recognize that URL pattern and attribute it to the correct
        federated server (e.g. ITEM_INVENTORY.py's Hosted/Federated/
        Registered classification) can match this GUID directly instead of
        relying only on "site_name" (which that proxy URL pattern does not
        expose). "server_id" may be None if the federation API did not
        report an 'id' for that entry.

    discover_data_store(session, hosting_admin_url, token, logger=None)
        -> dict
        {
          "host":       str | None,
          "fqdns":      [str, ...],
          "items":      [ {"name","path","type","primary","standby",
                           "all_machines","ambiguous"}, ... ],
          "ambiguous":  bool,
          "error":      str | None,
        }

    hostname_from_url(url) -> str | None
    looks_like_fqdn(value) -> bool
    short_hostname(fqdn) -> str
    site_name_from_url(url) -> str
==============================================================================
"""

import re
from urllib.parse import urlparse

from .restutils import get_json, DEFAULT_TIMEOUT


_FQDN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def hostname_from_url(url):
    """Extract just the hostname (no scheme/port/path) from a URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return parsed.hostname
    except Exception:
        return None


def looks_like_fqdn(value):
    """True if value looks like a dotted hostname (IPs are accepted too)."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return False
    return bool(_FQDN_RE.match(v)) or v.count(".") >= 3


def short_hostname(fqdn):
    """Strip an FQDN down to just its leading label."""
    if not fqdn:
        return ""
    s = str(fqdn).strip()
    if not s:
        return ""
    return s.split(".", 1)[0]


def site_name_from_url(url):
    """Extract the web-adaptor SITE label (first path segment) from a URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return ""
    segments = [s for s in (parsed.path or "").split("/") if s]
    return segments[0] if segments else ""


def _admin_rest_info(session, base_url, token, logger, label):
    info_url = base_url.rstrip("/") + "/rest/info"
    data, err = get_json(session, info_url, token, timeout=DEFAULT_TIMEOUT)
    if err or not isinstance(data, dict):
        if logger is not None:
            logger.warning("  [%s] could not read %s: %s", label, info_url, err)
        return {}
    return data


def _post_json(session, url, token, params, timeout=DEFAULT_TIMEOUT):
    merged = {"f": "json", "token": token}
    merged.update(params or {})
    try:
        r = session.post(url, data=merged, timeout=timeout)
    except Exception as exc:
        return None, str(exc)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code} {r.reason}"
    try:
        data = r.json()
    except ValueError:
        snippet = (r.text or "")[:160].replace("\n", " ")
        return None, f"non-JSON response (status {r.status_code}): {snippet!r}"
    if isinstance(data, dict) and "error" in data:
        err = data.get("error") or {}
        return None, f"REST error {err.get('code', '?')}: {err.get('message', 'unknown')}"
    return data, None


def _list_server_machines(session, server_admin_url, token, logger, label):
    machines_url = server_admin_url.rstrip("/") + "/admin/machines"
    data, err = get_json(session, machines_url, token, timeout=DEFAULT_TIMEOUT)
    if err or not isinstance(data, dict):
        if logger is not None:
            logger.warning("  [%s] could not list machines at %s: %s",
                           label, machines_url, err)
        return []

    names = []
    for m in (data.get("machines") or []):
        if not isinstance(m, dict):
            continue
        name = m.get("machineName") or m.get("name")
        if name:
            names.append(name)
    return names


# ==========================================================================
# Data Store discovery -- standalone (no `gis` object, no federation walk)
# ==========================================================================
def discover_data_store(session, hosting_admin_url, token, logger=None):
    """
    Discover the relational Data Store item(s) registered with a hosting
    server, and resolve which machine is PRIMARY for each.
    """
    result = {"host": None, "fqdns": [], "items": [], "ambiguous": False,
             "error": None}

    if not hosting_admin_url:
        msg = "no hosting server admin URL was supplied"
        result["error"] = msg
        if logger is not None:
            logger.error("  [data-store-discovery] %s.", msg)
        return result
    if not session:
        msg = "no HTTP session was supplied"
        result["error"] = msg
        if logger is not None:
            logger.error("  [data-store-discovery] %s.", msg)
        return result
    if not token:
        msg = "no auth token was supplied"
        result["error"] = msg
        if logger is not None:
            logger.error("  [data-store-discovery] %s.", msg)
        return result

    base = hosting_admin_url.rstrip("/")
    find_items_url = base + "/admin/data/findItems"

    find_data, find_err = _post_json(
        session, find_items_url, token,
        {"ancestorPath": "/enterpriseDatabases"}, DEFAULT_TIMEOUT)

    if find_err or not isinstance(find_data, dict):
        msg = f"findItems call to {find_items_url} failed: {find_err}"
        result["error"] = msg
        if logger is not None:
            logger.error("  [data-store-discovery] %s", msg)
        return result

    if logger is not None:
        logger.debug("  [data-store-discovery] raw findItems response from "
                    "%s: %r", find_items_url, find_data)

    items = find_data.get("items") or []
    if not items:
        if logger is not None:
            logger.warning(
                "  [data-store-discovery] findItems returned no "
                "enterpriseDatabases item(s) from %s.", find_items_url)
        return result

    resolved_host = None
    all_fqdns = []

    for item_data in items:
        if not isinstance(item_data, dict):
            continue
        item_path = item_data.get("path", "")
        item_name = item_path.rstrip("/").rsplit("/", 1)[-1] or item_path

        info = item_data.get("info") or {}
        machines = info.get("machines") or []

        primary = None
        standby = []
        all_names = []
        any_role_present = False

        for m in machines:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            role = (m.get("role") or "").upper()
            if not name:
                continue
            all_names.append(name)
            if role:
                any_role_present = True
            if role == "PRIMARY":
                primary = name
            elif role == "STANDBY":
                standby.append(name)

        item_ambiguous = False
        if primary is None:
            if len(all_names) == 1 and not any_role_present:
                primary = all_names[0]
                if logger is not None:
                    logger.info(
                        "  [data-store-discovery] item '%s': single "
                        "machine with no role field reported (%s) -- "
                        "treating it as the (only) primary.",
                        item_name, primary)
            elif len(all_names) > 1:
                item_ambiguous = True
                if logger is not None:
                    logger.error(
                        "  [data-store-discovery] item '%s': %d machine(s) "
                        "reported (%s) but NONE has a 'PRIMARY' role -- "
                        "cannot determine which is live. Refusing to "
                        "guess.", item_name, len(all_names),
                        ", ".join(all_names))

        if logger is not None and not item_ambiguous:
            logger.info(
                "  [data-store-discovery] item '%s': %d machine(s) -- "
                "primary=%s, standby=%s", item_name, len(all_names),
                primary, standby or "none")

        result["items"].append({
            "name": item_name,
            "path": item_path,
            "type": item_data.get("type"),
            "primary": primary,
            "standby": standby,
            "all_machines": all_names,
            "ambiguous": item_ambiguous,
        })

        if item_ambiguous:
            result["ambiguous"] = True

        if primary and not resolved_host:
            resolved_host = primary
        all_fqdns.extend(all_names)

    result["host"] = resolved_host
    result["fqdns"] = all_fqdns
    return result


# ==========================================================================
# Full stack discovery (Portal + federated servers + Data Store)
# ==========================================================================
def discover_stack(gis, session, logger):
    """
    Build the full server inventory for this Portal deployment.

    Each federated server entry additionally carries "server_id" -- the raw
    federation GUID for that server (see module docstring).
    """
    token = gis._con.token
    result = {
        "portal": {"name": None, "url": None, "fqdns": [],
                  "primary": None, "standby": []},
        "hosting_server": {"url": None, "fqdns": [], "site_name": None},
        "data_store": {"host": None, "fqdns": [], "items": []},
        "federated_servers": [],
    }

    portal_name = gis.properties.get("name") or "Portal"
    portal_url = gis.url
    result["portal"]["name"] = portal_name
    result["portal"]["url"] = portal_url
    logger.info("Portal: name=%r url=%s", portal_name, portal_url)

    portal_admin_base = portal_url.rstrip("/") + "/sharing"
    p_info = _admin_rest_info(session, portal_admin_base, token, logger, "Portal")
    if p_info:
        logger.debug("  Portal rest/info: %s", p_info)

    portal_machines_url = portal_url.rstrip("/") + "/portaladmin/machines"
    pm_data, pm_err = get_json(session, portal_machines_url, token,
                               timeout=DEFAULT_TIMEOUT)

    if pm_err or not isinstance(pm_data, dict):
        logger.warning("  [Portal] could not list machines at %s: %s",
                       portal_machines_url, pm_err)
    else:
        logger.debug("Raw Portal machines response from %s: %r",
                    portal_machines_url, pm_data)
        for m in (pm_data.get("machines") or []):
            if not isinstance(m, dict):
                continue
            name = m.get("machineName") or m.get("name")
            role = (m.get("role") or "").upper()
            if not name:
                continue
            result["portal"]["fqdns"].append(name)
            if role == "PRIMARY":
                result["portal"]["primary"] = name
            elif role == "STANDBY":
                result["portal"]["standby"].append(name)

    if not result["portal"]["fqdns"]:
        host = hostname_from_url(portal_url)
        if host:
            result["portal"]["fqdns"].append(host)

    try:
        federation_servers = gis.admin.federation.servers.get("servers", [])
    except Exception as exc:
        logger.warning("Could not read federated servers via "
                       "gis.admin.federation: %s", exc)
        federation_servers = []

    for srv in federation_servers:
        if not isinstance(srv, dict):
            continue
        name = srv.get("name") or srv.get("id") or "UnknownServer"
        admin_url = srv.get("adminUrl") or srv.get("url")
        server_id = srv.get("id")
        is_hosting = bool(srv.get("isHosted") or "hosting" in name.lower())

        site_name = "HOSTING" if is_hosting else site_name_from_url(admin_url)

        logger.info("Federated server: name=%r admin_url=%s site=%r "
                   "server_id=%r hosting=%s", name, admin_url, site_name,
                   server_id, is_hosting)

        fqdns = []
        if admin_url:
            fqdns = _list_server_machines(session, admin_url, token, logger,
                                          name)

        if not fqdns:
            host_only = name.split(":")[0] if name else None
            if host_only and looks_like_fqdn(host_only):
                fqdns = [host_only]
                logger.info("  [%s] falling back to federation name field "
                           "as machine FQDN: %s", name, host_only)
            else:
                host = hostname_from_url(admin_url)
                if host:
                    fqdns = [host]
                    logger.warning("  [%s] no machine list and no usable "
                                   "name field; falling back to admin_url "
                                   "host: %s", name, host)

        entry = {"name": name, "admin_url": admin_url, "fqdns": fqdns,
                 "site_name": site_name, "server_id": server_id}
        result["federated_servers"].append(entry)

        if is_hosting:
            result["hosting_server"]["url"] = admin_url
            result["hosting_server"]["fqdns"] = list(fqdns)
            result["hosting_server"]["site_name"] = site_name

    hosting_admin_url = result["hosting_server"]["url"]
    if hosting_admin_url:
        ds = discover_data_store(session, hosting_admin_url, token,
                                 logger=logger)
        result["data_store"]["host"] = ds["host"]
        result["data_store"]["fqdns"] = ds["fqdns"]
        result["data_store"]["items"] = ds["items"]
        result["data_store"]["ambiguous"] = ds["ambiguous"]
        result["data_store"]["error"] = ds["error"]
    else:
        logger.warning("No hosting server admin URL resolved; skipping "
                       "Data Store discovery.")
        result["data_store"]["ambiguous"] = False
        result["data_store"]["error"] = "no hosting server admin URL resolved"

    return result