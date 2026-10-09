#!/usr/bin/env python3
r"""Scan AGOL or ArcGIS Enterprise Portal for large feature-layer attachments.

Stage 1 finds hosted feature-service layers/tables with attachments. Stage 2
measures attachment sizes for EVERY attachment and flags those at or above
--threshold-mb in a dedicated "Over_Threshold" column.

Common use:
    python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6
    python SCAN_ATTACHMENTS_SIZE.py --config org2.config.toml --target agol --threshold-mb 10 --no-test
    python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --no-test --exclude-group-id GROUP_ID
    python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --tls-mode os-trust-store
    python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
"""

import argparse
import itertools
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Dict, List
from urllib.parse import urlparse

import requests

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_PROJECT_ROOT = os.path.realpath(os.path.dirname(_SCRIPT_DIR))
_PROJECT_ROOT_KEY = os.path.normcase(_PROJECT_ROOT)

sys.path[:] = [
    entry for entry in sys.path
    if os.path.normcase(os.path.realpath(entry or os.curdir)) != _PROJECT_ROOT_KEY
]
sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _utilities  # noqa: E402

from UTILITIES import (   # noqa: E402
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    get_org_id,
    tune_gis_session,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_target_arg,
    add_config_arg,
    add_exclude_group_arg,
    resolve_excluded_item_ids,
    filter_excluded,
    subject_line,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    HOSTED_FS_CLAUSE,
    MB,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)
from UTILITIES.restutils import (   # noqa: E402
    rest_search_page, DEFAULT_CAP, resolve_items,
)
from UTILITIES.bucketing import bucketed_search               # noqa: E402
from UTILITIES.arcgis_rest import (   # noqa: E402
    get_service_definition,
    iter_service_sublayers,
    get_sublayer_definition,
    list_object_ids,
    query_attachments,
    fill_missing_sizes,
    resolve_items_metadata,
    QA_OID_BATCH,
)

_EXPECTED_UTILITIES_DIR = os.path.realpath(
    os.path.join(_PROJECT_ROOT, "UTILITIES")
)
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_utilities, "__file__", ""))
)

if os.path.normcase(_ACTUAL_UTILITIES_DIR) != os.path.normcase(
        _EXPECTED_UTILITIES_DIR):
    raise ImportError(
        "Refusing to load UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected "
        f"{_EXPECTED_UTILITIES_DIR!r}."
    )

EMAIL_SUBJECT_PREFIX = "Attachment Size Scan"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

MAX_ITEMS = 2000000
TEST_MODE_ITEM_LIMIT = 500

DEFAULT_WORKERS = 12
REQUEST_TIMEOUT = 60
OID_PAGE_SIZE = 5000

ITEM_META_BATCH = 100

ATTACHMENT_FIELDNAMES = [
    "platform",
    "title", "item_id", "owner",
    "layer_title", "layer_id", "service_url",
    "Object_ID", "Attachment_Name", "Attachment_Size", "Over_Threshold",
]

# --- TLS mode CLI choices <-> VerifyMode -----------------------------------
_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

# Thread-safe counter of same-origin URL validations. Rather than logging a
# DEBUG line per validated URL (thousands per run), _validate_service_url()
# just increments this, and Stage 1 logs a single summary line at the end.
_url_validation_count = itertools.count()


# ==========================================================================
# Helpers: URL manipulation and validation
# ==========================================================================
def _sublayer_url(service_url, layer_id):
    if layer_id is None:
        return service_url.rstrip("/")
    return f"{service_url.rstrip('/')}/{layer_id}"


def _split_layer_url(layer_url):
    clean = layer_url.rstrip("/")
    m = re.search(r"/(\d+)$", clean)
    if m:
        return clean[:m.start()], int(m.group(1))
    return clean, None


# Esri-owned parent domain that ALL ArcGIS Online hosted content lives under.
# AGOL hosted feature services are served from services*.arcgis.com,
# tiles*.arcgis.com, etc. -- DIFFERENT hostnames from the org's
# <org>.maps.arcgis.com portal URL -- so a strict same-origin check is wrong
# for AGOL. We instead require the service host to be within .arcgis.com.
_AGOL_ALLOWED_HOST_SUFFIX = ".arcgis.com"
_AGOL_ALLOWED_HOST_APEX = "arcgis.com"


