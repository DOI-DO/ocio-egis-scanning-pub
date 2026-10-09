#!/usr/bin/env python3
# ------------------------------------------------------------------------------
# Portions Copyright 2025 Esri
# Licensed under the Apache License, Version 2.0 (the "License");
# ------------------------------------------------------------------------------
r"""
DEPRECATED_CONTENT.py

Catalogs deprecated Configurable Web Apps and Portal AppBuilder Extensions in
ArcGIS Online or ArcGIS Enterprise, writes a CSV report, and emails the
results.

Uses the shared UTILITIES package (.\UTILITIES) for connection, logging,
email, timestamping, platform labeling, bucketed search, and configuration.
The auth token is held in a TokenProvider (UTILITIES.auth), never as a bare
string.

TLS verification: the one-time startup handshake check (--tls-mode) proves
the target host's certificate chain resolves before any scan traffic runs.
The real arcgis SDK connection used for the scan itself always verifies via
the standard CA list (certifi), or the same custom CA bundle when
--tls-mode custom-ca-bundle is used -- see connect_gis() in
UTILITIES.gis_connect for details.

Usage examples:
    python DEPRECATED_CONTENT.py --config org1.config.toml --target portal
    python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test
    python DEPRECATED_CONTENT.py --config org2.config.toml --target agol --no-test
    python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --tls-mode os-trust-store
    python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test --workers 16
    python DEPRECATED_CONTENT.py --config org2.config.toml --target agol --no-test --output-dir "D:\reports"
    python DEPRECATED_CONTENT.py --help
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from UTILITIES import (   # noqa: E402
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    bucketed_search,
    DEFAULT_CAP,
    get_org_id,
    tune_gis_session,
    epoch_to_str,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_target_arg,
    add_config_arg,
    subject_line,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)

EMAIL_SUBJECT_PREFIX = "Deprecated Content Scan"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

MAX_ITEMS = 2000000
TEST_MODE_ITEM_LIMIT = 250

DEFAULT_WORKERS = 10

DEPRECATED_PORTAL_APPS = [
    "apps/webappviewer/index.html", "apps/webappviewer3d/index.html",
    "apps/3DViz/index.html", "apps/AttachmentViewer/index.html",
    "apps/FilterGallery/index.html", "apps/CompareAnalysis/index.html",
    "apps/Compare3d/index.html", "apps/Directions/index.html",
    "apps/Editor/index.html", "apps/Profile/index.html",
    "apps/InteractiveFilter/index.html", "apps/ImageInterpretation/index.html",
    "apps/ImageryViewer/index.html", "apps/ImpactSummary/index.html",
    "apps/InformationLookup/index.html", "apps/InteractiveLegend/index.html",
    "apps/LayerShowcase/index.html", "apps/LocalPerspective/index.html",
    "apps/MapCarousel/index.html", "apps/MapsAndAppsGallery/index.html",
    "apps/Styler/index.html", "apps/MapTools/index.html",
    "apps/MapAndAppGallery/index.html", "apps/Media/index.html",
    "apps/MinimalGallery/index.html", "apps/Minimalist/index.html",
    "apps/PublicGallery/index.html", "apps/PublicInformation/index.html",
    "apps/3DInsetMap/index.html", "apps/SimpleViewer/index.html",
    "apps/3DScene/index.html", "apps/StoryMapBasic/index.html",
    "apps/Cascade/index.html", "apps/MapJournal/index.html",
    "apps/MapSeries/index.html", "apps/MapShortlist/index.html",
    "apps/StorytellingSwipe/index.html", "apps/MapTour/index.html",
    "apps/SummaryViewer/index.html", "apps/TimeAware/index.html",
    "apps/ZoneLookup/index.html", "apps/Viewer/index.html",
    "apps/Solutions/s2.html?appid=", "apps/GeoList/index.html",
    "apps/Elevations/index.html", "apps/View/index.html",
    "apps/Compare/index.html", "apps/CrowdsourceManager/index.html",
    "apps/CrowdsourcePolling/index.html", "apps/CrowdsourceReporter/index.html",
    "apps/GeoForm/index.html", "apps/ImageMask/index.html",
    "apps/ImageVisit/index.html",
]

_AGOL_EXCLUDED_PATTERNS = {
    "apps/webappviewer/index.html", "apps/webappviewer3d/index.html",
    "apps/mapsandappsgallery/index.html",
}

_DEPRECATED_PORTAL_LOWER = [a.strip().lower() for a in DEPRECATED_PORTAL_APPS]

_DEPRECATED_AGOL_RAW = [
    a.strip() for a in DEPRECATED_PORTAL_APPS
    if a.strip().lower() not in _AGOL_EXCLUDED_PATTERNS
]

_DEPRECATED_AGOL_LOWER = [a.lower() for a in _DEPRECATED_AGOL_RAW]

FIELDNAMES = [
    "platform",
    "item_id", "title", "item_type", "access", "app_url",
    "item_url", "numViews", "date_modified",
    "date_created",
    "contentStatus", "owner",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


def url_is_deprecated(app_url):
    if not app_url:
        return False
    low = app_url.lower()
    if "/apps" not in low:
        return False
    return any(a in low for a in _DEPRECATED_PORTAL_LOWER)


def url_is_deprecated_agol(app_url):
    if not app_url:
        return False
    low = app_url.lower()
    if "/apps" not in low:
        return False
    return any(a in low for a in _DEPRECATED_AGOL_LOWER)


def agol_has_reference(item, org_url, logger, skip_counter=None, skip_lock=None):
    try:
        app_data = item.get_data()
        if not isinstance(app_data, dict) or "values" not in app_data:
            return False
        values = app_data.get("values") or {}
        return any(k in ("webmap", "group") for k in values)
    except Exception as error:
        # A read failure is NOT the same as "no reference found"; tally it so
        # the run summary can report items that were skipped rather than
        # cleanly determined.
        logger.warning("Issue checking app data for '%s': %s",
                       getattr(item, "title", "?"), error)
        if skip_counter is not None and skip_lock is not None:
            with skip_lock:
                skip_counter[0] += 1
        return False


def _type_clause(platform):
    if platform == "AGOL":
        return '(type:"Web Mapping Application")'
    return ('(type:"Web Mapping Application" OR '
            'type:"AppBuilder Extension")')


def _search_agol(gis, max_items):
    return gis.content.search(
        query="", item_type="Web Mapping Application", max_items=max_items)


def _search_portal(gis, max_items):
    query = 'type:"Web Mapping Application" OR type:"AppBuilder Extension"'
    return gis.content.search(query=query, max_items=max_items)


def _bucketed_fallback(gis, platform, max_items, logger):
    logger.warning("Flat search hit the %d-result ceiling; falling back to "
                   "org-scoped date-range bucketing to enumerate beyond it ...",
                   DEFAULT_CAP)

    type_clause = _type_clause(platform)
    org_id = get_org_id(gis, logger)
    if org_id:
        logger.info("Scoping bucketed fallback to org id: %s", org_id)
        extra_clause = f'orgid:{org_id} AND {type_clause}'
    else:
        logger.warning("Could not determine org id; bucketed fallback will "
                       "NOT be org-scoped.")
        extra_clause = type_clause

    def search_fn(query, cap):
        return gis.content.search(query=query, max_items=cap)

    items_by_id = bucketed_search(
        search_fn, date_field="created", extra_clause=extra_clause,
        cap=DEFAULT_CAP, logger=logger)
    return list(items_by_id.values())[:max_items]


def search_candidate_items(gis, platform, max_items, logger):
    flat_cap = min(max_items, DEFAULT_CAP)
    logger.info("Flat search for up to %d item(s) (platform=%s) ...",
                flat_cap, platform)

    if platform == "AGOL":
        items = _search_agol(gis, flat_cap)
    else:
        items = _search_portal(gis, flat_cap)

    logger.info("Flat search returned %d item(s).", len(items))

    if max_items > DEFAULT_CAP and len(items) >= DEFAULT_CAP:
        items = _bucketed_fallback(gis, platform, max_items, logger)
        logger.info("Bucketed fallback returned %d item(s).", len(items))

    items = items[:max_items]
    logger.info("Found %d candidate item(s) to inspect (limit=%d).",
                len(items), max_items)
    return items


def _same_host(item_url, org_url):
    """
    True if item_url's hostname matches org_url's hostname (case-insensitive,
    exact host comparison). Replaces a plain `org_url in url` substring test,
    which a lookalike host such as 'org.maps.arcgis.com.evil.example' would
    pass as raw text.
    """
    try:
        item_host = (urlparse(item_url).hostname or "").lower()
        org_host = (urlparse(org_url).hostname or "").lower()
    except Exception:
        return False
    return bool(item_host) and item_host == org_host


def _is_agol_deprecated(item, org_url, logger, skip_counter=None,
                        skip_lock=None):
    url = getattr(item, "url", None)
    if not url:
        return False
    if "/apps" not in url or not _same_host(url, org_url):
        return False
    if not url_is_deprecated_agol(url):
        return False
    return agol_has_reference(item, org_url, logger,
                              skip_counter=skip_counter, skip_lock=skip_lock)


def _is_portal_deprecated(item):
    if getattr(item, "type", None) == "AppBuilder Extension":
        return True
    return url_is_deprecated(getattr(item, "url", None))


def _build_item_url(org_url, item_id):
    return f"{org_url}/home/item.html?id={item_id}"


def _extract_item_row(item, gis, org_url, logger):
    try:
        return {
            "item_id": item.id,
            "title": item.title,
            "item_type": item.type,
            "access": item.access,
            "app_url": item.url,
            "item_url": _build_item_url(org_url, item.id),
            "numViews": item.numViews,
            "date_modified": epoch_to_str(item.modified, fmt="%Y/%m/%d",
                                          utc=True),
            "date_created": epoch_to_str(item.created, fmt="%Y/%m/%d",
                                         utc=True),
            "contentStatus": getattr(item, "content_status", ""),
            "owner": item.owner,
        }
    except Exception as error:
        # Route the traceback through the redacting logger (exc_info=True)
        # instead of traceback.print_exc(), which writes the raw traceback
        # to stderr and bypasses the logging redaction boundary.
        logger.warning("Issue building row for '%s': %s",
                       getattr(item, "title", "?"), error, exc_info=True)
        return None


def _scan(items, candidates, gis, org_url, workers, logger, label):
    logger.info("%s: %d of %d candidate item(s) are deprecated.",
                label, len(candidates), len(items))

    results = []
    row_failures = 0
    lock = Lock()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_extract_item_row, it, gis, org_url, logger): it
                   for it in candidates}
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                row = fut.result()
                if row:
                    with lock:
                        results.append(row)
                else:
                    # _extract_item_row returned None -- a deprecated item
                    # whose row could not be built. Counted here in the single
                    # collector thread, so no lock is needed for the tally.
                    row_failures += 1
            except Exception as error:
                logger.warning("Worker error: %s", error)
                row_failures += 1
            if i % 100 == 0 or i == len(candidates):
                logger.info("%s scan progress: %d / %d item(s), %d row(s) "
                            "so far.", label, i, len(candidates), len(results))
    return results, row_failures


def scan_agol(items, gis, org_url, workers, logger):
    logger.info("AGOL: filtering %d item(s) for deprecated app URLs + "
                "webmap/group references ...", len(items))

    candidates = []
    lock = Lock()
    read_failures = [0]
    read_lock = Lock()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_is_agol_deprecated, it, org_url, logger,
                             read_failures, read_lock): it
                   for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                if fut.result():
                    with lock:
                        candidates.append(it)
            except Exception as error:
                logger.warning("AGOL filter error on '%s': %s",
                               getattr(it, "title", "?"), error)
                with read_lock:
                    read_failures[0] += 1

    rows, row_failures = _scan(items, candidates, gis, org_url, workers,
                               logger, "AGOL")
    rows.sort(key=lambda r: (r.get("numViews") or 0), reverse=True)
    return rows, read_failures[0], row_failures


def scan_portal(items, gis, org_url, workers, logger):
    candidates = [it for it in items if _is_portal_deprecated(it)]
    rows, row_failures = _scan(items, candidates, gis, org_url, workers,
                               logger, "Portal")
    return rows, 0, row_failures


def parse_args():
    parser = argparse.ArgumentParser(
        description="Catalog deprecated Configurable Web Apps (and, on "
                    "Portal, AppBuilder Extensions) in AGOL or Portal, write "
                    "a CSV report, then email it. TLS verification strategy "
                    "for this script's own direct REST calls is explicit "
                    "(--tls-mode); the real arcgis SDK connection always "
                    "verifies via the standard CA list (see module "
                    "docstring for why).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (first 250 items). Pass --no-test to "
            "scan everything.\n\n"
            "Examples:\n"
            "  python DEPRECATED_CONTENT.py --config org1.config.toml "
            "--target portal\n"
            "  python DEPRECATED_CONTENT.py --config org2.config.toml "
            "--target agol --no-test\n"
            "  python DEPRECATED_CONTENT.py --config org1.config.toml "
            "--target portal --tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    add_test_flag(
        parser,
        help_text="FULL mode: scan all matching items. If omitted, the script "
                  "runs in test mode and inspects only the first 250 items.",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How the ONE-TIME STARTUP HANDSHAKE CHECK (proving the target "
             "host's certificate chain resolves, before any scan traffic) "
             "verifies TLS. 'standard' (default): certifi's bundled public "
             "CA list -- correct for AGOL and any Portal with a "
             "commercially-issued cert. 'os-trust-store': verify the "
             "startup handshake against the operating system's native "
             "certificate store (requires the 'truststore' package) -- "
             "useful to confirm an internal/organizational CA chain "
             "resolves. 'custom-ca-bundle': verify the startup handshake "
             "against an explicit PEM file passed via --ca-bundle-path. "
             "IMPORTANT: regardless of this setting, the REAL arcgis SDK "
             "connection used for the actual scan ALWAYS verifies via the "
             "standard CA list (or --ca-bundle-path, if --tls-mode is "
             "custom-ca-bundle) -- 'os-trust-store' is never applied to "
             "the real connection. If your organization's CA is not "
             "already in certifi's bundle, use --tls-mode custom-ca-bundle "
             "with an exported CA .pem file instead of os-trust-store.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. When --tls-mode is "
             "custom-ca-bundle, this same file is used for BOTH the "
             "startup handshake check AND the real arcgis SDK connection.")
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
    per_run_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS
    org_prefix = args.org_name_safe

    ensure_output_dir(args.output_dir)

    log_file_path = os.path.join(
        args.output_dir,
        f"{org_prefix}_{platform}_deprecated_content_{timestamp}.log")

    logger = setup_logging(log_file_path, "deprecated_content_scan")
    logger.info("=" * 70)
    logger.info("Deprecated Content Scan - run started")
    logger.info("Org config: %s", args.config)
    logger.info("Org name: %s", args.org_name)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST (first 250 items)" if args.test else "FULL")
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("Output dir: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s", args.email_to)
    logger.info("Email from: %s", args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Worker threads: %d", args.workers)
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("=" * 70)

    subject = subject_line(EMAIL_SUBJECT_PREFIX, timestamp, platform,
                           org_name=args.org_name)

    try:
        handshake_mode = _TLS_MODE_CHOICES[args.tls_mode]
        handshake_session = build_secure_session(
            handshake_mode, ca_bundle_path=args.ca_bundle_path, logger=logger)
    except (ImportError, ValueError) as exc:
        logger.error("Could not build a secure session: %s",
                     redact_token(str(exc)))
        send_email(subject,
                   f"Run ABORTED. Could not establish TLS verification "
                   f"strategy '{args.tls_mode}': {redact_token(str(exc))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    # A missing/unreadable/malformed CA bundle can raise while the SSL
    # context is being built, before verify_tls_handshake() reaches its own
    # socket try/except. Catch it here and convert to the same (False,
    # detail) shape so the abort path below sends an email instead of dying
    # with a bare traceback.
    try:
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, handshake_session, logger=logger)
    except Exception as exc:
        handshake_ok = False
        handshake_detail = {"error": redact_token(str(exc))}
    if not handshake_ok:
        logger.error(
            "Aborting: TLS handshake verification to %s FAILED (%s).",
            args.portal_url, handshake_detail.get("error"))
        send_email(subject,
                   f"Run ABORTED. TLS handshake verification to "
                   f"{args.portal_url} failed: "
                   f"{redact_token(str(handshake_detail.get('error')))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    if handshake_mode is VerifyMode.CUSTOM_CA_BUNDLE:
        real_mode = VerifyMode.CUSTOM_CA_BUNDLE
        real_ca_path = args.ca_bundle_path
    else:
        real_mode = VerifyMode.STANDARD_CA
        real_ca_path = None

    try:
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          verify_mode=real_mode, ca_bundle_path=real_ca_path)
    except Exception as exc:
        logger.error("Could not connect to portal: %s", redact_token(str(exc)))
        send_email(subject,
                   f"Run ABORTED. Could not connect: {redact_token(str(exc))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
                type(token_provider).__name__, token_provider.is_present())

    tune_gis_session(gis, args.workers)

    org_url = args.portal_url.rstrip("/")
    if org_url.lower().endswith("/home"):
        org_url = org_url[: -len("/home")]
    logger.info("Org URL for URL membership tests: %s", org_url)

    logger.info("STEP 1/2: Searching for candidate items")
    items = search_candidate_items(gis, platform, per_run_limit, logger)

    logger.info("STEP 2/2: Inspecting items for deprecated content")
    if platform == "AGOL":
        rows, read_failures, row_failures = scan_agol(
            items, gis, org_url, args.workers, logger)
    else:
        rows, read_failures, row_failures = scan_portal(
            items, gis, org_url, args.workers, logger)

    csv_path = write_dict_csv(
        rows,
        os.path.join(args.output_dir,
                     f"{org_prefix}_{platform}_DEPRECATED_CONTENT_{timestamp}.csv"),
        fieldnames=FIELDNAMES, logger=logger, label="row",
        force_values={"platform": platform})

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "FULL")
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Items inspected: %d", len(items))
    logger.info("Deprecated rows: %d", len(rows))
    if platform == "AGOL":
        logger.info("Items skipped (app-data read errors): %d", read_failures)
    logger.info("Deprecated items skipped (row build errors): %d",
                row_failures)
    logger.info("Report CSV: %s", csv_path or "(none)")
    logger.info("=" * 70)

    skipped_note = ""
    if platform == "AGOL" and read_failures:
        skipped_note += (f"Items skipped (app-data read errors): "
                         f"{read_failures}\n")
    if row_failures:
        skipped_note += (f"Deprecated items skipped (row build errors): "
                         f"{row_failures}\n")
    if skipped_note:
        skipped_note = (
            "\nWARNING: some items could not be fully processed and were "
            "skipped. This run may be incomplete:\n" + skipped_note)

    body = (
        f"Deprecated Content Scan\n"
        f"Organization: {args.org_name}\n"
        f"Platform: {platform}\n"
        f"Run timestamp: {timestamp}\n"
        f"Mode: {'TEST (first 250 items)' if args.test else 'FULL'}\n"
        f"TLS mode: {args.tls_mode}\n"
        f"Username: {args.username}\n\n"
        f"Items inspected: {len(items)}\n"
        f"Deprecated rows: {len(rows)}\n"
        f"Report CSV: {csv_path or '(none)'}\n"
        f"{skipped_note}\n"
        f"{timing_block(start_wallclock, start_perf)}\n"
        f"See the attached log file and CSV for full detail.\n"
    )

    attachments, note = gather_attachments(
        log_file_path, [csv_path], args.output_dir, logger,
        cap_mb=max_email_mb)
    body += note

    send_email(subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    logger.info("Success")
    sys.exit(0)


if __name__ == "__main__":
    main()