"""Shared utilities for the PORTAL_* item-management scripts.

CONSOLIDATION NOTE: platform_detect.py, timestamps.py, and runtime.py have
been folded into restutils.py; reporting.py has been folded into emailer.py.
Every name below is exported with the SAME name as before, so
`from UTILITIES import X` is unaffected for any existing script -- only the
internal module layout changed (fewer files in the UTILITIES folder).

*** MULTI-ORG CHANGE ***: cli.add_config_arg and cli.get_max_email_mb (now
config-path-aware) are exported below. config.sanitize_org_name is exported
for any script that needs to build org-prefixed names outside of
finalize_config()'s automatic args.org_name / args.org_name_safe.

*** GROUP-EXCLUSION CHANGE ***: a new module, exclusions.py, provides the
shared --exclude-group-id CLI flag and the filtering helpers used by every
scan script that supports excluding a group's items from its results. See
exclusions.py for details.

*** STACK DISCOVERY / LOG DUMP CHANGE ***: PORTAL_STACK_SERVER_INVENTORY.py
and LOG_DUMP.bat are RETIRED (legacy, removed). Both are fully replaced by
PORTAL_STACK_LOG_DUMP.py, which discovers the deployment topology AND prints
the same console report AND performs the log-collection robocopy. Two new
UTILITIES modules support it:

  * stack_discovery.py - the Portal/hosting-server/Data Store/federated-
    server topology discovery logic (discover_stack() and its confirmed
    admin-REST helpers). Also provides short_hostname() to strip an FQDN
    down to its leading label (e.g. "big_serverGISAP33.enterprise.com" ->
    "big_serverGISAP33") for building UNC paths, and looks_like_fqdn()/
    hostname_from_url() used by the topology report.

    *** DATA STORE PRIMARY/HA DETECTION CHANGE ***: stack_discovery.py now
    also exposes discover_data_store(session, hosting_admin_url, token,
    logger=None) as a STANDALONE function (no `gis` object, no federation
    walk needed) -- it is the same Data Store findItems/parsing logic
    discover_stack() uses internally, extracted so any script that just
    needs "which machine is the live PRIMARY Data Store right now" (e.g.
    PORTAL_HOSTING_SVR_DS_INVENTORY.py) can call it directly and cheaply.
    See stack_discovery.py for full details on single-machine vs. HA
    (primary+standby) resolution and the "ambiguous" result flag.

  * robocopy_utils.py - the date-filter prompting (Previous Day / Date Range
    / Previous N Days) and robocopy invocation mechanics that used to live
    only in LOG_DUMP.bat, now available to any Python script that needs to
    drive the same UNC-share log collection against a dynamically discovered
    machine list.

*** BLOB DESTINATION CHANGE ***: PORTAL_STACK_LOG_DUMP.py now supports a
--destination {file,blob} option. "file" is the original, UNCHANGED local
robocopy behavior. "blob" additionally uploads the collected local
destination tree to an Azure Blob Storage container via azcopy.exe. A new
module, azcopy_utils.py, provides:

  * get_azcopy_path()      - resolve the (non-secret) azcopy-path setting
                              from the org config toml.
  * get_blob_sas_token()   - resolve the SAS token from Windows Credential
                              Manager (service=storage account, username=
                              container, password=token).
  * build_blob_url()       - build the azcopy destination URL.
  * validate_azcopy_exe()  - confirm azcopy.exe exists on disk.
  * run_azcopy_upload()    - run the actual azcopy copy call.
  * test_blob_connection() - read-only azcopy list connection check used by
                              Blob --test-mode, with full connection logging
                              (including warnings/failures).

*** DATASTORE_CONFIG EXPORT CHANGE ***: datastore_config.py (Data Store /
hosting-server key resolution for the NON-host keys: dbname, db-user,
db-port, keyring-service-db) is now ALSO re-exported through this
__init__.py (previously it had to be imported directly via
`from UTILITIES.datastore_config import ...`). Both import styles work now;
the direct-import style still works unchanged.

*** SECURITY / TLS CHANGE ***
-------------------------------------------------------------------------
connect_gis() (from gis_connect.py) now ALWAYS connects with TLS
verification enabled -- the former secure_gis_connect.connect_gis_secure()
behavior has been folded directly into it, so no script needs to change its
import to get the hardened behavior. secure_gis_connect.py itself is now
redundant/deprecated but left in place unmodified.

auth.py, secure_session.py, and datastore_config.py are exported here since
the hardened approach is the production approach:

  * auth.py - TokenProvider / PortalGISTokenProvider / AgolGISTokenProvider /
    StaticTokenProvider / provider_for_gis. Wraps a bare auth token string in
    an object whose repr()/str() never expose the value, so accidental
    logging of the provider itself can't leak a credential. Use
    provider_for_gis(gis, platform_label) to get the right provider for an
    already-connected gis, then call provider.auth_params() inline at each
    request-build call site.

    *** TOKEN REDACTION CHANGE ***: auth.py also exports redact_token(text),
    a text-level safety net that strips a 'token=<value>' query-string
    parameter out of any string (most commonly a caught requests
    exception's own str(exc), which can embed the full request URL) before
    that string reaches a log line, an email body, or a CSV cell.
    logging_setup.setup_logging() now installs a logging Filter that calls
    this automatically on every record, so a token cannot reach the console
    or the log file even if a call site forgets to redact explicitly. Call
    sites building email-body or CSV text directly from a caught exception
    should still call redact_token() themselves, since those paths do not
    go through the logging filter.

  * secure_session.py - VerifyMode / build_secure_session() /
    verify_tls_handshake() / allow_insecure_verify_false(). Builds a pooled+
    retrying requests.Session with an EXPLICIT, caller-chosen TLS
    verification strategy (STANDARD_CA / OS_TRUST_STORE / CUSTOM_CA_BUNDLE),
    plus a one-time startup handshake check that proves and logs the actual
    negotiated protocol/cipher/certificate. There is no default "just build
    me a session" call -- the caller must state its verification strategy.
    An explicit, loudly-logged allow_insecure_verify_false() escape hatch
    exists for time-boxed debugging only.

Both are now standard imports for any new script's own direct `requests`
session (separate from the arcgis SDK's own connection, which connect_gis()
already secures).
"""
from .restutils import (
    # was platform_detect.py
    get_platform,
    # was timestamps.py
    build_timestamp,
    # was runtime.py
    start_timer,
    timing_block,
    format_elapsed,
    elapsed_since,
    now_wallclock,
    # original restutils.py exports
    tune_gis_session,
    build_pooled_session,
    rest_search_page,
    bucketed_hosted_fs,
    resolve_items,
    get_org_id,
    get_json,
    count_query,
    epoch_to_str,
    epoch_to_date,
    HOSTED_FS_CLAUSE,
    DEFAULT_TIMEOUT,
)
from .logging_setup import setup_logging
from .gis_connect import connect_gis
from .emailer import (
    send_email,
    normalize_recipients,
    MAX_ATTACHMENTS_MB,
    SMTP_PORT,
    # was reporting.py
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    MB,
)
from .bucketing import bucketed_search, DEFAULT_CAP
from .cli import (
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_target_arg,
    add_test_flag,
    add_config_arg,          # MULTI-ORG: required --config flag
    subject_line,
    finalize_config,
    get_max_email_mb,
    normalize_target,
)
from . import config
from .config import (
    load_config,
    get_config_value,
    resolve,
    apply_to_args,
    require,
    normalize_target as config_normalize_target,
    sanitize_org_name,        # MULTI-ORG: raw org-name -> filename-safe token
    CONFIG_KEYS,
)
from . import exclusions
from .exclusions import (
    add_exclude_group_arg,
    resolve_excluded_item_ids,
    filter_excluded,
)
from . import stack_discovery
from .stack_discovery import (
    discover_stack,
    discover_data_store,
    hostname_from_url,
    looks_like_fqdn,
    short_hostname,
)
from . import robocopy_utils
from .robocopy_utils import (
    ROBOCOPY_BASE_OPTS,
    ROBOCOPY_SUCCESS_MAX,
    prompt_date_filter,
    date_filter_from_args,
    build_date_filter_opts,
    run_robocopy,
    copy_group,
)
from . import azcopy_utils
from .azcopy_utils import (
    get_azcopy_path,
    get_blob_sas_token,
    build_blob_url,
    validate_azcopy_exe,
    maxage_days_to_include_after,
    run_azcopy_upload,
    copy_group_to_blob,
    test_blob_connection,
)
from . import datastore_config
from .datastore_config import (
    add_datastore_args,
    get_datastore_settings,
    missing_datastore_keys,
)
from . import auth
from .auth import (
    TokenProvider,
    PortalGISTokenProvider,
    AgolGISTokenProvider,
    StaticTokenProvider,
    provider_for_gis,
    redact_token,
)
from . import secure_session
from .secure_session import (
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    allow_insecure_verify_false,
)

