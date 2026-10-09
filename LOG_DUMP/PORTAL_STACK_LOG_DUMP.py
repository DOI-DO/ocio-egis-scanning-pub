#!/usr/bin/env python3
r"""
PORTAL_STACK_LOG_DUMP.py

Discovers the live Portal / Hosting Server / Data Store / federated-server
machine list for a Portal deployment, and copies their logs to either a
local folder (robocopy) or Azure Blob Storage (azcopy). Log paths are
self-discovered per machine; an undiscoverable path is skipped and reported
rather than guessed. Every call this script makes -- both to Portal/AGOL and
its own direct discovery requests -- uses explicit TLS certificate
verification (see --tls-mode); nothing is ever sent with verification
disabled. The auth token is held in a TokenProvider (UTILITIES.auth) and
never printed, logged, or otherwise displayed.

Two run types, selected by --sync-mode:
  * Dated/scoped dump (default; requires --scope) -- a one-shot pull for one
    category or all of them, isolated into its own timestamped folder/prefix.
  * Standing mirror (--sync-mode mirror) -- always covers every category, no
    date filter, keeps the destination caught up without deleting anything.

Azure Blob authentication defaults to managed identity, so no SAS credential
is placed in an AzCopy process command line. Workload identity and Azure CLI
authentication are also supported. SAS is an explicit legacy fallback and is
accepted only when it is short-lived, HTTPS-only, container-scoped, and
least-privilege for the requested operation.

USAGE EXAMPLES
--------------
    # Dated dump, File destination, interactively prompted date filter, all categories
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root C:\Temp --scope all

    # Dated dump, File destination, previous 7 days, Portal only
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogDumps --scope portal --filter-mode previous-n-days 7

    # Dated dump, File destination, explicit date range, Hosting only
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogDumps --scope hosting --filter-mode --range-start 20260801 --range-end 20260828

    # Dated dump, Blob destination, previous-day (7-day) lookback, federated sites only
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --scope federated --filter-mode previous-day --destination blob

    # Override the Blob account/container from the command line
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --scope portal --filter-mode previous-day --destination blob --storage-account AZStorageAccountName --container logdump

    # Standing nightly mirror, File destination
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogMirror --sync-mode mirror

    # Standing nightly mirror, Blob destination (managed identity is default)
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob

    # Explicit legacy SAS fallback (strict maximum remaining lifetime: 24h)
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob --blob-auth-mode sas --sas-max-hours 24

    # Test mode (plan + connectivity check only, no copy/sync) -- works with any of the above
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob --test-mode

    # Non-default TLS verification strategy for this script's own discovery calls
    python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root C:\Temp --scope all --tls-mode os-trust-store
"""

import argparse
import logging
import os
import sys
import threading


def _log_lingering_threads(logger):
    """Diagnostic: log any thread or child process still alive at exit."""
    try:
        others = [t.name for t in threading.enumerate()
                 if t is not threading.current_thread()]
        if others:
            logger.info("Non-main thread(s) still alive at exit: %s",
                       others)
        else:
            logger.info("No lingering non-main threads at exit.")
    except Exception:
        pass

    try:
        import psutil
        me = psutil.Process(os.getpid())
        children = me.children(recursive=True)
        if children:
            logger.info(
                "Child process(es) still alive at exit: %s",
                [(c.pid, c.name()) for c in children])
        else:
            logger.info("No lingering child processes at exit.")
    except ImportError:
        logger.info(
            "psutil not installed -- skipping child-process check "
            "(pip install psutil to enable it).")
    except Exception as exc:
        logger.info("Could not check child processes: %s", exc)


def _clean_exit(logger, code=0):
    """
    Explicit hard exit via os._exit() (unlike sys.exit(), it terminates
    immediately regardless of any non-daemon threads still running).
    """
    _log_lingering_threads(logger)
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(code)


