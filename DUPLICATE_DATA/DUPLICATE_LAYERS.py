"""
DUPLICATE_LAYERS.py

Scans hosted feature services (Portal or AGOL) and reports layers duplicated
across services, with a duplicate-probability score, schema comparison, and
authoritative relationship / hosted-view checks to drop legitimate
service/view pairs. 

This script's own direct requests.Session (used for service-root/count/
relationship/schema REST calls, separate from the arcgis SDK's own
connection) uses an explicit, caller-chosen TLS verification strategy
(--tls-mode), with a one-time startup handshake check before any real API
traffic. Every item-supplied service URL is validated (HTTPS + trusted host)
before the token is ever sent to it, to prevent SSRF / token exfiltration.
The auth token is acquired via a TokenProvider (UTILITIES.auth); the wrapper
protects it from accidental repr()/str() exposure, but the raw value is
necessarily held in local variables during REST calls.

Failure handling: request failures across every stage (bucket search, service
roots, view sources, relationships, counts, schemas) are counted. If a run
could not fully examine everything it set out to, it is reported as INCOMPLETE
(subject tag, loud log/email warnings) but still exits 0, so schedulers see a
completed run while operators are clearly warned the report is not
authoritative.

Usage examples:
    python DUPLICATE_LAYERS.py --config org1.config.toml --target agol
    python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test
    python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --no-feature-count
    python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --no-feature-count --no-relationship-check
    python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test --name-only
    python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --workers 32
    python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test --email-to "thing@mail.com"
    python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --tls-mode os-trust-store
    python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path C:\\certs\\internal-ca.pem
"""


import argparse
import itertools
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urlparse

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PYTHON_DIR = os.path.dirname(_SCRIPT_DIR)
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from UTILITIES import (
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    tune_gis_session,
    bucketed_hosted_fs,
    get_org_id,
    resolve_items,
    get_json,
    count_query,
    start_timer,
    timing_block,
    subject_line,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    finalize_config,
    get_max_email_mb,
    add_target_arg,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_config_arg,
    add_exclude_group_arg,
    resolve_excluded_item_ids,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)
from UTILITIES.arcgis_rest import get_sublayer_definition

LOGGER_NAME = "DUPLICATE_LAYERS"
SUBJECT_PREFIX = "Duplicate Layer Scan"

TEST_LIMIT = 250

DEFAULT_WORKERS = 16
MAX_WORKERS = 64

COUNT_CLOSE_TOLERANCE = 0.05

COUNT_TOP_TIER = ("exact", "close", "empty")

_SYSTEM_FIELD_NAMES = {
    "objectid", "globalid", "shape", "shape__area", "shape__length",
    "created_user", "created_date", "last_edited_user", "last_edited_date",
}

SCORE_BASE = 50
SCORE_COUNT_EXACT = 30
SCORE_COUNT_CLOSE = 15
SCORE_COUNT_DIVERGENT = -25
SCORE_COUNT_EMPTY_CEILING = 55
SCORE_SCHEMA_MATCH = 8
SCORE_SCHEMA_MISMATCH = -30
SCORE_SCHEMA_MATCH_EMPTY_BONUS = 15
SCORE_GENERIC_NAME_PENALTY = -12

SCORE_MATCH_SCOPE_ALL_BONUS = 10
SCORE_MATCH_SCOPE_SOME_PENALTY = -6

_CLOSE_MIN_FRACTION = 0.25
_MISMATCH_MIN_FRACTION = 0.35
_MISMATCH_FULL_PENALTY_AT = 4

_GENERIC_LAYER_NAMES = {
    "polygon", "polygons", "polyline", "polylines", "line", "lines",
    "point", "points", "table", "tables", "layer", "layers",
    "featurelayer", "feature", "features", "new feature layer",
    "new shapefile", "shapefile",
}

CSV_FIELDS = [
    "platform",
    "title",
    "layer_name",
    "summary",
    "group_id",
    "match_type",
    "dup_probability",
    "feature_count",
    "schema_status",
    "generic_name",
    "geometry",
    "spatial_ref",
    "dup_service_count",
    "service_layer_count",
    "owner",
    "item_id",
    "item_url",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

# Esri-owned parent domain for AGOL hosted content (services*.arcgis.com,
# tiles*.arcgis.com, <org>.maps.arcgis.com, ...).
_AGOL_ALLOWED_HOST_SUFFIX = ".arcgis.com"
_AGOL_ALLOWED_HOST_APEX = "arcgis.com"

# Substrings that mark an error string as a TLS/certificate failure rather
# than an ordinary request error (get_json/count_query flatten SSLError into
# a plain connection-error string, so we classify by text).
_TLS_ERROR_MARKERS = (
    "ssl", "certificate verify failed", "certificate_verify_failed",
    "cert verify", "sslcertverification", "tlsv1", "wrong version number",
)

_url_validation_count = itertools.count()


def bounded_int(value):
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError("--workers must be >= 1.")
    if ivalue > MAX_WORKERS:
        raise argparse.ArgumentTypeError(
            f"--workers must be <= {MAX_WORKERS}.")
    return ivalue


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan hosted feature services for duplicated layers on "
                    "Portal or AGOL and report them in a single CSV. TLS "
                    "verification strategy for this script's own direct "
                    "REST calls is explicit (--tls-mode); the arcgis SDK "
                    "connection (via connect_gis()) always verifies TLS "
                    "regardless.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    add_exclude_group_arg(parser)
    add_test_flag(
        parser,
        help_text=(
            "Disable test mode. By DEFAULT the later scan stages are limited "
            f"to the first {TEST_LIMIT} hosted feature services (discovery "
            "still enumerates the whole org); pass --no-test to scan ALL "
            "hosted feature services."
        ),
    )
    parser.add_argument(
        "--name-only", action="store_true",
        help="Match layers on NAME ONLY, ignoring geometry type and spatial "
             "reference.",
    )
    parser.add_argument(
        "--workers", type=bounded_int, default=DEFAULT_WORKERS,
        help="Thread-pool size for the parallel service-root, count, and "
             f"relationship reads (1-{MAX_WORKERS}).",
    )
    parser.add_argument(
        "--no-feature-count", dest="feature_count", action="store_false",
        help="Skip per-layer feature-count queries (faster, but the "
             "duplicate-probability score loses its count signal and stays "
             "at the base score). Counting is ON by default.",
    )
    parser.set_defaults(feature_count=True)
    parser.add_argument(
        "--no-schema-check", dest="schema_check", action="store_false",
        help="Skip the field/schema comparison for layers whose feature "
             "counts already agree (exact or close). ON by default.",
    )
    parser.set_defaults(schema_check=True)
    parser.add_argument(
        "--no-relationship-check", dest="relationship_check",
        action="store_false",
        help="Skip the authoritative item-relationship check (Stage 2, "
             "relatedItems-based). ON by default.",
    )
    parser.set_defaults(relationship_check=True)
    parser.add_argument(
        "--no-view-detect", dest="view_detect", action="store_false",
        help="Skip Stage 0 (isView / FeatureServer/sources) hosted-view "
             "detection. ON by default.",
    )
    parser.set_defaults(view_detect=True)

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the service-"
             "root/count/relationship/schema REST calls -- NOT the arcgis "
             "SDK's own internal connection, which always verifies TLS via "
             "connect_gis()) verifies TLS certificates. 'standard' "
             "(default): certifi's bundled public CA list -- correct for "
             "AGOL and any Portal with a public CA cert. 'os-trust-store': "
             "verify against the operating system's native certificate "
             "store (requires the 'truststore' package) -- use for an "
             "internal Enterprise Portal signed by an organizational CA "
             "already trusted at the OS level. 'custom-ca-bundle': verify "
             "against an explicit PEM file passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")
    return parser.parse_args()