def _validate_service_url(item_url, portal_url, is_agol, logger):
    """
    Validate that an item's service URL is a trusted, HTTPS destination
    before any authenticated request (and thus the token) is sent to it.

    Prevents SSRF and token exfiltration by refusing to send the token to
    an untrusted host. The trust rule DIFFERS BY PLATFORM:

      * Enterprise Portal (is_agol False): STRICT same-origin. The service
        host:port must exactly match the portal host:port. Enterprise
        hosted services are served from the same web adaptor origin as the
        portal, so any other host is treated as untrusted.

      * ArcGIS Online (is_agol True): host must be within Esri's
        '.arcgis.com' domain. AGOL hosted feature services legitimately live
        on services*.arcgis.com / tiles*.arcgis.com / etc., which are
        DIFFERENT hostnames from the org's <org>.maps.arcgis.com portal URL.
        A strict same-origin check would reject essentially every real AGOL
        service, so instead we require the destination to be Esri-owned
        ('*.arcgis.com'). The token still cannot be sent anywhere outside
        arcgis.com.

    HTTPS is required in BOTH modes -- the token is never sent over plaintext.

    Parameters
    ----------
    item_url : str
        The service URL to validate (from an item's 'url' field).
    portal_url : str
        The authenticated portal URL (baseline for the Portal same-origin
        check; used only for diagnostics on AGOL).
    is_agol : bool
        True for ArcGIS Online, False for Enterprise Portal.
    logger : logging.Logger

    Raises
    ------
    ValueError
        If URL is malformed, uses non-HTTPS, or points at an untrusted host.
    """
    if not item_url or not isinstance(item_url, str):
        raise ValueError(f"item_url is not a non-empty string: {item_url!r}")

    # Parse both URLs
    try:
        item_parsed = urlparse(item_url if "://" in item_url else f"https://{item_url}")
        portal_parsed = urlparse(portal_url if "://" in portal_url else f"https://{portal_url}")
    except Exception as exc:
        raise ValueError(f"Could not parse URL: {exc}")

    # Enforce HTTPS (no plaintext token transmission) -- both platforms.
    if item_parsed.scheme.lower() != "https":
        raise ValueError(
            f"Service URL does not use HTTPS (scheme={item_parsed.scheme!r}); "
            f"refusing to send token over insecure connection")

    item_hostname = (item_parsed.hostname or "").lower()
    if not item_hostname:
        raise ValueError(f"Service URL has no resolvable hostname: {item_url!r}")

    if is_agol:
        # AGOL: destination must be Esri-owned (within .arcgis.com). This
        # covers services*.arcgis.com, tiles*.arcgis.com, <org>.maps.arcgis.com,
        # and any other Esri AGOL service subdomain.
        if not (item_hostname == _AGOL_ALLOWED_HOST_APEX
                or item_hostname.endswith(_AGOL_ALLOWED_HOST_SUFFIX)):
            raise ValueError(
                f"Service URL host {item_hostname!r} is not within Esri's "
                f"'{_AGOL_ALLOWED_HOST_APEX}' domain; refusing to send token "
                f"to untrusted host")
    else:
        # Enterprise Portal: STRICT same-origin (hostname + port).
        item_host = f"{item_hostname}:{item_parsed.port or 443}"
        portal_host = f"{(portal_parsed.hostname or '').lower()}:{portal_parsed.port or 443}"
        if item_host != portal_host:
            raise ValueError(
                f"Service URL is different-origin from Portal "
                f"(item={item_host}, portal={portal_host}); refusing to send "
                f"token to untrusted host")

    # Count validations instead of logging one line each; Stage 1 logs a
    # single summary count when it finishes (see itertools counter above).
    next(_url_validation_count)


# ==========================================================================
# Helpers: Output and reporting
# ==========================================================================
def _send_abort_email(subject, reason, log_path, start_wallclock, start_perf,
                      smtp_server, email_to, email_from, logger):
    """
    Send a consistent abort email with timing block.

    Parameters
    ----------
    subject : str
        Email subject line.
    reason : str
        Explanation of why the run aborted (will be in body).
    log_path : str
        Path to the log file to attach.
    start_wallclock, start_perf : from start_timer()
    smtp_server, email_to, email_from : email config
    logger : logging.Logger
    """
    body = f"Run ABORTED. {reason}\n\n" + timing_block(start_wallclock, start_perf)
    send_email(subject, body, [log_path], smtp_server, email_to,
               email_from, logger)


def _build_and_verify_session(tls_mode, ca_bundle_path, portal_url, logger):
    """
    Build a secure requests.Session and verify TLS handshake in one step.

    Disables redirect following for authenticated requests to prevent
    POST-redirect attacks that could leak tokens via 307/308 responses.

    Raises RuntimeError if handshake verification fails.

    Returns
    -------
    (session, mode) : (requests.Session, VerifyMode)
    """
    try:
        mode = _TLS_MODE_CHOICES[tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=ca_bundle_path,
            pool_connections=64, pool_maxsize=64, retry_total=2,
            logger=logger)
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            f"Could not build a secure session: {redact_token(str(exc))}")

    # Disable redirect following to prevent token leaks via POST-redirect.
    # HTTP 307/308 preserve POST body (including token). Callers must
    # explicitly re-request if a redirect is needed.
    session.max_redirects = 0
    logger.info(
        "HTTP redirects disabled for this session to prevent token leakage "
        "via POST-redirect attacks (307/308)")

    handshake_ok, handshake_detail = verify_tls_handshake(
        portal_url, session, logger=logger)
    if not handshake_ok:
        raise RuntimeError(
            f"TLS handshake verification to {portal_url} failed: "
            f"{redact_token(handshake_detail.get('error'))}")

    return session, mode


def _log_stage2_diagnostics(diag, threshold_mb, logger):
    """
    Log the Stage 2 diagnostic breakdown.

    Parameters
    ----------
    diag : dict
        Diagnostic counters from Stage 2.
    threshold_mb : int
        The threshold used.
    logger : logging.Logger
    """
    logger.info("-" * 70)
    logger.info("Stage 2 diagnostic breakdown:")
    logger.info("  layers with NO attachment rows      : %d   <-- "
                "query_attachments returned nothing", diag["layers_no_rows"])
    logger.info("  attachment rows enumerated (total)  : %d",
                diag["attachments_seen"])
    logger.info("  rows with size = 0 bytes            : %d",
                diag["zero_byte"])
    logger.info("  rows MISSING a size (listed, flagged Unknown) : %d   <-- "
                "size never resolved", diag["size_missing"])
    logger.info("  rows UNDER threshold (%d MB)         : %d",
                threshold_mb, diag["under_threshold"])
    logger.info("  rows AT/ABOVE threshold (flagged)   : %d",
                diag["over_threshold"])
    logger.info("-" * 70)