# Path bootstrap: allow this script to live in a subfolder that is NOT the
# project root, while UTILITIES\ stays at the project root.
def _bootstrap_utilities_path(max_levels_up=4):
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = here
    for _ in range(max_levels_up + 1):
        if os.path.isdir(os.path.join(candidate, "UTILITIES")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:  # reached filesystem root
            break
        candidate = parent
    return None


_bootstrap_utilities_path()

from UTILITIES import (
    add_config_arg,
    add_common_portal_args,
    add_output_dir_arg,
    finalize_config,
    setup_logging,
    connect_gis,
    build_timestamp,
    start_timer,
    timing_block,
    # token handling + explicit TLS verification for this script's own
    # direct requests.Session
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES import config as _config
from UTILITIES.stack_discovery import discover_stack, short_hostname, looks_like_fqdn
from UTILITIES.path_discovery import resolve_log_paths
from UTILITIES.robocopy_utils import (
    prompt_date_filter,
    date_filter_from_args,
    build_date_filter_opts,
    copy_group,
    ROBOCOPY_SUCCESS_MAX,
)
from UTILITIES.azcopy_utils import (
    AZCOPY_AUTH_MODES,
    AZCOPY_AUTH_SAS,
    DEFAULT_AZCOPY_AUTH_MODE,
    DEFAULT_SAS_MAX_HOURS,
    MAX_SAS_MAX_HOURS,
    normalize_azcopy_auth_mode,
    validate_azcopy_auth_environment,
    validate_sas_token,
    get_azcopy_path,
    get_blob_sas_token,
    validate_azcopy_exe,
    maxage_days_to_include_after,
    copy_group_to_blob,
    copy_group_to_blob_mirror,
    test_blob_connection,
)

SCRIPT_LABEL = "PORTAL_STACK_LOG_DUMP"

REQUIRED_SETTINGS = ("portal-url", "username", "keyring-service",
                    "output-dir")

DEFAULT_DOMAIN_SUFFIX = "enterprise.com"

CATEGORY_PATHS_REFERENCE_ONLY = {
    "Portal": [
        r"arcgisportal\logs",
        r"Program Files\Portal\framework\runtime\tomcat\logs",
    ],
    "Hosting": [
        r"arcgisserver\logs",
        r"Program Files\Server\framework\runtime\tomcat\logs",
    ],
    "DataStore": [
        r"Datastore\logs",
        r"Program Files\DataStore\framework\runtime\tomcat\logs",
    ],
}

LOGDUMP_BLOB_CONTAINER_KEY = "logdump-blob-container"
LOGDUMP_BLOB_ACCOUNT_KEY = "logdump-blob-account"
LOGMIRROR_BLOB_CONTAINER_KEY = "logmirror-blob-container"
LOGMIRROR_BLOB_ACCOUNT_KEY = "logmirror-blob-account"
AZCOPY_AUTH_MODE_KEY = "azcopy-auth-mode"
AZCOPY_MANAGED_IDENTITY_CLIENT_ID_KEY = "azcopy-managed-identity-client-id"
AZCOPY_SAS_MAX_HOURS_KEY = "azcopy-sas-max-hours"

# Dated-dump "previous-day" looks back 7 days (not 1) so a missed run
# doesn't lose a day before 30-day source retention rolls it off.
PREVIOUS_DAY_LOOKBACK_DAYS = 7

VALID_DESTINATIONS = ("file", "blob")
VALID_SCOPES = ("portal", "hosting", "datastore", "federated", "all")
VALID_SYNC_MODES = ("mirror",)

# --- TLS mode CLI choices <-> VerifyMode -----------------------------------
_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


# ==========================================================================
# CLI
# ==========================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Discover the live Portal/Hosting/Data Store machine "
                    "list for a deployment, and either pull a dated, "
                    "scoped log dump or run a standing nightly mirror. TLS "
                    "verification strategy for this script's own direct "
                    "REST calls is explicit (--tls-mode); the arcgis SDK "
                    "connection (via connect_gis()) always verifies TLS "
                    "regardless."
    )
    add_config_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)

    parser.add_argument(
        "--domain-suffix", default=DEFAULT_DOMAIN_SUFFIX,
        help="Domain suffix appended to each short machine name to rebuild "
             "the FQDN used in UNC source paths (default: %(default)s).",
    )
    parser.add_argument(
        "--dest-root", default=None,
        help="Root directory for this run's timestamped subfolder. "
             "Defaults to the org config's 'output-dir' if omitted.",
    )
    parser.add_argument(
        "--destination", default="file", type=str.lower,
        choices=list(VALID_DESTINATIONS),
        help="'file': robocopy to --dest-root. 'blob': azcopy to Azure "
             "Blob Storage (copy for a dated dump, sync for mirror). "
             "Container/account may be supplied on the command line or "
             "read from config based on --sync-mode.",
    )

    blob_auth_group = parser.add_argument_group(
        "Azure Blob destination and authentication")
    blob_auth_group.add_argument(
        "--storage-account",
        default=None,
        help="Azure Storage account name for --destination blob. Overrides "
             "the logdump-blob-account/logmirror-blob-account value in the "
             "org config.",
    )
    blob_auth_group.add_argument(
        "--container",
        default=None,
        help="Azure Blob container name for --destination blob. Overrides "
             "the logdump-blob-container/logmirror-blob-container value in "
             "the org config.",
    )
    blob_auth_group.add_argument(
        "--blob-auth-mode",
        choices=list(AZCOPY_AUTH_MODES),
        default=None,
        help="Authentication for --destination blob. Defaults to the "
             "'azcopy-auth-mode' config value, or managed-identity when "
             "unset. SAS is an explicit legacy fallback; it is never used "
             "automatically.",
    )
    blob_auth_group.add_argument(
        "--managed-identity-client-id",
        default=None,
        help="Optional client ID for a user-assigned managed identity. "
             "Defaults to 'azcopy-managed-identity-client-id' in the org "
             "config. Omit for a system-assigned identity.",
    )
    blob_auth_group.add_argument(
        "--sas-max-hours",
        type=float,
        default=None,
        help="Maximum remaining/signed SAS lifetime when --blob-auth-mode "
             "sas is explicitly selected. Defaults to config key "
             "'azcopy-sas-max-hours', then 24 hours; hard maximum is 168 "
             "hours.",
    )
    parser.add_argument(
        "--scope", required=False, default=None, type=str.lower,
        choices=list(VALID_SCOPES),
        help="Machine category to copy from. REQUIRED for a dated dump "
             "(--sync-mode not passed). Not used with --sync-mode mirror.",
    )
    parser.add_argument(
        "--sync-mode", default=None, type=str.lower,
        choices=list(VALID_SYNC_MODES),
        help="Omit for a dated/scoped dump. Pass 'mirror' for a standing "
             "nightly mirror (no --scope, no date filter).",
    )
    parser.add_argument(
        "--filter-mode", nargs="*", default=None, metavar="MODE [N]",
        help="Dated-dump date filter: 'previous-day'; 'previous-n-days N'; "
             "or no value with --range-start/--range-end. Omit to be "
             "prompted. Not accepted with --sync-mode mirror.",
    )
    parser.add_argument(
        "--range-start", default=None,
        help="Start date, YYYYMMDD (dated dump only).",
    )
    parser.add_argument(
        "--range-end", default=None,
        help="End date, YYYYMMDD (dated dump only).",
    )
    parser.add_argument(
        "--test-mode", action="store_true",
        help="Print the plan (and, in Blob mode, run the connection "
             "check) without copying/syncing anything.",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for stack/log-path "
             "discovery -- NOT the arcgis SDK's own internal connection, "
             "which always verifies TLS via connect_gis()) verifies TLS "
             "certificates. 'standard' (default): certifi's bundled "
             "public CA list -- correct for any Portal with a public CA "
             "cert. 'os-trust-store': verify against the operating "
             "system's native certificate store (requires the "
             "'truststore' package) -- use for an internal Enterprise "
             "Portal signed by an organizational CA already trusted at "
             "the OS level. 'custom-ca-bundle': verify against an "
             "explicit PEM file passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")

    args = parser.parse_args()

    args.sync_mode_explicit = args.sync_mode is not None
    is_mirror = args.sync_mode == "mirror"

    if is_mirror:
        if args.scope is not None and args.scope != "all":
            parser.error(
                "--scope is not used with --sync-mode mirror; omit it, "
                f"or pass --scope all. Got --scope {args.scope}.")
        args.scope = "all"

        if args.filter_mode is not None or args.range_start or args.range_end:
            parser.error(
                "--filter-mode/--range-start/--range-end are not used "
                "with --sync-mode mirror.")
    else:
        if args.scope is None:
            parser.error(
                "--scope is required for a dated/scoped dump. Pass one "
                "of: " + ", ".join(VALID_SCOPES) + ", or pass "
                "--sync-mode mirror instead.")

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        parser.error(
            "--ca-bundle-path is required when --tls-mode is "
            "custom-ca-bundle.")

    args.dest_root_defaulted = False

    return args