# ==========================================================================
# URL validation (SSRF / token-exfiltration guard)
# ==========================================================================
def _validate_service_url(item_url, portal_url, is_agol, logger):
    """Raise ValueError unless item_url is HTTPS and points at a trusted host.

    Portal: strict same-origin (host:port match). AGOL: host must be within
    Esri's .arcgis.com domain, since hosted services live on
    services*.arcgis.com etc., not the <org>.maps.arcgis.com portal host.
    """
    if not item_url or not isinstance(item_url, str):
        raise ValueError(f"item_url is not a non-empty string: {item_url!r}")

    try:
        item_parsed = urlparse(
            item_url if "://" in item_url else f"https://{item_url}")
        portal_parsed = urlparse(
            portal_url if "://" in portal_url else f"https://{portal_url}")
    except Exception as exc:
        raise ValueError(f"could not parse URL: {exc}")

    if item_parsed.scheme.lower() != "https":
        raise ValueError(
            f"service URL does not use HTTPS (scheme={item_parsed.scheme!r}); "
            f"refusing to send token over an insecure connection")

    item_hostname = (item_parsed.hostname or "").lower()
    if not item_hostname:
        raise ValueError(f"service URL has no resolvable hostname: {item_url!r}")

    if is_agol:
        if not (item_hostname == _AGOL_ALLOWED_HOST_APEX
                or item_hostname.endswith(_AGOL_ALLOWED_HOST_SUFFIX)):
            raise ValueError(
                f"service URL host {item_hostname!r} is not within Esri's "
                f"'{_AGOL_ALLOWED_HOST_APEX}' domain; refusing to send token "
                f"to an untrusted host")
    else:
        item_host = f"{item_hostname}:{item_parsed.port or 443}"
        portal_host = (f"{(portal_parsed.hostname or '').lower()}:"
                       f"{portal_parsed.port or 443}")
        if item_host != portal_host:
            raise ValueError(
                f"service URL is different-origin from Portal "
                f"(item={item_host}, portal={portal_host}); refusing to send "
                f"token to an untrusted host")

    next(_url_validation_count)


def _is_tls_error(err_text):
    if not err_text:
        return False
    low = str(err_text).lower()
    return any(marker in low for marker in _TLS_ERROR_MARKERS)


# ==========================================================================
# Layer key / signature helpers
# ==========================================================================
def _normalize_name(name):
    return (name or "").strip().lower()


def _extract_wkid(sr):
    if isinstance(sr, dict):
        wkid = sr.get("latestWkid") or sr.get("wkid")
        if wkid is not None:
            return str(wkid)
        if sr.get("wkt"):
            return "wkt"
        return "none"
    if isinstance(sr, bool):
        return "none"
    if isinstance(sr, (int, float)):
        return str(int(sr))
    if isinstance(sr, str) and sr.strip():
        return sr.strip()
    return "none"


def _spatial_ref_wkid(entry, service_defn=None):
    sr = entry.get("spatialReference")
    if sr is None:
        ext = entry.get("extent")
        if isinstance(ext, dict):
            sr = ext.get("spatialReference")
    if sr is None and isinstance(service_defn, dict):
        sr = service_defn.get("spatialReference")
        if sr is None:
            full_ext = service_defn.get("fullExtent") or service_defn.get("extent")
            if isinstance(full_ext, dict):
                sr = full_ext.get("spatialReference")

    return _extract_wkid(sr)


def _layer_key(entry, name_only, service_defn=None):
    nm = _normalize_name(entry.get("name"))
    if not nm:
        return None
    if name_only:
        return nm
    geom = (entry.get("geometryType") or "table").strip().lower()
    wkid = _spatial_ref_wkid(entry, service_defn=service_defn)
    return f"{nm}|{geom}|sr:{wkid}"


def get_service_layer_keys(session, service_url, token_provider, name_only,
                           is_agol, portal_url, logger, timeout=30):
    try:
        _validate_service_url(service_url, portal_url, is_agol, logger)
    except ValueError as exc:
        return [], {}, {}, {}, f"URL validation failed: {exc}"

    data, err = get_json(session, service_url, token_provider.get(),
                         timeout=timeout)
    if err is not None:
        return [], {}, {}, {}, err
    if not isinstance(data, dict):
        return [], {}, {}, {}, "service root was not a JSON object"

    keys = set()
    key_to_name = {}
    key_to_index = {}
    for section in ("layers", "tables"):
        for entry in data.get(section, []) or []:
            if isinstance(entry, dict):
                k = _layer_key(entry, name_only, service_defn=data)
                if k:
                    keys.add(k)
                    orig = (entry.get("name") or "").strip()
                    if k not in key_to_name and orig:
                        key_to_name[k] = orig
                    idx = entry.get("id")
                    if k not in key_to_index and idx is not None:
                        key_to_index[k] = idx
    return sorted(keys), key_to_name, key_to_index, data, None


def _is_view_from_defn(service_defn):
    if not isinstance(service_defn, dict):
        return False
    return bool(service_defn.get("isView"))


def _fetch_view_sources(session, service_url, token_provider, is_agol,
                        portal_url, logger, timeout=30):
    try:
        _validate_service_url(service_url, portal_url, is_agol, logger)
    except ValueError as exc:
        return set(), f"URL validation failed: {exc}"

    sources_url = f"{service_url.rstrip('/')}/sources"
    data, err = get_json(session, sources_url, token_provider.get(),
                         timeout=timeout)
    if err is not None or not isinstance(data, dict):
        return set(), err
    source_ids = set()
    for entry in (data.get("services") or []):
        if isinstance(entry, dict):
            sid = entry.get("serviceItemId")
            if sid:
                source_ids.add(sid)
    return source_ids, None


def detect_hosted_views(records, session, token_provider, is_agol, portal_url,
                        workers, logger):
    candidates = [rec for rec in records
                  if _is_view_from_defn(rec.get("service_defn"))]
    logger.info("Stage 0 (view detection): %d of %d service(s) report "
                "isView=true on their service definition.",
                len(candidates), len(records))

    view_source_ids = {}
    errors = 0
    tls_errors = 0
    if not candidates:
        return view_source_ids, errors, tls_errors

    def _work(rec):
        src_ids, err = _fetch_view_sources(
            session, rec["service_url"], token_provider, is_agol, portal_url,
            logger)
        return rec["item_id"], src_ids, err

    done = 0
    total = len(candidates)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, rec) for rec in candidates]
        for fut in as_completed(futures):
            try:
                item_id, src_ids, err = fut.result()
            except Exception as exc:
                errors += 1
                logger.warning("Stage 0: unexpected worker failure: %s",
                               redact_token(str(exc)))
                continue
            view_source_ids[item_id] = src_ids
            if err is not None:
                errors += 1
                if _is_tls_error(err):
                    tls_errors += 1
                logger.warning(
                    "Stage 0: view %s confirmed (isView=true) but its "
                    "source could not be resolved via FeatureServer/sources "
                    "(%s).", item_id, redact_token(str(err)))
            done += 1
            if done % 100 == 0 or done == total:
                logger.info("Stage 0 source-lookup progress: %d / %d.",
                            done, total)

    n_resolved = sum(1 for s in view_source_ids.values() if s)
    logger.info("Stage 0 complete: %d confirmed view(s), %d with a resolved "
                "source id, %d lookup error(s).",
                len(view_source_ids), n_resolved, errors)
    return view_source_ids, errors, tls_errors


def strip_view_layers_from_signatures(records, view_source_ids, logger):
    if not view_source_ids:
        return 0

    by_item_id = {rec["item_id"]: rec for rec in records}
    removed = 0

    for view_id, source_ids in view_source_ids.items():
        view_rec = by_item_id.get(view_id)
        if view_rec is None or not source_ids:
            continue
        source_sig = set()
        for sid in source_ids:
            src_rec = by_item_id.get(sid)
            if src_rec is not None:
                source_sig |= src_rec["signature"]
        if not source_sig:
            continue
        overlap = view_rec["signature"] & source_sig
        if overlap:
            view_rec["signature"] = view_rec["signature"] - overlap
            removed += len(overlap)

    if removed:
        logger.info("Stage 0: removed %d (view, layer) instance(s) from "
                    "duplicate consideration.", removed)
    return removed


