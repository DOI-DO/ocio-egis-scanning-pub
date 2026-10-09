#!/usr/bin/env python3
r"""
INVENTORY_FS_REPLICAS.py

Inventories Feature Service replicas across an ArcGIS Enterprise Portal
deployment (hosting + federated servers) or an ArcGIS Online organization's
hosted feature services.

TLS verification is always on (UTILITIES.secure_session), with an explicit
--tls-mode. Tokens are held only in a UTILITIES.auth TokenProvider and are
never written to the log, console, email, or CSV.

Uses the shared UTILITIES package (one directory up) for connection,
logging, email, timestamping, platform labeling, bucketed search, and
configuration. See readme.md for full documentation.

Examples:
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal
    python INVENTORY_FS_REPLICAS.py --config org2.config.toml --target agol
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --workers 96
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --output-dir "D:\reports"
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --test-service https://enterprise.com/server/rest/services/Hosted/Foo/FeatureServer
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --tls-mode os-trust-store
    python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
"""

import argparse
import csv
import datetime as dtmod
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from UTILITIES import (
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    get_org_id,
    epoch_to_date,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_target_arg,
    add_config_arg,
    subject_line,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
)
from UTILITIES.bucketing import bucketed_search, DEFAULT_CAP
from UTILITIES.arcgis_rest import resolve_owners_batched
from UTILITIES.secure_session import VerifyMode, build_secure_session, verify_tls_handshake
from UTILITIES.auth import provider_for_gis

EMAIL_SUBJECT_PREFIX = "All FS Replicas Inventory"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

DEFAULT_WORKERS = 64
HTTP_TIMEOUT = 300
RETRY_TOTAL = 4
RETRY_BACKOFF = 1.5
OWNER_BATCH_SIZE = 100
MB = 1024 * 1024

FETCH_REPLICA_DETAIL = True
LEAN_DETAIL = True
STALE_AFTER_DAYS = 90
RESOLVE_SERVICE_OWNER = True

AGOL_HOSTED_FS_CLAUSE = 'type:"Feature Service" AND typekeywords:"Hosted Service"'
AGOL_SERVER_ROLE = "HOSTED"

DATE_FMT = "%Y/%m/%d"

CREATED_KEYS = ["creationDate", "createdDate", "created", "creation_date", "dateCreated"]
LASTSYNC_KEYS = ["lastSyncDate", "lastSyncedDate", "lastSync", "last_sync_date",
                 "lastSyncTime", "lastSyncedTime"]
OWNER_KEYS = ["replicaOwner", "owner", "userName", "ownerName", "user", "creator"]

GUID_KEYS = ["replicaGuid", "replicaGUID", "replicaID_guid", "guid",
             "GlobalID", "globalId", "globalid"]
NUM_ID_KEYS = ["replicaID", "replicaId", "id"]
ID_KEYS = GUID_KEYS + NUM_ID_KEYS
DETAIL_ID_KEYS = ["replicaID"]
NAME_KEYS = ["replicaName", "name"]

_EPOCH_UTC = dtmod.datetime(1970, 1, 1, tzinfo=dtmod.timezone.utc)