def _resolve_date_filter(args):
    """Resolve/prompt for the dated-dump date filter. Never called for mirror."""
    if args.filter_mode is None:
        filter_info = prompt_date_filter()
        if filter_info.get("mode") == "AGE" and filter_info.get("maxage") == 1:
            filter_info = date_filter_from_args(
                "PREVIOUS_N", previous_n_days=PREVIOUS_DAY_LOOKBACK_DAYS)
        return filter_info

    tokens = args.filter_mode

    if len(tokens) == 0:
        if not args.range_start or not args.range_end:
            print("--range-start and --range-end are both required when "
                 "--filter-mode is passed with no value (range mode).",
                 file=sys.stderr)
            raise SystemExit(2)
        return date_filter_from_args("RANGE", range_start=args.range_start,
                                     range_end=args.range_end)

    mode = tokens[0].strip().lower()

    if mode == "previous-day":
        return date_filter_from_args(
            "PREVIOUS_N", previous_n_days=PREVIOUS_DAY_LOOKBACK_DAYS)

    if mode == "previous-n-days":
        if len(tokens) < 2:
            print("--filter-mode previous-n-days requires a day count, "
                 "e.g. --filter-mode previous-n-days 7.", file=sys.stderr)
            raise SystemExit(2)
        try:
            n = int(tokens[1])
        except ValueError:
            print(f"--filter-mode previous-n-days: {tokens[1]!r} is not a "
                 f"valid integer day count.", file=sys.stderr)
            raise SystemExit(2)
        return date_filter_from_args("PREVIOUS_N", previous_n_days=n)

    print(f"--filter-mode: unrecognized value {mode!r}. Expected "
         f"'previous-day', 'previous-n-days <N>', or no value (for a "
         f"date range with --range-start/--range-end).", file=sys.stderr)
    raise SystemExit(2)


def _resolve_effective_filter_info(args, logger):
    """None means 'no date filter -- let the copy/sync mechanism decide'."""
    if args.sync_mode == "mirror":
        logger.info(
            "Sync mode: MIRROR -- no date filter. File destination relies "
            "on robocopy's default unchanged-file skip; Blob destination "
            "uses azcopy sync --delete-destination=false.")
        return None

    return _resolve_date_filter(args)


def _include_after_for_blob(filter_info, logger):
    """Dated-dump Blob path only -- translate the filter into --include-after."""
    mode = filter_info.get("mode")
    if mode == "AGE":
        return maxage_days_to_include_after(filter_info["maxage"])
    if mode == "RANGE":
        include_after = maxage_days_to_include_after(filter_info["maxage"])
        if logger is not None:
            logger.warning(
                "--destination blob: a Date Range filter (%s to %s) was "
                "selected. azcopy has no MINAGE equivalent, so Blob mode "
                "approximates this range using only its START date as the "
                "--include-after cutoff (%s).",
                filter_info.get("range_start"), filter_info.get("range_end"),
                include_after)
        return include_after
    if logger is not None:
        logger.warning("--destination blob: unrecognized date-filter mode "
                      "%r; no --include-after filter will be applied.", mode)
    return None


# ==========================================================================
# Console topology report
# ==========================================================================
def print_stack_report(stack, logger):
    print("\n" + "=" * 78)
    print("PORTAL STACK SERVER INVENTORY")
    print("=" * 78)

    all_fqdns = set()
    role_by_fqdn = {}

    print(f"\nPORTAL: {stack['portal']['name']}")
    print(f"  URL: {stack['portal']['url']}")
    if stack["portal"]["primary"] or stack["portal"]["standby"]:
        if stack["portal"]["primary"]:
            f = stack["portal"]["primary"]
            print(f"  PRIMARY Machine FQDN: {f}")
            if looks_like_fqdn(f):
                all_fqdns.add(f)
                role_by_fqdn[f] = "PORTAL-PRIMARY"
        for f in stack["portal"]["standby"]:
            print(f"  STANDBY Machine FQDN: {f}")
            if looks_like_fqdn(f):
                all_fqdns.add(f)
                role_by_fqdn[f] = "PORTAL-STANDBY"
    else:
        for f in stack["portal"]["fqdns"]:
            print(f"  Machine FQDN: {f}")
            if looks_like_fqdn(f):
                all_fqdns.add(f)

    if stack["hosting_server"]["url"]:
        print(f"\nHOSTING SERVER")
        print(f"  Admin URL: {stack['hosting_server']['url']}")
        for f in stack["hosting_server"]["fqdns"]:
            print(f"  Machine FQDN: {f}")
            if looks_like_fqdn(f):
                all_fqdns.add(f)
                role_by_fqdn[f] = "SITE-HOSTING"
    else:
        print("\nHOSTING SERVER: not found among federated servers")

    if stack["data_store"]["items"]:
        print(f"\nDATA STORE ({len(stack['data_store']['items'])} "
              f"registered relational data store item(s)):")
        for item in stack["data_store"]["items"]:
            print(f"  - {item['name']}")
            if item["primary"]:
                print(f"      PRIMARY: {item['primary']}")
                if looks_like_fqdn(item["primary"]):
                    all_fqdns.add(item["primary"])
                    role_by_fqdn[item["primary"]] = "DATASTORE-PRIMARY"
            else:
                print(f"      PRIMARY: (not reported)")
            if item["standby"]:
                for s in item["standby"]:
                    print(f"      STANDBY: {s}")
                    if looks_like_fqdn(s):
                        all_fqdns.add(s)
                        role_by_fqdn[s] = "DATASTORE-STANDBY"
            else:
                print(f"      STANDBY: none (standalone data store)")
    else:
        print("\nDATA STORE: not found / could not be resolved")

    print(f"\nFEDERATED SERVERS ({len(stack['federated_servers'])}):")
    for srv in stack["federated_servers"]:
        site_label = srv.get("site_name") or "?"
        print(f"  - site={site_label}  {srv['name']}  (admin: {srv['admin_url']})")
        for f in srv["fqdns"]:
            print(f"      Machine FQDN: {f}")
            if looks_like_fqdn(f):
                all_fqdns.add(f)
                role_by_fqdn[f] = f"SITE-{site_label.upper()}"

    print("\n" + "-" * 78)
    print(f"ALL UNIQUE SERVER FQDNs IN THE STACK ({len(all_fqdns)}):")
    print("-" * 78)
    for f in sorted(all_fqdns):
        role = role_by_fqdn.get(f)
        if role:
            print(f"  {f}   [{role}]")
        else:
            print(f"  {f}")
    print("=" * 78 + "\n")

    logger.info("Total unique server FQDNs found: %d", len(all_fqdns))
    for f in sorted(all_fqdns):
        role = role_by_fqdn.get(f)
        if role:
            logger.info("  FQDN: %s [%s]", f, role)
        else:
            logger.info("  FQDN: %s", f)