def split_layer_key(key, name_only, display_name=None):
    if name_only or "|" not in key:
        return (display_name or key), "", ""
    parts = key.split("|")
    key_name = parts[0]
    geom = parts[1] if len(parts) > 1 else ""
    if geom.startswith("esrigeometry"):
        geom = geom[len("esrigeometry"):]
    spatial_ref = ""
    if len(parts) > 2 and parts[2].startswith("sr:"):
        spatial_ref = parts[2][len("sr:"):]
    return (display_name or key_name), geom, spatial_ref


def _is_generic_layer_name(display_name):
    nm = (display_name or "").strip().lower()
    if not nm:
        return False
    nm_stripped = re.sub(r"[\s_]*\d+$", "", nm).strip()
    return nm in _GENERIC_LAYER_NAMES or nm_stripped in _GENERIC_LAYER_NAMES


def _service_url_for_item(item):
    url = (getattr(item, "url", None) or "").rstrip("/")
    if not url:
        return ""
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and parts[1].isdigit():
        url = parts[0]
    return url


def collect_signatures(items, session, token_provider, base_url, name_only,
                       is_agol, portal_url, workers, logger):
    records = []
    stats = {"no_url": 0, "fetch_errors": 0, "empty_sig": 0, "tls_errors": 0}
    key_names = {}

    jobs = []
    for item in items:
        url = _service_url_for_item(item)
        if not url:
            stats["no_url"] += 1
            continue
        jobs.append((item, url))

    total = len(jobs)
    logger.info("Reading %d service root(s) with %d worker(s) ...",
                total, workers)

    def _work(job):
        item, url = job
        keys, key_to_name, key_to_index, service_defn, err = \
            get_service_layer_keys(session, url, token_provider, name_only,
                                   is_agol, portal_url, logger)
        return item, url, keys, key_to_name, key_to_index, service_defn, err

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, j) for j in jobs]
        for fut in as_completed(futures):
            try:
                (item, url, keys, key_to_name, key_to_index, service_defn,
                 err) = fut.result()
            except Exception as exc:
                stats["fetch_errors"] += 1
                logger.warning("Service-root: unexpected worker failure: %s",
                               redact_token(str(exc)))
                done += 1
                continue
            done += 1
            if err is not None:
                stats["fetch_errors"] += 1
                if _is_tls_error(err):
                    stats["tls_errors"] += 1
                logger.warning("Could not read layers for %s (%s): %s",
                               getattr(item, "title", "?"), item.id,
                               redact_token(str(err)))
            else:
                for k, nm in key_to_name.items():
                    if k not in key_names:
                        key_names[k] = nm
                sig = frozenset(keys)
                if not sig:
                    stats["empty_sig"] += 1
                records.append({
                    "signature": sig,
                    "item_id": item.id,
                    "title": getattr(item, "title", "") or "",
                    "owner": getattr(item, "owner", "") or "",
                    "item_url": f"{base_url.rstrip('/')}"
                                f"/home/item.html?id={item.id}",
                    "service_url": url,
                    "layer_count": len(sig),
                    "key_to_index": key_to_index,
                    "service_defn": service_defn,
                })
            if done % 100 == 0 or done == total:
                logger.info("Service-root progress: %d / %d.", done, total)

    return records, stats, key_names


def analyze_duplicates(records):
    indexed = [r for r in records if r["signature"]]

    inverted = {}
    for i, rec in enumerate(indexed):
        for key in rec["signature"]:
            inverted.setdefault(key, []).append(i)

    layer_counts = {k: len(v) for k, v in inverted.items()}
    dup_layers = {k for k, n in layer_counts.items() if n >= 2}

    keep = [i for i, rec in enumerate(indexed)
            if rec["signature"] & dup_layers]
    keep_set = set(keep)

    parent = {i: i for i in keep}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for key in dup_layers:
        members = [i for i in inverted[key] if i in keep_set]
        for j in members[1:]:
            union(members[0], j)

    root_to_group = {}
    next_group = 1
    dup_records = []
    for i in keep:
        rec = indexed[i]
        my_dupes = rec["signature"] & dup_layers
        match_type = "ALL" if my_dupes == rec["signature"] else "SOME"
        root = find(i)
        if root not in root_to_group:
            root_to_group[root] = next_group
            next_group += 1
        dup_records.append({
            **rec,
            "match_type": match_type,
            "group_id": root_to_group[root],
            "dup_layers": my_dupes,
        })

    dup_records.sort(key=lambda r: (r["group_id"],
                                    0 if r["match_type"] == "ALL" else 1,
                                    r["title"].lower()))
    return dup_records


def group_titles(dup_records):
    by_group = {}
    for rec in dup_records:
        by_group.setdefault(rec["group_id"], []).append(rec["title"])
    return by_group


def build_key_to_services(dup_records):
    key_to_services = {}
    for rec in dup_records:
        for key in rec["dup_layers"]:
            key_to_services.setdefault(key, []).append(
                (rec["item_id"], rec["title"], rec["item_url"], rec["owner"]))
    return key_to_services


_ALL_RELATIONSHIP_TYPES = (
    "Service2Service",
    "Service2Data",
    "Service2Layer",
    "Map2Service",
    "Map2Data",
    "Item2Item",
    "Item2Attachment",
    "Service2Route",
    "Area2Package",
    "Map2FeatureCollection",
    "WMA2Code",
    "Listed2Provisioned",
    "APIKey2Item",
    "Solution2Item",
)


def _related_ids_for_item(session, base_url, token_provider, item_id,
                          timeout=30):
    """Return (related_ids, n_call_failures, saw_tls_error).

    base_url is the authenticated Portal/AGOL origin (validated at startup),
    not an item-supplied URL, so no per-URL trust check is needed here.
    """
    related = set()
    failures = 0
    tls_seen = False
    endpoint = (f"{base_url.rstrip('/')}/sharing/rest/content/items/"
                f"{item_id}/relatedItems")
    for direction in ("forward", "reverse"):
        for rel_type in _ALL_RELATIONSHIP_TYPES:
            data, err = get_json(
                session, endpoint, token_provider.get(), timeout=timeout,
                params={"relationshipType": rel_type, "direction": direction})
            if err is not None or not isinstance(data, dict):
                failures += 1
                if _is_tls_error(err):
                    tls_seen = True
                continue
            for entry in data.get("relatedItems", []) or []:
                if isinstance(entry, dict) and entry.get("id"):
                    related.add(entry["id"])
    return related, failures, tls_seen


def fetch_relationships(dup_records, session, base_url, token_provider,
                        workers, logger):
    ids = [rec["item_id"] for rec in dup_records]
    total = len(ids)
    logger.info("Checking authoritative item relationships for %d service(s) "
                "with %d worker(s) ...", total, workers)

    rel_map = {}
    fail_counter = {"calls": 0, "items_with_failures": 0, "tls": 0}

    def _work(item_id):
        return (item_id,) + _related_ids_for_item(
            session, base_url, token_provider, item_id)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, i) for i in ids]
        for fut in as_completed(futures):
            try:
                item_id, related, failures, tls_seen = fut.result()
            except Exception as exc:
                fail_counter["items_with_failures"] += 1
                logger.warning("Relationship: unexpected worker failure: %s",
                               redact_token(str(exc)))
                done += 1
                continue
            rel_map[item_id] = related
            if failures:
                fail_counter["calls"] += failures
                fail_counter["items_with_failures"] += 1
                if tls_seen:
                    fail_counter["tls"] += 1
            done += 1
            if done % 50 == 0 or done == total:
                logger.info("Relationship progress: %d / %d.", done, total)

    if fail_counter["items_with_failures"]:
        logger.warning(
            "Relationship stage had failures: %d relatedItems call(s) failed "
            "across %d item(s); such items may show fewer relationships than "
            "they truly have, which can leave real service/view pairs in the "
            "report.", fail_counter["calls"],
            fail_counter["items_with_failures"])

    return rel_map, fail_counter