_TLS_MODE_MAP = {
    "standard-ca": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


class PhaseTimer:
    def __init__(self, logger):
        self.logger = logger
        self.records = []

    def phase(self, name):
        return _PhaseCtx(self, name)

    def record(self, name, seconds):
        self.records.append((name, seconds))
        self.logger.info("PHASE DONE: %-45s %8.1fs", name, seconds)

    def log_summary(self):
        self.logger.info("=" * 70)
        self.logger.info("PHASE TIMING SUMMARY (largest first)")
        total = sum(s for _, s in self.records) or 1.0
        for name, secs in sorted(self.records, key=lambda r: r[1], reverse=True):
            self.logger.info("  %-45s %8.1fs  (%4.1f%%)",
                             name, secs, 100.0 * secs / total)
        self.logger.info("  %-45s %8.1fs", "TOTAL (sum of phases)", total)
        self.logger.info("=" * 70)


class _PhaseCtx:
    def __init__(self, timer, name):
        self.timer = timer
        self.name = name
        self.t0 = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.timer.logger.info("PHASE START: %s", self.name)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.timer.record(self.name, time.perf_counter() - self.t0)
        return False


def make_session(pool_size, tls_mode, ca_bundle_path, logger):
    session = build_secure_session(
        tls_mode, ca_bundle_path=ca_bundle_path,
        pool_connections=pool_size, pool_maxsize=pool_size * 2,
        retry_total=RETRY_TOTAL, backoff_factor=RETRY_BACKOFF, logger=logger)
    return session


def _get_json(session, url, token_provider, extra_params=None):
    params = {"f": "json", **token_provider.auth_params()}
    if extra_params:
        params.update(extra_params)
    try:
        resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code >= 400:
            resp = session.post(url, data=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as ex:
        return {"__error__": f"request failed: {ex}"}
    except ValueError as ex:
        return {"__error__": f"JSON parse failed: {ex}"}


def _get_json_fast(session, url, token_provider, extra_params=None):
    params = {"f": "json", **token_provider.auth_params()}
    if extra_params:
        params.update(extra_params)
    try:
        resp = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def _to_epoch_ms(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def epoch_ms_to_datetime(ms, logger=None):
    num = _to_epoch_ms(ms)
    if num is None:
        return None
    try:
        return _EPOCH_UTC + dtmod.timedelta(milliseconds=num)
    except (OverflowError, OSError, ValueError) as ex:
        if logger:
            logger.warning("Could not convert timestamp %r: %s", ms, ex)
        return None


def compute_synced(created_ms, last_sync_ms):
    created = _to_epoch_ms(created_ms)
    last_sync = _to_epoch_ms(last_sync_ms)
    if last_sync is None:
        return "never"
    if created is not None and created == last_sync:
        return "never"
    dt = epoch_ms_to_datetime(last_sync)
    if dt is None:
        return "never"
    delta_days = (dtmod.datetime.now(dtmod.timezone.utc) - dt).days
    return f"{max(delta_days, 0)} days ago"


def derive_status(last_sync_ms, stale_after_days):
    dt = epoch_ms_to_datetime(last_sync_ms)
    if dt is None:
        return ""
    age_days = (dtmod.datetime.now(dtmod.timezone.utc) - dt).total_seconds() / 86400.0
    return "Stale" if age_days > stale_after_days else "Active"


def coerce_replicas_payload(data):
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "error" in data or "__error__" in data:
            return []
        reps = data.get("replicas")
        if isinstance(reps, list):
            return reps
        if isinstance(reps, dict):
            return [reps]
        candidate_keys = {"replicaID", "replicaId", "id", "replicaGuid",
                          "replicaGUID", "GlobalID", "globalId",
                          "replicaOwner", "userName",
                          "creationDate", "lastSyncDate"}
        if candidate_keys.intersection(set(data.keys())):
            return [data]
    return []


def first_non_empty(d, keys):
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return ""


def _looks_like_guid(val):
    if not isinstance(val, str):
        return False
    s = val.strip().strip("{}")
    return "-" in s and len(s) >= 32


def replica_needs_detail(rep):
    if not isinstance(rep, dict):
        return True
    if not first_non_empty(rep, OWNER_KEYS):
        return True
    if not first_non_empty(rep, CREATED_KEYS):
        return True
    if not first_non_empty(rep, LASTSYNC_KEYS):
        return True
    return False


def merge_non_empty(base, extra):
    merged = dict(base) if isinstance(base, dict) else {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v not in (None, "", []):
                merged[k] = v
    return merged


def get_federation_servers(session, portal_url, token_provider, logger):
    fed_url = portal_url.rstrip("/") + "/portaladmin/federation/servers"
    data = _get_json(session, fed_url, token_provider)
    if not isinstance(data, dict) or "__error__" in data:
        logger.error("Could not read federation servers from Portal admin API: %s",
                     (data or {}).get("__error__", "unknown error"))
        return []
    servers = data.get("servers")
    return servers if isinstance(servers, list) else []


def is_hosting_server(server):
    if not isinstance(server, dict):
        return False
    if str(server.get("serverRole", "")).upper() == "HOSTING_SERVER":
        return True
    if server.get("isHosted") is True:
        return True
    return False


def server_site_name(server):
    if isinstance(server, dict) and server.get("name"):
        return server["name"]
    url = (server.get("url") if isinstance(server, dict) else "") or ""
    parsed = urlparse(url)
    host = parsed.netloc or url
    context = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
    return f"{host}/{context}" if context else host


def normalize_rest_root(server_url):
    if not server_url:
        return ""
    u = server_url.rstrip("/")
    lower = u.lower()
    if lower.endswith("/rest/services"):
        return u
    if "/rest/services" in lower:
        return u[:lower.find("/rest/services")] + "/rest/services"
    if lower.endswith("/rest"):
        return u + "/services"
    return u + "/rest/services"


def list_feature_services_for_server(session, rest_root, token_provider, workers, logger):
    if not rest_root:
        return []

    root = _get_json(session, rest_root, token_provider)
    if not isinstance(root, dict) or "__error__" in root:
        logger.warning("Could not read catalog root %s: %s", rest_root,
                       (root or {}).get("__error__", "unknown error"))
        return []

    feature_services = []

    def collect(catalog_json):
        out = []
        services = catalog_json.get("services") if isinstance(catalog_json, dict) else None
        if isinstance(services, list):
            for svc in services:
                if not isinstance(svc, dict):
                    continue
                if str(svc.get("type", "")).lower() != "featureserver":
                    continue
                name = svc.get("name", "")
                item_id = svc.get("serviceItemId", "") or ""
                url = rest_root.rstrip("/") + "/" + name + "/FeatureServer"
                out.append((url, item_id))
        return out

    feature_services.extend(collect(root))

    folders = root.get("folders") if isinstance(root, dict) else None
    if isinstance(folders, list) and folders:
        def fetch_folder(folder):
            folder_url = rest_root.rstrip("/") + "/" + str(folder)
            fj = _get_json(session, folder_url, token_provider)
            return collect(fj) if isinstance(fj, dict) else []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for found in pool.map(fetch_folder, folders):
                feature_services.extend(found)

    seen = set()
    unique = []
    for url, item_id in feature_services:
        key = url.lower()
        if key not in seen:
            seen.add(key)
            unique.append((url, item_id))
    return unique


def service_name_from_url(service_url):
    if not service_url:
        return ""
    u = service_url.rstrip("/")
    if u.lower().endswith("/featureserver"):
        u = u[:-len("/FeatureServer")]
    parts = urlparse(u).path.rstrip("/").split("/")
    if "services" in parts:
        idx = len(parts) - 1 - parts[::-1].index("services")
        return "/".join(parts[idx + 1:])
    return parts[-1] if parts else ""


def service_item_id_from_url(session, service_url, token_provider, logger):
    if not service_url:
        return ""
    data = _get_json(session, service_url.rstrip("/"), token_provider)
    if isinstance(data, dict) and "__error__" not in data:
        return data.get("serviceItemId", "") or ""
    logger.warning("Could not read service root for %s (serviceItemId "
                   "unavailable).", service_url)
    return ""


def collect_service_jobs_agol(gis, session, token_provider, org_id, workers, logger):
    base_url = gis.url.rstrip("/")
    extra_clause = f'{AGOL_HOSTED_FS_CLAUSE} AND orgid:{org_id}'
    logger.info("Enumerating hosted feature services on AGOL (bucketed "
                "search, org-scoped to %s) ...", org_id)

    def search_fn(query, cap):
        from UTILITIES.restutils import rest_search_page
        return rest_search_page(gis, base_url, query, cap, session=session,
                                timeout=HTTP_TIMEOUT)

    items_by_id = bucketed_search(
        search_fn, date_field="created", extra_clause=extra_clause,
        cap=DEFAULT_CAP, logger=logger)

    jobs = []
    skipped_no_url = 0
    seen = set()
    for iid, item in items_by_id.items():
        url = (item.get("url") if isinstance(item, dict) else "") or ""
        if not url:
            skipped_no_url += 1
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        jobs.append((base_url, AGOL_SERVER_ROLE, base_url, url.rstrip("/"),
                    item.get("id") or iid or ""))

    if skipped_no_url:
        logger.info("Skipped %d hosted feature service item(s) with no URL "
                    "(nothing to scan).", skipped_no_url)
    logger.info("Total unique AGOL hosted feature service(s): %d", len(jobs))
    return jobs


def list_replicas_for_service(session, service_url, token_provider):
    if not service_url:
        return []
    replicas_url = service_url.rstrip("/") + "/replicas"
    return coerce_replicas_payload(_get_json(session, replicas_url, token_provider))


def replica_detail_ids(rep):
    candidates = []
    for k in DETAIL_ID_KEYS:
        v = rep.get(k) if isinstance(rep, dict) else None
        if v in (None, "", []):
            continue
        sv = str(v)
        if sv not in candidates:
            candidates.append(sv)
    candidates.sort(key=lambda s: (0 if _looks_like_guid(s) else 1))
    return candidates


def get_replica_detail(session, service_url, rep, token_provider, logger=None):
    if not service_url or not isinstance(rep, dict):
        return {}
    ids = replica_detail_ids(rep)
    if not ids:
        return {}
    extra = {"returnGeometry": "false"} if LEAN_DETAIL else None
    base = service_url.rstrip("/") + "/replicas/"

    last_data = {}
    for rid in ids:
        detail_url = base + str(rid)
        data = _get_json_fast(session, detail_url, token_provider, extra)
        if isinstance(data, dict) and "error" not in data:
            if (first_non_empty(data, CREATED_KEYS)
                    or first_non_empty(data, LASTSYNC_KEYS)
                    or first_non_empty(data, OWNER_KEYS)):
                return data
            last_data = data
        if logger:
            logger.debug("Detail call returned no usable dates/owner: %s",
                         detail_url)
    return last_data


def build_row(server_site, server_role, server_url, service_url,
              service_owner, rep, logger):
    replica_id = first_non_empty(rep, ID_KEYS)
    replica_owner = first_non_empty(rep, OWNER_KEYS)
    replica_name = first_non_empty(rep, NAME_KEYS)
    created = first_non_empty(rep, CREATED_KEYS)
    last_sync = first_non_empty(rep, LASTSYNC_KEYS)

    return {
        "server_site": server_site,
        "server_role": server_role,
        "server_url": server_url,
        "service_name": service_name_from_url(service_url),
        "service_url": service_url,
        "service_owner": service_owner or "",
        "replica_id": str(replica_id or ""),
        "replica_owner": str(replica_owner or ""),
        "date_created": epoch_to_date(created, fmt=DATE_FMT, logger=logger),
        "last_sync_date": epoch_to_date(last_sync, fmt=DATE_FMT, logger=logger),
        "replica_name": replica_name or "",
        "status": derive_status(last_sync, STALE_AFTER_DAYS),
        "synced": compute_synced(created, last_sync),
    }


CSV_FIELDS = [
    "server_site", "server_role", "server_url", "service_name", "service_url",
    "service_owner", "replica_id", "replica_owner", "date_created",
    "last_sync_date", "replica_name", "status", "synced",
]


def write_csv(rows, path, logger):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d replica row(s) to %s", len(rows), path)


def enumerate_servers(session, portal_url, token_provider, logger):
    all_servers = get_federation_servers(session, portal_url, token_provider, logger)
    logger.info("Federated servers reported by Portal: %d", len(all_servers))

    servers = []
    for srv in all_servers:
        url = (srv.get("url") if isinstance(srv, dict) else "") or ""
        site = server_site_name(srv)
        if not url:
            logger.warning("SKIP server with no public URL: %s", site)
            continue
        role = "HOSTING_SERVER" if is_hosting_server(srv) else "FEDERATED_SERVER"
        rest_root = normalize_rest_root(url)
        servers.append((site, role, url, rest_root))
        logger.info("INCLUDE %-16s %s -> %s", role, site, rest_root)
    return servers


def collect_service_jobs_portal(session, servers, token_provider, workers, logger):
    jobs = []
    seen = set()
    lock = Lock()

    n_servers = max(1, len(servers))
    per_server_workers = max(4, workers // n_servers)

    def walk(server_tuple):
        site, role, server_url, rest_root = server_tuple
        svc_pairs = list_feature_services_for_server(
            session, rest_root, token_provider, per_server_workers, logger)
        return site, role, server_url, svc_pairs

    logger.info("Walking %d server catalog(s) concurrently "
                "(~%d folder-worker(s) each) ...", n_servers, per_server_workers)

    with ThreadPoolExecutor(max_workers=n_servers) as pool:
        for fut in as_completed(pool.submit(walk, s) for s in servers):
            site, role, server_url, svc_pairs = fut.result()
            logger.info("%-40s : %d feature service(s)", site, len(svc_pairs))
            with lock:
                for su, item_id in svc_pairs:
                    key = su.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    jobs.append((site, role, server_url, su, item_id))

    logger.info("Total unique feature services across all servers: %d", len(jobs))
    return jobs


def scan_replicas(session, service_jobs, owner_by_item, token_provider, workers,
                  logger, phases):
    list_results = []
    done = 0
    lock = Lock()
    with phases.phase("Pass 1/2: replica lists"):
        logger.info("Pass 1/2: fetching replica lists for %d service(s) with %d "
                    "worker(s) ...", len(service_jobs), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map = {
                pool.submit(list_replicas_for_service, session, su, token_provider):
                    (site, role, server_url, su, item_id)
                for site, role, server_url, su, item_id in service_jobs
            }
            for fut in as_completed(fut_map):
                site, role, server_url, su, item_id = fut_map[fut]
                replicas = fut.result() or []
                svc_owner = owner_by_item.get(item_id, "") if item_id else ""
                list_results.append((site, role, server_url, su, svc_owner, replicas))
                with lock:
                    done += 1
                    if done % 500 == 0 or done == len(service_jobs):
                        logger.info("Pass 1 progress: %d / %d service(s) listed.",
                                    done, len(service_jobs))

    total_replicas = sum(len(r) for *_, r in list_results)
    services_with = sum(1 for *_, r in list_results if r)
    logger.info("Pass 1 complete: %d replica(s) across %d service(s) with "
                "replicas.", total_replicas, services_with)

    rows = []
    detail_jobs = []
    for site, role, server_url, service_url, svc_owner, replicas in list_results:
        for rep in replicas:
            if FETCH_REPLICA_DETAIL and replica_needs_detail(rep):
                detail_jobs.append((site, role, server_url, service_url,
                                    svc_owner, rep))
            else:
                rows.append(build_row(site, role, server_url, service_url,
                                      svc_owner, rep, logger))

    if total_replicas:
        pct = 100.0 * len(detail_jobs) / total_replicas
    else:
        pct = 0.0
    logger.info("Replicas complete from list alone: %d", len(rows))
    logger.info("Replicas needing a detail call   : %d (%.1f%% of all replicas)",
                len(detail_jobs), pct)
    if pct >= 80.0:
        logger.warning(
            "NOTE: %.1f%% of replicas triggered a per-replica detail call.", pct)

    if detail_jobs:
        svc_counts = {}
        for _, _, _, svc_url, _, _ in detail_jobs:
            svc_counts[svc_url] = svc_counts.get(svc_url, 0) + 1
        detail_jobs.sort(key=lambda j: svc_counts.get(j[3], 0), reverse=True)

        detail_ok = 0
        detail_empty = 0
        dlock = Lock()

        with phases.phase("Pass 2/2: replica detail"):
            logger.info("Pass 2/2: fetching %d replica detail record(s) with %d "
                        "worker(s) ...", len(detail_jobs), workers)

            def fetch_detail(job):
                site, role, server_url, service_url, svc_owner, rep = job
                detail = get_replica_detail(session, service_url, rep,
                                            token_provider, logger)
                enriched = merge_non_empty(rep, detail)
                got_date = bool(first_non_empty(enriched, CREATED_KEYS) or
                                first_non_empty(enriched, LASTSYNC_KEYS))
                return got_date, build_row(site, role, server_url, service_url,
                                           svc_owner, enriched, logger)

            d_done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for fut in as_completed(pool.submit(fetch_detail, j) for j in detail_jobs):
                    got_date, row = fut.result()
                    rows.append(row)
                    with dlock:
                        if got_date:
                            detail_ok += 1
                        else:
                            detail_empty += 1
                    d_done += 1
                    if d_done % 1000 == 0 or d_done == len(detail_jobs):
                        logger.info("Pass 2 progress: %d / %d detail(s) fetched.",
                                    d_done, len(detail_jobs))

        logger.info("Pass 2 detail results: %d with date(s), %d still blank.",
                    detail_ok, detail_empty)

    return rows


def run_test_service(session, service_url, portal_url, token_provider, logger,
                     phases):
    service_url = service_url.rstrip("/")
    logger.info("TEST MODE: scanning single service: %s", service_url)

    svc_owner = ""
    if RESOLVE_SERVICE_OWNER:
        item_id = service_item_id_from_url(session, service_url, token_provider, logger)
        if item_id:
            owner_map = resolve_owners_batched(
                session, portal_url, token_provider.get(), {item_id},
                batch_size=OWNER_BATCH_SIZE, timeout=HTTP_TIMEOUT, logger=logger)
            svc_owner = owner_map.get(item_id, "")
            logger.info("Service item id: %s -> owner: %s",
                        item_id, svc_owner or "(none)")
        else:
            logger.info("No serviceItemId resolved; owner left blank.")

    parsed = urlparse(service_url)
    site = parsed.netloc or service_url
    role = "TEST_SERVICE"
    server_url = f"{parsed.scheme}://{parsed.netloc}"

    single_job = [(site, role, server_url, service_url, svc_owner)]
    owner_by_item = {}

    with phases.phase("TEST: scan single service"):
        rows = scan_replicas(session, single_job, owner_by_item, token_provider,
                             workers=8, logger=logger, phases=phases)

    if svc_owner:
        for r in rows:
            if not r["service_owner"]:
                r["service_owner"] = svc_owner

    logger.info("-" * 70)
    logger.info("TEST RESULTS (%d replica(s)):", len(rows))
    for r in rows:
        logger.info("  id=%-38s created=%-12s last_sync=%-12s owner=%-12s status=%s",
                    r["replica_id"] or "(none)",
                    r["date_created"] or "(blank)",
                    r["last_sync_date"] or "(blank)",
                    r["replica_owner"] or "(blank)",
                    r["status"] or "(n/a)")
    blank = sum(1 for r in rows
                if not r["date_created"] and not r["last_sync_date"])
    logger.info("Replicas with BOTH dates blank: %d / %d", blank, len(rows))
    logger.info("-" * 70)
    return rows


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inventory Feature Service replicas across an ArcGIS "
                    "Enterprise Portal deployment (hosting + all federated "
                    "servers) OR an ArcGIS Online organization.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python INVENTORY_FS_REPLICAS.py --config org1.config.toml "
            "--target portal\n"
            "  python INVENTORY_FS_REPLICAS.py --config org2.config.toml "
            "--target agol\n"
            "  python INVENTORY_FS_REPLICAS.py --config org1.config.toml "
            "--target portal \\\n"
            "      --test-service https://enterprise.com/server/rest/services/"
            "Hosted/Foo/FeatureServer\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--test-service", type=str, default=None, metavar="FEATURESERVER_URL",
        help="TEST MODE. Provide the full FeatureServer REST URL of a single "
             "hosted feature service (Portal or AGOL). Only that service's "
             "replicas are scanned.",
    )
    parser.add_argument(
        "--tls-mode", choices=list(_TLS_MODE_MAP.keys()), default="standard-ca",
        help="TLS verification strategy: 'standard-ca' (public CA, default), "
             "'os-trust-store' (verify against the OS cert store; requires "
             "the 'truststore' package), or 'custom-ca-bundle' (requires "
             "--ca-bundle-path).",
    )
    parser.add_argument(
        "--ca-bundle-path", default=None,
        help="Path to a PEM CA bundle. Required when --tls-mode is "
             "custom-ca-bundle.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    test_mode = bool(args.test_service)

    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(args.target, args.config)

    tls_mode = _TLS_MODE_MAP[args.tls_mode]
    if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE and not args.ca_bundle_path:
        print("--tls-mode custom-ca-bundle requires --ca-bundle-path.",
             file=sys.stderr)
        sys.exit(2)

    platform = get_platform(args.portal_url)
    org_prefix = args.org_name_safe
    csv_base_name = (
        f"{org_prefix}_TEST_FS_REPLICAS" if test_mode
        else f"{org_prefix}_{platform}_ALL_FS_REPLICAS")

    ensure_output_dir(args.output_dir)

    log_name = ("test_fs_replicas" if test_mode
                else f"{platform.lower()}_all_fs_replicas")
    log_file_path = os.path.join(
        args.output_dir, f"{org_prefix}_{log_name}_{timestamp}.log")
    csv_path = os.path.join(args.output_dir, f"{csv_base_name}_{timestamp}.csv")

    logger = setup_logging(log_file_path, log_name)
    logger.info("=" * 70)
    logger.info("All FS Replicas Inventory - run started%s",
                " [TEST MODE]" if test_mode else "")
    logger.info("=" * 70)
    logger.info("Organization       : %s", args.org_name)
    logger.info("Platform           : %s", platform)
    logger.info("Target             : %s", args.target)
    logger.info("Config path        : %s", args.config)
    logger.info("Portal URL         : %s", args.portal_url)
    logger.info("Username           : %s", args.username)
    logger.info("Keyring service    : %s", args.keyring_service)
    logger.info("Output directory   : %s", args.output_dir)
    logger.info("Log file           : %s", log_file_path)
    logger.info("Output CSV         : %s", csv_path)
    logger.info("SMTP server        : %s", args.smtp_server)
    logger.info("Email to           : %s", args.email_to)
    logger.info("Email from         : %s", args.email_from)
    logger.info("Max email MB       : %s", max_email_mb)
    logger.info("TLS mode           : %s", args.tls_mode)
    if test_mode:
        logger.info("Test service       : %s", args.test_service)
    logger.info("Worker threads     : %d", args.workers)
    logger.info("Stale threshold    : %d days", STALE_AFTER_DAYS)
    logger.info("Fetch replica detail: %s | Lean detail: %s",
                FETCH_REPLICA_DETAIL, LEAN_DETAIL)
    logger.info("Resolve service owner: %s | batch size: %d",
                RESOLVE_SERVICE_OWNER, OWNER_BATCH_SIZE)
    logger.info("=" * 70)

    phases = PhaseTimer(logger)
    subject = subject_line(EMAIL_SUBJECT_PREFIX + (" [TEST]" if test_mode else ""),
                           timestamp, platform, org_name=args.org_name)

    try:
        with phases.phase("STEP 0: connect"):
            gis = connect_gis(args.portal_url, args.username,
                              args.keyring_service, logger)
    except Exception as exc:
        logger.error("Could not connect: %s", exc)
        send_email(subject,
                   f"Run ABORTED. Could not connect: {exc}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
               token_provider.__class__.__name__, token_provider.is_present())

    portal_url = gis.url.rstrip("/")
    session = make_session(args.workers, tls_mode, args.ca_bundle_path, logger)

    ok, _detail = verify_tls_handshake(portal_url, session, logger=logger)
    if not ok:
        logger.error("TLS handshake/verification to %s failed; aborting.",
                     portal_url)
        send_email(subject,
                   f"Run ABORTED. TLS verification to {portal_url} failed. "
                   "See attached log for details.\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    rows = []
    csv_written = False
    run_error = None

    try:
        with session:
            if test_mode:
                rows = run_test_service(session, args.test_service, portal_url,
                                        token_provider, logger, phases)
            elif args.target == "agol":
                logger.info("STEP 1/3: Resolving AGOL organization id")
                with phases.phase("STEP 1/3: resolve org id"):
                    org_id = get_org_id(gis, logger=logger)
                if not org_id:
                    raise RuntimeError(
                        "Could not determine the AGOL organization id. "
                        "Refusing to run an UNSCOPED catalog search.")
                logger.info("Org id: %s", org_id)

                logger.info("STEP 2/3: Enumerating hosted feature services "
                           "(bucketed search)")
                with phases.phase("STEP 2/3: bucketed service search"):
                    service_jobs = collect_service_jobs_agol(
                        gis, session, token_provider, org_id, args.workers,
                        logger)
                if not service_jobs:
                    logger.warning("No hosted feature services found in the "
                                   "org; an empty CSV will be written.")

                logger.info("STEP 3/3: Resolving service owners and scanning "
                           "replicas")
                owner_by_item = {}
                if RESOLVE_SERVICE_OWNER:
                    distinct_item_ids = {
                        item_id for *_, item_id in service_jobs if item_id
                    }
                    with phases.phase("STEP 3/3a: resolve owners (batched)"):
                        owner_by_item = resolve_owners_batched(
                            session, portal_url, token_provider.get(),
                            distinct_item_ids, batch_size=OWNER_BATCH_SIZE,
                            timeout=HTTP_TIMEOUT, logger=logger)
                else:
                    logger.info("Service owner resolution disabled; leaving blank.")

                with phases.phase("STEP 3/3b: scan replicas (both passes)"):
                    rows = scan_replicas(session, service_jobs, owner_by_item,
                                         token_provider, args.workers, logger,
                                         phases)
            else:
                logger.info("STEP 1/4: Enumerating hosting + federated servers")
                with phases.phase("STEP 1/4: enumerate servers"):
                    servers = enumerate_servers(session, portal_url,
                                                token_provider, logger)
                if not servers:
                    raise RuntimeError(
                        "No servers returned from the federation endpoint.")

                n_host = sum(1 for _, role, *_ in servers if role == "HOSTING_SERVER")
                n_fed = len(servers) - n_host
                logger.info("Servers to scan: %d (hosting=%d, federated=%d)",
                            len(servers), n_host, n_fed)

                logger.info("STEP 2/4: Enumerating FeatureServer services")
                with phases.phase("STEP 2/4: walk catalogs"):
                    service_jobs = collect_service_jobs_portal(
                        session, servers, token_provider, args.workers, logger)
                if not service_jobs:
                    logger.warning("No feature services found on any server; an "
                                   "empty CSV will be written.")

                logger.info("STEP 3/4: Resolving service owners")
                owner_by_item = {}
                if RESOLVE_SERVICE_OWNER:
                    distinct_item_ids = {
                        item_id for *_, item_id in service_jobs if item_id
                    }
                    with phases.phase("STEP 3/4: resolve owners (batched)"):
                        owner_by_item = resolve_owners_batched(
                            session, portal_url, token_provider.get(),
                            distinct_item_ids, batch_size=OWNER_BATCH_SIZE,
                            timeout=HTTP_TIMEOUT, logger=logger)
                else:
                    logger.info("Service owner resolution disabled; leaving blank.")

                logger.info("STEP 4/4: Scanning replicas")
                with phases.phase("STEP 4/4: scan replicas (both passes)"):
                    rows = scan_replicas(session, service_jobs, owner_by_item,
                                         token_provider, args.workers, logger,
                                         phases)
    except Exception as exc:
        run_error = str(exc)
        logger.exception("Scan FAILED: %s", exc)

    try:
        with phases.phase("write CSV"):
            write_csv(rows, csv_path, logger)
        csv_written = True
    except Exception as exc:
        logger.error("Failed to write CSV: %s", exc)

    stale = sum(1 for r in rows if r["status"] == "Stale")
    active = sum(1 for r in rows if r["status"] == "Active")
    sites = len({r["server_site"] for r in rows})
    dated = sum(1 for r in rows
                if r["date_created"] or r["last_sync_date"])

    logger.info("=" * 70)
    logger.info("RUN SUMMARY%s", " [TEST MODE]" if test_mode else "")
    logger.info("=" * 70)
    logger.info("Organization              : %s", args.org_name)
    logger.info("Platform                  : %s", platform)
    logger.info("Server sites w/ replicas  : %d", sites)
    logger.info("Replicas found            : %d", len(rows))
    logger.info("  With date(s)            : %d", dated)
    logger.info("  Active                  : %d", active)
    logger.info("  Stale                   : %d", stale)
    logger.info("Status                    : %s", "SUCCESS" if run_error is None
                else f"FAILED ({run_error})")
    logger.info("Output CSV                : %s", csv_path if csv_written else "(NOT WRITTEN)")
    logger.info("=" * 70)

    phases.log_summary()

    status_line = ("Status        : SUCCESS\n" if run_error is None
                   else f"Status        : FAILED ({run_error})\n")

    phase_lines = "\n".join(
        f"  {name:<40} {secs:8.1f}s"
        for name, secs in sorted(phases.records, key=lambda r: r[1], reverse=True)
    )

    body = (
        f"All FS Replicas Inventory\n"
        f"Organization      : {args.org_name}\n"
        f"Platform          : {platform}\n"
        f"Target            : {args.target}\n"
        f"Run timestamp     : {timestamp}\n"
        f"Portal URL        : {args.portal_url}\n"
        f"Username          : {args.username}\n"
        f"{status_line}\n"
        f"Server sites w/ replicas : {sites}\n"
        f"Replicas found           : {len(rows)}\n"
        f"  With date(s)           : {dated}\n"
        f"  Active                 : {active}\n"
        f"  Stale (> {STALE_AFTER_DAYS} days)      : {stale}\n"
        f"Output CSV               : {csv_path if csv_written else '(NOT WRITTEN)'}\n\n"
        f"Phase timing (largest first):\n{phase_lines}\n\n"
        f"{timing_block(start_wallclock, start_perf)}\n"
        f"See the attached log file and CSV for full detail.\n"
    )

    attachments, note = gather_attachments(
        log_file_path, [csv_path] if csv_written else [],
        args.output_dir, logger, cap_mb=max_email_mb)
    body += note

    send_email(subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    logger.info("Success" if run_error is None else "Completed with errors")
    sys.exit(0 if (csv_written and run_error is None) else 1)


if __name__ == "__main__":
    main()