# ==========================================================================
# Machine-list extraction
# ==========================================================================
def _category_machines(stack):
    def _short_list(fqdns):
        seen = set()
        out = []
        for f in fqdns:
            s = short_hostname(f)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "Portal": _short_list(stack["portal"]["fqdns"]),
        "Hosting": _short_list(stack["hosting_server"]["fqdns"]),
        "DataStore": _short_list(stack["data_store"]["fqdns"]),
    }


def _federated_site_machines(stack):
    def _short_list(fqdns):
        seen = set()
        out = []
        for f in fqdns:
            s = short_hostname(f)
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    sites = {}
    for srv in stack["federated_servers"]:
        site_name = srv.get("site_name")
        if site_name == "HOSTING":
            continue
        label = site_name or srv.get("name") or "UnknownSite"
        machines = _short_list(srv.get("fqdns") or [])
        if not machines:
            continue
        existing = sites.setdefault(label, [])
        for m in machines:
            if m not in existing:
                existing.append(m)
    return sites


def _scope_category_matcher(scope):
    scope = scope.lower()
    if scope == "all":
        return lambda category: True
    if scope == "portal":
        return lambda category: category == "Portal"
    if scope == "hosting":
        return lambda category: category == "Hosting"
    if scope == "datastore":
        return lambda category: category == "DataStore"
    if scope == "federated":
        return lambda category: category not in ("Portal", "Hosting",
                                                  "DataStore")
    return lambda category: False


def _filter_plan_by_scope(plan, scope, logger=None):
    matcher = _scope_category_matcher(scope)
    kept = [entry for entry in plan if matcher(entry[0])]

    if logger is not None:
        dropped = len(plan) - len(kept)
        if scope == "all":
            logger.info("Scope: ALL -- no COPY PLAN entries excluded.")
        else:
            excluded_categories = sorted({entry[0] for entry in plan
                                         if not matcher(entry[0])})
            logger.info(
                "Scope: %s -- %d of %d COPY PLAN entr%s kept, %d excluded "
                "(excluded categories: %s).", scope.upper(), len(kept),
                len(plan), "y" if len(plan) == 1 else "ies", dropped,
                ", ".join(excluded_categories) if excluded_categories
                else "none")
    return kept


# ==========================================================================
# Blob authentication settings
# ==========================================================================
def _resolve_blob_auth_settings(args, logger):
    """Resolve identity-first AzCopy auth settings from CLI/config."""
    raw_mode = args.blob_auth_mode or _config.get_config_value(
        AZCOPY_AUTH_MODE_KEY, args.target, path=args.config
    ) or DEFAULT_AZCOPY_AUTH_MODE
    try:
        args.blob_auth_mode = normalize_azcopy_auth_mode(raw_mode)
    except ValueError as exc:
        logger.error("Invalid Blob authentication mode: %s", exc)
        return False

    raw_client_id = (
        args.managed_identity_client_id
        or _config.get_config_value(
            AZCOPY_MANAGED_IDENTITY_CLIENT_ID_KEY,
            args.target,
            path=args.config,
        )
    )
    args.managed_identity_client_id = (
        str(raw_client_id).strip() if raw_client_id else None
    )

    raw_max_hours = args.sas_max_hours
    if raw_max_hours is None:
        raw_max_hours = _config.get_config_value(
            AZCOPY_SAS_MAX_HOURS_KEY, args.target, path=args.config
        )
    if raw_max_hours is None:
        raw_max_hours = DEFAULT_SAS_MAX_HOURS
    try:
        args.sas_max_hours = float(raw_max_hours)
    except (TypeError, ValueError):
        logger.error(
            "Invalid %s value %r; expected a number greater than 0 and no "
            "more than %g.",
            AZCOPY_SAS_MAX_HOURS_KEY,
            raw_max_hours,
            MAX_SAS_MAX_HOURS,
        )
        return False
    if not (0 < args.sas_max_hours <= MAX_SAS_MAX_HOURS):
        logger.error(
            "%s must be greater than 0 and no more than %g hours; got %s.",
            AZCOPY_SAS_MAX_HOURS_KEY,
            MAX_SAS_MAX_HOURS,
            args.sas_max_hours,
        )
        return False

    env_ok, env_detail = validate_azcopy_auth_environment(
        args.blob_auth_mode,
        managed_identity_client_id=args.managed_identity_client_id,
    )
    if not env_ok:
        logger.error("Blob authentication environment is not ready: %s",
                     env_detail)
        return False

    logger.info("AzCopy Blob authentication mode: %s", args.blob_auth_mode)
    if args.blob_auth_mode == "managed-identity":
        logger.info(
            "Managed identity: %s (no Blob credential will be placed in "
            "the AzCopy command line).",
            ("user-assigned client id " + args.managed_identity_client_id)
            if args.managed_identity_client_id
            else "system-assigned",
        )
    elif args.blob_auth_mode == "workload-identity":
        logger.info(
            "Workload identity variables validated (no SAS credential will "
            "be placed in the AzCopy command line)."
        )
    elif args.blob_auth_mode == "azure-cli":
        logger.info(
            "AzCopy will use the Azure CLI identity cache (no SAS credential "
            "will be placed in the AzCopy command line)."
        )
    else:
        logger.warning(
            "LEGACY SAS MODE EXPLICITLY SELECTED. AzCopy requires the SAS in "
            "its destination argument, so local administrators, EDR, or "
            "process-creation auditing may observe it. Use managed/workload "
            "identity to eliminate that exposure. SAS will be rejected "
            "unless it is HTTPS-only, container-scoped, least-privilege, "
            "and expires within %.1f hour(s).",
            args.sas_max_hours,
        )
    return True