def drop_related_duplicates(dup_records, rel_map, logger):
    key_holders = {}
    for rec in dup_records:
        for key in rec["dup_layers"]:
            key_holders.setdefault(key, set()).add(rec["item_id"])

    n_layers_dropped = 0
    kept = []
    for rec in dup_records:
        me = rec["item_id"]
        my_related = rel_map.get(me, set())
        remaining = set()
        for key in rec["dup_layers"]:
            others = key_holders.get(key, set()) - {me}
            unrelated_others = {o for o in others if o not in my_related}
            if unrelated_others:
                remaining.add(key)
            else:
                n_layers_dropped += 1
        if remaining:
            rec["dup_layers"] = remaining
            kept.append(rec)

    logger.info("Authoritative relationships removed %d duplicated-layer "
                "instance(s); %d service(s) remain with real duplicates "
                "(was %d).", n_layers_dropped, len(kept), len(dup_records))
    return kept, n_layers_dropped


def fetch_feature_counts(dup_records, session, token_provider, workers,
                         logger):
    jobs = []
    for rec in dup_records:
        k2i = rec.get("key_to_index", {})
        for key in rec["dup_layers"]:
            idx = k2i.get(key)
            if idx is None:
                jobs.append((rec["item_id"], key, None))
            else:
                jobs.append((rec["item_id"], key,
                             f"{rec['service_url'].rstrip('/')}/{idx}"))

    total = len(jobs)
    logger.info("Querying feature counts for %d duplicated-layer instance(s) "
                "with %d worker(s) ...", total, workers)

    counts = {}
    fail_counter = {"failures": 0, "tls": 0}
    fail_lock = Lock()

    def _work(job):
        item_id, key, layer_url = job
        if layer_url is None:
            return item_id, key, None
        count, err = count_query(session, layer_url, token_provider.get())
        if err is not None:
            with fail_lock:
                fail_counter["failures"] += 1
                if _is_tls_error(err):
                    fail_counter["tls"] += 1
            return item_id, key, None
        return item_id, key, count

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, j) for j in jobs]
        for fut in as_completed(futures):
            try:
                item_id, key, count = fut.result()
            except Exception as exc:
                with fail_lock:
                    fail_counter["failures"] += 1
                logger.warning("Feature-count: unexpected worker failure: %s",
                               redact_token(str(exc)))
                done += 1
                continue
            counts[(item_id, key)] = count
            done += 1
            if done % 200 == 0 or done == total:
                logger.info("Feature-count progress: %d / %d.", done, total)

    if fail_counter["failures"]:
        logger.warning(
            "Feature-count stage had %d failed count query/queries; affected "
            "layers are treated as 'unknown' count, which can suppress or "
            "distort duplicate scoring for those layers.",
            fail_counter["failures"])

    return counts, fail_counter


def count_comparison_tiers(dup_records, counts, tolerance):
    per_key = {}
    for rec in dup_records:
        for key in rec["dup_layers"]:
            per_key.setdefault(key, []).append(
                (rec["item_id"], counts.get((rec["item_id"], key))))

    tiers = {}
    for key, pairs in per_key.items():
        values = [c for (_iid, c) in pairs]
        known = [v for v in values if v is not None]

        if len(known) < len(values) or not known:
            for iid, _c in pairs:
                tiers[(iid, key)] = (
                    "unknown", "one or more copies had no readable feature count")
            continue

        cmin, cmax = min(known), max(known)
        all_equal = (cmin == cmax)

        if all_equal and cmax == 0:
            for iid, _c in pairs:
                tiers[(iid, key)] = ("empty", "all copies are empty (0 features)")
            continue

        if all_equal:
            detail = f"all copies have {cmin} features"
            for iid, _c in pairs:
                tiers[(iid, key)] = ("exact", detail)
            continue

        spread_fraction = (cmax - cmin) / cmax if cmax > 0 else 1.0
        group_is_close = cmax > 0 and (cmax - cmin) <= tolerance * cmax

        if group_is_close:
            detail = (f"counts are close across all copies (range "
                      f"{cmin}-{cmax}, within {int(tolerance * 100)}% "
                      f"end-to-end)")
            for iid, _c in pairs:
                tiers[(iid, key)] = ("close", detail, spread_fraction)
        else:
            detail = f"counts range {cmin}-{cmax} across {len(known)} copies"
            for iid, c in pairs:
                if c is None:
                    tiers[(iid, key)] = ("unknown", "this copy had no readable feature count")
                else:
                    tiers[(iid, key)] = (
                        "divergent",
                        f"this copy has {c} features; {detail}")
    return tiers


def _tier_spread_fraction(tier_tuple):
    if not tier_tuple or tier_tuple[0] != "close":
        return None
    if len(tier_tuple) >= 3:
        return tier_tuple[2]
    return 0.0


def _extract_fields_from_sublayer(defn):
    if not isinstance(defn, dict):
        return None
    fields = defn.get("fields")
    if not isinstance(fields, list):
        return None
    out = {}
    for f in fields:
        if not isinstance(f, dict):
            continue
        nm = (f.get("name") or "").strip().lower()
        if not nm or nm in _SYSTEM_FIELD_NAMES:
            continue
        out[nm] = f.get("type") or ""
    return out


def fetch_schemas(dup_records, count_tiers, session, token_provider, workers,
                  logger):
    jobs = []
    seen = set()
    n_no_index = 0
    for rec in dup_records:
        k2i = rec.get("key_to_index", {})
        for key in rec["dup_layers"]:
            tier_tuple = count_tiers.get((rec["item_id"], key), ("unknown", ""))
            tier = tier_tuple[0]
            if tier not in COUNT_TOP_TIER:
                continue
            job_key = (rec["item_id"], key)
            if job_key in seen:
                continue
            seen.add(job_key)
            idx = k2i.get(key)
            if idx is None:
                jobs.append((rec["item_id"], key, None, rec["title"]))
                n_no_index += 1
            else:
                jobs.append((rec["item_id"], key,
                             f"{rec['service_url'].rstrip('/')}/{idx}",
                             rec["title"]))

    total = len(jobs)
    if total == 0:
        return {}, {"failures": 0}
    logger.info("Fetching field schemas for %d top-tier duplicated-layer "
                "instance(s) with %d worker(s) ...", total, workers)
    if n_no_index:
        logger.warning("%d of %d instance(s) have no layer-index entry "
                       "for this key and will be marked unreadable without "
                       "a network call.", n_no_index, total)

    schema_fields = {}
    fetch_errors = []

    def _work(job):
        item_id, key, layer_url, title = job
        if layer_url is None:
            return item_id, key, None, title, "no layer-index entry for this key"
        defn = get_sublayer_definition(session, layer_url, token_provider.get())
        fields = _extract_fields_from_sublayer(defn)
        if fields is None:
            reason = ("sub-layer definition had no readable 'fields' list "
                      "(request failed or returned an unexpected shape)")
            return item_id, key, None, title, reason
        return item_id, key, fields, title, None

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_work, j) for j in jobs]
        for fut in as_completed(futures):
            try:
                item_id, key, fields, title, err_reason = fut.result()
            except Exception as exc:
                fetch_errors.append(("?", "?", "?",
                                     redact_token(str(exc))))
                done += 1
                continue
            schema_fields[(item_id, key)] = fields
            if err_reason is not None:
                fetch_errors.append((item_id, key, title, err_reason))
            done += 1
            if done % 100 == 0 or done == total:
                logger.info("Schema-fetch progress: %d / %d.", done, total)

    if fetch_errors:
        logger.warning("%d of %d schema fetch(es) could not produce a "
                       "readable field list; marked schema_status='unknown'. "
                       "First %d shown:",
                       len(fetch_errors), total, min(10, len(fetch_errors)))
        for item_id, key, title, reason in fetch_errors[:10]:
            logger.warning("%s (%s) / layer key '%s': %s",
                           title, item_id, key, redact_token(str(reason)))

    return schema_fields, {"failures": len(fetch_errors)}


