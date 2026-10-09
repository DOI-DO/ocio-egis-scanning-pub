#!/usr/bin/env python3
"""Inventory every user account in an ArcGIS Online or Portal deployment,
with optional forest-wide Active Directory cross-check.

Common usage:
    python USER_REPORT.py --config ORG.config.toml --target portal
    python USER_REPORT.py --config OTHER.config.toml --target agol --no-test
    python USER_REPORT.py --config ORG.config.toml --target portal --no-test \
        --ad-check --dc dc01.enterprise.com
    python USER_REPORT.py --config OTHER.config.toml --target agol --no-test \
        --count-delay 0.3
"""

import argparse
import contextlib
import datetime
import math
import os
import sys
import time
from urllib.parse import urlsplit

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from UTILITIES import (
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    get_org_id,
    subject_line,
    start_timer,
    timing_block,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    add_target_arg,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_config_arg,
    finalize_config,
    get_max_email_mb,
    provider_for_gis,
    redact_token,
)
from UTILITIES.secure_session import (
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES.ad_resolver import (
    ADResolver,
    validate_domain_controller_name,
)

_EXCLUDE_USERS = (
    "esri_boundaries", "esri_demographics", "esri_livingatlas",
    "esri_nav", "esri_webstyles",
)

TEST_USER_LIMIT = 250
NEW_USER_WINDOW_DAYS = 7

BASE_FIELDNAMES = [
    "platform", "username", "fullName", "firstName", "lastName", "email",
    "email_verified", "role", "roleid", "userLicenseTypeId", "idpUsername",
    "disabled", "provider", "date_created", "new_user", "date_modified",
    "last_login", "numItems", "numGroups", "description", "data_issue",
]
AD_FIELDNAMES = ["ad_status", "ad_ou"]

_BUILTIN_PROVIDER = "arcgis"

_EMAIL_STATUS_MAP = {
    "verified": "verified",
    "notverified": "unverified",
    "unverified": "unverified",
}

_TLS_MODE_MAP = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_MILLISECONDS_PER_DAY = 86_400_000
_MAX_ARTIFACT_COLLISION_ATTEMPTS = 1_000

# Report column -> User attribute, for plain pass-through fields that may be
# absent on some accounts (e.g. SSO users with no firstName/email). These are
# read through _user_attr because the ArcGIS User object's __getattr__ raises
# KeyError (not AttributeError) for a missing field, which defeats the usual
# getattr(..., default) safety net.
_PASSTHROUGH_ATTRS = {
    "fullName": "fullName",
    "firstName": "firstName",
    "lastName": "lastName",
    "email": "email",
    "role": "role",
    "roleid": "roleId",
    "userLicenseTypeId": "userLicenseTypeId",
    "idpUsername": "idpUsername",
    "disabled": "disabled",
    "provider": "provider",
    "description": "description",
}


class _ReportAbort(RuntimeError):
    """Expected run failure that should be reported without a traceback."""

    def __init__(self, subject_label, message):
        super().__init__(message)
        self.subject_label = subject_label


def _user_attr(u, attr, default="", *, missing=None):
    """getattr for ArcGIS User objects whose __getattr__ raises KeyError.

    The SDK's User.__getattr__ delegates to dict.__getitem__, which raises
    KeyError (not AttributeError) for an absent field, so the usual
    getattr(u, attr, default) default is never applied. Catch both and, when a
    field is genuinely missing, record it so the caller can log it.
    """
    try:
        return getattr(u, attr)
    except (AttributeError, KeyError):
        if missing is not None:
            missing.append(attr)
        return default


def _user_json_keys(u):
    """Return the sorted key names present in a User's backing JSON.

    Keys only, never values, so nothing sensitive (email, UPN, etc.) is logged.
    This must never raise: the ArcGIS User object's __getattr__ delegates to
    dict.__getitem__, so even getattr(u, "_user_dict", None) raises KeyError
    for an absent attribute. The User object itself is dict-like, so iterating
    its own keys is the reliable source.
    """
    raw = None
    try:
        raw = u.__dict__.get("_user_dict")
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        try:
            raw = dict(u)
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        try:
            raw = {k: None for k in u.keys()}
        except Exception:
            raw = {}
    try:
        return sorted(str(k) for k in raw.keys())
    except Exception:
        return []


def _log_missing_user_fields(u, username, missing_fields, logger):
    logger.warning(
        "User %r is missing expected field(s): %s. Substituting blanks. "
        "JSON keys present: %s",
        username, ", ".join(sorted(set(missing_fields))),
        ", ".join(_user_json_keys(u)))


def _nonnegative_finite_float(raw):
    """Argparse type for finite delays greater than or equal to zero."""
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"expected a number, got {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(
            "value must be finite and greater than or equal to zero")
    return value