# ==========================================================================
# Blob destination helpers
# ==========================================================================
def _blob_config_keys(sync_mode):
    if sync_mode == "mirror":
        return LOGMIRROR_BLOB_CONTAINER_KEY, LOGMIRROR_BLOB_ACCOUNT_KEY
    return LOGDUMP_BLOB_CONTAINER_KEY, LOGDUMP_BLOB_ACCOUNT_KEY


def _resolve_blob_container_and_account(args, logger):
    """Resolve Blob destination with CLI values taking precedence."""
    container_key, account_key = _blob_config_keys(args.sync_mode)

    cli_container = str(args.container).strip() if args.container else None
    cli_account = (
        str(args.storage_account).strip() if args.storage_account else None
    )
    container = cli_container or _config.get_config_value(
        container_key, args.target, path=args.config
    )
    account = cli_account or _config.get_config_value(
        account_key, args.target, path=args.config
    )

    ok = True
    if not container:
        logger.error(
            "No --container value was supplied and no '%s' setting was "
            "found in the [%s] or [common] section of %s -- required for "
            "--destination blob with --sync-mode %s.",
            container_key, args.target, args.config,
            args.sync_mode or "(dated dump)")
        ok = False
    if not account:
        logger.error(
            "No --storage-account value was supplied and no '%s' setting "
            "was found in the [%s] or [common] section of %s -- required "
            "for --destination blob with --sync-mode %s.",
            account_key, args.target, args.config,
            args.sync_mode or "(dated dump)")
        ok = False
    if not ok:
        return None, None

    container_source = "--container" if cli_container else container_key
    account_source = "--storage-account" if cli_account else account_key
    logger.info(
        "Blob destination resolved: account=%s (source: %s), "
        "container=%s (source: %s), run type=%s.",
        account, account_source, container, container_source,
        args.sync_mode or "dated dump"
    )
    return container, account


def _resolve_blob_connection(args, logger, context_label):
    logger.info("")
    logger.info("=" * 60)
    logger.info(context_label)
    logger.info("=" * 60)

    azcopy_path = get_azcopy_path(args.target, args.config, logger=logger)
    ok, err = validate_azcopy_exe(azcopy_path, logger=logger)
    if not ok:
        logger.error("%s FAILED at azcopy.exe validation: %s", context_label,
                     err)
        return None, None, None, None

    container, account = _resolve_blob_container_and_account(args, logger)
    if not container or not account:
        logger.error("%s FAILED at container/account resolution -- see "
                     "errors above.", context_label)
        return None, None, None, None

    logger.info("Storage account: %s", account)
    logger.info("Container      : %s", container)
    logger.info("Authentication : %s", args.blob_auth_mode)

    token = None
    if args.blob_auth_mode == AZCOPY_AUTH_SAS:
        # Preserve the project's existing keyring lookup convention. No SAS
        # lookup is performed for any Entra identity mode.
        token, err = get_blob_sas_token(account, container, logger=logger)
        if not token:
            logger.error("%s FAILED at SAS credential lookup: %s",
                         context_label, err)
            return None, None, None, None

        operation = "sync" if args.sync_mode == "mirror" else "copy"
        ok, detail = validate_sas_token(
            token,
            operation=operation,
            max_remaining_hours=args.sas_max_hours,
        )
        if not ok:
            logger.error("%s FAILED at SAS policy validation: %s",
                         context_label, detail)
            return None, None, None, None
        logger.info("%s", detail)
    else:
        logger.info(
            "No SAS credential requested or loaded; AzCopy will receive an "
            "unsigned HTTPS Blob URL."
        )

    return azcopy_path, container, account, token


def _run_blob_test_mode_check(args, logger):
    azcopy_path, container, account, token = _resolve_blob_connection(
        args, logger, "BLOB DESTINATION CONNECTION TEST (--test-mode)"
    )
    if not azcopy_path:
        return False
    if args.blob_auth_mode == AZCOPY_AUTH_SAS and not token:
        return False

    operation = "sync" if args.sync_mode == "mirror" else "copy"
    ok, detail = test_blob_connection(
        azcopy_path,
        account,
        container,
        token,
        logger=logger,
        auth_mode=args.blob_auth_mode,
        managed_identity_client_id=args.managed_identity_client_id,
        sas_max_hours=args.sas_max_hours,
        operation=operation,
    )
    if not ok:
        logger.error("Blob connection test FAILED: %s", detail)
        return False

    logger.info("Blob connection test PASSED: %s", detail)
    return True