def compare_schemas(dup_records, count_tiers, schema_fields, logger):
    per_key_all = {}
    for rec in dup_records:
        for key in rec["dup_layers"]:
            tier_tuple = count_tiers.get((rec["item_id"], key), ("unknown", ""))
            per_key_all.setdefault(key, []).append((rec["item_id"], tier_tuple[0]))

    results = {}
    n_mismatch = 0
    n_match = 0
    n_unknown = 0

    for key, all_members in per_key_all.items():
        top_tier_members = [(iid, t) for iid, t in all_members if t in COUNT_TOP_TIER]
        if not top_tier_members:
            continue

        entries = [(iid, schema_fields.get((iid, key))) for iid, _t in top_tier_members]
        n_ineligible_peers = len(all_members) - len(top_tier_members)

        for i, (iid_a, fields_a) in enumerate(entries):
            if fields_a is None:
                results[(iid_a, key)] = (
                    "unknown",
                    "this copy's own field list could not be read "
                    "(sub-layer definition fetch failed)", None, 0)
                n_unknown += 1
                continue

            count_a = len(fields_a)
            names_a = set(fields_a.keys())

            mismatch_detail = None
            mismatch_diff_count = 0
            any_comparable = False
            n_unreadable_peers = 0
            for j, (_iid_b, fields_b) in enumerate(entries):
                if j == i:
                    continue
                if fields_b is None:
                    n_unreadable_peers += 1
                    continue
                any_comparable = True
                names_b = set(fields_b.keys())
                count_b = len(fields_b)

                extra_in_a = sorted(names_a - names_b)
                extra_in_b = sorted(names_b - names_a)
                type_mismatches = sorted(
                    n for n in (names_a & names_b)
                    if fields_a.get(n) != fields_b.get(n))

                if count_a != count_b or extra_in_a or extra_in_b or type_mismatches:
                    pieces = []
                    if count_a != count_b:
                        pieces.append(f"{count_a} fields vs {count_b} fields")
                    if extra_in_a:
                        pieces.append("this copy has extra field(s): "
                                      + ", ".join(extra_in_a))
                    if extra_in_b:
                        pieces.append("the other copy has extra field(s): "
                                      + ", ".join(extra_in_b))
                    if type_mismatches:
                        pieces.append("field type differs on: "
                                      + ", ".join(type_mismatches))
                    mismatch_detail = "; ".join(pieces)
                    mismatch_diff_count = len(
                        set(extra_in_a) | set(extra_in_b) | set(type_mismatches))
                    break

            if not any_comparable:
                if n_ineligible_peers and not n_unreadable_peers:
                    reason = (
                        f"{n_ineligible_peers} other copy/copies in this "
                        f"group have feature counts that diverge too much "
                        f"to compare, so no schema fetch was attempted for "
                        f"them")
                elif n_unreadable_peers and not n_ineligible_peers:
                    reason = (
                        f"{n_unreadable_peers} other copy/copies were "
                        f"eligible but their field lists could not be read")
                elif n_ineligible_peers and n_unreadable_peers:
                    reason = (
                        f"{n_ineligible_peers} other copy/copies diverge on "
                        f"feature count (not compared) and "
                        f"{n_unreadable_peers} more could not be read")
                else:
                    reason = "no other copy of this layer exists"
                results[(iid_a, key)] = ("unknown", reason, count_a, 0)
                n_unknown += 1
            elif mismatch_detail:
                results[(iid_a, key)] = (
                    "mismatch", mismatch_detail, count_a, mismatch_diff_count)
                n_mismatch += 1
            else:
                results[(iid_a, key)] = (
                    "match", "field schemas match (same field count and "
                    "names)", count_a, 0)
                n_match += 1

    if per_key_all:
        logger.info("Schema comparison (top-tier feature-count matches "
                    "only): %d match, %d mismatch, %d unknown "
                    "(of %d instance(s) checked).",
                    n_match, n_mismatch, n_unknown, len(results))
    return results


def calculate_duplicate_score(count_tier, schema_status, is_generic_name,
                              match_type, count_spread_fraction=None,
                              schema_diff_count=0):
    score = SCORE_BASE

    if count_tier == "exact":
        score += SCORE_COUNT_EXACT
    elif count_tier == "close":
        if count_spread_fraction is None:
            fraction_of_boost = 1.0
        else:
            tol = COUNT_CLOSE_TOLERANCE if COUNT_CLOSE_TOLERANCE > 0 else 1.0
            closeness = 1.0 - min(max(count_spread_fraction / tol, 0.0), 1.0)
            fraction_of_boost = (
                _CLOSE_MIN_FRACTION
                + (1.0 - _CLOSE_MIN_FRACTION) * closeness)
        score += SCORE_COUNT_CLOSE * fraction_of_boost
    elif count_tier == "divergent":
        score += SCORE_COUNT_DIVERGENT
    elif count_tier == "empty":
        score = min(score, SCORE_COUNT_EMPTY_CEILING)

    if schema_status == "match":
        score += SCORE_SCHEMA_MATCH
        if count_tier == "empty":
            score += SCORE_SCHEMA_MATCH_EMPTY_BONUS
    elif schema_status == "mismatch":
        if schema_diff_count and schema_diff_count > 0:
            magnitude = min(schema_diff_count / _MISMATCH_FULL_PENALTY_AT, 1.0)
            fraction_of_penalty = (
                _MISMATCH_MIN_FRACTION
                + (1.0 - _MISMATCH_MIN_FRACTION) * magnitude)
        else:
            fraction_of_penalty = 1.0
        score += SCORE_SCHEMA_MISMATCH * fraction_of_penalty

    if is_generic_name:
        score += SCORE_GENERIC_NAME_PENALTY

    if match_type == "ALL":
        score += SCORE_MATCH_SCOPE_ALL_BONUS
    elif match_type == "SOME":
        score += SCORE_MATCH_SCOPE_SOME_PENALTY

    return max(0, min(100, round(score)))


def _fmt_service_ref(title, item_url, owner):
    t = title or "(untitled)"
    inner_pieces = []
    if item_url:
        inner_pieces.append(item_url)
    if owner:
        inner_pieces.append(f"owned by {owner}")
    ref = f"'{t}'"
    if inner_pieces:
        ref += f" ({' '.join(inner_pieces)})"
    return ref


def _fmt_service_list(services, limit=3):
    labeled = [_fmt_service_ref(t, url, own) for (t, _iid, url, own) in services]
    if len(labeled) <= limit:
        if len(labeled) == 1:
            return labeled[0]
        return ", ".join(labeled[:-1]) + f" and {labeled[-1]}"
    shown = ", ".join(labeled[:limit])
    return f"{shown}, and {len(labeled) - limit} more"


