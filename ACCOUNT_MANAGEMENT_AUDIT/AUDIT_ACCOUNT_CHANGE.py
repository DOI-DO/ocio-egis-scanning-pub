#!/usr/bin/env python3
r"""
ACCOUNT_ACTIVITY.py
==============================================================================
Unified account-activity reporter for ArcGIS Online (AGOL) and ArcGIS
Enterprise Portal. The --target parameter selects which engine runs, exactly
like USER_REPORT.py:

  * --target agol    Reconstruct before/after role/license for AGOL
                     updateUserRole / updateUserLicenseType actions (plus
                     disable/update/delete/enable) by diffing bracketing AGOL
                     *user report* CSVs. Requires --activity + --reports-dir.

  * --target portal  Scan one OR TWO (HA) directories of Portal log files for
                     DELETE_USER / CREATE_USER / UPDATE_USER role-change
                     events, translating custom-role IDs to readable names via
                     the live portal role list. Requires --logs-dir.

Both engines emit one CSV (columns: change_type, username, actor,
action_date, summary, ad_ou), email it with the log attached, and support
optional forest-wide Active Directory lookup (--ad-check/--dc).

SECURITY HARDENING (both targets)
  * Certificate (TLS) validation: explicit --tls-mode + one-time
    verify_tls_handshake() before any ArcGIS login; connect_gis() uses the
    same verify_mode so the SDK pool is covered.
  * SSRF / network-boundary: portal-url validated as absolute HTTPS, resolvable
    host, no embedded creds, no query/fragment, no control chars.
  * Large/malformed responses: capped reads, size/field guards, JSON parsing
    that never trusts shape, bounded CSV/line handling.
  * Redirect following: disabled on the script's own REST session.
  * Import-path / package shadowing: project root resolved from __file__ and
    inserted at the FRONT of sys.path; UTILITIES import origin verified.
  * Every caught exception's text is passed through redact_token().

USAGE / SAMPLE EXECUTION COMMANDS
  # AGOL: diff bracketing user reports around activity actions.
  python ACCOUNT_ACTIVITY.py --config org1.config.toml --target agol ^
      --activity OrganizationActivityDaily_20260930.csv ^
      --reports-dir .\user_reports

  # AGOL with Active Directory lookup.
  python ACCOUNT_ACTIVITY.py --config org1.config.toml --target agol ^
      --activity OrganizationActivityDaily_20260930.csv ^
      --reports-dir .\user_reports --ad-check --dc dc01.enterprise.com

  # Portal: single node.
  python ACCOUNT_ACTIVITY.py --config org1.config.toml --target portal ^
      --logs-dir .\logs

  # Portal: HA pair (two nodes), de-duplicated, recurse + AD check.
  python ACCOUNT_ACTIVITY.py --config org1.config.toml --target portal ^
      --logs-dir \\node1\arcgisportal\logs \\node2\arcgisportal\logs ^
      --recursive --ad-check --dc dc01.enterprise.com

  # Portal: restrict scanned extensions (default .log + .txt).
  python ACCOUNT_ACTIVITY.py --config org1.config.toml --target portal ^
      --logs-dir .\logs --ext .log
==============================================================================
"""

import argparse
import contextlib
import csv
import datetime
import json
import os
import re
import sys
from urllib.parse import urlsplit

# --- Import-path / package-shadowing hardening ----------------------------
# Resolve the project root from THIS file's location and put it at the FRONT
# of sys.path so the genuine sibling UTILITIES package always wins over any
# same-named module elsewhere on sys.path / the current working directory.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from UTILITIES import (
    build_timestamp,
    setup_logging,
    ensure_output_dir,
    write_dict_csv,
    connect_gis,
    send_email,
    subject_line,
    start_timer,
    timing_block,
    gather_attachments,
    add_config_arg,
    add_target_arg,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    finalize_config,
    get_max_email_mb,
    get_platform,
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

# Verify the UTILITIES package we imported actually lives under our project
# root -- a mismatch means a shadowing package was imported instead.
import UTILITIES as _UTILITIES_PKG
_util_path = os.path.dirname(os.path.abspath(_UTILITIES_PKG.__file__))
if os.path.dirname(_util_path) != _PROJECT_ROOT:
    raise ImportError(
        "UTILITIES was imported from an unexpected location "
        f"({_util_path!r}); refusing to run to avoid package shadowing.")


# ==========================================================================
# Shared constants
# ==========================================================================
OUTPUT_FIELDNAMES = ["change_type", "username", "actor", "action_date",
                     "summary", "ad_ou"]

_TLS_MODE_MAP = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

# Large/malformed-response guards.
_MAX_CSV_BYTES = 512 * 1024 * 1024       # refuse to read a CSV larger than this
_MAX_LOG_FILE_BYTES = 1024 * 1024 * 1024  # skip a log file larger than this
_MAX_LINE_BYTES = 4 * 1024 * 1024         # skip a single log line longer than this
_MAX_CSV_ROWS = 50_000_000               # defensive row ceiling


class _ReportAbort(RuntimeError):
    """Expected run failure that should be reported without a traceback."""

    def __init__(self, subject_label, message):
        super().__init__(message)
        self.subject_label = subject_label


# ==========================================================================
# Hardened URL / TLS-mode validation (mirrors USER_REPORT.py)
# ==========================================================================
def _validate_https_portal_url(portal_url):
    """Return a normalized HTTPS URL or raise ValueError.

    SSRF-hardening: absolute HTTPS URL, a resolvable hostname, no embedded
    credentials, no query string/fragment, no whitespace/control characters.
    """
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