def _build_stage2_body(platform, org_name, timestamp, mode_label,
                       threshold_mb, total_count, oversize_count,
                       total_oversize_mb, savings_to_5mb_mb, attachment_csv,
                       errors_csv, total_tls_errors, start_wallclock,
                       start_perf):
    """
    Construct the email body for a normal completion.

    Sensitive operational metadata (usernames, config paths, email addresses)
    is minimized in email output to limit information disclosure; full details
    are available in attached log files for authorized recipients only.

    Returns
    -------
    str
        Email body text.
    """
    body = (
        f"Attachment Size Scan\n"
        f"Organization: {org_name}\n"
        f"Platform: {platform}\n"
        f"Run timestamp: {timestamp}\n"
        f"Mode: {mode_label}\n"
        f"Threshold (MB): {threshold_mb}\n\n"
        f"Total attachments catalogued: {total_count}\n"
        f"Oversize attachments (>= {threshold_mb} MB): {oversize_count}\n"
        f"Total oversize size: {total_oversize_mb:.2f} MB\n"
    )
    if savings_to_5mb_mb > 0:
        body += (f"Potential savings (if all reduced to 5 MB): "
                 f"{savings_to_5mb_mb:.2f} MB\n")
    body += "\n"
    if attachment_csv or errors_csv:
        body += "Reports:\n"
        if attachment_csv:
            body += (f"  - All attachments (oversize flagged): "
                     f"{attachment_csv}\n")
        if errors_csv:
            body += f"  - Errors: {errors_csv}\n"
    if total_tls_errors:
        body += (f"\nWARNING: {total_tls_errors} TLS/CERT verification "
                 f"failure(s) detected. See log for details. Results may be "
                 f"incomplete or untrustworthy.\n")
    body += f"\n{timing_block(start_wallclock, start_perf)}\n"
    body += "See the attached log file for full details, including configuration, "
    body += "diagnostics, and error information.\n"
    return body


# ==========================================================================
# Stage 1: Service and layer discovery
# ==========================================================================
def _resolve_org_id_or_abort(gis, logger):
    """
    Resolve the authenticated organization id.

    Organization scoping is MANDATORY for this inventory operation.
    Raises RuntimeError if org id cannot be determined.

    Returns
    -------
    str
        The organization id.

    Raises
    ------
    RuntimeError
        If org id cannot be resolved from the GIS connection.
    """
    org_id = get_org_id(gis, logger)
    if not org_id:
        raise RuntimeError(
            "ORGANIZATION SCOPING FAILED (CRITICAL). Could not resolve the "
            "authenticated organization id. Organization-scoped search is "
            "MANDATORY for this inventory operation; running unscoped would "
            "broaden the search to include cross-organization items and "
            "invalid results would be emailed. Refusing to proceed. "
            "Verify Portal/AGOL connectivity, user permissions, and the "
            "user's organization membership."
        )
    logger.info("Organization id resolved: %s", org_id)
    return org_id


def _body_has_attachments(j):
    """
    Parse a queryAttachments response body for a presence verdict.

    Returns:
      True  : confirmed has attachments
      False : confirmed NO attachments (trustworthy empty signal)
      None  : INCONCLUSIVE -- caller MUST fall through to the objectIds path

    Trust the fast-path only for explicit attachment counts. Ambiguous responses
    fall through to object-id enumeration rather than being treated as no attachments.
    """
    if not isinstance(j, dict) or "error" in j:
        return None

    # Explicit integer top-level count is authoritative in both directions.
    c = j.get("count")
    if isinstance(c, int):
        return c > 0

    groups = j.get("attachmentGroups")
    if isinstance(groups, list):
        # An absent/empty group list from the returnCountOnly fast path is
        # NOT a trustworthy "no" on Portal -> inconclusive, fall through.
        if not groups:
            return None

        saw_trustworthy_zero = False
        for g in groups:
            if not isinstance(g, dict):
                continue
            gc = g.get("count")
            if isinstance(gc, int):
                if gc > 0:
                    return True
                saw_trustworthy_zero = True
                continue
            infos = g.get("attachmentInfos")
            if isinstance(infos, list) and infos:
                return True
            # A group with neither a usable integer count nor a non-empty
            # attachmentInfos list tells us nothing reliable here.

        # Only call it a confirmed "no" if every group we could read gave an
        # explicit integer count of 0. Otherwise stay inconclusive so the
        # caller falls through to the objectIds enumeration.
        return False if saw_trustworthy_zero else None

    return None