def human_summary(rec, layer_key, layer_name, geometry, spatial_ref,
                  key_to_services, score, count_tier, count_detail,
                  schema_status, schema_detail, is_generic_name, name_only,
                  feature_count):
    others = [(t, iid, url, own)
              for (iid, t, url, own) in key_to_services.get(layer_key, [])
              if iid != rec["item_id"]]
    who = _fmt_service_list(others) if others else "other service(s)"
    me_ref = _fmt_service_ref(rec["title"], rec["item_url"], rec["owner"])
    extra = f" ({len(others)} other services)" if len(others) > 1 else ""
    lead = (f"Service {me_ref} shares its \"{layer_name}\" layer with "
            f"{who}{extra}.")

    criteria = ["identical layer name"]
    if not name_only:
        criteria.append(f"same geometry ({geometry or 'n/a'})")
        criteria.append(f"same spatial reference ({spatial_ref or 'n/a'})")
    if len(criteria) > 1:
        crit_text = ", ".join(criteria[:-1]) + f", and {criteria[-1]}"
    else:
        crit_text = criteria[0]

    fc_here = "" if feature_count == "" else (f" This copy has "
                                              f"{feature_count} features.")

    if count_tier == "unknown":
        count_clause = "Feature counts could not be fully compared."
    else:
        count_clause = f"{count_detail[:1].upper()}{count_detail[1:]}."

    if schema_status == "match":
        schema_clause = f" Field schemas were also compared: {schema_detail}."
    elif schema_status == "mismatch":
        schema_clause = (f" Field schemas were compared and DIFFER "
                         f"({schema_detail}), which weighs against this "
                         f"being a true duplicate despite the other matches.")
    elif schema_status == "unknown" and count_tier in COUNT_TOP_TIER:
        schema_reason = (schema_detail
                         or "no readable field list was available for "
                            "comparison")
        schema_clause = (f" Field schemas could not be confirmed "
                         f"({schema_reason}), so this score does not reflect "
                         f"a schema check.")
    else:
        schema_clause = ""

    generic_clause = ""
    if is_generic_name:
        generic_clause = (f" Caveat: \"{layer_name}\" is a generic/default "
                          f"ArcGIS layer name that many unrelated services "
                          f"use coincidentally, so the name match alone is "
                          f"weaker evidence than a distinctive shared name "
                          f"would be; this has reduced the score below.")

    verdict = f" Overall duplicate-probability score: {score}/100."

    if rec["match_type"] == "ALL":
        scope = (" Note: EVERY layer in this service is duplicated "
                 "elsewhere, which is strong evidence this whole service is "
                 "a duplicated copy, not just a single shared layer -- the "
                 "score above has been increased to reflect that.")
    else:
        scope = (" Note: only SOME of this service's layers are "
                 "duplicated, so this shared layer is weaker evidence that "
                 "the whole SERVICE is a duplicate -- the score above has "
                 "been reduced to reflect that, even though this specific "
                 "layer may still be a strong match.")

    return (f"{lead} They match on {crit_text}.{fc_here} {count_clause}"
            f"{schema_clause}{generic_clause}{verdict}{scope}")