__all__ = [
    "build_timestamp",
    "setup_logging",
    "connect_gis",
    "send_email",
    "normalize_recipients",
    "MAX_ATTACHMENTS_MB",
    "SMTP_PORT",
    "get_platform",
    "bucketed_search",
    "DEFAULT_CAP",
    "start_timer",
    "timing_block",
    "format_elapsed",
    "elapsed_since",
    "now_wallclock",
    # REST / HTTP helpers
    "tune_gis_session",
    "build_pooled_session",
    "rest_search_page",
    "bucketed_hosted_fs",
    "resolve_items",
    "get_org_id",
    "get_json",
    "count_query",
    "epoch_to_str",
    "epoch_to_date",
    "HOSTED_FS_CLAUSE",
    "DEFAULT_TIMEOUT",
    # run-scaffolding helpers (formerly reporting.py)
    "ensure_output_dir",
    "gather_attachments",
    "write_dict_csv",
    "MB",
    # CLI
    "add_common_portal_args",
    "add_email_args",
    "add_output_dir_arg",
    "add_target_arg",
    "add_test_flag",
    "add_config_arg",
    "subject_line",
    "finalize_config",
    "get_max_email_mb",
    "normalize_target",
    # shared configuration
    "config",
    "load_config",
    "get_config_value",
    "resolve",
    "apply_to_args",
    "require",
    "config_normalize_target",
    "sanitize_org_name",
    "CONFIG_KEYS",
    # group exclusion (shared, used by the scan scripts that support it)
    "exclusions",
    "add_exclude_group_arg",
    "resolve_excluded_item_ids",
    "filter_excluded",
    # stack discovery (used by PORTAL_STACK_LOG_DUMP.py and
    # PORTAL_HOSTING_SVR_DS_INVENTORY.py)
    "stack_discovery",
    "discover_stack",
    "discover_data_store",
    "hostname_from_url",
    "looks_like_fqdn",
    "short_hostname",
    # robocopy / log-collection helpers (replaces LOG_DUMP.bat logic)
    "robocopy_utils",
    "ROBOCOPY_BASE_OPTS",
    "ROBOCOPY_SUCCESS_MAX",
    "prompt_date_filter",
    "date_filter_from_args",
    "build_date_filter_opts",
    "run_robocopy",
    "copy_group",
    # azcopy / Azure Blob upload helpers (--destination blob)
    "azcopy_utils",
    "get_azcopy_path",
    "get_blob_sas_token",
    "build_blob_url",
    "validate_azcopy_exe",
    "maxage_days_to_include_after",
    "run_azcopy_upload",
    "copy_group_to_blob",
    "test_blob_connection",
    # Data Store / hosting-server key resolution (non-host keys)
    "datastore_config",
    "add_datastore_args",
    "get_datastore_settings",
    "missing_datastore_keys",
    # token handling (never pass a bare token string; wrap it) + redaction
    "auth",
    "TokenProvider",
    "PortalGISTokenProvider",
    "AgolGISTokenProvider",
    "StaticTokenProvider",
    "provider_for_gis",
    "redact_token",
    # explicit, caller-chosen TLS verification for a script's own direct
    # requests.Session (separate from connect_gis()'s own, now-always-secure
    # arcgis SDK connection)
    "secure_session",
    "VerifyMode",
    "build_secure_session",
    "verify_tls_handshake",
    "allow_insecure_verify_false",
]