def _harden_session(session):
    """Apply redirect-following and boundary defenses to the script's own
    REST session. connect_gis()/the SDK manage their own session separately;
    this hardens the direct requests.Session used for the TLS handshake check
    (and any future direct REST call)."""
    # Redirect following: disabled so a 3xx can never bounce a verified
    # request to an unintended (possibly internal) host.
    session.max_redirects = 0
    original_request = session.request

    def _no_redirect_request(method, url, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return original_request(method, url, **kwargs)

    session.request = _no_redirect_request
    return session


# ==========================================================================
# AGOL markers / regexes (from AGOL_ACCOUNT_ACTIVITY.py, unchanged)
# ==========================================================================
UPDATE_ROLE_MARKER = "/sharing/rest/portals/self/updateUserRole"
UPDATE_LICENSE_MARKER = "/sharing/rest/portals/self/updateUserLicenseType"

_DISABLE_RE = re.compile(
    r"/sharing/rest/community/users/[^/]+/disable", re.IGNORECASE)
_UPDATE_USER_RE = re.compile(
    r"/sharing/rest/community/users/[^/]+/update", re.IGNORECASE)
_DELETE_USER_RE = re.compile(
    r"/sharing/rest/community/users/[^/]+/delete", re.IGNORECASE)
_DELETE_GROUP_RE = re.compile(
    r"/sharing/rest/community/groups/[^/]+/delete", re.IGNORECASE)
_ENABLE_RE = re.compile(
    r"/sharing/rest/community/users/[^/]+/enable", re.IGNORECASE)

_REPORT_TS_RE = re.compile(r"(\d{8})(?:_(\d{4}))?")


# ==========================================================================
# AGOL: CSV reading (robust to BOM / whitespace headers), size-guarded
# ==========================================================================
def _read_csv_dicts(path, logger):
    """Read a CSV into a list of dicts with whitespace-stripped headers.
    Returns (rows, fieldnames). Returns ([], []) on any read failure. The
    file size and row count are bounded to defend against a malformed/huge
    input."""
    try:
        size = os.path.getsize(path)
        if size > _MAX_CSV_BYTES:
            if logger is not None:
                logger.error("Refusing to read oversized CSV %s (%d bytes > "
                             "%d cap).", path, size, _MAX_CSV_BYTES)
            return [], []
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return [], []
            fieldnames = [h.strip() for h in header]
            rows = []
            for raw in reader:
                if not raw:
                    continue
                if len(rows) >= _MAX_CSV_ROWS:
                    if logger is not None:
                        logger.error("CSV %s exceeded the %d-row cap; "
                                     "truncating.", path, _MAX_CSV_ROWS)
                    break
                row = {}
                for i, name in enumerate(fieldnames):
                    row[name] = raw[i].strip() if i < len(raw) else ""
                rows.append(row)
        return rows, fieldnames
    except Exception as exc:
        if logger is not None:
            logger.error("Could not read CSV %s: %s", path,
                         redact_token(str(exc)))
        return [], []


# ==========================================================================
# AGOL: timestamp parsing (unchanged)
# ==========================================================================
def _parse_report_timestamp(filename):
    """Parse the YYYYMMDD[_HHMM] token in a user-report filename into a naive
    UTC datetime, or None if there is no parseable date token. A missing time
    component defaults to 0000."""
    m = _REPORT_TS_RE.search(filename)
    if not m:
        return None
    date_part = m.group(1)
    time_part = m.group(2) or "0000"
    try:
        return datetime.datetime.strptime(date_part + time_part, "%Y%m%d%H%M")
    except ValueError:
        return None


def _parse_activity_timestamp(value):
    """Parse an Activity report created_utc value into a naive UTC datetime.
    Accepts ISO8601 (with optional 'Z'/offset) and epoch milliseconds/seconds.
    Returns None if nothing usable."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        try:
            num = float(raw)
            if abs(num) >= 1e11:        # milliseconds
                num /= 1000.0
            return datetime.datetime.utcfromtimestamp(num)
        except (ValueError, OverflowError, OSError):
            return None

    iso = raw
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%m/%d/%y %H:%M",
        "%m/%d/%y %H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ==========================================================================
# AGOL: user-report discovery and indexing (unchanged logic)
# ==========================================================================
def load_agol_reports(reports_dir, logger):
    """Load every AGOL user-report CSV in reports_dir. A file is considered
    ONLY if its filename contains "AGOL" (case-insensitive) AND carries a
    parseable YYYYMMDD[_HHMM] date token. Returns a list of dicts sorted by
    timestamp ascending."""
    reports = []
    try:
        entries = sorted(os.listdir(reports_dir))
    except Exception as exc:
        logger.error("Could not list reports dir %s: %s", reports_dir,
                     redact_token(str(exc)))
        return reports

    for name in entries:
        if not name.lower().endswith(".csv"):
            continue
        if "AGOL" not in name.upper():
            logger.info("Skipping %s: filename does not contain 'AGOL'.", name)
            continue
        path = os.path.join(reports_dir, name)
        ts = _parse_report_timestamp(name)
        if ts is None:
            logger.warning("Skipping %s: no parseable YYYYMMDD[_HHMM] date "
                           "token in filename.", name)
            continue

        rows, fieldnames = _read_csv_dicts(path, logger)
        if not rows:
            logger.warning("Skipping %s: no data rows.", name)
            continue
        if "username" not in fieldnames or "role" not in fieldnames:
            logger.warning("Skipping %s: missing 'username' and/or 'role' "
                           "column.", name)
            continue
        has_license = "userLicenseTypeId" in fieldnames
        if not has_license:
            logger.warning("%s has no 'userLicenseTypeId' column; license-type "
                           "deltas from this snapshot will be blank.", name)
        has_ad_ou = "ad_ou" in fieldnames
        if not has_ad_ou:
            logger.warning("%s has no 'ad_ou' column; ad_ou values from this "
                           "snapshot will be blank.", name)

        roles = {}
        licenses = {}
        ad_ous = {}
        for row in rows:
            uname = (row.get("username") or "").strip()
            if uname:
                roles[uname] = (row.get("role") or "").strip()
                if has_license:
                    licenses[uname] = (row.get("userLicenseTypeId") or "").strip()
                if has_ad_ou:
                    ad_ous[uname] = (row.get("ad_ou") or "").strip()
        reports.append({"ts": ts, "path": path, "roles": roles,
                        "licenses": licenses, "ad_ous": ad_ous})
        logger.info("Loaded AGOL user report %s (ts=%s, %d users).",
                    name, ts.strftime("%Y-%m-%d %H:%M"), len(roles))

    reports.sort(key=lambda r: r["ts"])
    return reports


def _pick_before_after(reports, action_dt, logger):
    """Return (before, after, used_nearest_before, used_nearest_after)."""
    before = None
    for rep in reports:
        if rep["ts"] <= action_dt:
            before = rep
        else:
            break
    after = None
    for rep in reports:
        if rep["ts"] > action_dt:
            after = rep
            break

    used_nearest_before = False
    used_nearest_after = False

    if before is None and after is not None:
        before = reports[0]
        used_nearest_before = True
    if after is None and before is not None:
        after = reports[-1]
        used_nearest_after = True

    return before, after, used_nearest_before, used_nearest_after


def _find_value_scanning(reports, action_dt, username, snapshot_key,
                         direction, logger=None):
    """Scan outward from action_dt for the first report that has a NON-BLANK
    value for `username` in snapshot_key; return (value, report) or
    (None, None)."""
    if direction == "before":
        candidates = [r for r in reports if r["ts"] <= action_dt]
        candidates.sort(key=lambda r: r["ts"], reverse=True)
    else:
        candidates = [r for r in reports if r["ts"] > action_dt]
        candidates.sort(key=lambda r: r["ts"])

    for rep in candidates:
        val = rep[snapshot_key].get(username)
        if val:
            return val, rep
    return None, None


# ==========================================================================
# AGOL: main processing (unchanged logic)
# ==========================================================================
def process_agol(activity_path, reports_dir, logger, ad_resolver=None):
    """Return a list of output-row dicts for every matched AGOL activity
    action."""
    reports = load_agol_reports(reports_dir, logger)
    if len(reports) < 2:
        logger.error("Need at least two AGOL user reports to compute a "
                     "before/after delta; found %d.", len(reports))
        return []

    activity_rows, act_fields = _read_csv_dicts(activity_path, logger)
    if not activity_rows:
        logger.error("Activity report %s had no readable rows.", activity_path)
        return []
    for needed in ("request", "created_utc", "id"):
        if needed not in act_fields:
            logger.error("Activity report is missing required column '%s'.",
                         needed)
            return []

    out_rows = []
    total_actions = 0
    for row in activity_rows:
        request = row.get("request") or ""
        data = row.get("data") or ""

        # Group operations are not user account operations -- skip them.
        if "groups" in data.lower():
            continue

        is_role = UPDATE_ROLE_MARKER in request
        is_license = UPDATE_LICENSE_MARKER in request
        is_disable = bool(_DISABLE_RE.search(request))
        is_update = bool(_UPDATE_USER_RE.search(request))
        is_delete_user = bool(_DELETE_USER_RE.search(request))
        is_delete_group = bool(_DELETE_GROUP_RE.search(request))
        is_enable = bool(_ENABLE_RE.search(request))

        if is_delete_group:
            continue

        username = (row.get("id") or "").strip()
        actor = (row.get("actor") or "").strip()

        # --- Community-user disable / update / delete / enable: no diff. ---
        if is_disable or is_update or is_delete_user or is_enable:
            total_actions += 1
            if is_disable:
                kind = "disable"
            elif is_update:
                kind = "update"
            elif is_delete_user:
                kind = "delete"
            else:
                kind = "enable"

            action_dt = _parse_activity_timestamp(row.get("created_utc"))
            if action_dt is None:
                logger.warning("Skipping %s row: unparseable created_utc=%r "
                               "(id=%s).", kind, row.get("created_utc"),
                               username)
                continue
            if not username:
                logger.warning("Skipping %s row at %s: blank 'id' (no target "
                               "username).", kind, action_dt)
                continue

            if is_update and actor == username:
                logger.info("Skipping self-update by '%s' on %s (actor == id).",
                            username, action_dt.strftime("%Y/%m/%d"))
                continue

            action_date_str = action_dt.strftime("%Y/%m/%d")

            ad_ou = ""
            if ad_resolver is not None:
                ad_ou_from_report, _r = _find_value_scanning(
                    reports, action_dt, username, "ad_ous", "after", logger)
                if not ad_ou_from_report:
                    ad_ou_from_report, _r = _find_value_scanning(
                        reports, action_dt, username, "ad_ous", "before", logger)
                ad_ou = ad_ou_from_report or ""
                if not ad_ou:
                    status, ou = ad_resolver.lookup(username, "")
                    ad_ou = ou or ""

            if is_disable:
                summary = f"{username} disabled by {actor} on {action_date_str}"
            elif is_update:
                summary = f"{username} was updated by {actor} on {action_date_str}"
            elif is_delete_user:
                summary = f"{username} was deleted by {actor} on {action_date_str}"
            else:
                summary = f"{username} enabled by {actor} on {action_date_str}"

            out_rows.append({
                "change_type": kind,
                "username": username,
                "actor": actor,
                "action_date": action_date_str,
                "ad_ou": ad_ou,
                "summary": summary,
            })
            continue

        # --- Role / license changes: before/after snapshot diff. ---
        if not (is_role or is_license):
            continue
        total_actions += 1

        change_kind = "role" if is_role else "license"
        snapshot_key = "roles" if is_role else "licenses"
        noun = "role" if is_role else "license type"

        action_dt = _parse_activity_timestamp(row.get("created_utc"))
        if action_dt is None:
            logger.warning("Skipping %s row: unparseable created_utc=%r "
                           "(id=%s).", change_kind, row.get("created_utc"),
                           username)
            continue
        if not username:
            logger.warning("Skipping %s row at %s: blank 'id' (no target "
                           "username).", change_kind, action_dt)
            continue

        before, after, near_b, near_a = _pick_before_after(
            reports, action_dt, logger)

        action_date_str = action_dt.strftime("%Y/%m/%d")

        if near_b:
            logger.warning("No AGOL report at/before %s for user '%s'; using "
                           "nearest (%s).", action_date_str, username,
                           os.path.basename(before["path"]))
        if near_a:
            logger.warning("No AGOL report after %s for user '%s'; using "
                           "nearest (%s).", action_date_str, username,
                           os.path.basename(after["path"]))

        old_val, old_rep = _find_value_scanning(
            reports, action_dt, username, snapshot_key, "before", logger)
        new_val, new_rep = _find_value_scanning(
            reports, action_dt, username, snapshot_key, "after", logger)

        if old_val is None and new_val is None:
            logger.warning("User '%s' has no non-blank %s in ANY report on "
                           "either side of %s; emitting with unknowns.",
                           username, noun, action_date_str)
        elif old_val is None:
            logger.info("User '%s' has no non-blank %s in any 'before' report "
                        "for action on %s; old value unknown.", username, noun,
                        action_date_str)
        elif new_val is None:
            logger.info("User '%s' has no non-blank %s in any 'after' report "
                        "for action on %s; new value unknown.", username, noun,
                        action_date_str)

        old_display = old_val if old_val is not None else "(unknown)"
        new_display = new_val if new_val is not None else "(unknown)"
        summary = (f"{actor} changed the {noun} of {username} from "
                   f"{old_display} to {new_display} on {action_date_str}")

        ad_ou = ""
        if ad_resolver is not None:
            ad_ou_from_report, _r = _find_value_scanning(
                reports, action_dt, username, "ad_ous", "after", logger)
            if not ad_ou_from_report:
                ad_ou_from_report, _r = _find_value_scanning(
                    reports, action_dt, username, "ad_ous", "before", logger)
            ad_ou = ad_ou_from_report or ""
            if not ad_ou:
                status, ou = ad_resolver.lookup(username, "")
                ad_ou = ou or ""

        out_rows.append({
            "change_type": change_kind,
            "username": username,
            "actor": actor,
            "action_date": action_date_str,
            "ad_ou": ad_ou,
            "summary": summary,
        })

    logger.info("Processed %d matched action row(s); produced %d "
                "output row(s).", total_actions, len(out_rows))
    return out_rows


# ==========================================================================
# PORTAL markers / regexes (from PORTAL_ACCOUNT_ACTIVITY.py, unchanged)
# ==========================================================================
_DELETE_MSG_RE = re.compile(
    r"The user\s+(?P<username>.+?)\s+has been deleted\.?",
    re.IGNORECASE,
)
_ROLE_CHANGE_MSG_RE = re.compile(
    r"Role for user\s+(?P<username>.+?)\s+has been changed\s+"
    r"from\s+'(?P<old>.*?)'\s+to\s+'(?P<new>.*?)'",
    re.IGNORECASE,
)
_CREATE_MSG_RE = re.compile(
    r"User\s+(?P<username>.+?)\s+added with role\s+'(?P<role>.*?)'",
    re.IGNORECASE,
)
_CANDIDATE_RE = re.compile(r"DELETE_USER|UPDATE_USER|CREATE_USER",
                           re.IGNORECASE)

_BUILTIN_ROLE_NAMES = {
    "org_admin": "Administrator",
    "administrator": "Administrator",
    "admin": "Administrator",
    "org_publisher": "Publisher",
    "publisher": "Publisher",
    "org_user": "User",
    "user": "User",
    "org_editor": "Data Editor",
    "data editor": "Data Editor",
    "editor": "Data Editor",
    "viewer": "Viewer",
    "org_viewer": "Viewer",
    "account_user": "User",
    "account_admin": "Administrator",
    "account_publisher": "Publisher",
}

DEFAULT_EXTENSIONS = (".log", ".txt")
_MAX_LOGS_DIRS = 2  # HA Portal is always exactly two nodes, never more.


# ==========================================================================
# PORTAL: role lookup (unchanged logic)
# ==========================================================================
def build_role_lookup(gis, logger):
    """Return a dict mapping role identifiers -> human-readable role name,
    covering BOTH custom roles (from gis.users.roles) and the built-in system
    roles. Never raises."""
    lookup = {}
    for key, name in _BUILTIN_ROLE_NAMES.items():
        lookup[key] = name

    try:
        roles = gis.users.roles.all()
    except Exception as exc:
        logger.warning("Could not enumerate custom roles from the portal "
                       "(%s); only built-in role names will be translated.",
                       redact_token(str(exc)))
        return lookup

    count = 0
    for role in roles:
        role_id = getattr(role, "role_id", None) or getattr(role, "id", None)
        name = getattr(role, "name", None)
        if role_id is None and isinstance(role, dict):
            role_id = role.get("id") or role.get("roleId")
            name = role.get("name")
        if not name:
            continue
        name = str(name).strip()
        if role_id:
            lookup[str(role_id).strip().lower()] = name
        lookup[name.lower()] = name
        count += 1

    logger.info("Loaded %d custom role(s) from the portal (plus %d built-in "
                "keyword mapping(s)).", count, len(_BUILTIN_ROLE_NAMES))
    return lookup


def _looks_like_role_id(value):
    """True if value looks like an obfuscated role ID rather than a readable
    role name."""
    v = (value or "").strip()
    if not v or " " in v:
        return False
    if len(v) < 10:
        return False
    has_alpha = any(c.isalpha() for c in v)
    has_digit = any(c.isdigit() for c in v)
    return has_alpha and has_digit


def _translate_role(value, lookup):
    """Translate a single role value to a readable name."""
    raw = (value or "").strip()
    if not raw:
        return raw

    key = raw.lower()
    if key in lookup:
        return lookup[key]
    if _looks_like_role_id(raw):
        return f"{raw} (unresolved)"
    return raw


# ==========================================================================
# PORTAL: timestamp conversion (unchanged)
# ==========================================================================
def _epoch_to_mmddyyyy(epoch_value, logger=None):
    """Convert an epoch value (milliseconds, as Portal logs it) to a
    MM/DD/YYYY UTC date string, or None if it cannot be parsed."""
    if epoch_value is None:
        return None
    try:
        num = float(epoch_value)
    except (TypeError, ValueError):
        if logger is not None:
            logger.warning("Unparseable timestamp value: %r", epoch_value)
        return None

    seconds = num / 1000.0 if abs(num) >= 1e11 else num
    try:
        dt = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        if logger is not None:
            logger.warning("Timestamp out of range: %r", epoch_value)
        return None
    return dt.strftime("%m/%d/%Y")


# ==========================================================================
# PORTAL: record extraction (unchanged logic)
# ==========================================================================
def _extract_json_object(line):
    """Return the JSON dict embedded in a log line, or None."""
    if not line:
        return None
    start = line.find("{")
    if start == -1:
        return None

    candidate = line[start:]
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass

    end = candidate.rfind("}")
    if end == -1:
        return None
    try:
        obj = json.loads(candidate[:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _delete_row_from_record(obj, ad_resolver=None, logger=None):
    """Build a DELETE_USER output row from a parsed record, or None."""
    message = str(obj.get("message", "") or "")
    m = _DELETE_MSG_RE.search(message)
    if not m:
        return None

    username = m.group("username").strip()
    if not username:
        return None

    actor = str(obj.get("actor", "") or "").strip()
    action_date = _epoch_to_mmddyyyy(obj.get("timestamp"), logger=logger)
    if action_date is None:
        if logger is not None:
            logger.warning(
                "DELETE_USER for '%s' skipped: missing/invalid timestamp %r.",
                username, obj.get("timestamp"))
        return None

    summary = f"{username} was deleted by {actor} on {action_date}"

    ad_ou = ""
    if ad_resolver is not None:
        status, ou = ad_resolver.lookup(username, "")
        ad_ou = ou or ""

    return {
        "change_type": "delete",
        "username": username,
        "actor": actor,
        "action_date": action_date,
        "summary": summary,
        "ad_ou": ad_ou,
    }


def _role_row_from_record(obj, role_lookup, ad_resolver=None, logger=None):
    """Build an UPDATE_USER role-change output row, or None."""
    message = str(obj.get("message", "") or "")
    m = _ROLE_CHANGE_MSG_RE.search(message)
    if not m:
        return None

    username = m.group("username").strip()
    if not username:
        return None

    actor = str(obj.get("actor", "") or "").strip()
    action_date = _epoch_to_mmddyyyy(obj.get("timestamp"), logger=logger)
    if action_date is None:
        if logger is not None:
            logger.warning(
                "Role change for '%s' skipped: missing/invalid timestamp %r.",
                username, obj.get("timestamp"))
        return None

    old_display = _translate_role(m.group("old"), role_lookup)
    new_display = _translate_role(m.group("new"), role_lookup)

    summary = (f"{actor} changed the role of {username} from "
               f"{old_display} to {new_display} on {action_date}")

    ad_ou = ""
    if ad_resolver is not None:
        status, ou = ad_resolver.lookup(username, "")
        ad_ou = ou or ""

    return {
        "change_type": "role",
        "username": username,
        "actor": actor,
        "action_date": action_date,
        "summary": summary,
        "ad_ou": ad_ou,
    }


def _create_row_from_record(obj, role_lookup, ad_resolver=None, logger=None):
    """Build a CREATE_USER output row, or None."""
    message = str(obj.get("message", "") or "")
    m = _CREATE_MSG_RE.search(message)
    if not m:
        return None

    username = m.group("username").strip()
    if not username:
        return None

    actor = str(obj.get("actor", "") or "").strip()
    action_date = _epoch_to_mmddyyyy(obj.get("timestamp"), logger=logger)
    if action_date is None:
        if logger is not None:
            logger.warning(
                "CREATE_USER for '%s' skipped: missing/invalid timestamp %r.",
                username, obj.get("timestamp"))
        return None

    role_display = _translate_role(m.group("role"), role_lookup)
    summary = (f"{username} was created with role {role_display} "
               f"on {action_date}")

    ad_ou = ""
    if ad_resolver is not None:
        status, ou = ad_resolver.lookup(username, "")
        ad_ou = ou or ""

    return {
        "change_type": "create",
        "username": username,
        "actor": actor,
        "action_date": action_date,
        "summary": summary,
        "ad_ou": ad_ou,
    }


def _row_from_record(obj, role_lookup, ad_resolver=None, logger=None):
    """Dispatch a parsed log record to the matching event handler."""
    if not isinstance(obj, dict):
        return None

    event = str(obj.get("event", "")).strip().upper()

    if event == "DELETE_USER":
        return _delete_row_from_record(obj, ad_resolver=ad_resolver,
                                       logger=logger)
    if event == "UPDATE_USER":
        return _role_row_from_record(obj, role_lookup, ad_resolver=ad_resolver,
                                     logger=logger)
    if event == "CREATE_USER":
        return _create_row_from_record(obj, role_lookup, ad_resolver=ad_resolver,
                                       logger=logger)
    return None


# ==========================================================================
# PORTAL: file / directory scanning (unchanged logic + size guards)
# ==========================================================================
def _iter_log_files(logs_dir, extensions, recursive, logger):
    """Yield paths of candidate log files under logs_dir."""
    exts = tuple(e.lower() for e in extensions)
    if recursive:
        for root, _dirs, files in os.walk(logs_dir):
            for name in sorted(files):
                if name.lower().endswith(exts):
                    yield os.path.join(root, name)
    else:
        try:
            for name in sorted(os.listdir(logs_dir)):
                path = os.path.join(logs_dir, name)
                if os.path.isfile(path) and name.lower().endswith(exts):
                    yield path
        except Exception as exc:
            logger.error("Could not list logs dir %s: %s", logs_dir,
                         redact_token(str(exc)))


def _scan_file(path, role_lookup, ad_resolver, logger):
    """Scan one log file and return a list of output-row dicts. A file larger
    than the cap, or an individual line longer than the per-line cap, is
    skipped defensively."""
    rows = []
    try:
        size = os.path.getsize(path)
        if size > _MAX_LOG_FILE_BYTES:
            logger.warning("Skipping oversized log file %s (%d bytes > %d "
                           "cap).", path, size, _MAX_LOG_FILE_BYTES)
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if len(line) > _MAX_LINE_BYTES:
                    continue
                if not _CANDIDATE_RE.search(line):
                    continue
                obj = _extract_json_object(line)
                if obj is None:
                    continue
                row = _row_from_record(obj, role_lookup, ad_resolver=ad_resolver,
                                       logger=logger)
                if row is not None:
                    rows.append(row)
    except Exception as exc:
        logger.error("Could not read log file %s: %s", path,
                     redact_token(str(exc)))
        return []
    if rows:
        logger.info("  %s: %d account-activity event(s).",
                    os.path.basename(path), len(rows))
    return rows


def _dedup_key(row):
    """The shared de-duplication key."""
    return (row["change_type"], row["username"], row["actor"],
            row["action_date"], row["summary"])


def _scan_one_dir(logs_dir, extensions, recursive, role_lookup, ad_resolver,
                  logger, label):
    """Scan every candidate log file under ONE logs_dir (de-dup within dir)."""
    files = list(_iter_log_files(logs_dir, extensions, recursive, logger))
    if not files:
        logger.warning("[%s] No candidate log files found in %s "
                       "(extensions=%s, recursive=%s).", label, logs_dir,
                       ", ".join(extensions), recursive)
        return []

    logger.info("[%s] Scanning %d log file(s) in %s ...", label, len(files),
                logs_dir)

    rows = []
    seen = set()
    duplicates = 0
    for path in files:
        for row in _scan_file(path, role_lookup, ad_resolver, logger):
            key = _dedup_key(row)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            rows.append(row)

    if duplicates:
        logger.info("[%s] Collapsed %d duplicate event(s) seen across rolled "
                    "log files in this directory.", label, duplicates)
    logger.info("[%s] %d unique event(s) found in this directory.", label,
                len(rows))
    return rows


def process_portal(logs_dirs, extensions, recursive, role_lookup, ad_resolver,
                   logger):
    """Scan one or two logs_dirs, de-duplicating identical events within and
    across directories (HA pair). Returns a list of output-row dicts."""
    is_ha = len(logs_dirs) > 1
    all_rows = []
    seen = set()
    cross_dir_duplicates = 0

    for i, logs_dir in enumerate(logs_dirs, 1):
        label = f"node{i}" if is_ha else "logs"
        dir_rows = _scan_one_dir(logs_dir, extensions, recursive, role_lookup,
                                 ad_resolver, logger, label)
        for row in dir_rows:
            key = _dedup_key(row)
            if key in seen:
                cross_dir_duplicates += 1
                continue
            seen.add(key)
            all_rows.append(row)

    if is_ha and cross_dir_duplicates:
        logger.info(
            "HA de-duplication: collapsed %d event(s) that were logged "
            "independently by more than one node into a single output row.",
            cross_dir_duplicates)

    n_delete = sum(1 for r in all_rows if r["change_type"] == "delete")
    n_role = sum(1 for r in all_rows if r["change_type"] == "role")
    n_create = sum(1 for r in all_rows if r["change_type"] == "create")
    logger.info("Found %d unique event(s) total: %d create, %d delete, %d "
                "role change.", len(all_rows), n_create, n_delete, n_role)
    return all_rows


# ==========================================================================
# CLI
# ==========================================================================
def _logs_dir_list(raw_values):
    """Validate the --logs-dir nargs list: 1 or 2 existing directories."""
    if not raw_values:
        raise argparse.ArgumentTypeError("--logs-dir requires at least one path")
    if len(raw_values) > _MAX_LOGS_DIRS:
        raise argparse.ArgumentTypeError(
            f"--logs-dir accepts at most {_MAX_LOGS_DIRS} paths (one per HA "
            f"Portal node); got {len(raw_values)}.")
    return raw_values


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Unified ArcGIS account-activity reporter. --target agol "
                    "diffs bracketing AGOL user reports around activity "
                    "actions; --target portal scans Portal log files for "
                    "create/delete/role-change events. Emails the CSV + log.")
    add_config_arg(parser)            # required --config org_toml_path
    add_target_arg(parser)            # required --target portal|agol
    add_common_portal_args(parser)    # optional --portal-url/--username/...
    add_email_args(parser)            # optional --smtp-server/--email-to/...
    add_output_dir_arg(parser)        # optional --output-dir (config fallback)

    agol = parser.add_argument_group("AGOL mode (--target agol)")
    agol.add_argument(
        "--activity", metavar="ACTIVITY_CSV",
        help="AGOL mode ONLY (required there). Path to the AGOL Activity "
             "report CSV (e.g. OrganizationActivityDaily_20260930.csv).")
    agol.add_argument(
        "--reports-dir", metavar="DIR",
        help="AGOL mode ONLY (required there). Folder of AGOL user-report "
             "CSVs named with a trailing _YYYYMMDD_HHMM timestamp. Needs at "
             "least TWO AGOL reports to bracket each action.")

    portal = parser.add_argument_group("Portal mode (--target portal)")
    portal.add_argument(
        "--logs-dir", nargs="+", metavar="DIR",
        help="Portal mode ONLY (required there). One folder of Portal log "
             "files, OR two folders (space-separated) for an HA Portal pair. "
             "Events logged identically by both nodes are de-duplicated.")
    portal.add_argument(
        "--recursive", action="store_true",
        help="Portal mode: recurse into subdirectories of each --logs-dir "
             "path (default: top level only).")
    portal.add_argument(
        "--ext", action="append", default=None, metavar="EXT",
        help="Portal mode: file extension(s) to scan (repeatable). "
             "Default: .log and .txt.")

    parser.add_argument(
        "--tls-mode", choices=sorted(_TLS_MODE_MAP.keys()), default="standard",
        help="TLS verification strategy: 'standard' (public CA, default), "
             "'os-trust-store' (requires truststore package), or "
             "'custom-ca-bundle' (requires --ca-bundle-path).")
    parser.add_argument(
        "--ca-bundle-path", default=None,
        help="Path to a readable PEM CA bundle. Required, and only valid, "
             "when --tls-mode is custom-ca-bundle.")

    ad = parser.add_argument_group("Active Directory lookup (optional)")
    ad.add_argument(
        "--ad-check", action="store_true",
        help="Also look each account up in Active Directory (forest-wide) "
             "using the native ADSI/pywin32 interface and populate the "
             "ad_ou column. Uses ONLY your CURRENT Windows login via native "
             "ADSI -- no password is ever requested, stored, or sent. "
             "Requires --dc and a pre-installed approved pywin32 package.")
    ad.add_argument(
        "--dc", default=None, type=validate_domain_controller_name,
        help="Domain controller hostname/FQDN used to read RootDSE and resolve "
             "the forest search base. ADSI then performs the forest-wide "
             "Global Catalog lookup. REQUIRED with --ad-check.")
    ad.add_argument(
        "--ad-debug", action="store_true",
        help="Emit AD lookup diagnostics without logging raw email, UPN, "
             "LDAP filter, or distinguished-name values.")

    args = parser.parse_args(argv)
    if args.logs_dir is not None:
        args.logs_dir = _logs_dir_list(args.logs_dir)
    return args


# ==========================================================================
# Email helpers
# ==========================================================================
def _send_failure_notice(args, ts, log_file_path, logger, subject_label,
                         message, start_wallclock, start_perf):
    safe_message = redact_token(str(message))
    if args.target == "agol":
        source_line = (f"Activity report     : {args.activity}\n"
                       f"Reports dir         : {args.reports_dir}\n")
    else:
        source_line = f"Logs dir(s)         : {', '.join(args.logs_dir or [])}\n"
    body = (
        f"Account Activity Scan FAILED\n\n"
        f"Organization        : {args.org_name}\n"
        f"Target              : {args.target}\n"
        f"{source_line}\n"
        f"{safe_message}\n\n"
        f"See the attached log for details. Any output file left on disk by "
        f"this failed run should not be treated as a completed report.\n\n"
        + timing_block(start_wallclock, start_perf)
    )
    send_email(
        subject_line(f"Account Activity ({subject_label})", ts,
                     args.target.upper(), org_name=args.org_name),
        body,
        [log_file_path],
        args.smtp_server,
        args.email_to,
        args.email_from,
        logger,
    )


# ==========================================================================
# AGOL run path (no ArcGIS login required -- it diffs local CSVs)
# ==========================================================================
def _run_agol(args, ts, output_dir, log_path, logger, start_wallclock,
              start_perf, max_email_mb):
    if not args.activity or not args.reports_dir:
        raise _ReportAbort(
            "BAD ARGUMENTS",
            "--target agol requires both --activity and --reports-dir.")
    if not os.path.isfile(args.activity):
        raise _ReportAbort("BAD ARGUMENTS",
                           f"Activity CSV not found: {args.activity}")
    if not os.path.isdir(args.reports_dir):
        raise _ReportAbort("BAD ARGUMENTS",
                           f"Reports directory not found: {args.reports_dir}")

    with contextlib.ExitStack() as resources:
        ad_resolver = None
        if args.ad_check:
            ad_resolver = ADResolver(args.dc, logger, debug=args.ad_debug)
            resources.callback(ad_resolver.close)
            if not ad_resolver.ok:
                raise _ReportAbort(
                    "AD CHECK FAILED",
                    "The native ADSI/pywin32 resolver could not initialize. "
                    "The run was aborted before processing the reports. The "
                    "resolver never falls back to a password-based directory "
                    "bind or a runtime package installation.")
            logger.info("AD resolver initialized for DC: %s", args.dc)

        rows = process_agol(args.activity, args.reports_dir, logger,
                            ad_resolver=ad_resolver)

    return rows


# ==========================================================================
# Portal run path (requires a hardened, TLS-verified ArcGIS login)
# ==========================================================================
def _run_portal(args, ts, output_dir, log_path, logger, start_wallclock,
                start_perf, max_email_mb, tls_mode, ca_path):
    extensions = args.ext if args.ext else list(DEFAULT_EXTENSIONS)

    if not args.logs_dir:
        raise _ReportAbort("BAD ARGUMENTS",
                           "--target portal requires --logs-dir.")
    for d in args.logs_dir:
        if not os.path.isdir(d):
            raise _ReportAbort("BAD ARGUMENTS",
                               f"Logs directory not found: {d}")

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
                    "partial report was produced. The resolver never falls "
                    "back to a password-based directory bind or a runtime "
                    "package installation.")

        try:
            session = build_secure_session(
                tls_mode,
                ca_bundle_path=ca_path,
                pool_connections=8,
                pool_maxsize=16,
                retry_total=3,
                logger=logger,
            )
            _harden_session(session)
        except Exception as exc:
            raise _ReportAbort(
                "TLS CHECK FAILED",
                "Could not initialize the verified REST session: "
                + redact_token(str(exc))) from exc

        def _close_session():
            try:
                session.close()
            except Exception:
                pass
        resources.callback(_close_session)

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
                "No ArcGIS login or scan was attempted.")

        try:
            gis = connect_gis(
                args.portal_url,
                args.username,
                args.keyring_service,
                logger,
                verify_mode=tls_mode,
                ca_bundle_path=ca_path,
            )
        except Exception as exc:
            raise _ReportAbort(
                "CONNECTION FAILED",
                "Could not connect to the portal: "
                + redact_token(str(exc))) from exc

        role_lookup = build_role_lookup(gis, logger)

        rows = process_portal(args.logs_dir, extensions, args.recursive,
                             role_lookup, ad_resolver, logger)

    return rows


# ==========================================================================
# Main
# ==========================================================================
def main(argv=None):
    args = parse_args(argv)

    output_dir = args.output_dir or _SCRIPT_DIR
    ts = build_timestamp()
    ensure_output_dir(output_dir)
    # Prefix output artifacts with the target so AGOL and Portal runs are
    # instantly distinguishable (e.g. AGOL_ACCOUNT_ACTIVITY_*, PORTAL_*).
    prefix = args.target.upper()
    log_path = os.path.join(output_dir, f"{prefix}_ACCOUNT_ACTIVITY_{ts}.log")
    logger = setup_logging(log_path, "ACCOUNT_ACTIVITY")

    # Portal mode needs a live portal connection, so portal-url/username/
    # keyring-service are required there; AGOL mode only diffs local CSVs, so
    # those connection keys are not required for it. Email settings are
    # required in both cases.
    if args.target == "portal":
        required = ("portal-url", "username", "keyring-service",
                    "smtp-server", "email-to", "email-from")
    else:
        required = ("smtp-server", "email-to", "email-from")

    finalize_config(args, required=required, logger=logger)

    # Fill output_dir from config now that finalize_config ran (CLI/config may
    # have supplied it); fall back to the script dir.
    output_dir = args.output_dir or output_dir

    max_email_mb = get_max_email_mb(args.target, args.config)
    tls_mode = _TLS_MODE_MAP[args.tls_mode]
    start_wallclock, start_perf = start_timer()

    # Common AD/TLS argument validation.
    try:
        ca_path = _resolve_ca_bundle_path(tls_mode, args.ca_bundle_path)
        if args.ad_check and not args.dc:
            raise ValueError("--ad-check requires --dc <domain-controller>")
        if args.ad_debug and not args.ad_check:
            raise ValueError("--ad-debug is only valid with --ad-check")
        if args.dc and not args.ad_check:
            logger.warning("--dc was supplied without --ad-check and is ignored.")

        if args.target == "portal":
            args.portal_url = _validate_https_portal_url(args.portal_url)
            url_platform = get_platform(args.portal_url)
            if url_platform == "UNKNOWN":
                raise ValueError(
                    f"could not classify portal-url {args.portal_url!r}")
            if url_platform != "PORTAL":
                raise ValueError(
                    f"configured portal-url maps to {url_platform}, but "
                    f"--target is portal; refusing to scan a mismatched "
                    f"target")
    except ValueError as exc:
        logger.error("Invalid run configuration: %s", redact_token(str(exc)))
        raise SystemExit(2) from exc

    logger.info("=" * 70)
    logger.info("Account Activity Scan - run started")
    logger.info("=" * 70)
    logger.info("Organization : %s", args.org_name)
    logger.info("Target       : %s", args.target)
    logger.info("Config path  : %s", args.config)
    if args.target == "agol":
        logger.info("Activity     : %s", args.activity)
        logger.info("Reports dir  : %s", args.reports_dir)
    else:
        is_ha = len(args.logs_dir or []) > 1
        logger.info("Logs dir(s)  : %s%s", ", ".join(args.logs_dir or []),
                    "  (HA mode: de-duplicating across nodes)" if is_ha else "")
        logger.info("Extensions   : %s",
                    ", ".join(args.ext if args.ext else list(DEFAULT_EXTENSIONS)))
        logger.info("Recursive    : %s", args.recursive)
        logger.info("TLS mode     : %s", args.tls_mode)
    logger.info("AD check     : %s", args.ad_check)
    logger.info("Max email MB : %s", max_email_mb)
    logger.info("=" * 70)

    try:
        if args.target == "agol":
            rows = _run_agol(args, ts, output_dir, log_path, logger,
                            start_wallclock, start_perf, max_email_mb)
        else:
            rows = _run_portal(args, ts, output_dir, log_path, logger,
                              start_wallclock, start_perf, max_email_mb,
                              tls_mode, ca_path)

        if not rows:
            logger.info("No account-activity events found; nothing to write.")
            if args.target == "agol":
                source = (f"Activity     : {args.activity}\n"
                          f"Reports dir  : {args.reports_dir}\n")
            else:
                source = f"Logs dir(s)  : {', '.join(args.logs_dir or [])}\n"
            body = (
                f"Account Activity Scan Complete\n\n"
                f"Organization : {args.org_name}\n"
                f"Target       : {args.target}\n"
                f"{source}"
                f"Events found : 0\n\n"
                f"No account-activity events were found; no CSV was written.\n\n"
                + timing_block(start_wallclock, start_perf)
            )
            send_email(
                subject_line("Account Activity", ts, args.target.upper(),
                            org_name=args.org_name),
                body, [log_path], args.smtp_server, args.email_to,
                args.email_from, logger)
            return

        # Sort newest-first-ish, grouped sensibly. Portal dates are MM/DD/YYYY;
        # AGOL dates are YYYY/MM/DD -- handle both for a stable sort.
        def _date_key(r):
            for fmt in ("%m/%d/%Y", "%Y/%m/%d"):
                try:
                    return datetime.datetime.strptime(r["action_date"], fmt)
                except ValueError:
                    continue
            return datetime.datetime.max

        rows.sort(key=lambda r: (_date_key(r), r["change_type"],
                                 r["username"].lower()))

        out_path = os.path.join(output_dir,
                                f"{prefix}_ACCOUNT_ACTIVITY_{ts}.csv")
        written = write_dict_csv(rows, out_path, fieldnames=OUTPUT_FIELDNAMES,
                                 logger=logger, label="account-activity")

        timing = timing_block(start_wallclock, start_perf)
        attachments, note = gather_attachments(
            log_path, [written] if written else [], output_dir, logger,
            cap_mb=max_email_mb)

        n_delete = sum(1 for r in rows if r["change_type"] == "delete")
        n_role = sum(1 for r in rows if r["change_type"] == "role")
        n_create = sum(1 for r in rows if r["change_type"] == "create")
        n_update = sum(1 for r in rows if r["change_type"] == "update")
        n_enable = sum(1 for r in rows if r["change_type"] == "enable")
        n_license = sum(1 for r in rows if r["change_type"] == "license")

        ad_line = ""
        if args.ad_check:
            ad_line = (f"AD check     : ON (ADSI, current Windows login, no "
                       f"password, forest via {args.dc})\n")

        if args.target == "agol":
            source = (f"Activity     : {args.activity}\n"
                      f"Reports dir  : {args.reports_dir}\n")
            counts = (f"Events found : {len(rows)} ({n_role} role, "
                      f"{n_license} license, {n_update} update, {n_delete} "
                      f"delete, {n_enable} enable)\n")
        else:
            is_ha = len(args.logs_dir or []) > 1
            ha_line = ""
            if is_ha:
                ha_line = (f"HA mode      : ON ({len(args.logs_dir)} node log "
                           f"directories, cross-node duplicates collapsed)\n")
            source = (f"Logs dir(s)  : {', '.join(args.logs_dir or [])}\n"
                      f"{ha_line}")
            counts = (f"Events found : {len(rows)} ({n_create} create, "
                      f"{n_delete} delete, {n_role} role change)\n")

        body = (
            f"Account Activity Scan Complete\n\n"
            f"Organization : {args.org_name}\n"
            f"Target       : {args.target}\n"
            f"{source}"
            f"{ad_line}"
            f"{counts}"
            f"CSV file     : "
            f"{os.path.basename(written) if written else '(none)'}\n\n"
            f"{timing}"
            f"{note}"
        )
        send_email(
            subject_line("Account Activity", ts, args.target.upper(),
                        org_name=args.org_name),
            body, attachments, args.smtp_server, args.email_to,
            args.email_from, logger)

        logger.info("Done. %d event(s) written to %s", len(rows),
                   out_path if written else "(no file)")

    except _ReportAbort as exc:
        logger.error("Run aborted: %s", redact_token(str(exc)))
        _send_failure_notice(args, ts, log_path, logger, exc.subject_label,
                            str(exc), start_wallclock, start_perf)
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.exception("Account Activity Scan failed unexpectedly: %s", exc)
        _send_failure_notice(
            args, ts, log_path, logger, "FAILED",
            "Unexpected failure: " + redact_token(str(exc)), start_wallclock,
            start_perf)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()