def main():
    args = parse_args()

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
             "custom-ca-bundle.", file=sys.stderr)
        sys.exit(2)

    finalize_config(
        args,
        required=("portal-url", "username", "keyring-service",
                  "output-dir", "smtp-server", "email-to", "email-from"),
    )

    max_email_mb = get_max_email_mb(args.target, args.config)

    ensure_output_dir(args.output_dir)

    timestamp = build_timestamp()
    platform = get_platform(args.portal_url)
    org_prefix = args.org_name_safe

    file_prefix = (f"{org_prefix}_{platform}_DUPLICATE_LAYERS"
                   if platform in ("PORTAL", "AGOL")
                   else f"{org_prefix}_{LOGGER_NAME}")

    log_file_path = os.path.join(
        args.output_dir, f"{file_prefix}_{timestamp}.log")
    logger = setup_logging(log_file_path, LOGGER_NAME)

    start_wallclock, start_perf = start_timer()

    # Running list of reasons this run is not authoritative. Non-empty => the
    # report is flagged INCOMPLETE in the log/email, but the process still
    # exits 0 so schedulers see a completed run.
    incomplete_reasons = []

    def flag_incomplete(reason):
        incomplete_reasons.append(reason)
        logger.warning("INCOMPLETE-RUN condition: %s", reason)

    logger.info("=" * 70)
    logger.info("%s - run started", LOGGER_NAME)
    logger.info("Org config: %s", args.config)
    logger.info("Org name: %s", args.org_name)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s",
                "TEST (first %d services)" % TEST_LIMIT if args.test else "FULL")
    logger.info("Match mode: %s",
                "name only" if args.name_only else "name + geometry + SR")
    logger.info("Feature count: %s", args.feature_count)
    logger.info("Schema check: %s", args.schema_check)
    logger.info("View detect: %s", args.view_detect)
    logger.info("Relationship check: %s", args.relationship_check)
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Workers: %d", args.workers)
    logger.info("Output dir: %s", args.output_dir)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Exclude group id(s): %s", args.exclude_group_id or "(none)")
    logger.info("=" * 70)

    if platform not in ("PORTAL", "AGOL"):
        flag_incomplete(
            f"platform could not be classified from portal-url "
            f"(got {platform!r}); Portal-style behavior is assumed.")
    if args.target == "agol" and platform != "AGOL":
        flag_incomplete(
            f"--target agol but portal-url classified as {platform}; "
            f"check the config/URL.")
    if args.target == "portal" and platform == "AGOL":
        flag_incomplete(
            "--target portal but portal-url classified as AGOL; "
            "check the config/URL.")

    subject_connect_fail = subject_line(
        SUBJECT_PREFIX + " (CONNECT FAILED)", timestamp, platform,
        org_name=args.org_name)

    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path,
            pool_connections=64, pool_maxsize=64, retry_total=2,
            logger=logger)
    except (ImportError, ValueError) as exc:
        logger.error("Could not build a secure session: %s",
                     redact_token(str(exc)))
        send_email(subject_connect_fail,
                   f"Run ABORTED. Could not establish TLS verification "
                   f"strategy '{args.tls_mode}': {redact_token(str(exc))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        raise SystemExit(1)

    session.max_redirects = 0
    logger.info("HTTP redirects disabled for this session to prevent token "
                "leakage via POST-redirect (307/308).")

    try:
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, session, logger=logger)
    except Exception as exc:
        logger.error("Could not run TLS handshake verification: %s",
                     redact_token(str(exc)))
        send_email(subject_connect_fail,
                   f"Run ABORTED. TLS handshake verification to "
                   f"{args.portal_url} could not run: "
                   f"{redact_token(str(exc))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        raise SystemExit(1)

    if not handshake_ok:
        logger.error(
            "Aborting: TLS handshake verification to %s FAILED (%s).",
            args.portal_url, redact_token(handshake_detail.get("error")))
        send_email(subject_connect_fail,
                   f"Run ABORTED. TLS handshake verification to "
                   f"{args.portal_url} failed: "
                   f"{redact_token(handshake_detail.get('error'))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        raise SystemExit(1)

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          verify_mode=mode, ca_bundle_path=ca_path)
    except Exception as exc:
        logger.error("Could not connect: %s", redact_token(str(exc)))
        body = (
            f"{LOGGER_NAME} FAILED to connect to {args.portal_url}.\n\n"
            f"Organization: {args.org_name}\n"
            f"Error: {redact_token(str(exc))}\n\n"
            + timing_block(start_wallclock, start_perf)
        )
        send_email(subject_connect_fail, body, [log_file_path],
                   args.smtp_server, args.email_to, args.email_from, logger)
        raise SystemExit(1)

    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
                type(token_provider).__name__, token_provider.is_present())

    excluded_ids, exclusion_ok = _resolve_exclusions(
        gis, args.exclude_group_id, logger)
    if not exclusion_ok:
        flag_incomplete(
            "one or more --exclude-group-id group(s) could not be resolved; "
            "their members were NOT excluded, so the report may include items "
            "that should have been filtered out.")

    tune_gis_session(gis, workers=args.workers)
    base_url = gis.url

    is_agol = (platform == "AGOL")
    org_id = get_org_id(gis, logger=logger) if is_agol else None
    if is_agol:
        logger.info("AGOL target: scoping the scan to org id %s.",
                    org_id or "(unresolved)")
        if not org_id:
            flag_incomplete(
                "AGOL org id could not be resolved; the hosted-feature-service "
                "search runs UNSCOPED and may include cross-organization / "
                "public items. Treat the result as NOT organization-limited.")

    logger.info("Enumerating hosted feature services (bucketed search) ...")
    fs_by_id = bucketed_hosted_fs(gis, base_url, session=session,
                                  logger=logger, org_only=is_agol,
                                  org_id=org_id)

    if is_agol and org_id:
        before = len(fs_by_id)
        fs_by_id = {
            iid: rec for iid, rec in fs_by_id.items()
            if (rec.get("orgId") if isinstance(rec, dict)
                else getattr(rec, "orgId", None)) in (None, org_id)
        }
        removed = before - len(fs_by_id)
        if removed:
            logger.info("Dropped %d out-of-org item(s) after enumeration.",
                        removed)

    all_ids = list(fs_by_id.keys())

    if excluded_ids:
        before = len(all_ids)
        all_ids = [iid for iid in all_ids if iid not in excluded_ids]
        logger.info("Excluded %d hosted feature service id(s) belonging to "
                    "the --exclude-group-id group(s) before resolving items.",
                    before - len(all_ids))

    total_found = len(all_ids)
    logger.info("Hosted feature services found (post-exclusion): %d",
                total_found)

    if args.test:
        all_ids = all_ids[:TEST_LIMIT]
        logger.info("TEST MODE: limiting the scan stages to the first %d "
                    "service(s) (discovery already enumerated the whole org).",
                    len(all_ids))

    logger.info("Resolving %d item(s) ...", len(all_ids))
    items, skipped = resolve_items(gis, all_ids, logger=logger)
    logger.info("Resolved %d item(s) (%d skipped).", len(items), skipped)
    n_unresolved = len(all_ids) - len(items)
    if n_unresolved > 0:
        flag_incomplete(
            f"{n_unresolved} of {len(all_ids)} item id(s) did not resolve to "
            f"an item and were dropped before scanning.")

    records, stats, key_names = collect_signatures(
        items, session, token_provider, base_url, args.name_only,
        is_agol, args.portal_url, args.workers, logger)

    logger.info("Layer signatures collected for %d service(s) "
                "(%d no URL, %d fetch error(s), %d no readable layers).",
                len(records), stats["no_url"], stats["fetch_errors"],
                stats["empty_sig"])
    if stats["fetch_errors"]:
        flag_incomplete(
            f"{stats['fetch_errors']} service root(s) could not be read; "
            f"their layers were not considered for duplicate detection.")
    if stats["tls_errors"]:
        flag_incomplete(
            f"{stats['tls_errors']} service-root failure(s) were TLS/certificate "
            f"errors, indicating a trust problem partway through the run.")

    if args.view_detect:
        view_source_ids, view_lookup_errors, view_tls_errors = \
            detect_hosted_views(records, session, token_provider, is_agol,
                                args.portal_url, args.workers, logger)
        n_view_layers_removed = strip_view_layers_from_signatures(
            records, view_source_ids, logger)
        n_views_confirmed = len(view_source_ids)
        if view_lookup_errors:
            flag_incomplete(
                f"{view_lookup_errors} hosted-view source lookup(s) failed; "
                f"some view/source pairs may not have been removed from "
                f"duplicate consideration.")
        if view_tls_errors:
            flag_incomplete(
                f"{view_tls_errors} view source-lookup failure(s) were "
                f"TLS/certificate errors.")
    else:
        view_source_ids = {}
        view_lookup_errors = 0
        n_view_layers_removed = 0
        n_views_confirmed = 0
        logger.info("Stage 0 (view detection) skipped (--no-view-detect).")

    dup_records = analyze_duplicates(records)
    logger.info("Duplicated layers before relationship check: %d service(s).",
                len(dup_records))

    if args.relationship_check:
        rel_map, rel_fail = fetch_relationships(
            dup_records, session, base_url, token_provider, args.workers,
            logger)
        dup_records, n_rel_dropped = drop_related_duplicates(
            dup_records, rel_map, logger)

        for rec in dup_records:
            rec["match_type"] = ("ALL" if rec["dup_layers"] == rec["signature"]
                                 else "SOME")
        if rel_fail["items_with_failures"]:
            flag_incomplete(
                f"{rel_fail['calls']} relationship call(s) failed across "
                f"{rel_fail['items_with_failures']} item(s); some legitimate "
                f"service/view pairs may not have been dropped.")
        if rel_fail["tls"]:
            flag_incomplete(
                f"{rel_fail['tls']} item(s) had TLS/certificate errors during "
                f"the relationship check.")
    else:
        n_rel_dropped = 0
        logger.info("Relationship check skipped (--no-relationship-check).")

    layer_service_count = {}
    for rec in dup_records:
        for key in rec["dup_layers"]:
            layer_service_count[key] = layer_service_count.get(key, 0) + 1

    by_group = group_titles(dup_records)
    key_to_services = build_key_to_services(dup_records)
    n_groups = len(by_group)
    n_all = sum(1 for r in dup_records if r["match_type"] == "ALL")
    n_some = sum(1 for r in dup_records if r["match_type"] == "SOME")
    logger.info("Services with duplicated layers: %d "
                "(%d ALL, %d SOME) across %d group(s).",
                len(dup_records), n_all, n_some, n_groups)

    if args.feature_count:
        counts, count_fail = fetch_feature_counts(
            dup_records, session, token_provider, args.workers, logger)
        count_tiers = count_comparison_tiers(
            dup_records, counts, COUNT_CLOSE_TOLERANCE)
        n_exact = sum(1 for t in count_tiers.values() if t[0] == "exact")
        n_divergent = sum(1 for t in count_tiers.values() if t[0] == "divergent")
        logger.info("Feature-count comparison: %d exact, %d divergent "
                    "(of %d duplicated-layer instance(s)).",
                    n_exact, n_divergent, len(count_tiers))
        if count_fail["failures"]:
            flag_incomplete(
                f"{count_fail['failures']} feature-count query/queries failed; "
                f"affected layers were scored as 'unknown' count.")
        if count_fail["tls"]:
            flag_incomplete(
                f"{count_fail['tls']} feature-count failure(s) were "
                f"TLS/certificate errors.")
    else:
        counts = {}
        count_tiers = {}
        logger.info("Feature counting skipped (--no-feature-count).")

    if args.schema_check and args.feature_count:
        schema_fields, schema_fail = fetch_schemas(
            dup_records, count_tiers, session, token_provider, args.workers,
            logger)
        schema_results = compare_schemas(
            dup_records, count_tiers, schema_fields, logger)
        if schema_fail["failures"]:
            flag_incomplete(
                f"{schema_fail['failures']} schema fetch(es) could not produce "
                f"a readable field list; those instances were scored without "
                f"a confirmed schema check.")
    else:
        schema_results = {}
        if not args.feature_count:
            logger.info("Schema check skipped (requires feature counting; "
                        "--no-feature-count was passed).")
        else:
            logger.info("Schema check skipped (--no-schema-check).")

    rows = []
    all_scores = []
    for rec in dup_records:
        for layer in sorted(rec["dup_layers"]):
            lname, lgeom, lsr = split_layer_key(
                layer, args.name_only, key_names.get(layer))
            fc = counts.get((rec["item_id"], layer))
            fc_val = "" if fc is None else fc

            if args.feature_count:
                tier_tuple = count_tiers.get(
                    (rec["item_id"], layer),
                    ("unknown", "feature counts were not compared"))
            else:
                tier_tuple = ("unknown", "feature counting was disabled for this run")

            count_tier = tier_tuple[0]
            count_detail = tier_tuple[1]
            spread_fraction = _tier_spread_fraction(tier_tuple)

            schema_status, schema_detail, _schema_fc, schema_diff_count = \
                schema_results.get((rec["item_id"], layer),
                                   ("unknown", "", None, 0))

            is_generic = _is_generic_layer_name(lname)
            score = calculate_duplicate_score(
                count_tier, schema_status, is_generic, rec["match_type"],
                count_spread_fraction=spread_fraction,
                schema_diff_count=schema_diff_count)
            all_scores.append(score)

            summary = human_summary(
                rec, layer, lname, lgeom, lsr, key_to_services,
                score, count_tier, count_detail, schema_status,
                schema_detail, is_generic, args.name_only, fc_val)

            rows.append({
                "title": rec["title"],
                "layer_name": lname,
                "summary": summary,
                "group_id": rec["group_id"],
                "match_type": rec["match_type"],
                "dup_probability": score,
                "feature_count": fc_val,
                "schema_status": schema_status,
                "generic_name": is_generic,
                "geometry": lgeom,
                "spatial_ref": lsr,
                "dup_service_count": layer_service_count.get(layer, 0),
                "service_layer_count": rec["layer_count"],
                "owner": rec["owner"],
                "item_id": rec["item_id"],
                "item_url": rec["item_url"],
            })

    if all_scores:
        logger.info(
            "Duplicate-probability score distribution: min=%d max=%d "
            "avg=%.1f, %d instance(s) >=80, %d instance(s) <30 (of %d total).",
            min(all_scores), max(all_scores),
            sum(all_scores) / len(all_scores),
            sum(1 for s in all_scores if s >= 80),
            sum(1 for s in all_scores if s < 30),
            len(all_scores))

    csv_path = os.path.join(
        args.output_dir, f"{file_prefix}_{timestamp}.csv")
    written = write_dict_csv(
        rows, csv_path, fieldnames=CSV_FIELDS, logger=logger,
        label="duplicate-layer row", force_values={"platform": platform})

    csv_paths = [written] if written else []
    attachments, size_note = gather_attachments(
        log_file_path, csv_paths, args.output_dir, logger,
        cap_mb=max_email_mb, label="CSV")

    run_incomplete = bool(incomplete_reasons)
    n_url_validated = next(_url_validation_count)

    mode_line = (f"TEST (first {TEST_LIMIT} services)"
                 if args.test else "FULL")
    match_line = ("name only" if args.name_only
                  else "name + geometry + spatial reference")
    if is_agol:
        scope_line = (f"org-scoped (orgid: {org_id})" if org_id
                      else "UNSCOPED (AGOL org id unresolved)")
    else:
        scope_line = "single org (Portal)"
    view_line = (f"{n_views_confirmed} confirmed view(s), "
                f"{n_view_layers_removed} (view, layer) instance(s) removed "
                f"from consideration, {view_lookup_errors} source-lookup "
                f"error(s)" if args.view_detect else "skipped")
    score_line = (f"min {min(all_scores)}, max {max(all_scores)}, "
                  f"avg {sum(all_scores)/len(all_scores):.1f} "
                  f"(0-100 scale)" if all_scores else "n/a (no duplicates found)")
    completeness_line = ("INCOMPLETE - results are NOT authoritative"
                         if run_incomplete else "complete")

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Target: %s", args.target)
    logger.info("Mode: %s", mode_line)
    logger.info("Completeness: %s", completeness_line)
    logger.info("Scope: %s", scope_line)
    logger.info("Match on: %s", match_line)
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Service/layer URLs validated: %d", n_url_validated)
    logger.info("Feature counting: %s", "on" if args.feature_count else "off")
    logger.info("Schema check: %s",
                "on" if (args.schema_check and args.feature_count) else "off")
    logger.info("View detection: %s", view_line)
    logger.info("Relationship check: %s",
                "on" if args.relationship_check else "off")
    logger.info("Related dropped: %d", n_rel_dropped)
    logger.info("Excluded group(s): %s", args.exclude_group_id or "(none)")
    logger.info("Hosted FS found: %d", total_found)
    logger.info("Services scanned: %d", len(records))
    logger.info("  - no URL: %d", stats["no_url"])
    logger.info("  - fetch errors: %d", stats["fetch_errors"])
    logger.info("  - no readable layers: %d", stats["empty_sig"])
    logger.info("Services w/ dupes: %d (%d ALL, %d SOME)",
                len(dup_records), n_all, n_some)
    logger.info("Duplicate groups: %d", n_groups)
    logger.info("Dup-probability: %s", score_line)
    if run_incomplete:
        logger.warning("This run is INCOMPLETE for the following reason(s):")
        for reason in incomplete_reasons:
            logger.warning("  - %s", reason)
    logger.info("=" * 70)

    body_lines = [
        f"{LOGGER_NAME} completed.",
        "",
    ]
    if run_incomplete:
        body_lines.append(
            "*** WARNING: THIS RUN IS INCOMPLETE - the report below is NOT an "
            "authoritative inventory. ***")
        body_lines.append(
            "One or more data-acquisition steps failed; some services or "
            "checks were not fully examined. Reason(s):")
        for reason in incomplete_reasons:
            body_lines.append(f"  - {reason}")
        body_lines.append("")
    body_lines.extend([
        f"Organization: {args.org_name}",
        f"Platform: {platform}",
        f"Target: {args.target}",
        f"Mode: {mode_line}",
        f"Completeness: {completeness_line}",
        f"Scope: {scope_line}",
        f"Match on: {match_line}",
        f"TLS mode: {args.tls_mode}",
        f"Service/layer URLs validated: {n_url_validated}",
        f"Feature counting: {'on' if args.feature_count else 'off'}",
        f"Schema check: {'on' if (args.schema_check and args.feature_count) else 'off'}",
        f"View detection: {view_line}",
        f"Relationship check: {'on' if args.relationship_check else 'off'}",
        f"Related dropped: {n_rel_dropped} duplicated-layer instance(s)",
        f"Excluded group(s): {args.exclude_group_id or '(none)'}",
        f"Hosted FS found: {total_found}",
        f"Services scanned: {len(records)}",
        f"  - no URL: {stats['no_url']}",
        f"  - fetch errors: {stats['fetch_errors']}",
        f"  - no readable layers: {stats['empty_sig']}",
        f"Services w/ dupes: {len(dup_records)} ({n_all} ALL, {n_some} SOME)",
        f"Duplicate groups: {n_groups}",
        f"Dup-probability: {score_line}",
        "",
    ])
    body_lines.append(
        f"Output CSV: {written}" if written
        else "Output CSV: (none - no duplicated-layer rows were written; "
             "note this can result from request failures, not only from a "
             "genuine absence of duplicates)")
    if size_note:
        body_lines.append(size_note)
    body_lines.append("")
    body_lines.append(timing_block(start_wallclock, start_perf))
    body = "\n".join(body_lines)

    subject_prefix = (SUBJECT_PREFIX + " (INCOMPLETE)"
                      if run_incomplete else SUBJECT_PREFIX)
    subject = subject_line(subject_prefix, timestamp, platform,
                           org_name=args.org_name)
    send_email(subject, body, attachments, args.smtp_server, args.email_to,
               args.email_from, logger)

    logger.info("%s finished (%s).", LOGGER_NAME, completeness_line)


def _resolve_exclusions(gis, exclude_group_id, logger):
    """Resolve --exclude-group-id, returning (excluded_ids, all_ok).

    all_ok is False if any requested group failed to resolve/read, so the
    caller can flag the run incomplete rather than silently under-excluding.
    """
    if not exclude_group_id:
        return set(), True

    requested = [g.strip() for g in str(exclude_group_id).split(",")
                 if g.strip()]
    excluded = resolve_excluded_item_ids(gis, exclude_group_id, logger=logger)

    all_ok = True
    for gid in requested:
        try:
            grp = gis.groups.get(gid)
        except Exception:
            grp = None
        if grp is None:
            all_ok = False
            logger.warning(
                "--exclude-group-id: group '%s' could not be resolved for "
                "the completeness check.", gid)
    return excluded, all_ok


if __name__ == "__main__":
    main()