# ==========================================================================
# Main
# ==========================================================================
def main():
    args = parse_args()

    args.target = "portal"

    logger = setup_logging(None, SCRIPT_LABEL)  # console-only until we know dest_root

    finalize_config(args, required=REQUIRED_SETTINGS, logger=logger)

    if args.destination == "blob":
        if not _resolve_blob_auth_settings(args, logger):
            raise SystemExit(2)

    if not args.dest_root:
        args.dest_root = args.output_dir
        args.dest_root_defaulted = True

    is_mirror = args.sync_mode == "mirror"

    timestamp = build_timestamp()
    run_root = os.path.join(args.dest_root, timestamp)

    try:
        os.makedirs(run_root, exist_ok=True)
    except Exception as exc:
        print(f"Could not create run folder '{run_root}': {exc}",
              file=sys.stderr)
        sys.exit(1)

    dest_root = run_root

    debug_log_path = os.path.join(run_root, "_debug_summary.txt")
    logger = setup_logging(debug_log_path, SCRIPT_LABEL)
    if args.test_mode:
        logger.info("*** TEST MODE: no copy/sync step will be executed "
                   "(no robocopy, no azcopy). ***")

    logger.info("=== %s starting (org=%s, config=%s) ===",
               SCRIPT_LABEL, args.org_name, args.config)
    if args.dest_root_defaulted:
        logger.info("Run folder (no --dest-root given; defaulted to the "
                   "org config's 'output-dir'): %s", run_root)
    else:
        logger.info("Run folder: %s", run_root)
    logger.info("Destination mode: %s", args.destination.upper())
    if is_mirror:
        logger.info("Sync mode: MIRROR -- scope: ALL")
    else:
        logger.info("Sync mode: dated/scoped dump -- scope: %s",
                   args.scope.upper())
    logger.info("This script never deletes files at the destination, in "
               "either mode, in either destination.")
    logger.info("TLS verification mode (this script's own REST calls): %s",
               args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)

    blob_container = None
    blob_account = None
    if args.destination == "blob":
        blob_container, blob_account = _resolve_blob_container_and_account(
            args, logger)
        if not blob_container or not blob_account:
            logger.error("Cannot proceed with --destination blob -- "
                        "container/account could not be resolved from the "
                        "command line or config. See the error(s) above.")
            sys.exit(1)

    if args.destination == "file":
        logger.info("File mode: logs will be ROBOCOPIED into: %s", dest_root)
    else:
        container_key, account_key = _blob_config_keys(args.sync_mode)
        azcopy_verb = "synced (azcopy sync --delete-destination=false)" \
            if is_mirror else "uploaded (azcopy copy)"
        account_source = (
            "--storage-account" if args.storage_account else account_key
        )
        container_source = "--container" if args.container else container_key
        logger.info("Blob mode: logs will be %s DIRECTLY via azcopy to "
                   "account=%s container=%s (sources: %s/%s; auth=%s).",
                   azcopy_verb, blob_account, blob_container, account_source,
                   container_source, args.blob_auth_mode)
        if not is_mirror:
            logger.info("  ... under the run folder '%s/' inside that "
                       "container.", timestamp)
        else:
            logger.info("  ... under a stable, non-timestamped folder "
                       "inside that container.")
    logger.info("Log paths are self-discovered per machine; a path that "
               "cannot be discovered is skipped and reported.")

    try:
        filter_info = _resolve_effective_filter_info(args, logger)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    date_filter_opts = None
    include_after = None
    if filter_info is None:
        if is_mirror:
            logger.info("No date filter will be applied (mirror run).")
        else:
            logger.info("No date filter will be applied -- the full "
                       "source tree will be copied for every path in "
                       "scope.")
    elif args.destination == "file":
        date_filter_opts = build_date_filter_opts(filter_info)
        logger.info("Filter mode: %s", filter_info["mode"])
        if filter_info["mode"] == "RANGE":
            logger.info("Range: %s to %s", filter_info["range_start"],
                       filter_info["range_end"])
        logger.info("Resolved robocopy date-filter args: %s",
                   " ".join(date_filter_opts))
    else:
        logger.info("Filter mode: %s", filter_info["mode"])
        if filter_info["mode"] == "RANGE":
            logger.info("Range: %s to %s", filter_info["range_start"],
                       filter_info["range_end"])
        include_after = _include_after_for_blob(filter_info, logger)
        logger.info("Resolved azcopy date-filter: --include-after=%s",
                   include_after)

    start_wallclock, start_perf = start_timer()

    # This script decides and constructs the TLS verification strategy for
    # its OWN direct requests.Session (used for stack/log-path discovery),
    # explicitly, before any network call. (The arcgis SDK connection below
    # is secured separately, always, inside connect_gis() itself.)
    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path, logger=logger)
    except (ImportError, ValueError) as exc:
        logger.error("Could not build a secure session: %s", exc)
        sys.exit(1)

    # One-time startup proof that the TLS handshake to the portal is real
    # and (when verification is enabled) actually verified. Runs once here,
    # not per-request.
    handshake_ok, handshake_detail = verify_tls_handshake(
        args.portal_url, session, logger=logger)
    if not handshake_ok:
        logger.error(
            "Aborting: TLS handshake verification to %s FAILED (%s).",
            args.portal_url, handshake_detail.get("error"))
        sys.exit(1)

    # connect_gis() (UTILITIES/gis_connect.py) always verifies TLS. ca_path
    # is passed through so the SDK connection trusts the same CA as this
    # script's own session when --tls-mode custom-ca-bundle is used.
    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    gis = connect_gis(args.portal_url, args.username, args.keyring_service,
                      logger, ca_bundle_path=ca_path)

    # Token acquisition, centralized. From this point on, nothing in this
    # script reads gis._con.token directly or handles a bare token string --
    # discover_stack()/resolve_log_paths() take a token positionally (shared
    # utility signatures), so .get() is called immediately before each call,
    # keeping the bare string's lifetime as short as possible.
    token_provider = provider_for_gis(gis, "PORTAL")
    logger.info("Token acquired via %s (token present: %s).",
               type(token_provider).__name__, token_provider.is_present())

    logger.info("Beginning stack discovery ...")
    stack = discover_stack(gis, session, logger)

    print_stack_report(stack, logger)

    machines_by_category = _category_machines(stack)
    for category, names in machines_by_category.items():
        if names:
            logger.info("%s machine(s) discovered: %s", category,
                       ", ".join(names))
        else:
            logger.warning("%s: no machine(s) discovered; skipping this "
                          "category's log copy.", category)

    federated_sites = _federated_site_machines(stack)
    for site_name, names in federated_sites.items():
        logger.info("Federated site '%s' machine(s) discovered: %s",
                   site_name, ", ".join(names))
    if not federated_sites:
        logger.info("No non-hosting federated ArcGIS Server sites "
                   "discovered; nothing additional to copy.")

    admin_url_by_site = {"HOSTING": stack["hosting_server"]["url"]}
    for srv in stack["federated_servers"]:
        site_name = srv.get("site_name")
        if site_name and site_name != "HOSTING":
            admin_url_by_site[site_name] = srv.get("admin_url")

    def _resolve_paths_for(category, short_name):
        if category == "Portal":
            admin_url = args.portal_url
        elif category == "Hosting":
            admin_url = admin_url_by_site.get("HOSTING")
        elif category == "DataStore":
            admin_url = None
        else:
            admin_url = admin_url_by_site.get(category)
        return resolve_log_paths(
            category=category, short_hostname=short_name,
            domain_suffix=args.domain_suffix, session=session,
            admin_url=admin_url, token=token_provider.get(), logger=logger)

    plan = []
    for category, names in machines_by_category.items():
        for short_name in names:
            r = _resolve_paths_for(category, short_name)
            paths = [
                ("data-log", r["data_log_path"], r["data_log_error"]),
                ("install-log", r["install_log_path"], r["install_log_error"]),
            ]
            plan.append((category, short_name, paths))
    for site_name, names in federated_sites.items():
        for short_name in names:
            r = _resolve_paths_for(site_name, short_name)
            paths = [
                ("data-log", r["data_log_path"], r["data_log_error"]),
                ("install-log", r["install_log_path"], r["install_log_error"]),
            ]
            plan.append((site_name, short_name, paths))

    effective_scope = "all" if is_mirror else args.scope
    plan = _filter_plan_by_scope(plan, effective_scope, logger=logger)

    logger.info("")
    logger.info("=" * 60)
    logger.info("COPY PLAN (%d machine/category entr%s) -- destination: %s "
              "-- scope: %s -- run type: %s",
              len(plan), "y" if len(plan) == 1 else "ies",
              args.destination.upper(), effective_scope.upper(),
              "MIRROR" if is_mirror else "DATED DUMP")
    logger.info("=" * 60)
    if not plan:
        logger.warning(
            "COPY PLAN is EMPTY for --scope %s -- either no machines were "
            "discovered for this category, or the category itself was not "
            "present in this deployment. Nothing will be copied this run.",
            effective_scope)
    skipped_count = 0
    usable_count = 0
    for category, short_name, paths in plan:
        fqdn = f"{short_name}.{args.domain_suffix}"
        if not args.test_mode:
            logger.info("[%s] %s", category, fqdn)
        for label, relpath, error in paths:
            if relpath:
                if not args.test_mode:
                    source = f"\\\\{fqdn}\\D$\\{relpath}"
                    logger.info("    [%s] source: %s", label, source)
                    if args.destination == "file":
                        dest = os.path.join(dest_root, category, fqdn, relpath)
                        logger.info("    [%s] dest  : %s", label, dest)
                    else:
                        if is_mirror:
                            blob_prefix = "/".join(
                                [category, fqdn, relpath.replace("\\", "/")])
                            verb = "sync"
                        else:
                            blob_prefix = "/".join(
                                [timestamp, category, fqdn,
                                 relpath.replace("\\", "/")])
                            verb = "dest"
                        auth_suffix = (
                            "?<SAS-REDACTED>"
                            if args.blob_auth_mode == AZCOPY_AUTH_SAS
                            else ""
                        )
                        logger.info("    [%s] %s  : blob prefix %s/%s%s",
                                    label, verb, blob_container, blob_prefix,
                                    auth_suffix)
                usable_count += 1
            else:
                logger.error("    [%s] SKIPPED -- discovery failed: %s",
                            label, error)
                skipped_count += 1

    if args.test_mode:
        logger.info("(--test-mode: per-path source/destination lines "
                   "suppressed above -- %d path(s) resolved successfully, "
                   "see the summary line below.)", usable_count)

    logger.info("-" * 60)
    logger.info("Plan summary: %d path(s) will be copied/synced, %d "
               "path(s) SKIPPED due to failed discovery.", usable_count,
               skipped_count)
    if skipped_count:
        logger.warning("%d path(s) could not be self-discovered and will "
                      "NOT be copied (no hard-coded fallback is used).",
                      skipped_count)

    if args.test_mode:
        blob_test_ok = True
        if args.destination == "blob":
            blob_test_ok = _run_blob_test_mode_check(args, logger)

        logger.info("")
        logger.info("*** TEST MODE: plan printed above; NOT running the "
                   "copy/sync step (%s). %d path(s) would be SKIPPED due "
                   "to failed discovery. Scope: %s. Run type: %s. ***",
                   "azcopy" if args.destination == "blob" else "robocopy",
                   skipped_count, effective_scope.upper(),
                   "MIRROR" if is_mirror else "DATED DUMP")
        if args.destination == "blob":
            logger.info("*** Blob connection test result: %s ***",
                       "PASSED" if blob_test_ok else "FAILED (see errors "
                       "above)")
        logger.info(timing_block(start_wallclock, start_perf))
        logger.info("=== %s complete (test mode) ===", SCRIPT_LABEL)
        if args.destination == "blob" and not blob_test_ok:
            _clean_exit(logger, 1)
        _clean_exit(logger, 0)

    azcopy_path = None
    sas_token = None
    if args.destination == "blob":
        azcopy_path, blob_container, blob_account, sas_token = \
            _resolve_blob_connection(args, logger, "BLOB DESTINATION")
        if (not azcopy_path or
                (args.blob_auth_mode == AZCOPY_AUTH_SAS and not sas_token)):
            logger.error("Cannot proceed with --destination blob -- see the "
                         "errors above. No azcopy calls will be made.")
            logger.info(timing_block(start_wallclock, start_perf))
            logger.info("=== %s ABORTED ===", SCRIPT_LABEL)
            _clean_exit(logger, 1)

    print()
    print("=" * 60)
    print(" ArcGIS Log Collector (discovery-driven)")
    print(f" Scope: {effective_scope.upper()}")
    print(f" Run type: {'MIRROR (additive-only, never deletes)' if is_mirror else 'DATED DUMP'}")
    if args.destination == "file":
        print(f" Destination: {dest_root}")
    else:
        print(f" Destination: Azure Blob Storage -- account="
              f"{blob_account} container={blob_container}")
        print(f" Authentication: {args.blob_auth_mode}")
        if is_mirror:
            print(f" Stable folder in container (mirror): {blob_container}/")
        else:
            print(f" Run folder in container: {blob_container}/{timestamp}/")
        print(" (no local copy of log data will be made)")
    print("=" * 60)
    print()
    print("Starting copy/sync... this may take a moment.")
    print("(A debug summary will be shown at the end of this run.)")
    print()

    if not plan:
        logger.warning("Nothing to copy -- COPY PLAN is empty for --scope "
                      "%s. Skipping the copy/sync step entirely.",
                      effective_scope)
        print("Nothing to copy for the selected --scope. Skipping the "
             "copy/sync step.")
        print()
        logger.info(timing_block(start_wallclock, start_perf))
        logger.info("=== %s complete (nothing to copy) ===", SCRIPT_LABEL)
        _clean_exit(logger, 0)

    all_codes = []
    for category, short_name, paths in plan:
        usable_relpaths = [relpath for _, relpath, _ in paths if relpath]
        if not usable_relpaths:
            logger.error("[%s] %s.%s: NO usable paths discovered; skipping "
                        "this machine entirely.", category, short_name,
                        args.domain_suffix)
            continue

        if args.destination == "file":
            codes = copy_group(
                category=category,
                server_short_name=short_name,
                domain_suffix=args.domain_suffix,
                path_pairs=usable_relpaths,
                dest_root=dest_root,
                date_filter_opts=date_filter_opts,
                logger=logger,
            )
            all_codes.extend(codes)
        elif is_mirror:
            codes = copy_group_to_blob_mirror(
                category=category,
                server_short_name=short_name,
                domain_suffix=args.domain_suffix,
                path_pairs=usable_relpaths,
                storage_account=blob_account,
                container=blob_container,
                sas_token=sas_token,
                azcopy_path=azcopy_path,
                logger=logger,
                auth_mode=args.blob_auth_mode,
                managed_identity_client_id=args.managed_identity_client_id,
                sas_max_hours=args.sas_max_hours,
            )
            all_codes.extend(codes)
        else:
            codes = copy_group_to_blob(
                category=category,
                server_short_name=short_name,
                domain_suffix=args.domain_suffix,
                path_pairs=usable_relpaths,
                storage_account=blob_account,
                container=blob_container,
                sas_token=sas_token,
                azcopy_path=azcopy_path,
                run_timestamp=timestamp,
                include_after=include_after,
                logger=logger,
                auth_mode=args.blob_auth_mode,
                managed_identity_client_id=args.managed_identity_client_id,
                sas_max_hours=args.sas_max_hours,
            )
            all_codes.extend(codes)

    print()
    print("=" * 60)
    print("All copy/sync operations complete.")
    if args.destination == "file":
        print(f"Output located at: {dest_root}")
    elif is_mirror:
        print(f"Output synced to: {blob_account} / {blob_container}/")
    else:
        print(f"Output uploaded to: {blob_account} / "
             f"{blob_container}/{timestamp}/")
    print("=" * 60)
    print()

    if args.destination == "file":
        failures = [c for c in all_codes if c > ROBOCOPY_SUCCESS_MAX or c < 0]
        if failures:
            logger.warning("%d of %d robocopy call(s) reported a failure "
                          "exit code (>%d or launch failure).",
                          len(failures), len(all_codes),
                          ROBOCOPY_SUCCESS_MAX)
        else:
            logger.info("All %d robocopy call(s) completed successfully "
                       "(exit code <= %d).", len(all_codes),
                       ROBOCOPY_SUCCESS_MAX)
    else:
        failures = [c for c in all_codes if c != 0]
        verb = "sync" if is_mirror else "copy"
        if failures:
            logger.warning(
                "%d of %d azcopy %s call(s) reported a non-zero exit "
                "code. A job marked 'CompletedWithErrors' due to a small "
                "number of locked/actively-written files is common and "
                "not necessarily a failed run -- check each job's output "
                "above.", len(failures), len(all_codes), verb)
        else:
            logger.info("All %d azcopy %s call(s) completed successfully "
                       "(exit code 0).", len(all_codes), verb)

    if skipped_count:
        logger.warning("Additionally, %d path(s) were SKIPPED entirely "
                      "because they could not be self-discovered.",
                      skipped_count)

    logger.info(timing_block(start_wallclock, start_perf))
    logger.info("=== %s complete ===", SCRIPT_LABEL)

    print("=" * 60)
    print(" DEBUG SUMMARY (also saved to)", debug_log_path)
    print("=" * 60)
    try:
        with open(debug_log_path, "r", encoding="utf-8") as fh:
            print(fh.read())
    except Exception:
        pass
    print("=" * 60)

    if failures:
        _clean_exit(logger, 1)
    _clean_exit(logger, 0)


if __name__ == "__main__":
    main()