def _layer_has_attachments(session, layer_url, token_provider, is_agol,
                          logger, portal_url):
    """
    Cheap presence check: does this layer have ANY attachments?
    Returns (has_attachments, reachable).

    Validates layer URL is same-origin as portal before making requests.

    Fast path (Portal): a lightweight returnCountOnly / where=1=1
    queryAttachments call. Its verdict is only trusted when it is
    DEFINITIVE (see _body_has_attachments). An inconclusive fast-path
    result falls through to the authoritative objectIds enumeration below
    -- this fall-through is what makes detection complete on Portal.

    For AGOL and the Portal fallback, enumerate objectIds and probe
    attachments in batches, stopping early after finding the first batch
    with at least one attachment rather than enumerating everything.
    """
    # Validate the layer URL before making any authenticated requests
    try:
        _validate_service_url(layer_url, portal_url, is_agol, logger)
    except ValueError as exc:
        logger.warning("Layer URL validation failed for %s: %s", layer_url, exc)
        return False, False

    qa_url = f"{layer_url.rstrip('/')}/queryAttachments"

    if not is_agol:
        try:
            r = session.post(qa_url, data={
                "f": "json", "token": token_provider.get(),
                "returnCountOnly": "true", "where": "1=1"},
                timeout=REQUEST_TIMEOUT)
            if r.ok:
                verdict = _body_has_attachments(r.json())
                if verdict is True:
                    return True, True
                if verdict is False:
                    return False, True
                # verdict is None -> INCONCLUSIVE; fall through to the
                # authoritative objectIds path below. (Do NOT treat an
                # empty/ambiguous fast-path body as "no attachments".)
        except requests.exceptions.TooManyRedirects:
            logger.warning("Portal queryAttachments returned redirect (possible attack); "
                          "refusing to follow")
            return False, True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Portal queryAttachments pre-check failed: %s", exc)
            # Fall through to objectIds path

    # AGOL, or Portal fallback: enumerate objectIds, but stop early
    # on first batch with at least one attachment.
    try:
        oids = list_object_ids(session, layer_url, token_provider.get(),
                               page_size=OID_PAGE_SIZE, timeout=REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not enumerate objectIds for %s: %s", layer_url, exc)
        return False, False

    if not oids:
        return False, True

    # Probe attachments in batches, stop at first hit
    reachable = False
    for i in range(0, len(oids), QA_OID_BATCH):
        chunk = oids[i:i + QA_OID_BATCH]
        try:
            r = session.post(qa_url, data={
                "f": "json", "token": token_provider.get(),
                "objectIds": ",".join(map(str, chunk))},
                timeout=REQUEST_TIMEOUT)
            if not r.ok:
                continue
            j = r.json()
            if isinstance(j, dict) and "error" in j:
                continue
            groups = j.get("attachmentGroups")
            if isinstance(groups, list):
                reachable = True
                for g in groups:
                    if isinstance(g, dict) and g.get("attachmentInfos"):
                        return True, True
        except requests.exceptions.TooManyRedirects:
            logger.warning("queryAttachments returned redirect (possible attack); "
                          "refusing to follow")
            continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("queryAttachments batch failed: %s", exc)
            continue

    return False, reachable


def _scan_service(item, session, token_provider, is_agol, logger, portal_url):
    """
    Worker: read one service definition and probe its layers for
    attachments. Returns (layer_rows, errors).

    Validates service URL before making any authenticated requests to prevent
    token exfiltration and SSRF attacks.

    Per-sublayer exception handling ensures that one failing sublayer
    errors is a list of error dicts for this service (one per failed sublayer).
    """
    svc_url = item["url"]
    layer_rows: List[Dict] = []
    errors: List[Dict] = []

    # VALIDATE service URL before ANY authenticated request
    try:
        _validate_service_url(svc_url, portal_url, is_agol, logger)
    except ValueError as exc:
        return layer_rows, [{
            "title": item.get("title"), "item_id": item["id"],
            "service_url": svc_url, "owner": item.get("owner"),
            "Error": f"Service URL validation failed: {str(exc)}",
        }]

    # NOW safe to make authenticated requests
    try:
        token = token_provider.get()
        defn = get_service_definition(session, svc_url, token, REQUEST_TIMEOUT)
    except requests.exceptions.TooManyRedirects:
        return layer_rows, [{
            "title": item.get("title"), "item_id": item["id"],
            "service_url": svc_url, "owner": item.get("owner"),
            "Error": "Service definition request returned redirect (possible attack); "
                     "refusing to follow",
        }]
    except requests.exceptions.SSLError as exc:
        return layer_rows, [{
            "title": item.get("title"), "item_id": item["id"],
            "service_url": svc_url, "owner": item.get("owner"),
            "Error": redact_token(f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}"),
        }]
    except Exception as exc:  # noqa: BLE001
        return layer_rows, [{
            "title": item.get("title"), "item_id": item["id"],
            "service_url": svc_url, "owner": item.get("owner"),
            "Error": redact_token(f"could not read service definition: {exc}"),
        }]

    if not defn:
        return layer_rows, [{
            "title": item.get("title"), "item_id": item["id"],
            "service_url": svc_url, "owner": item.get("owner"),
            "Error": "service definition is empty or malformed",
        }]

    sublayers = iter_service_sublayers(defn)

    for sub in sublayers:
        lid = sub["id"]
        name = sub["name"]

        # Per-sublayer exception handling: skip this sublayer on error
        try:
            token = token_provider.get()
            sub_defn = get_sublayer_definition(
                session, _sublayer_url(svc_url, lid), token, REQUEST_TIMEOUT)

            # Validate sub_defn before use
            if not isinstance(sub_defn, dict):
                logger.warning(
                    "Service %s layer %s: definition is not a dict (got %s); skipping",
                    svc_url, lid, type(sub_defn).__name__)
                continue

            has_prop = bool(sub_defn.get("hasAttachments", False))
            if not name:
                name = sub_defn.get("name")

            if not has_prop:
                continue

            lyr_url = _sublayer_url(svc_url, lid)
            present, _reachable = _layer_has_attachments(
                session, lyr_url, token_provider, is_agol, logger, portal_url)

            if not present:
                continue

            layer_rows.append({
                "title": item.get("title"),
                "item_id": item["id"],
                "service_url": lyr_url,
                "owner": item.get("owner"),
                "layer_title": name,
                "layer_id": lid,
            })

        except requests.exceptions.TooManyRedirects:
            errors.append({
                "title": item.get("title"), "item_id": item["id"],
                "service_url": svc_url, "layer_id": lid, "owner": item.get("owner"),
                "Error": f"Layer {lid} request returned redirect (possible attack); "
                         "refusing to follow",
            })
        except requests.exceptions.SSLError as exc:
            errors.append({
                "title": item.get("title"), "item_id": item["id"],
                "service_url": svc_url, "layer_id": lid, "owner": item.get("owner"),
                "Error": redact_token(
                    f"TLS/CERTIFICATE VERIFICATION FAILED (layer {lid}): {exc}"),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append({
                "title": item.get("title"), "item_id": item["id"],
                "service_url": svc_url, "layer_id": lid, "owner": item.get("owner"),
                "Error": redact_token(f"layer {lid} scan failed: {exc}"),
            })

    return layer_rows, errors


def search_hosted_feature_service_dicts(gis, base_url, session, max_items,
                                        logger):
    """
    Search for hosted feature service items.

    ORGANIZATION SCOPING IS MANDATORY. This function raises RuntimeError
    if the org id cannot be resolved. We will not proceed with an unscoped
    search that could return cross-organization items.

    Raises
    ------
    RuntimeError
        If org id cannot be determined.
    """
    org_id = _resolve_org_id_or_abort(gis, logger)

    type_clause = HOSTED_FS_CLAUSE
    type_clause = f'orgid:{org_id} AND {type_clause}'

    def search_fn(query, cap):
        return rest_search_page(gis, base_url, query, cap, session=session,
                                timeout=REQUEST_TIMEOUT)

    items_by_id = bucketed_search(
        search_fn, date_field="created", extra_clause=type_clause,
        cap=DEFAULT_CAP, logger=logger)

    ids = list(items_by_id.keys())[:max_items]
    logger.info("Resolving %d hosted feature service item(s) ...", len(ids))

    items, skipped = resolve_items(
        gis, ids, logger=logger,
        skip_quiet_substrings=("403", "not accessible", "canceled"))
    if skipped:
        logger.info("Skipped %d inaccessible item(s) (permission/subscription).",
                    skipped)

    out = []
    for itm in items:
        out.append({
            "id": itm.id,
            "title": itm.title,
            "owner": itm.owner,
            "url": (getattr(itm, "url", "") or "").rstrip("/"),
        })
    logger.info("Found %d hosted feature service item(s) to scan.", len(out))
    return out


def stage1_scan_layers_with_attachments(gis, base_url, session, token_provider,
                                        is_agol, platform, output_dir,
                                        timestamp, max_items, workers, logger,
                                        excluded_ids, org_prefix, portal_url):
    """
    Stage 1: Inventory layers/tables with attachments across all services.

    Calls search_hosted_feature_service_dicts(), which will raise RuntimeError
    if org scoping fails. This exception propagates to main() for abort handling.

    portal_url is passed to workers for URL origin validation.

    Returns a dict with layer_rows, errors_csv path, and stats.
    """
    items = search_hosted_feature_service_dicts(
        gis, base_url, session, max_items, logger)

    items = filter_excluded(
        items, excluded_ids, id_getter=lambda d: d.get("id"),
        logger=logger, label="hosted feature service item")

    layer_results: List[Dict] = []
    errors: List[Dict] = []
    stats = {"items_scanned": len(items), "services_with_attachments": 0,
             "layers_with_attachments": 0, "errors": 0, "tls_errors": 0}

    logger.info("Stage 1: reading %d service definition(s) and probing "
                "attachments with %d worker(s) ...", len(items), workers)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_scan_service, item, session, token_provider,
                             is_agol, logger, portal_url): item for item in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                lyr_rows, svc_errors = fut.result()
            except Exception as exc:  # noqa: BLE001
                lyr_rows, svc_errors = [], [{
                    "title": item.get("title"), "item_id": item["id"],
                    "service_url": item["url"], "owner": item.get("owner"),
                    "Error": redact_token(str(exc))}]

            done += 1
            if svc_errors:
                errors.extend(svc_errors)
                stats["errors"] += len(svc_errors)
                for err in svc_errors:
                    if "TLS/CERTIFICATE VERIFICATION FAILED" in str(
                            err.get("Error", "")):
                        stats["tls_errors"] += 1
            if lyr_rows:
                layer_results.extend(lyr_rows)
                stats["layers_with_attachments"] += len(lyr_rows)
                stats["services_with_attachments"] += 1
            if done % 100 == 0 or done == len(items):
                logger.info("  Stage 1 progress: %d / %d service(s), "
                            "%d layer(s) with attachments so far.",
                            done, len(items),
                            stats["layers_with_attachments"])

    if stats["tls_errors"]:
        logger.error(
            "*** %d of %d Stage 1 error(s) were TLS/CERTIFICATE "
            "VERIFICATION FAILURES, not ordinary request errors -- this "
            "indicates the server's certificate stopped being trusted "
            "partway through this run. Do not treat this run's results as "
            "complete or trustworthy; investigate the certificate before "
            "re-running.", stats["tls_errors"], stats["errors"])

    errors_csv = None
    if errors:
        errors_csv = os.path.join(
            output_dir, f"{org_prefix}_{platform}_SCAN_ERRORS_{timestamp}.csv")
        error_keys = sorted({key for e in errors for key in e})
        fieldnames = ["platform"] + error_keys
        write_dict_csv(errors, errors_csv, fieldnames=fieldnames,
                       logger=logger, label="error row",
                       force_values={"platform": platform})

    logger.info("Stage 1 diagnostics: services=%d services_with_attachments=%d "
                "layers_with_attachments=%d errors=%d",
                stats["items_scanned"], stats["services_with_attachments"],
                stats["layers_with_attachments"], stats["errors"])

    validated = next(_url_validation_count)
    logger.info("Stage 1: %d service/layer URL(s) validated same-origin as "
                "portal (%s).", validated, portal_url)

    return {
        "errors_csv": errors_csv,
        "layer_rows": layer_results,
        "stats": stats,
    }


# ==========================================================================
# Stage 2: Attachment size measurement
# ==========================================================================
def stage2_scan_attachment_sizes(base_url, session, token_provider,
                                 is_agol, platform, output_dir, timestamp,
                                 threshold_mb, layer_rows, workers, logger,
                                 org_prefix):
    """
    Stage 2: Measure EVERY attachment's size and flag those at or above the
    threshold.

    All attachments are written to the report (one row each). The
    "Over_Threshold" column is "Yes" for attachments at or above
    --threshold-mb, "No" below it, and "Unknown" for attachments whose size
    could not be resolved (those rows are still listed with a blank size).

    Totals (oversize count, total oversize MB, potential savings) are computed
    only from the at/above-threshold rows, using unrounded byte counts for
    accuracy; per-row sizes are rounded only for CSV output.

    Returns a dict with csv_path, counts, and totals.
    """
    threshold_bytes = int(threshold_mb) * MB

    empty_result = {"csv_path": None, "oversize_count": 0, "total_count": 0,
                    "total_oversize_mb": 0.0, "savings_to_5mb_mb": 0.0,
                    "tls_errors": 0}

    layers = [r for r in (layer_rows or []) if r.get("service_url")]
    if not layers:
        logger.info("Stage 2: no Stage 1 layers with attachments; nothing to do.")
        return empty_result

    seen = set()
    uniq = []
    for r in layers:
        u = r["service_url"].rstrip("/")
        if u not in seen:
            seen.add(u)
            uniq.append(r)

    inner_workers = max(2, workers // 4)

    diag = {
        "layers_no_rows": 0,
        "attachments_seen": 0,
        "zero_byte": 0,
        "size_missing": 0,
        "under_threshold": 0,
        "over_threshold": 0,
    }
    diag_lock = Lock()
    tls_error_count = [0]

    def scan_one(row):
        layer_url = row["service_url"].rstrip("/")
        _, lid = _split_layer_url(layer_url)
        token = token_provider.get()

        try:
            rows = query_attachments(session, layer_url, token, is_agol,
                                     timeout=REQUEST_TIMEOUT)
        except requests.exceptions.SSLError as exc:
            with diag_lock:
                tls_error_count[0] += 1
            logger.warning("Stage 2: TLS/CERTIFICATE VERIFICATION FAILED for "
                           "%s: %s", layer_url, redact_token(str(exc)))
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage 2: scan failed for %s: %s",
                           layer_url, redact_token(str(exc)))
            return []
        if not rows:
            with diag_lock:
                diag["layers_no_rows"] += 1
            return []
        rows = fill_missing_sizes(session, layer_url, token, rows,
                                  workers=inner_workers, timeout=REQUEST_TIMEOUT)

        title = row.get("title") or "N/A"
        owner = row.get("owner") or "N/A"
        item_id = row.get("item_id")
        layer_title = row.get("layer_title") or "N/A"
        layer_id = row.get("layer_id")
        layer_id = str(layer_id) if layer_id is not None else \
            (str(lid) if lid is not None else "N/A")

        out = []
        for a in rows:
            size_bytes = a.get("size")
            with diag_lock:
                diag["attachments_seen"] += 1
                if size_bytes is None:
                    diag["size_missing"] += 1
                elif size_bytes == 0:
                    diag["zero_byte"] += 1
                elif size_bytes >= threshold_bytes:
                    diag["over_threshold"] += 1
                else:
                    diag["under_threshold"] += 1

            # Flag: Unknown for unresolved size (row still listed, never
            # miscounted as over/under), Yes at/above threshold, else No.
            if size_bytes is None:
                over_flag = "Unknown"
            elif size_bytes >= threshold_bytes:
                over_flag = "Yes"
            else:
                over_flag = "No"

            # EVERY attachment is emitted now, not just oversize ones.
            out.append({
                "title": title,
                "item_id": item_id or "N/A",
                "owner": owner,
                "layer_title": layer_title,
                "layer_id": layer_id,
                "service_url": layer_url,
                "Object_ID": a.get("oid"),
                "Attachment_Name": a.get("name"),
                "Attachment_Size_Bytes": size_bytes,  # Keep unrounded for calc
                "Over_Threshold": over_flag,
            })
        return out

    logger.info("Stage 2: measuring ALL attachment sizes (flagging those at or "
                "above %d MB) for %d layer(s) with %d worker(s) ...",
                threshold_mb, len(uniq), workers)

    results: List[Dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(scan_one, row): row for row in uniq}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                rows = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Stage 2: scan failed for %s: %s",
                               row.get("service_url"), redact_token(str(exc)))
                rows = []
            done += 1
            if rows:
                results.extend(rows)
            if done % 25 == 0 or done == len(uniq):
                logger.info(
                    "  Stage 2 progress: %d / %d layer(s), %d "
                    "attachment(s) catalogued so far.",
                    done, len(uniq), len(results))

    if tls_error_count[0]:
        logger.error(
            "*** %d Stage 2 layer scan(s) failed with a TLS/CERTIFICATE "
            "VERIFICATION FAILURE, not an ordinary request error -- this "
            "indicates the server's certificate stopped being trusted "
            "partway through this run. Do not treat this run's results as "
            "complete or trustworthy; investigate the certificate before "
            "re-running.", tls_error_count[0])

    if not results:
        logger.info("Stage 2 complete: no attachments found on any layer.")
        empty_result["tls_errors"] = tls_error_count[0]
        return empty_result

    _log_stage2_diagnostics(diag, threshold_mb, logger)

    # Oversize subset drives the totals/savings figures.
    oversize_rows = [r for r in results if r.get("Over_Threshold") == "Yes"]

    # Calculate totals from BYTE counts (not rounded values) for accuracy.
    total_bytes = sum(r.get("Attachment_Size_Bytes", 0) or 0
                      for r in oversize_rows)
    total_oversize_mb = round(total_bytes / MB, 2)

    # Savings target: only report if attachments exceed 5 MB threshold.
    savings_to_5mb_mb = 0.0
    savings_target_bytes = 5 * MB
    for r in oversize_rows:
        size_bytes = r.get("Attachment_Size_Bytes", 0) or 0
        if size_bytes > savings_target_bytes:
            savings_to_5mb_mb += (size_bytes - savings_target_bytes) / MB
    savings_to_5mb_mb = round(savings_to_5mb_mb, 2)

    logger.info("Stage 2 totals: %d attachment(s) catalogued, %d at/above the "
                "%d MB threshold (%.2f MB total, %.2f MB reclaimable if "
                "reduced to 5 MB each).",
                len(results), len(oversize_rows), threshold_mb,
                total_oversize_mb, savings_to_5mb_mb)

    # Round individual sizes for CSV output (after all calculations). A None
    # size (unresolved) is written as blank rather than 0.
    for r in results:
        size_bytes = r.pop("Attachment_Size_Bytes")
        r["Attachment_Size"] = (round(size_bytes / MB, 2)
                                if size_bytes is not None else "")

    csv_path = write_dict_csv(
        results,
        os.path.join(output_dir,
                     f"{org_prefix}_{platform}_ATTACHMENT_SIZE_{timestamp}.csv"),
        fieldnames=ATTACHMENT_FIELDNAMES,
        logger=logger, label="attachment row",
        force_values={"platform": platform})

    return {
        "csv_path": csv_path,
        "oversize_count": len(oversize_rows),
        "total_count": len(results),
        "total_oversize_mb": total_oversize_mb,
        "savings_to_5mb_mb": savings_to_5mb_mb,
        "tls_errors": tls_error_count[0],
    }


# ==========================================================================
# CLI and main
# ==========================================================================
def positive_int(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(
            "--threshold-mb must be a positive whole integer.")
    return ivalue


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan AGOL or Portal for feature-layer attachments, list "
                    "every attachment, flag those at or above a threshold, "
                    "write CSV reports, then email them. "
                    "TLS verification strategy for this script's own direct "
                    "REST calls is explicit (--tls-mode); the arcgis SDK "
                    "connection (via connect_gis()) always verifies TLS "
                    "regardless.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (scans only 500 services). Pass\n"
            "--no-test to scan everything.\n\n"
            "Examples:\n"
            "  python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml "
            "--target portal --threshold-mb 6\n"
            "  python SCAN_ATTACHMENTS_SIZE.py --config org2.config.toml "
            "--target agol --threshold-mb 10 \\\n"
            "      --no-test --output-dir \"D:\\reports\"\n"
            "  python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml "
            "--target portal --threshold-mb 6 --tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    add_exclude_group_arg(parser)
    add_test_flag(
        parser,
        help_text="FULL mode: scan all hosted feature services (up to the cap). "
                  "If omitted, the script runs in test mode and scans only 500 "
                  "services.",
    )
    parser.add_argument(
        "--threshold-mb",
        required=True,
        type=positive_int,
        help="Attachment size threshold in whole MB (required, > 0). Every "
             "attachment is listed; those at or above this size are flagged "
             "'Yes' in the Over_Threshold column.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the attachment/"
             "layer REST calls -- NOT the arcgis SDK's own internal "
             "connection, which always verifies TLS via connect_gis()) "
             "verifies TLS certificates. 'standard' (default): certifi's "
             "bundled public CA list -- correct for AGOL and any Portal "
             "with a public CA cert. 'os-trust-store': verify against the "
             "operating system's native certificate store (requires the "
             "'truststore' package) -- use for an internal Enterprise "
             "Portal signed by an organizational CA already trusted at the "
             "OS level. 'custom-ca-bundle': verify against an explicit PEM "
             "file passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
             "custom-ca-bundle.", file=sys.stderr)
        sys.exit(2)

    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(args.target, args.config)

    platform = get_platform(args.portal_url)
    is_agol = (platform == "AGOL")
    per_run_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS
    org_prefix = args.org_name_safe

    ensure_output_dir(args.output_dir)

    log_file_path = os.path.join(
        args.output_dir,
        f"{org_prefix}_{platform}_attachment_size_scan_{timestamp}.log")

    logger = setup_logging(log_file_path, "attachment_size_scan")

    logger.info("=" * 70)
    logger.info("Attachment Size Scan - run started")
    logger.info("Org config: %s (org-name: %s)", args.config, args.org_name)
    logger.info("Config target: %s (from --target)", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST (500 services)" if args.test else "FULL")
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("TLS verification mode (this script's own REST calls): %s",
               args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Output dir: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s | from: %s", args.email_to, args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Threshold (MB): %d", args.threshold_mb)
    logger.info("Worker threads: %d", args.workers)
    logger.info("Exclude group id(s): %s", args.exclude_group_id or "(none)")
    logger.info("=" * 70)

    subject = subject_line(EMAIL_SUBJECT_PREFIX, timestamp, platform,
                           org_name=args.org_name)

    # Build and verify TLS session
    try:
        session, mode = _build_and_verify_session(
            args.tls_mode, args.ca_bundle_path, args.portal_url, logger)
    except RuntimeError as exc:
        _send_abort_email(subject, str(exc), log_file_path, start_wallclock,
                          start_perf, args.smtp_server, args.email_to,
                          args.email_from, logger)
        sys.exit(1)

    # Connect to GIS
    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          ca_bundle_path=ca_path)
    except Exception as exc:
        logger.error("Could not connect to portal: %s", redact_token(str(exc)))
        _send_abort_email(
            subject,
            f"Could not connect: {redact_token(str(exc))}",
            log_file_path, start_wallclock, start_perf, args.smtp_server,
            args.email_to, args.email_from, logger)
        sys.exit(1)

    # Token acquisition, centralized
    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
               type(token_provider).__name__, token_provider.is_present())

    excluded_ids = resolve_excluded_item_ids(
        gis, args.exclude_group_id, logger=logger)

    tune_gis_session(gis, args.workers, timeout=REQUEST_TIMEOUT)
    base_url = gis._portal.url

    # Stage 1: Inventory layers with attachments
    # This will raise RuntimeError if org scoping fails, caught below
    logger.info("STEP 1/2: Inventory layers/tables with attachments")
    try:
        stage1 = stage1_scan_layers_with_attachments(
            gis, base_url, session, token_provider, is_agol, platform,
            args.output_dir, timestamp, per_run_limit, args.workers, logger,
            excluded_ids, org_prefix, args.portal_url)
    except RuntimeError as exc:
        logger.error("FATAL: Stage 1 aborted due to org scoping failure: %s", exc)
        _send_abort_email(subject, str(exc), log_file_path, start_wallclock,
                          start_perf, args.smtp_server, args.email_to,
                          args.email_from, logger)
        sys.exit(1)

    # Stage 2: Measure attachment sizes
    logger.info("STEP 2/2: Measure all attachment sizes; flag those at or "
                "above %d MB", args.threshold_mb)
    stage2 = stage2_scan_attachment_sizes(
        base_url, session, token_provider, is_agol, platform,
        args.output_dir, timestamp, args.threshold_mb,
        stage1.get("layer_rows"), args.workers, logger, org_prefix)

    # Collect results
    attachment_csv = stage2.get("csv_path")
    total_count = stage2.get("total_count", 0)
    total_oversize_mb = stage2.get("total_oversize_mb", 0.0)
    savings_to_5mb_mb = stage2.get("savings_to_5mb_mb", 0.0)
    oversize_count = stage2.get("oversize_count", 0)
    total_tls_errors = (stage1.get("stats", {}).get("tls_errors", 0)
                        + stage2.get("tls_errors", 0))

    # Log summary
    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Org: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "FULL")
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Threshold (MB): %d", args.threshold_mb)
    logger.info("Attachment report: %s",
                attachment_csv or "(no attachments found)")
    logger.info("Total attachments catalogued: %d", total_count)
    logger.info("Oversize count (>= %d MB): %d", args.threshold_mb,
                oversize_count)
    logger.info("Total oversize size: %.2f MB", total_oversize_mb)
    if savings_to_5mb_mb > 0:
        logger.info("Savings if all -> 5 MB: %.2f MB", savings_to_5mb_mb)
    logger.info("Errors CSV: %s", stage1.get("errors_csv") or "(none)")
    if total_tls_errors:
        logger.info("  of which TLS/CERT verification failures: %d",
                   total_tls_errors)
    logger.info("=" * 70)

    # Build email
    mode_label = "TEST (500 services)" if args.test else "FULL"
    body = _build_stage2_body(
        platform, args.org_name, timestamp, mode_label,
        args.threshold_mb, total_count, oversize_count,
        total_oversize_mb, savings_to_5mb_mb, attachment_csv,
        stage1.get("errors_csv"), total_tls_errors, start_wallclock, start_perf)

    written = [attachment_csv, stage1.get("errors_csv")]
    attachments, note = gather_attachments(
        log_file_path, written, args.output_dir, logger, cap_mb=max_email_mb)
    body += note

    send_email(subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    logger.info("Success")
    sys.exit(0)


if __name__ == "__main__":
    main()