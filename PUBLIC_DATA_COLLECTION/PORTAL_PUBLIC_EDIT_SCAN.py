"""
PORTAL_PUBLIC_EDIT_SCAN.py

Scans AGOL for hosted feature services that allow PUBLIC (unauthenticated,
access="public") editing, and classifies exactly what the public can do:

    The Public Can Submit New Edits
    The Public Can View Edits
    The Public Can View and Update Edits
    The Public Can View and Delete Edits

Output CSV only includes services where Create, Update, or Delete is
publicly enabled (read-only public view services are excluded).

USAGE
-----
    python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from UTILITIES import (
    add_config_arg,
    add_target_arg,
    add_common_portal_args,
    add_output_dir_arg,
    add_email_args,
    add_test_flag,
    add_exclude_group_arg,
    finalize_config,
    get_max_email_mb,
    setup_logging,
    connect_gis,
    send_email,
    subject_line,
    build_timestamp,
    start_timer,
    timing_block,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    get_platform,
    bucketed_hosted_fs,
    resolve_excluded_item_ids,
    filter_excluded,
    tune_gis_session,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES.arcgis_rest import (
    get_service_definition,
    iter_service_sublayers,
    get_sublayer_definition,
)

SCRIPT_TAG = "PUBLIC_EDIT_SCAN"
REQUIRED_SETTINGS = ("portal-url", "username", "keyring-service",
                     "output-dir", "smtp-server", "email-to", "email-from")
TEST_COUNT = 25
WORKERS = 16

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan AGOL for hosted feature services publicly "
                    "shared with editing capabilities enabled.")
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    add_exclude_group_arg(parser)
    add_test_flag(
        parser,
        help_text="Run against ALL public feature services instead of just "
                  f"the first {TEST_COUNT} (test mode is the default).")
    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the service-"
             "definition and sublayer REST calls -- NOT the arcgis SDK's "
             "own internal connection, which always verifies TLS via "
             "connect_gis()) verifies TLS certificates. 'standard' "
             "(default): certifi's bundled public CA list -- correct for "
             "any Portal with a public CA cert. 'os-trust-store': verify "
             "against the operating system's native certificate store "
             "(requires the 'truststore' package) -- use for an internal "
             "Enterprise Portal signed by an organizational CA already "
             "trusted at the OS level. 'custom-ca-bundle': verify against "
             "an explicit PEM file passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")
    return parser.parse_args()


def classify_capabilities(capabilities):
    caps = {c.strip().lower() for c in (capabilities or "").split(",") if c.strip()}
    return {
        "The Public Can Submit New Edits": "create" in caps,
        "The Public Can View Edits": "query" in caps,
        "The Public Can View and Update Edits": "update" in caps,
        "The Public Can View and Delete Edits": "delete" in caps,
    }


def merge_flags(flags_a, flags_b):
    return {k: (flags_a.get(k, False) or flags_b.get(k, False))
            for k in flags_a}


def collect_effective_capabilities(session, url, token, root_defn):
    caps = [root_defn.get("capabilities", "")]
    for sub in iter_service_sublayers(root_defn):
        layer_id = sub.get("id")
        if layer_id is None:
            continue
        layer_url = f"{url.rstrip('/')}/{layer_id}"
        layer_defn = get_sublayer_definition(session, layer_url, token)
        layer_caps = layer_defn.get("capabilities")
        if layer_caps:
            caps.append(layer_caps)
    return ",".join(c for c in caps if c)


def main():
    args = parse_args()

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
             "custom-ca-bundle.", file=sys.stderr)
        sys.exit(2)

    finalize_config(args, required=REQUIRED_SETTINGS)

    if args.target != "agol":
        raise SystemExit("This script only scans AGOL (--target agol).")

    ts = build_timestamp()
    output_dir = args.output_dir
    ensure_output_dir(output_dir)

    log_path = os.path.join(
        output_dir, f"{args.org_name_safe}_{SCRIPT_TAG}_{args.target}_{ts}.log")
    logger = setup_logging(log_path, SCRIPT_TAG)

    start_wallclock, start_perf = start_timer()

    logger.info("=" * 70)
    logger.info("Public Edit Scan - run started")
    logger.info("=" * 70)
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: AGOL")
    logger.info("Mode: %s", "TEST" if args.test else "PRODUCTION")
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Output directory: %s", output_dir)
    logger.info("Log file: %s", log_path)
    logger.info("=" * 70)

    def fail(message):
        logger.error(message)
        sys.exit(1)

    try:
        logger.info("STEP 1/3: Establish verified TLS sessions and connect to AGOL")
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path,
            pool_connections=WORKERS, pool_maxsize=WORKERS, retry_total=3,
            logger=logger)
    except (ImportError, ValueError) as exc:
        fail(f"Could not establish TLS verification strategy "
            f"'{args.tls_mode}': {exc}")

    handshake_ok, handshake_detail = verify_tls_handshake(
        args.portal_url, session, logger=logger)
    if not handshake_ok:
        fail(f"TLS handshake verification to {args.portal_url} FAILED: "
            f"{handshake_detail.get('error')}")

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None

    gis = connect_gis(args.portal_url, args.username, args.keyring_service,
                      logger, verify_mode=mode, ca_bundle_path=ca_path)
    platform = get_platform(args.portal_url)
    session = tune_gis_session(gis, WORKERS)

    logger.info("STEP 2/3: Enumerate and filter hosted feature services")
    base_url = gis.url.rstrip("/")
    if base_url.lower().endswith("/home"):
        base_url = base_url[:-len("/home")]

    excluded_ids = resolve_excluded_item_ids(gis, args.exclude_group_id, logger)

    logger.info("Enumerating hosted feature services ...")
    items_by_id = bucketed_hosted_fs(
        gis, base_url, session=session, logger=logger,
        org_only=True)
    logger.info("Found %d hosted feature service item(s) total.",
               len(items_by_id))

    all_items = list(items_by_id.values())
    all_items = filter_excluded(
        all_items, excluded_ids, id_getter=lambda d: d.get("id"),
        logger=logger, label="hosted feature service item")

    public_items = [it for it in all_items
                    if str(it.get("access", "")).strip().lower() == "public"]
    logger.info("%d of %d item(s) are publicly shared (access=public).",
               len(public_items), len(all_items))

    if args.test:
        public_items = public_items[:TEST_COUNT]
        logger.info("TEST MODE: limited to first %d public item(s).",
                   len(public_items))

    logger.info("STEP 3/3: Analyze editing capabilities and write report")
    rows = []
    for idx, item_dict in enumerate(public_items, start=1):
        item_id = item_dict.get("id")
        title = item_dict.get("title", "")
        owner = item_dict.get("owner", "")
        url = item_dict.get("url", "")

        if not url:
            logger.warning("  [%d/%d] %s (%s): no service URL on item dict; "
                          "skipping.", idx, len(public_items), title, item_id)
            continue

        defn = get_service_definition(session, url, gis._con.token)
        capabilities = collect_effective_capabilities(
            session, url, gis._con.token, defn)

        flags = classify_capabilities(capabilities)
        if not (flags["The Public Can Submit New Edits"]
                or flags["The Public Can View and Update Edits"]
                or flags["The Public Can View and Delete Edits"]):
            continue

        rows.append({
            "PLATFORM": platform,
            "ITEM_ID": item_id,
            "TITLE": title,
            "OWNER": owner,
            "URL": url,
            "CAPABILITIES": capabilities,
            **flags,
        })

        if idx % 20 == 0:
            logger.info("  ... %d/%d public item(s) checked, %d flagged so "
                       "far.", idx, len(public_items), len(rows))

    logger.info("Scan complete: %d publicly-editable feature service(s) "
               "found.", len(rows))

    fieldnames = [
        "PLATFORM", "ITEM_ID", "TITLE", "OWNER", "URL", "CAPABILITIES",
        "The Public Can Submit New Edits",
        "The Public Can View Edits",
        "The Public Can View and Update Edits",
        "The Public Can View and Delete Edits",
    ]
    csv_name = f"{args.org_name_safe}_{SCRIPT_TAG}_{args.target}_{ts}.csv"
    csv_path = os.path.join(output_dir, csv_name)
    write_dict_csv(rows, csv_path, fieldnames=fieldnames, logger=logger,
                   label="publicly-editable feature service")

    max_email_mb = get_max_email_mb(args.target, args.config)
    attachments, note = gather_attachments(
        log_path, [csv_path] if rows else [], output_dir, logger,
        cap_mb=max_email_mb, label="CSV")

    timing = timing_block(start_wallclock, start_perf)
    subject = subject_line("Public Edit Scan", ts, platform, args.org_name)
    body = (
        f"AGOL public-edit scan complete for org '{args.org_name}'.\n\n"
        f"Publicly-shared feature services with editing capabilities: "
        f"{len(rows)}\n\n"
        f"{timing}\n"
        f"{note}"
    )
    send_email(subject, body, attachments, args.smtp_server, args.email_to,
              args.email_from, logger)

    logger.info("=" * 70)
    logger.info("%s complete", SCRIPT_TAG)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()