def _validate_https_portal_url(portal_url):
    """Return a normalized HTTPS URL or raise ValueError."""
    value = str(portal_url or "").strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Force validation of a malformed/non-numeric port.
    except ValueError as exc:
        raise ValueError(f"invalid portal URL {value!r}: {exc}") from exc

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(
            "portal-url must be an absolute HTTPS URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("portal-url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("portal-url must not contain a query string or fragment")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("portal-url contains whitespace or a control character")
    return value.rstrip("/")


def _resolve_ca_bundle_path(tls_mode, configured_path):
    """Validate TLS-mode/CA-path pairing and return an absolute path or None."""
    raw_path = str(configured_path or "").strip()
    if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE:
        if not raw_path:
            raise ValueError(
                "--tls-mode custom-ca-bundle requires --ca-bundle-path")
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.isfile(path):
            raise ValueError(f"CA bundle is not an existing file: {path}")
        if not os.access(path, os.R_OK):
            raise ValueError(f"CA bundle is not readable: {path}")
        return path

    if raw_path:
        raise ValueError(
            "--ca-bundle-path is only valid with --tls-mode custom-ca-bundle")
    return None


def _reserve_log_path(output_dir, org_prefix, platform, base_timestamp):
    """Atomically reserve a run stamp that collides with no existing output."""
    for collision in range(_MAX_ARTIFACT_COLLISION_ATTEMPTS):
        stamp = (base_timestamp if collision == 0
                 else f"{base_timestamp}_{collision + 1}")
        csv_path = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_USER_REPORT_{stamp}.csv")
        if os.path.exists(csv_path):
            continue
        path = os.path.join(
            output_dir, f"{org_prefix}_USER_REPORT_{stamp}.log")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(fd)
            return stamp, path
    raise RuntimeError(
        "could not reserve a unique USER_REPORT log filename after "
        f"{_MAX_ARTIFACT_COLLISION_ATTEMPTS} attempts")


def _email_verified_status(u, logger):
    provider = (_user_attr(u, "provider", "") or "").strip().lower()
    if provider != _BUILTIN_PROVIDER:
        return ""

    try:
        raw = _user_attr(u, "emailStatus", None)
        if raw is None:
            raw = (u.__dict__.get("_user_dict") or {}).get("emailStatus")
        if raw is None:
            return "unknown"
        return _EMAIL_STATUS_MAP.get(str(raw).strip().lower(), "unknown")
    except Exception as exc:
        logger.warning("Could not read emailStatus for %s: %s",
                       _user_attr(u, "username", "?"), exc)
        return "unknown"

_NEVER_LOGIN = -1
_NEVER_LOGIN_DATE = "1969-01-01"

_COUNT_RETRY_ATTEMPTS = 4
_COUNT_RETRY_BACKOFF_SECONDS = 2.0
_COUNT_REQUEST_TIMEOUT_SECONDS = 30
_THROTTLE_MARKERS = ("too many requests", "429")
_TOKEN_ERROR_MARKERS = (
    "rest error 498", "rest error 499", "invalid token", "token required",
    "token has expired", "expired token",
)


def _is_throttling_error(exc):
    msg = str(exc).lower()
    return any(marker in msg for marker in _THROTTLE_MARKERS)


def _call_with_retry(fn, username, label, logger, *, raise_on_failure=False):
    delay = _COUNT_RETRY_BACKOFF_SECONDS
    for attempt in range(1, _COUNT_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            is_throttle = _is_throttling_error(exc)
            if attempt < _COUNT_RETRY_ATTEMPTS and is_throttle:
                logger.warning(
                    "%s for %s throttled (attempt %d/%d); retrying in "
                    "%.1fs ...", label, username, attempt,
                    _COUNT_RETRY_ATTEMPTS, delay)
                time.sleep(delay)
                delay *= 2
                continue
            safe_error = redact_token(str(exc))
            logger.warning("Could not read %s for %s: %s", label,
                           username, safe_error)
            if raise_on_failure:
                raise RuntimeError(safe_error) from exc
            return ""

    raise AssertionError("retry loop exited without returning or raising")


def _epoch_ms_to_date(epoch_ms):
    if epoch_ms in (None, "", 0):
        return ""
    try:
        return (
            datetime.datetime.fromtimestamp(int(epoch_ms) / 1000.0)
            .date()
            .isoformat()
        )
    except Exception:
        return ""


def _is_new_user(created_epoch_ms, run_start_epoch_ms,
                 window_days=NEW_USER_WINDOW_DAYS):
    if created_epoch_ms in (None, "", 0):
        return "no"
    try:
        created_ms = int(created_epoch_ms)
        run_ms = int(run_start_epoch_ms)
    except (TypeError, ValueError, OverflowError):
        return "no"

    cutoff_ms = run_ms - int(window_days * _MILLISECONDS_PER_DAY)
    if cutoff_ms <= created_ms <= run_ms:
        return "yes"
    return "no"


def _build_user_query(org_id=None):
    nots = " AND ".join(f"NOT username:{u}" for u in _EXCLUDE_USERS)
    query = f"* AND {nots}"
    if org_id:
        query = f"{query} AND orgid:{org_id}"
    return query


def _resolve_user_search_org_id(gis, platform, logger):
    """Resolve the explicit org scope shared by SDK search and REST counting."""
    org_id = get_org_id(gis, logger=logger)
    if not org_id:
        raise _ReportAbort(
            "ORG SCOPE FAILED",
            f"The authenticated {platform} organization id could not be "
            "resolved. The run was aborted because the SDK user search and "
            "the independent REST completeness check must use the same "
            "organization scope.")

    org_id = str(org_id).strip()
    if not org_id:
        raise _ReportAbort(
            "ORG SCOPE FAILED",
            f"The authenticated {platform} organization id was blank. The "
            "run was aborted because completeness cannot be verified without "
            "an explicit organization scope.")

    logger.info(
        "Scoping %s user search and completeness check to org id %s.",
        platform, org_id)
    return org_id


def _search_query_literal(value):
    escaped = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_token_error(message):
    text = str(message or "").lower()
    return any(marker in text for marker in _TOKEN_ERROR_MARKERS)


def _post_json(session, url, token, *, timeout, params=None):
    """POST ArcGIS form data so the bearer token is not placed in the URL."""
    form = {"f": "json", "token": token}
    if params:
        form.update(params)

    try:
        response = session.post(url, data=form, timeout=timeout)
    except Exception as exc:
        return None, "request error: " + redact_token(str(exc))

    if response.status_code >= 400:
        return None, f"HTTP {response.status_code} {response.reason}"

    try:
        data = response.json()
    except ValueError:
        snippet = (response.text or "")[:160].replace("\n", " ")
        return None, (
            f"non-JSON response (status {response.status_code}): "
            f"{redact_token(snippet)!r}")

    if isinstance(data, dict) and "error" in data:
        error = data.get("error") or {}
        code = error.get("code", "?")
        message = error.get("message", "unknown REST error")
        details = "; ".join(error.get("details", []) or [])
        rendered = f"REST error {code}: {message}"
        if details:
            rendered += f" ({details})"
        return None, redact_token(rendered)
    return data, None


def _search_total(session, search_url, token_provider, query, username,
                  label, logger, *, raise_on_failure=False):
    def _request_once():
        data, err = _post_json(
            session,
            search_url,
            token_provider.get(),
            timeout=_COUNT_REQUEST_TIMEOUT_SECONDS,
            params={"q": query, "start": 1, "num": 1},
        )

        if err and _is_token_error(err):
            token_provider.refresh()
            data, err = _post_json(
                session,
                search_url,
                token_provider.get(),
                timeout=_COUNT_REQUEST_TIMEOUT_SECONDS,
                params={"q": query, "start": 1, "num": 1},
            )

        if err:
            raise RuntimeError(redact_token(str(err)))
        if not isinstance(data, dict) or "total" not in data:
            raise RuntimeError(
                "search response did not contain an integer 'total' field")
        try:
            total = int(data["total"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"search response returned an invalid total: "
                f"{data.get('total')!r}") from exc
        if total < 0:
            raise RuntimeError(f"search response returned total={total}")
        return total

    return _call_with_retry(
        _request_once, username, label, logger,
        raise_on_failure=raise_on_failure)


def _count_items(session, portal_url, token_provider, username, logger):
    query = f"owner:{_search_query_literal(username)}"
    search_url = portal_url.rstrip("/") + "/sharing/rest/search"
    return _search_total(session, search_url, token_provider, query,
                         username, "items", logger)


def _count_groups(session, portal_url, token_provider, username, logger):
    query = f"owner:{_search_query_literal(username)}"
    search_url = portal_url.rstrip("/") + "/sharing/rest/community/groups"
    return _search_total(session, search_url, token_provider, query,
                         username, "groups", logger)


def _user_search_total(session, portal_url, token_provider, query, logger):
    search_url = portal_url.rstrip("/") + "/sharing/rest/community/users"
    return _search_total(
        session, search_url, token_provider, query, "organization",
        "user-search total", logger, raise_on_failure=True)


def _validated_user_list(users):
    """Materialize and reject malformed or duplicate SDK user results."""
    materialized = list(users or [])
    seen = set()
    for user in materialized:
        username = str(_user_attr(user, "username", "") or "").strip()
        if not username:
            raise RuntimeError(
                "the ArcGIS user search returned an entry without a username")
        key = username.casefold()
        if key in seen:
            raise RuntimeError(
                f"the ArcGIS user search returned duplicate username "
                f"{username!r}; refusing to produce a potentially incomplete "
                "or duplicated report")
        seen.add(key)
    return materialized


def _search_users(gis, session, portal_url, token_provider, query,
                  test_mode, logger):
    """Search users and prove that a full-mode result was not truncated."""
    if test_mode:
        users = gis.users.search(query=query, max_users=TEST_USER_LIMIT)
        return _validated_user_list(users)[:TEST_USER_LIMIT]

    # A stable count before/after the SDK call lets us detect the historical
    # silent 10,000-user truncation. One retry tolerates ordinary account churn.
    for attempt in range(1, 3):
        total_before = _user_search_total(
            session, portal_url, token_provider, query, logger)
        requested = max(total_before, 1)
        users = _validated_user_list(
            gis.users.search(query=query, max_users=requested))
        total_after = _user_search_total(
            session, portal_url, token_provider, query, logger)

        if total_before == total_after:
            if len(users) == total_before:
                return users

            logger.warning(
                "Organization-scoped REST total was stable at %d, but the SDK "
                "returned %d user(s) (attempt=%d/2).", total_before,
                len(users), attempt)
            if attempt < 2:
                continue
            raise RuntimeError(
                "Organization-scoped ArcGIS REST search reported %d matching "
                "users, but the SDK returned %d after two attempts. The report "
                "is being aborted instead of silently writing a truncated "
                "result." % (total_before, len(users)))

        logger.warning(
            "User total changed during enumeration (before=%d, after=%d, "
            "returned=%d, attempt=%d/2).", total_before, total_after,
            len(users), attempt)

        # If the SDK result matches one of the two authoritative snapshots,
        # it is complete for a point in time during this changing interval.
        if len(users) in {total_before, total_after}:
            return users

    raise RuntimeError(
        "The organization user count kept changing while the user list was "
        "being enumerated, so completeness could not be proven. Re-run the "
        "report rather than accepting an uncertain snapshot.")


def _user_row(u, platform, session, portal_url, token_provider, logger,
              run_start_epoch_ms, ad_resolver=None):
    missing_fields = []

    username = str(
        _user_attr(u, "username", "", missing=missing_fields) or "").strip()
    if not username:
        _log_missing_user_fields(u, "<unknown>", ["username"], logger)
        raise RuntimeError(
            "cannot build a report row for a user with no username")

    last_login_raw = _user_attr(u, "lastLogin", None)
    if last_login_raw == _NEVER_LOGIN:
        last_login = _NEVER_LOGIN_DATE
    else:
        last_login = _epoch_ms_to_date(last_login_raw)

    created_raw = _user_attr(u, "created", None)

    row = {
        "platform": platform,
        "username": username,
        "email_verified": _email_verified_status(u, logger),
        "date_created": _epoch_ms_to_date(created_raw),
        "new_user": _is_new_user(created_raw, run_start_epoch_ms),
        "date_modified": _epoch_ms_to_date(_user_attr(u, "modified", None)),
        "last_login": last_login,
        "numItems": _count_items(
            session, portal_url, token_provider, username, logger),
        "numGroups": _count_groups(
            session, portal_url, token_provider, username, logger),
    }

    for column, attr in _PASSTHROUGH_ATTRS.items():
        row[column] = _user_attr(u, attr, "", missing=missing_fields)

    if missing_fields:
        unique_missing = sorted(set(missing_fields))
        _log_missing_user_fields(u, username, unique_missing, logger)
        row["data_issue"] = (
            "missing field(s): " + ", ".join(unique_missing)
            + " (blank substituted)")
    else:
        row["data_issue"] = ""

    if ad_resolver is not None:
        status, ou = ad_resolver.lookup(row["idpUsername"], row["email"])
        row["ad_status"] = status
        row["ad_ou"] = ou

    return row


def build_parser():
    parser = argparse.ArgumentParser(
        description="Inventory Portal/AGOL users to a CSV and email it. "
                    "Defaults to TEST MODE (first 250 users).")
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    add_test_flag(
        parser,
        help_text=(f"Run against ALL users. Without this flag the script runs "
                   f"in TEST MODE and inventories only the first "
                   f"{TEST_USER_LIMIT} users."))

    ad = parser.add_argument_group("Active Directory status (optional)")
    ad.add_argument(
        "--ad-check", action="store_true",
        help="Also look each account up in Active Directory (forest-wide) "
             "and add 'ad_status'/'ad_ou' columns. Uses ONLY your CURRENT "
             "Windows login via native ADSI -- no password is ever "
             "requested, stored, or sent. Requires --dc and a pre-installed "
             "approved pywin32 package.")
    ad.add_argument(
        "--dc", default=None, type=validate_domain_controller_name,
        help="Domain controller hostname/FQDN used to read RootDSE and resolve "
             "the forest search base. ADSI then performs the forest-wide "
             "Global Catalog lookup. REQUIRED with --ad-check.")
    ad.add_argument(
        "--ad-debug", action="store_true",
        help="Emit AD lookup diagnostics without logging raw email, UPN, "
             "LDAP filter, or distinguished-name values.")
    parser.add_argument(
        "--count-delay", type=_nonnegative_finite_float, default=0.0,
        help="Seconds to pause between users before the numItems/numGroups "
             "lookups (default 0).")
    parser.add_argument(
        "--tls-mode", choices=sorted(_TLS_MODE_MAP.keys()), default="standard",
        help="TLS verification strategy: 'standard' (public CA, default), "
             "'os-trust-store' (requires truststore package), or "
             "'custom-ca-bundle' (requires --ca-bundle-path).")
    parser.add_argument(
        "--ca-bundle-path", default=None,
        help="Path to a readable PEM CA bundle. Required, and only valid, "
             "when --tls-mode is custom-ca-bundle.")
    return parser



def _portal_rest_base(portal_url):
    base = portal_url.rstrip("/")
    if base.lower().endswith("/home"):
        base = base[:-len("/home")]
    return base


def _close_resource(resource, label, logger):
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        logger.warning("Could not close %s cleanly: %s", label,
                       redact_token(str(exc)))


def _inventory_users(args, platform, tls_mode, ca_path, logger,
                     run_start_epoch_ms):
    """Perform network/AD work and close every acquired resource on exit."""
    with contextlib.ExitStack() as resources:
        ad_resolver = None
        if args.ad_check:
            ad_resolver = ADResolver(args.dc, logger, debug=args.ad_debug)
            resources.callback(ad_resolver.close)
            if not ad_resolver.ok:
                raise _ReportAbort(
                    "AD CHECK FAILED",
                    "The native ADSI/pywin32 resolver could not initialize. "
                    "The run was aborted before contacting ArcGIS, and no "
                    "partial report was produced. The report never falls back "
                    "to a password-based directory bind or a runtime package "
                    "installation.")

        try:
            session = build_secure_session(
                tls_mode,
                ca_bundle_path=ca_path,
                pool_connections=8,
                pool_maxsize=16,
                retry_total=3,
                logger=logger,
            )
        except Exception as exc:
            raise _ReportAbort(
                "TLS CHECK FAILED",
                "Could not initialize the verified REST session: "
                + redact_token(str(exc))) from exc
        resources.callback(_close_resource, session, "REST session", logger)

        try:
            ok, _detail = verify_tls_handshake(
                args.portal_url, session, logger=logger)
        except Exception as exc:
            raise _ReportAbort(
                "TLS CHECK FAILED",
                "The TLS verification check raised an error: "
                + redact_token(str(exc))) from exc
        if not ok:
            raise _ReportAbort(
                "TLS CHECK FAILED",
                f"TLS certificate verification to {args.portal_url} failed. "
                "No ArcGIS login or report generation was attempted.")

        gis = connect_gis(
            args.portal_url,
            args.username,
            args.keyring_service,
            logger,
            verify_mode=tls_mode,
            ca_bundle_path=ca_path,
        )
        sdk_session = getattr(getattr(gis, "_con", None), "_session", None)
        if sdk_session is not None and sdk_session is not session:
            resources.callback(
                _close_resource, sdk_session, "ArcGIS SDK session", logger)

        token_provider = provider_for_gis(gis, platform)
        logger.info("Token acquired via %s (token present: %s).",
                    token_provider.__class__.__name__,
                    token_provider.is_present())

        portal_rest_url = _portal_rest_base(args.portal_url)
        logger.info(
            "Per-user item/group counts: verified direct REST session using "
            "POST form data (token excluded from request URLs).")

        # This report inventories the authenticated organization. Put the same
        # explicit orgid clause in the query used by both the SDK search and the
        # independent REST total; an unscoped REST count can include other visible
        # users and produce a false "truncated result" failure.
        org_id = _resolve_user_search_org_id(gis, platform, logger)
        query = _build_user_query(org_id=org_id)
        if args.test:
            logger.info("Searching users (test limit=%d) ...", TEST_USER_LIMIT)
        else:
            logger.info(
                "Searching all users with authoritative total verification ...")
        user_list = _search_users(
            gis, session, portal_rest_url, token_provider, query,
            args.test, logger)

        logger.info("Inventorying %d user(s).", len(user_list))
        fieldnames = list(BASE_FIELDNAMES)
        if args.ad_check:
            fieldnames += AD_FIELDNAMES

        rows = []
        new_user_count = 0
        total_users = len(user_list)
        log_every = 25
        for index, user in enumerate(user_list, 1):
            if args.count_delay > 0 and index > 1:
                time.sleep(args.count_delay)
            try:
                row = _user_row(
                    user, platform, session, portal_rest_url, token_provider,
                    logger, run_start_epoch_ms, ad_resolver=ad_resolver)
            except Exception as exc:
                try:
                    uname = str(_user_attr(user, "username", "") or "<unknown>")
                except Exception:
                    uname = "<unknown>"
                try:
                    keys = ", ".join(_user_json_keys(user))
                except Exception:
                    keys = "<unavailable>"
                logger.error(
                    "Skipping user #%d/%d (%r) due to row build error: %s "
                    "(JSON keys present: %s)",
                    index, total_users, uname, redact_token(str(exc)), keys)
                continue
            if row.get("new_user") == "yes":
                new_user_count += 1
            rows.append(row)
            if index % log_every == 0 or index == total_users:
                logger.info("Inventoried %d/%d user(s) ...",
                            index, total_users)

        return rows, new_user_count, fieldnames


def _send_failure_notice(args, timestamp, platform, log_file_path, logger,
                         subject_label, message, start_wallclock, start_perf):
    safe_message = redact_token(str(message))
    body = (
        f"User Report FAILED\n\n"
        f"Organization        : {args.org_name}\n"
        f"Platform            : {platform}\n"
        f"Target              : {args.target}\n\n"
        f"{safe_message}\n\n"
        f"See the attached log for details. Any output file left on disk by "
        f"this failed run should not be treated as a completed report.\n\n"
        + timing_block(start_wallclock, start_perf)
    )
    send_email(
        subject_line(f"User Report ({subject_label})", timestamp, platform,
                     org_name=args.org_name),
        body,
        [log_file_path],
        args.smtp_server,
        args.email_to,
        args.email_from,
        logger,
    )


def _log_run_header(args, platform, max_email_mb, start_wallclock, logger):
    logger.info("=" * 70)
    logger.info("User Report - run started")
    logger.info("=" * 70)
    logger.info("Organization       : %s", args.org_name)
    logger.info("Platform           : %s", platform)
    logger.info("Target             : %s", args.target)
    logger.info("Config path        : %s", args.config)
    logger.info("Test mode          : %s", args.test)
    logger.info("AD check           : %s", args.ad_check)
    if args.ad_check:
        logger.info("AD domain controller: %s", args.dc)
    logger.info("TLS mode           : %s", args.tls_mode)
    logger.info("Max email MB       : %s", max_email_mb)
    logger.info("New user window    : %d day(s) from run start (%s)",
                NEW_USER_WINDOW_DAYS, start_wallclock)
    logger.info("=" * 70)


def main():
    parser = build_parser()
    args = parser.parse_args()

    base_timestamp = build_timestamp()
    logger = setup_logging(None, "USER_REPORT")

    finalize_config(
        args,
        required=("portal-url", "username", "keyring-service", "output-dir",
                  "smtp-server", "email-to", "email-from"),
        logger=logger)

    max_email_mb = get_max_email_mb(args.target, args.config)
    ensure_output_dir(args.output_dir)
    org_prefix = args.org_name_safe
    platform = args.target.upper()

    try:
        timestamp, log_file_path = _reserve_log_path(
            args.output_dir, org_prefix, platform, base_timestamp)
    except Exception as exc:
        logger.error("Could not reserve the run log path: %s",
                     redact_token(str(exc)))
        raise SystemExit(1) from exc

    logger = setup_logging(log_file_path, "USER_REPORT")
    tls_mode = _TLS_MODE_MAP[args.tls_mode]

    try:
        args.portal_url = _validate_https_portal_url(args.portal_url)
        ca_path = _resolve_ca_bundle_path(tls_mode, args.ca_bundle_path)
        if args.ad_check and not args.dc:
            raise ValueError("--ad-check requires --dc <domain-controller>")
        if args.ad_debug and not args.ad_check:
            raise ValueError("--ad-debug is only valid with --ad-check")
        if args.dc and not args.ad_check:
            logger.warning("--dc was supplied without --ad-check and is ignored.")

        url_platform = get_platform(args.portal_url)
        if url_platform == "UNKNOWN":
            raise ValueError(
                f"could not classify portal-url {args.portal_url!r}")
        if url_platform != platform:
            raise ValueError(
                f"configured portal-url maps to {url_platform}, but --target "
                f"is {args.target!r}; refusing to scan a mismatched target")
    except ValueError as exc:
        logger.error("Invalid run configuration: %s", exc)
        raise SystemExit(2) from exc

    start_wallclock, start_perf = start_timer()
    run_start_epoch_ms = time.time_ns() // 1_000_000
    _log_run_header(args, platform, max_email_mb, start_wallclock, logger)

    try:
        rows, new_user_count, fieldnames = _inventory_users(
            args, platform, tls_mode, ca_path, logger, run_start_epoch_ms)

        csv_name = f"{org_prefix}_{platform}_USER_REPORT_{timestamp}.csv"
        csv_path = os.path.join(args.output_dir, csv_name)
        written = write_dict_csv(
            rows, csv_path, fieldnames=fieldnames, logger=logger, label="user")

        timing = timing_block(start_wallclock, start_perf)
        attachments, note = gather_attachments(
            log_file_path, [written] if written else [], args.output_dir,
            logger, cap_mb=max_email_mb)

        ad_line = ""
        if args.ad_check:
            ad_line = (
                f"AD check            : ON (ADSI, current Windows login, "
                f"no password, forest via {args.dc})\n")

        mode_description = (
            f"first {TEST_USER_LIMIT} users" if args.test else "all users")
        body = (
            f"User Report Complete\n\n"
            f"Organization        : {args.org_name}\n"
            f"Platform            : {platform}\n"
            f"Target              : {args.target}\n"
            f"Test mode           : {args.test} ({mode_description})\n"
            f"{ad_line}"
            f"Users found         : {len(rows)}\n"
            f"New users (within {NEW_USER_WINDOW_DAYS} days): "
            f"{new_user_count}\n"
            f"CSV file            : "
            f"{csv_name if written else '(none - no users)'}\n\n"
            f"{timing}"
            f"{note}"
        )

        subject = subject_line(
            "User Report", timestamp, platform, org_name=args.org_name)
        send_email(
            subject, body, attachments, args.smtp_server, args.email_to,
            args.email_from, logger)

        logger.info(
            "Done. %d user(s) written to %s (%d flagged new_user=yes)",
            len(rows), csv_path if written else "(no file)", new_user_count)

    except _ReportAbort as exc:
        logger.error("Run aborted: %s", redact_token(str(exc)))
        _send_failure_notice(
            args, timestamp, platform, log_file_path, logger,
            exc.subject_label, str(exc), start_wallclock, start_perf)
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.exception("User Report failed unexpectedly: %s", exc)
        _send_failure_notice(
            args, timestamp, platform, log_file_path, logger,
            "FAILED", "Unexpected failure: " + redact_token(str(exc)),
            start_wallclock, start_perf)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()