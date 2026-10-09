#!/usr/bin/env python
r"""
PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py

Compares ArcGIS Enterprise Portal item IDs against the item-ID folders on
disk in the Portal content directory (content\items). Portal-only -- there
is no --target flag; args.target is always "portal". 

Uses the shared .\UTILITIES package for connection, logging, email,
timestamping, platform labeling, configuration, and explicit TLS verification.
The ArcGIS SDK is used only to authenticate. Catalog, user, and item-detail
traffic uses separately built, verified requests.Session objects. Tokens are
held centrally in a TokenProvider and exposed only while an outgoing request
is constructed. Because a disk-only ORPHAN decision requires distinguishing a
missing item from an inaccessible one, the authenticated principal must be an
organization administrator or hold organization-wide item/user view privileges.

Examples:
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org2.config.toml "D:\reports" --content-items-dir "\\big_servergis-f5e6\gis01\PRODPTL\content\items"
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml "D:\reports\scan.csv"
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --workers 8
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --tls-mode os-trust-store
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
"""

import argparse
import csv
import datetime
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests


# Verify the project package location before importing package code. Production
# scripts normally live one directory below the project root; accepting a script
# placed directly in the root preserves the original layout flexibility.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if os.path.isdir(os.path.join(_PARENT_DIR, "UTILITIES")):
    _PROJECT_ROOT = _PARENT_DIR
elif os.path.isdir(os.path.join(_SCRIPT_DIR, "UTILITIES")):
    _PROJECT_ROOT = _SCRIPT_DIR
else:
    _PROJECT_ROOT = _PARENT_DIR
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_EXPECTED_UTILITIES_DIR = os.path.realpath(
    os.path.join(_PROJECT_ROOT, "UTILITIES")
)
_UTILITIES_SPEC = importlib.util.find_spec("UTILITIES")
_UTILITIES_ORIGIN = getattr(_UTILITIES_SPEC, "origin", None)
_ACTUAL_UTILITIES_DIR = (
    os.path.realpath(os.path.dirname(_UTILITIES_ORIGIN))
    if _UTILITIES_ORIGIN else ""
)
if _ACTUAL_UTILITIES_DIR != _EXPECTED_UTILITIES_DIR:
    raise ImportError(
        "Refusing to load UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected {_EXPECTED_UTILITIES_DIR!r}."
    )

from UTILITIES import (  # noqa: E402
    build_timestamp,
    get_platform,
    connect_gis,
    setup_logging,
    send_email,
    start_timer,
    timing_block,
    format_elapsed,
    add_common_portal_args,
    add_email_args,
    add_config_arg,
    subject_line,
    finalize_config,
    get_config_value,
    get_max_email_mb,
    gather_attachments,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    provider_for_gis,
    redact_token,
)


TARGET = "portal"
EXPECTED_PLATFORM = "PORTAL"

SYSTEM_OWNERS = {
    "esri", "esri_apps", "esri_boundaries", "esri_demographics",
    "esri_livingatlas", "esri_nav", "system_publisher",
}

EXCLUDED_FOLDERS = {
    "groupinfo", "userinfo", "accountresources", "portal",
    "userappresources", "kml", "portal-ha",
}

DEFAULT_WORKERS = 16
REQUEST_TIMEOUT = 60
SEARCH_PAGE_SIZE = 100
USER_PAGE_SIZE = 100
# A safety ceiling, not a normal truncation limit. The original 10,000-user
# ceiling silently omitted owners and could create false orphan/system results.
MAX_ORG_USERS = 2_000_000
MAX_PORTAL_ITEMS = 2_000_000
MAX_DISK_FOLDERS = 2_000_000
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_SEARCH_RESULTS_PER_BUCKET = 10_000
MAX_ERROR_SAMPLES = 50
EMAIL_SUBJECT = "Portal Content Directory Scan"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service",
    "smtp-server", "email-to", "email-from",
)

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_ITEM_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_ORG_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
_TOKEN_REFRESH_LOCK = threading.Lock()

logger = None


class ScanAbort(RuntimeError):
    """Controlled failure that invalidates the comparison."""


class PortalRestError(RuntimeError):
    """Sanitized Portal REST failure with machine-readable classifications."""

    def __init__(self, code, message, *, http_status=None, tls_error=False,
                 auth_error=False, transient=False):
        self.code = code
        self.http_status = http_status
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.transient = bool(transient)
        self.message_text = redact_token(str(message))
        super().__init__(f"ArcGIS REST error {code}: {self.message_text}")


class WorkerSessionPool:
    """Provide one pre-created requests.Session to each worker thread."""

    def __init__(self, mode, ca_bundle_path, initial_session=None, capacity=1):
        self._assignment_lock = threading.Lock()
        self._assignments = {}
        self._next_session = 0
        self._worker_sessions = []
        self._all_sessions = []
        self._initial_session = initial_session
        self._initial_thread = (
            threading.get_ident() if initial_session is not None else None
        )
        if initial_session is not None:
            self._all_sessions.append(initial_session)
        try:
            for _ in range(max(1, int(capacity))):
                session = build_secure_session(
                    mode,
                    ca_bundle_path=ca_bundle_path,
                    pool_connections=2,
                    pool_maxsize=4,
                    retry_total=3,
                    logger=None,
                )
                self._worker_sessions.append(session)
                self._all_sessions.append(session)
        except Exception:
            self.close()
            raise

    def get(self):
        thread_id = threading.get_ident()
        if (self._initial_session is not None
                and thread_id == self._initial_thread):
            return self._initial_session
        with self._assignment_lock:
            assigned = self._assignments.get(thread_id)
            if assigned is not None:
                return assigned
            assigned = self._worker_sessions[
                self._next_session % len(self._worker_sessions)
            ]
            self._next_session += 1
            self._assignments[thread_id] = assigned
            return assigned

    def close(self):
        seen = set()
        for session in self._all_sessions:
            if id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
        self._all_sessions.clear()
        self._worker_sessions.clear()
        self._assignments.clear()


class FixedSessionPool:
    """Compatibility adapter for callers supplying one existing Session."""

    def __init__(self, session):
        self._session = session

    def get(self):
        return self._session


def _bounded_future_results(executor, items, worker, max_in_flight):
    iterator = iter(items)
    pending = {}

    def submit_one():
        try:
            item = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(worker, item)] = item
        return True

    for _ in range(max(1, int(max_in_flight))):
        if not submit_one():
            break

    while pending:
        completed, _ = wait(set(pending), return_when=FIRST_COMPLETED)
        for future in completed:
            item = pending.pop(future)
            yield item, future
            submit_one()


def _bounded_int(label, minimum, maximum):
    def parse(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer between {minimum} and {maximum}."
            ) from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}."
            )
        return parsed
    return parse


def _canonical_item_id(value):
    text = str(value or "").strip()
    if _ITEM_ID_RE.fullmatch(text):
        return text.lower()
    return text


def _validated_item_id(value):
    text = str(value or "").strip()
    return text.lower() if _ITEM_ID_RE.fullmatch(text) else None


def _safe_org_id(value):
    text = str(value or "").strip()
    if not _ORG_ID_RE.fullmatch(text):
        raise ScanAbort(f"Invalid or unavailable organization id: {text!r}")
    return text


def _validated_portal_url(raw):
    text = str(raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ScanAbort(f"Portal URL could not be parsed: {text!r}") from exc
    if parts.scheme.lower() != "https":
        raise ScanAbort(f"Portal URL must use HTTPS: {text!r}")
    if not parts.hostname:
        raise ScanAbort(f"Portal URL contains no hostname: {text!r}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ScanAbort(f"Portal URL contains an invalid port: {text!r}") from exc
    if parts.username is not None or parts.password is not None:
        raise ScanAbort("Portal URL must not contain user-info credentials.")
    if parts.query or parts.fragment:
        raise ScanAbort("Portal URL must not contain a query or fragment.")
    raw_path = parts.path or ""
    lowered = raw_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise ScanAbort("Portal URL contains an encoded slash or backslash.")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise ScanAbort("Portal URL contains unsafe path characters.")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise ScanAbort("Portal URL contains path traversal.")
    path = raw_path.rstrip("/")
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def _origin(raw):
    parts = urlsplit(raw)
    return (
        parts.scheme.lower(),
        (parts.hostname or "").lower().rstrip("."),
        parts.port or 443,
    )


def _authenticated_portal_base(gis, configured_url):
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    return _validated_portal_url(raw or configured_url)


def _validate_portal_identity(configured_url, authenticated_url):
    configured = _validated_portal_url(configured_url)
    authenticated = _validated_portal_url(authenticated_url)
    if (_origin(configured) != _origin(authenticated)
            or urlsplit(configured).path.rstrip("/").lower()
            != urlsplit(authenticated).path.rstrip("/").lower()):
        raise ScanAbort(
            "Authenticated Portal URL does not match the configured Portal URL: "
            f"configured={configured!r}, authenticated={authenticated!r}."
        )




def _normalize_privileges(raw):
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        try:
            values = list(raw)
        except (TypeError, ValueError):
            values = []
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _validate_inventory_principal(gis):
    """Require authority to see every org user and item.

    A content-directory reconciliation cannot safely call a disk folder an
    orphan when the authenticated principal might simply be unable to see the
    corresponding Portal item. Standard administrators are accepted directly;
    custom roles are accepted when the SDK exposes both organization-wide view
    privileges.
    """
    try:
        me = gis.users.me
    except Exception as exc:
        raise ScanAbort(
            f"Could not resolve the authenticated Portal user: {exc}"
        ) from exc
    if me is None:
        raise ScanAbort(
            "Could not resolve the authenticated Portal user; refusing an "
            "orphan comparison without a provably complete visibility boundary."
        )

    username = str(getattr(me, "username", "") or "").strip()
    role = str(
        getattr(me, "role", None)
        or getattr(me, "roleId", None)
        or ""
    ).strip().lower()
    if role in {"org_admin", "account_admin", "administrator"}:
        return username or "unknown", role, "standard administrator"

    privileges = _normalize_privileges(getattr(me, "privileges", None))
    if not privileges:
        try:
            properties = getattr(me, "properties", None) or {}
            if hasattr(properties, "get"):
                privileges = _normalize_privileges(properties.get("privileges"))
        except Exception:
            privileges = set()

    required = {"portal:admin:viewitems", "portal:admin:viewusers"}
    if required.issubset(privileges):
        return username or "unknown", role or "custom", "custom view privileges"

    missing = ", ".join(sorted(required - privileges))
    raise ScanAbort(
        "This reconciliation requires an organization administrator, or a "
        "custom role that can view every organization item and user. The "
        f"authenticated principal {username or '<unknown>'!r} has role "
        f"{role or '<unknown>'!r} and could not prove required privilege(s): "
        f"{missing or 'unknown'}. Without complete visibility, inaccessible "
        "items could be falsely reported as disk-only orphans."
    )


def _validate_portal_request_url(raw, expected_origin=None,
                                 expected_sharing_prefix=None):
    text = str(raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise PortalRestError("?", f"invalid Portal REST URL: {text!r}") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise PortalRestError("?", "Portal REST requests must use HTTPS.")
    try:
        _ = parts.port
    except ValueError as exc:
        raise PortalRestError("?", "Portal REST URL contains an invalid port.") from exc
    if parts.username is not None or parts.password is not None:
        raise PortalRestError("?", "Portal REST URL contains user-info credentials.")
    if parts.query or parts.fragment:
        raise PortalRestError("?", "Portal REST URL must not contain a query or fragment.")
    raw_path = parts.path or ""
    lower_raw = raw_path.lower()
    if "%2f" in lower_raw or "%5c" in lower_raw:
        raise PortalRestError("?", "Portal REST URL contains an encoded slash or backslash.")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise PortalRestError("?", "Portal REST URL contains unsafe path characters.")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise PortalRestError("?", "Portal REST URL contains path traversal.")
    marker = lower_raw.find("/sharing/rest/")
    if marker < 0:
        raise PortalRestError("?", "Portal REST URL is outside /sharing/rest/.")
    normalized = urlunsplit(("https", parts.netloc, raw_path.rstrip("/"), "", ""))
    origin = _origin(normalized)
    sharing_prefix = raw_path[:marker + len("/sharing/rest/")]
    if expected_origin is not None and origin != expected_origin:
        raise PortalRestError("?", "Portal REST redirect changed origin.")
    if (expected_sharing_prefix is not None
            and not raw_path.lower().startswith(
                expected_sharing_prefix.lower())):
        raise PortalRestError("?", "Portal REST redirect left the authenticated sharing path.")
    return normalized, origin, sharing_prefix


def _is_auth_failure(code=None, status=None, text=""):
    if str(code) in {"401", "403", "498", "499"}:
        return True
    if status in {401, 403, 498, 499}:
        return True
    return bool(_AUTH_ERROR_RE.search(str(text or "")))


def _refresh_token_provider(token_provider, failed_token):
    with _TOKEN_REFRESH_LOCK:
        current = token_provider.get()
        if current and current != failed_token:
            return current
        gis = getattr(token_provider, "_gis", None)
        if gis is not None:
            try:
                _ = gis.properties.get("name")
            except Exception:
                pass
        token_provider.refresh()
        refreshed = token_provider.get()
        if not refreshed:
            raise PortalRestError(
                498, "token refresh returned no token", auth_error=True
            )
        return refreshed


def _read_response_json(response, url):
    status = int(getattr(response, "status_code", 0) or 0)
    reason = str(getattr(response, "reason", "") or "")
    declared = getattr(response, "headers", {}).get("Content-Length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except (TypeError, ValueError) as exc:
            raise PortalRestError(
                status or "?", f"invalid Content-Length header: {declared!r}",
                http_status=status or None,
            ) from exc
        if declared_size < 0 or declared_size > MAX_JSON_BYTES:
            raise PortalRestError(
                status or "?",
                f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit",
                http_status=status or None,
            )

    chunks = []
    total = 0
    try:
        iterator = response.iter_content(chunk_size=64 * 1024)
        for chunk in iterator:
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_JSON_BYTES:
                raise PortalRestError(
                    status or "?",
                    f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit",
                    http_status=status or None,
                )
            chunks.append(chunk)
    except PortalRestError:
        raise
    except requests.exceptions.SSLError as exc:
        raise PortalRestError(
            status or "?", f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
            http_status=status or None, tls_error=True,
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise PortalRestError(
            status or "?", f"request failed while reading: {exc}",
            http_status=status or None, transient=True,
        ) from exc
    finally:
        try:
            response.close()
        except Exception:
            pass

    body = b"".join(chunks)
    if not body:
        raise PortalRestError(
            status or "?", f"HTTP {status} {reason}: empty response body",
            http_status=status or None,
            auth_error=_is_auth_failure(status=status),
            transient=(status == 429 or status >= 500),
        )
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortalRestError(
            status or "?", f"Non-JSON response from {url} (HTTP {status})",
            http_status=status or None,
            auth_error=_is_auth_failure(status=status),
            transient=(status == 429 or status >= 500),
        ) from exc
    if not isinstance(data, dict):
        raise PortalRestError(
            status or "?",
            f"Expected a JSON object from {url}; got {type(data).__name__}.",
            http_status=status or None,
        )

    error = data.get("error")
    if error is not None:
        if isinstance(error, dict):
            code = error.get("code", status or "?")
            message = error.get("message", "unknown error")
            details_value = error.get("details")
            if isinstance(details_value, list):
                details = "; ".join(str(value) for value in details_value)
            elif details_value:
                details = str(details_value)
            else:
                details = ""
            if details:
                message = f"{message} ({details})"
        else:
            code = status or "?"
            message = str(error)
        raise PortalRestError(
            code, message, http_status=status or None,
            auth_error=_is_auth_failure(code=code, status=status, text=message),
            transient=(status == 429 or status >= 500),
        )

    if status >= 400:
        raise PortalRestError(
            status, f"HTTP {status} {reason}", http_status=status,
            auth_error=_is_auth_failure(status=status),
            transient=(status == 429 or status >= 500),
        )
    return data


def _portal_json_once(session, url, token, params=None,
                      timeout=REQUEST_TIMEOUT):
    safe_url, origin, sharing_prefix = _validate_portal_request_url(url)
    merged = {"f": "json"}
    if params:
        merged.update(params)
    merged.pop("token", None)
    merged["f"] = "json"
    merged["token"] = token

    current_url = safe_url
    response = None
    for redirect_count in range(4):
        try:
            response = session.get(
                current_url,
                params=merged,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.Timeout as exc:
            raise PortalRestError(
                "?", f"Request to {current_url} timed out after {timeout}s",
                transient=True,
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise PortalRestError(
                "?", f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                tls_error=True,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise PortalRestError(
                "?", f"Request to {current_url} failed: {exc}",
                transient=True,
            ) from exc

        status = int(response.status_code)
        if not 300 <= status < 400:
            break
        location = response.headers.get("Location")
        response.close()
        response = None
        if redirect_count >= 3:
            raise PortalRestError("?", "Too many Portal REST redirects.")
        if not location:
            raise PortalRestError(
                status, f"HTTP {status} redirect supplied no Location header."
            )
        candidate = urljoin(current_url, location)
        current_url, _origin_value, _prefix = _validate_portal_request_url(
            candidate,
            expected_origin=origin,
            expected_sharing_prefix=sharing_prefix,
        )

    if response is None:
        raise PortalRestError("?", "Portal REST request produced no response.")
    return _read_response_json(response, current_url)


def _portal_json(session, url, token_provider, params=None,
                 timeout=REQUEST_TIMEOUT):
    """Strict Portal JSON GET with one synchronized token-refresh retry."""
    token = token_provider.get()
    try:
        return _portal_json_once(session, url, token, params=params,
                                 timeout=timeout)
    except PortalRestError as exc:
        if not exc.auth_error:
            raise
    refreshed = _refresh_token_provider(token_provider, token)
    return _portal_json_once(session, url, refreshed, params=params,
                             timeout=timeout)


def _next_start(data, current_start, batch_size):
    raw = data.get("nextStart")
    if raw is None:
        return current_start + batch_size if batch_size else -1
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ArcGIS returned invalid nextStart={raw!r}.") from exc
    if value in (-1, 0):
        return -1
    if value < -1:
        raise RuntimeError(f"ArcGIS returned invalid nextStart={value}.")
    if value <= current_start:
        raise RuntimeError(
            f"ArcGIS pagination made no progress: start={current_start}, "
            f"nextStart={value}."
        )
    return value


def human_readable_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def _enumerate_users_from_endpoint(session, endpoint, token_provider,
                                   org_id):
    org_id = _safe_org_id(org_id)
    usernames = set()
    start = 1
    pages = 0
    total_seen = None
    # The server may return fewer rows than requested. Bound by the maximum
    # possible one-new-user-per-page progression rather than assuming it honors
    # USER_PAGE_SIZE.
    max_pages = MAX_ORG_USERS + 2

    while True:
        pages += 1
        if pages > max_pages:
            raise RuntimeError(
                f"User pagination exceeded {max_pages} pages at {endpoint}; "
                "refusing to loop indefinitely."
            )
        if len(usernames) >= MAX_ORG_USERS:
            raise RuntimeError(
                f"User enumeration reached the safety ceiling of "
                f"{MAX_ORG_USERS:,}; refusing to truncate silently."
            )
        num = min(USER_PAGE_SIZE, MAX_ORG_USERS - len(usernames))
        data = _portal_json(
            session,
            endpoint,
            token_provider,
            params={
                "q": f"orgid:{org_id}",
                "sortField": "username",
                "sortOrder": "asc",
                "start": start,
                "num": num,
            },
        )

        if "users" in data:
            batch = data.get("users")
        elif "results" in data:
            batch = data.get("results")
        else:
            raise RuntimeError(
                f"User endpoint {endpoint} returned neither 'users' nor 'results'."
            )
        if not isinstance(batch, list):
            raise RuntimeError(
                f"User endpoint {endpoint} returned a malformed user array."
            )

        raw_total = data.get("total")
        if raw_total is not None:
            try:
                parsed_total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"User endpoint returned invalid total={raw_total!r}."
                ) from exc
            if parsed_total < 0:
                raise RuntimeError("User endpoint returned a negative total.")
            if parsed_total > MAX_ORG_USERS:
                raise RuntimeError(
                    f"Portal reports {parsed_total:,} users, above the "
                    f"{MAX_ORG_USERS:,} safety ceiling."
                )
            if total_seen is None:
                total_seen = parsed_total
            elif parsed_total != total_seen:
                raise RuntimeError(
                    f"User total changed during pagination: {total_seen} -> "
                    f"{parsed_total}."
                )

        if not batch:
            if total_seen not in (None, 0) and len(usernames) < total_seen:
                raise RuntimeError(
                    f"User endpoint ended after {len(usernames)} of "
                    f"{total_seen} declared users."
                )
            break

        before = len(usernames)
        for user in batch:
            if not isinstance(user, dict):
                raise RuntimeError("User endpoint returned a non-object user entry.")
            username = user.get("username")
            if not isinstance(username, str) or not username.strip():
                raise RuntimeError("User endpoint returned a user without a username.")
            explicit_org = user.get("orgId")
            if explicit_org and str(explicit_org) != org_id:
                raise RuntimeError(
                    f"User {username!r} belongs to org {explicit_org!r}, not "
                    f"the authenticated org {org_id!r}."
                )
            usernames.add(username.strip())

        if len(usernames) == before:
            raise RuntimeError(
                f"User pagination at start={start} returned no new usernames."
            )

        if total_seen is not None and len(usernames) >= total_seen:
            break

        next_start = _next_start(data, start, len(batch))
        if next_start < 0:
            if total_seen is not None and len(usernames) < total_seen:
                raise RuntimeError(
                    f"User endpoint stopped after {len(usernames)} of "
                    f"{total_seen} declared users."
                )
            break
        if len(batch) < num and data.get("nextStart") is None:
            if total_seen is not None and len(usernames) < total_seen:
                raise RuntimeError(
                    f"User endpoint returned a short page before its declared total."
                )
            break
        start = next_start

    return usernames


def _resolve_org_id(gis, session, portal_url, token_provider):
    portal_self = _portal_json(
        session,
        f"{portal_url.rstrip('/')}/sharing/rest/portals/self",
        token_provider,
    )
    candidates = []
    for value in (portal_self.get("id"),):
        if value:
            candidates.append(str(value))
    try:
        properties = getattr(gis, "properties", None)
        value = properties.get("id") if hasattr(properties, "get") else None
        if value:
            candidates.append(str(value))
    except Exception:
        pass
    try:
        me = gis.users.me
        value = getattr(me, "orgId", None) if me is not None else None
        if value:
            candidates.append(str(value))
    except Exception:
        pass
    normalized = {_safe_org_id(value) for value in candidates}
    if not normalized:
        raise ScanAbort(
            "Could not determine the authenticated Portal organization id."
        )
    if len(normalized) != 1:
        raise ScanAbort(
            f"Conflicting organization ids were returned: {sorted(normalized)!r}."
        )
    return normalized.pop()


def get_org_usernames(session, portal_url, token_provider, org_id=None):
    logger.info("Retrieving org user list through the verified REST session ...")
    if org_id is None:
        portal_self = _portal_json(
            session,
            f"{portal_url.rstrip('/')}/sharing/rest/portals/self",
            token_provider,
        )
        org_id = portal_self.get("id")
    org_id = _safe_org_id(org_id)
    logger.info("Portal organization id: %s", org_id)

    endpoints = [
        f"{portal_url.rstrip('/')}/sharing/rest/portals/self/users/search",
        f"{portal_url.rstrip('/')}/sharing/rest/community/users",
    ]
    errors = []
    for endpoint in endpoints:
        try:
            usernames = _enumerate_users_from_endpoint(
                session, endpoint, token_provider, org_id
            )
        except Exception as exc:
            rendered = redact_token(str(exc))
            errors.append(f"{endpoint}: {rendered}")
            logger.warning(
                "User enumeration endpoint failed; trying the verified "
                "REST fallback: %s", rendered,
            )
            continue
        if usernames:
            logger.info("Found %d org users.", len(usernames))
            return usernames

    raise RuntimeError(
        "Could not enumerate any Portal organization users through the "
        "verified REST session. " + " | ".join(errors)
    )


def _search_portal_page(session, portal_url, token_provider, query, cap):
    search_url = f"{portal_url.rstrip('/')}/sharing/rest/search"
    results = []
    seen_ids = set()
    start = 1
    pages = 0
    total_seen = None
    # The server may return fewer rows than requested. Bound by the maximum
    # possible one-new-item-per-page progression rather than assuming it honors
    # SEARCH_PAGE_SIZE.
    max_pages = cap + 2

    while len(results) < cap:
        pages += 1
        if pages > max_pages:
            raise RuntimeError(
                f"Portal item-search pagination exceeded {max_pages} pages; "
                "refusing to loop indefinitely."
            )
        num = min(SEARCH_PAGE_SIZE, cap - len(results))
        data = _portal_json(
            session,
            search_url,
            token_provider,
            params={
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": num,
            },
        )

        if "results" in data:
            batch = data.get("results")
        elif "items" in data:
            batch = data.get("items")
        else:
            raise RuntimeError(
                "Portal item search returned neither 'results' nor 'items'."
            )
        if not isinstance(batch, list):
            raise RuntimeError("Portal item search returned a malformed result array.")

        raw_total = data.get("total")
        if raw_total is not None:
            try:
                parsed_total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Portal item search returned invalid total={raw_total!r}."
                ) from exc
            if parsed_total < 0:
                raise RuntimeError("Portal item search returned a negative total.")
            if total_seen is None:
                total_seen = parsed_total
            elif parsed_total != total_seen:
                raise RuntimeError(
                    f"Portal item total changed during pagination: "
                    f"{total_seen} -> {parsed_total}."
                )

        if not batch:
            expected = min(total_seen, cap) if total_seen is not None else None
            if expected is not None and len(results) < expected:
                raise RuntimeError(
                    f"Portal item search ended after {len(results)} of "
                    f"{expected} expected results."
                )
            break

        new_count = 0
        for item in batch:
            if not isinstance(item, dict):
                raise RuntimeError("Portal item search returned a non-object item.")
            item_id = _validated_item_id(item.get("id"))
            if not item_id:
                raise RuntimeError(
                    f"Portal item search returned an invalid item id: "
                    f"{item.get('id')!r}."
                )
            owner = item.get("owner")
            if not isinstance(owner, str) or not owner.strip():
                raise RuntimeError(
                    f"Portal item {item_id} has no valid owner in search results."
                )
            if item_id in seen_ids:
                continue
            normalized = dict(item)
            normalized["id"] = item_id
            normalized["owner"] = owner.strip()
            results.append(normalized)
            seen_ids.add(item_id)
            new_count += 1
            if len(results) >= cap:
                break

        if new_count == 0:
            raise RuntimeError(
                f"Portal item pagination at start={start} returned no new IDs."
            )

        expected = min(total_seen, cap) if total_seen is not None else None
        if expected is not None and len(results) >= expected:
            break

        next_start = _next_start(data, start, len(batch))
        if next_start < 0:
            if expected is not None and len(results) < expected:
                raise RuntimeError(
                    f"Portal item search stopped after {len(results)} of "
                    f"{expected} expected results."
                )
            break
        if len(batch) < num and data.get("nextStart") is None:
            if expected is not None and len(results) < expected:
                raise RuntimeError(
                    "Portal item search returned a short page before its "
                    "declared total."
                )
            break
        start = next_start

    return results[:cap]


def _strict_bucketed_item_search(session, portal_url, token_provider,
                                 org_id, logger_obj):
    items_by_id = {}
    stats = {"queries": 0}
    span_end = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=365)
    )
    # Preserve the original Portal-only catalog semantics: enumerate the local
    # Portal catalog, then apply the authoritative org-user owner filter when
    # building the report. The org id is still required for complete user
    # enumeration, but adding it here would alter which local system records are
    # seen during discovery.
    _safe_org_id(org_id)

    def search_range(lo_ms, hi_ms):
        query = f"created:[{lo_ms} TO {hi_ms}]"
        stats["queries"] += 1
        results = _search_portal_page(
            session, portal_url, token_provider, query,
            MAX_SEARCH_RESULTS_PER_BUCKET,
        )
        count = len(results)
        if count >= MAX_SEARCH_RESULTS_PER_BUCKET and hi_ms > lo_ms:
            mid = lo_ms + (hi_ms - lo_ms) // 2
            if mid <= lo_ms:
                mid = lo_ms + 1
            search_range(lo_ms, mid)
            search_range(mid + 1, hi_ms)
            return
        if count >= MAX_SEARCH_RESULTS_PER_BUCKET:
            raise RuntimeError(
                f"Portal item bucket [{lo_ms} TO {hi_ms}] still reached the "
                f"{MAX_SEARCH_RESULTS_PER_BUCKET:,}-result cap at one "
                "millisecond; completeness cannot be established."
            )
        for item in results:
            items_by_id[item["id"]] = item
            if len(items_by_id) > MAX_PORTAL_ITEMS:
                raise RuntimeError(
                    f"Portal item discovery exceeded the {MAX_PORTAL_ITEMS:,} "
                    "safety ceiling; refusing an unbounded in-memory scan."
                )
        if stats["queries"] % 20 == 0:
            logger_obj.info(
                "  %d strict item-search requests, %d unique item(s) so far.",
                stats["queries"], len(items_by_id),
            )

    cursor = _SEARCH_START
    while cursor < span_end:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)
        lo_ms = int(cursor.timestamp() * 1000)
        hi_ms = int(nxt.timestamp() * 1000) - 1
        search_range(lo_ms, hi_ms)
        cursor = nxt

    logger_obj.info(
        "Strict item search complete: %d request(s), %d unique item(s).",
        stats["queries"], len(items_by_id),
    )
    return items_by_id


def get_portal_items(session, portal_url, token_provider, org_id=None):
    logger.info("Retrieving all organization Portal items via strict "
                "date-bucketed search ...")
    query_start = time.perf_counter()
    if org_id is None:
        portal_self = _portal_json(
            session,
            f"{portal_url.rstrip('/')}/sharing/rest/portals/self",
            token_provider,
        )
        org_id = portal_self.get("id")
    items_by_id = _strict_bucketed_item_search(
        session, portal_url, token_provider, org_id, logger
    )
    logger.info("Date-bucketed search completed in %s.",
                format_elapsed(time.perf_counter() - query_start))
    logger.info("Found %d unique organization items in the Portal.",
                len(items_by_id))
    return items_by_id


def _is_reparse_point(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(flag and attrs & flag)


def _measure_folder_size(folder_path):
    total_bytes = 0
    error_count = 0
    errors = []

    def record_error(message):
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_ERROR_SAMPLES:
            errors.append(redact_token(str(message)))

    def on_walk_error(exc):
        record_error(exc)

    for root, dirs, files in os.walk(
            folder_path, topdown=True, onerror=on_walk_error,
            followlinks=False):
        safe_dirs = []
        for dirname in dirs:
            path = os.path.join(root, dirname)
            if _is_reparse_point(path):
                record_error(f"skipped reparse/symlink directory: {path}")
                continue
            safe_dirs.append(dirname)
        dirs[:] = safe_dirs

        for filename in files:
            file_path = os.path.join(root, filename)
            try:
                file_stat = os.stat(file_path, follow_symlinks=False)
                flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                attrs = getattr(file_stat, "st_file_attributes", 0)
                if stat.S_ISLNK(file_stat.st_mode) or (flag and attrs & flag):
                    record_error(f"skipped reparse/symlink file: {file_path}")
                    continue
                total_bytes += file_stat.st_size
            except (OSError, PermissionError) as exc:
                record_error(f"{file_path}: {exc}")
    return total_bytes, error_count, errors


def get_folder_size(folder_path):
    size_bytes, _error_count, _errors = _measure_folder_size(folder_path)
    return size_bytes


def _get_disk_item_folders_detailed(content_dir, workers=DEFAULT_WORKERS):
    logger.info("Scanning disk content directory:")
    logger.info("  %s", content_dir)

    if not os.path.isdir(content_dir):
        raise ScanAbort(
            f"Content directory not found or inaccessible: {content_dir}"
        )

    logger.info("Enumerating root-level folders ...")
    dir_entries = []
    reparse_item_entries = []
    skipped = []
    unsafe_entries = []
    try:
        with os.scandir(content_dir) as entries:
            for entry in entries:
                if entry.name.lower() in EXCLUDED_FOLDERS:
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            skipped.append(entry.name)
                    except OSError:
                        pass
                    continue
                if _is_reparse_point(entry.path):
                    # Preserve the on-disk presence decision without traversing
                    # outside the configured tree. Size is indeterminate and
                    # makes the run incomplete, but the item is not falsely
                    # changed from MATCHED to PORTAL_ONLY.
                    try:
                        points_to_dir = entry.is_dir(follow_symlinks=True)
                    except OSError as exc:
                        points_to_dir = False
                        unsafe_entries.append(f"{entry.path}: {exc}")
                    if points_to_dir:
                        reparse_item_entries.append(entry)
                    else:
                        unsafe_entries.append(
                            f"skipped non-directory reparse/symlink root "
                            f"entry: {entry.path}"
                        )
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    unsafe_entries.append(f"{entry.path}: {exc}")
                    continue
                if is_dir:
                    dir_entries.append(entry)
                if len(dir_entries) + len(reparse_item_entries) > MAX_DISK_FOLDERS:
                    raise ScanAbort(
                        f"Content directory contains more than "
                        f"{MAX_DISK_FOLDERS:,} root folders; refusing an "
                        "unbounded in-memory scan."
                    )
    except OSError as exc:
        raise ScanAbort(
            f"Could not enumerate content directory {content_dir!r}: {exc}"
        ) from exc

    if skipped:
        logger.info("Excluded %d non-item folders: %s",
                    len(skipped), ", ".join(sorted(skipped)))
    for message in unsafe_entries[:MAX_ERROR_SAMPLES]:
        logger.warning("Disk scan: %s", message)

    total = len(dir_entries) + len(reparse_item_entries)
    logger.info("Found %d root-level folders. Measuring sizes with %d worker "
                "threads ...", total, workers)

    folders = {}
    progress_done = 0
    running_bytes = 0
    scan_start = time.perf_counter()
    error_count = len(unsafe_entries)
    error_samples = list(unsafe_entries[:MAX_ERROR_SAMPLES])

    for entry in reparse_item_entries:
        key = _canonical_item_id(entry.name)
        if key in folders:
            raise ScanAbort(
                "Two content folders collapse to the same case-insensitive "
                f"item id: {folders[key]['folder_name']!r} and "
                f"{entry.name!r}."
            )
        folders[key] = {
            "folder_name": entry.name,
            "path": entry.path,
            "size_bytes": 0,
            "error_count": 1,
            "errors": [
                "did not traverse reparse/symlink item folder; "
                f"size is indeterminate: {entry.path}"
            ],
        }
        progress_done += 1
        error_count += 1
        if len(error_samples) < MAX_ERROR_SAMPLES:
            error_samples.append(folders[key]["errors"][0])

    def measure(entry):
        size_bytes, entry_error_count, errors = _measure_folder_size(entry.path)
        return {
            "folder_name": entry.name,
            "path": entry.path,
            "size_bytes": size_bytes,
            "error_count": entry_error_count,
            "errors": errors,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for entry, future in _bounded_future_results(
                executor, dir_entries, measure, max_in_flight=workers * 2):
            try:
                result = future.result()
            except Exception as exc:
                raise ScanAbort(
                    f"Unexpected folder-measurement failure for "
                    f"{entry.path!r}: {redact_token(str(exc))}"
                ) from exc
            key = _canonical_item_id(result["folder_name"])
            if key in folders:
                raise ScanAbort(
                    "Two content folders collapse to the same case-insensitive "
                    f"item id: {folders[key]['folder_name']!r} and "
                    f"{result['folder_name']!r}."
                )
            folders[key] = result
            progress_done += 1
            running_bytes += result["size_bytes"]
            error_count += result["error_count"]
            remaining_sample_slots = MAX_ERROR_SAMPLES - len(error_samples)
            if remaining_sample_slots > 0:
                error_samples.extend(result["errors"][:remaining_sample_slots])
            if progress_done <= 5 or progress_done % 25 == 0 or progress_done == total:
                elapsed = time.perf_counter() - scan_start
                rate = progress_done / elapsed if elapsed > 0 else 0
                remaining = (total - progress_done) / rate if rate > 0 else 0
                logger.info(
                    "  [%d/%d] %s = %s | cumulative %s | elapsed %s "
                    "| est. remaining %s",
                    progress_done, total, result["folder_name"],
                    human_readable_size(result["size_bytes"]),
                    human_readable_size(running_bytes),
                    format_elapsed(elapsed), format_elapsed(remaining),
                )

    for message in error_samples:
        logger.warning("Disk size measurement incomplete: %s", message)
    if error_count > len(error_samples):
        logger.warning(
            "Disk size measurement had %d additional error(s) not shown.",
            error_count - len(error_samples),
        )

    logger.info("Disk scan complete: %d folders, total footprint %s, in %s.",
                len(folders), human_readable_size(running_bytes),
                format_elapsed(time.perf_counter() - scan_start))
    return folders, {
        "size_errors": error_count,
        "error_samples": error_samples,
        "total_bytes": running_bytes,
        "folders": len(folders),
    }


def get_disk_item_folders(content_dir, workers=DEFAULT_WORKERS):
    folders, _stats = _get_disk_item_folders_detailed(
        content_dir, workers=workers
    )
    return folders


def resolve_output_path(user_path, timestamp, org_prefix, platform):
    filename = f"{org_prefix}_{platform}_PORTAL_ITEM_ID_ANALYSIS_{timestamp}.csv"
    if user_path.lower().endswith(".csv"):
        parent = os.path.dirname(os.path.abspath(user_path))
        os.makedirs(parent, exist_ok=True)
        return user_path
    os.makedirs(user_path, exist_ok=True)
    return os.path.join(user_path, filename)


def _reserve_run_paths(user_path, timestamp, org_prefix, platform):
    if user_path.lower().endswith(".csv"):
        output_csv = resolve_output_path(
            user_path, timestamp, org_prefix, platform
        )
        return {
            "timestamp": timestamp,
            "csv": output_csv,
            "log": os.path.splitext(output_csv)[0] + ".log",
            "explicit": True,
        }

    os.makedirs(user_path, exist_ok=True)
    for suffix in range(1000):
        run_stamp = timestamp if suffix == 0 else f"{timestamp}_{suffix:02d}"
        output_csv = resolve_output_path(
            user_path, run_stamp, org_prefix, platform
        )
        log_path = os.path.splitext(output_csv)[0] + ".log"
        if os.path.exists(output_csv):
            continue
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return {
            "timestamp": run_stamp,
            "csv": output_csv,
            "log": log_path,
            "explicit": False,
        }
    raise RuntimeError("Could not reserve a unique output name after 999 collisions.")


def _item_value(item, key, default=""):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _get_item_dict(session, portal_url, token_provider, item_id):
    valid_id = _validated_item_id(item_id)
    if valid_id is None:
        # A folder that is not a syntactically valid ArcGIS item id cannot map
        # to a Portal item, so retain the original orphan semantics without
        # placing arbitrary folder text into a URL path.
        return None
    item_url = (
        f"{portal_url.rstrip('/')}/sharing/rest/content/items/"
        f"{quote(valid_id, safe='')}"
    )
    try:
        data = _portal_json(session, item_url, token_provider)
    except PortalRestError as exc:
        try:
            code = int(exc.code)
        except (TypeError, ValueError):
            code = None
        if code in (400, 404) or exc.http_status == 404:
            return None
        raise
    returned_id = _validated_item_id(data.get("id"))
    if returned_id != valid_id:
        raise RuntimeError(
            f"Item endpoint for {valid_id} returned mismatched id "
            f"{data.get('id')!r}."
        )
    return data


def _build_and_write_report_detailed(
        items_by_id, disk_folders, output_csv, session, portal_url,
        token_provider, org_users, platform, workers=DEFAULT_WORKERS,
        session_pool=None):
    org_users_lc = {str(u).lower() for u in org_users}
    system_owners_lc = {s.lower() for s in SYSTEM_OWNERS}

    def is_org_owned(owner):
        owner_lc = str(owner or "").lower()
        return owner_lc in org_users_lc and owner_lc not in system_owners_lc

    org_items_by_id = {}
    for raw_id, item in items_by_id.items():
        item_id = _validated_item_id(raw_id or _item_value(item, "id"))
        if not item_id:
            raise ScanAbort(
                f"Portal catalog contained an invalid item id: {raw_id!r}."
            )
        if is_org_owned(_item_value(item, "owner")):
            org_items_by_id[item_id] = item

    portal_ids = set(org_items_by_id)
    disk_ids = set(disk_folders)
    unresolved_disk_ids = sorted(disk_ids - portal_ids)
    logger.info(
        "Resolving %d unmatched disk folders through the verified Portal "
        "REST session with %d threads ...",
        len(unresolved_disk_ids), workers,
    )

    pool = session_pool or FixedSessionPool(session)
    resolved = {}
    resolve_done = 0
    resolve_start = time.perf_counter()
    lookup_errors = []

    def lookup(item_id):
        return _get_item_dict(
            pool.get(), portal_url, token_provider, item_id
        )

    if unresolved_disk_ids:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for item_id, future in _bounded_future_results(
                    executor, unresolved_disk_ids, lookup,
                    max_in_flight=workers * 2):
                try:
                    item = future.result()
                except Exception as exc:
                    lookup_errors.append(
                        f"{item_id}: {redact_token(str(exc))}"
                    )
                    item = None
                if item is not None:
                    resolved[item_id] = item
                resolve_done += 1
                if (resolve_done <= 5 or resolve_done % 100 == 0
                        or resolve_done == len(unresolved_disk_ids)):
                    elapsed = time.perf_counter() - resolve_start
                    logger.info(
                        "  [%d/%d] lookups done | %d resolved | elapsed %s",
                        resolve_done, len(unresolved_disk_ids), len(resolved),
                        format_elapsed(elapsed),
                    )

    if lookup_errors:
        for message in lookup_errors[:MAX_ERROR_SAMPLES]:
            logger.error("Disk-folder Portal lookup failed: %s", message)
        raise ScanAbort(
            f"{len(lookup_errors)} disk-folder item lookup(s) failed; "
            "refusing to classify those folders as orphans."
        )

    logger.info("Disk-folder lookups complete: %d of %d resolved to a Portal "
                "item.", len(resolved), len(unresolved_disk_ids))

    all_ids = sorted(portal_ids | disk_ids)
    count_matched = count_portal_only = count_system = count_orphan = 0

    fieldnames = [
        "item_id", "status", "in_portal", "on_disk",
        "title", "owner", "item_type",
        "folder_size_bytes", "folder_size_mb", "folder_path",
    ]

    logger.info("Comparing Portal items against disk folders ...")
    logger.info("  %d org Portal IDs vs %d disk folders (%d unique IDs total).",
                len(portal_ids), len(disk_ids), len(all_ids))
    logger.info("Writing report to: %s", output_csv)
    logger.info("NOTE: SYSTEM / ESRI-owned content is EXCLUDED from the report; "
                "only org-user-owned content is inventoried.")

    output_fd = os.open(
        output_csv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    try:
        try:
            os.fchmod(output_fd, 0o600)
        except (AttributeError, OSError):
            pass
        fh_context = os.fdopen(
            output_fd, "w", newline="", encoding="utf-8-sig"
        )
    except Exception:
        os.close(output_fd)
        raise

    with fh_context as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()

        for item_id in all_ids:
            in_portal = item_id in portal_ids
            on_disk = item_id in disk_ids
            item = org_items_by_id.get(item_id) or resolved.get(item_id)

            if in_portal and on_disk:
                status = "MATCHED"
                count_matched += 1
            elif in_portal and not on_disk:
                status = "PORTAL_ONLY"
                count_portal_only += 1
            else:
                if item is not None:
                    if is_org_owned(_item_value(item, "owner")):
                        status = "MATCHED"
                        count_matched += 1
                    else:
                        count_system += 1
                        continue
                else:
                    status = "ORPHAN"
                    count_orphan += 1

            emit_item = item if (item is not None and is_org_owned(
                _item_value(item, "owner"))) else None

            if emit_item is not None:
                title = _item_value(emit_item, "title") or ""
                owner = _item_value(emit_item, "owner") or ""
                type_ = _item_value(emit_item, "type") or ""
                row_in_portal = True
                display_item_id = (
                    _item_value(emit_item, "id") or item_id
                )
            else:
                title = owner = type_ = ""
                row_in_portal = in_portal
                display_item_id = (
                    disk_folders[item_id].get("folder_name", item_id)
                    if on_disk else item_id
                )

            if on_disk:
                folder_info = disk_folders[item_id]
                size_bytes = folder_info["size_bytes"]
                folder_path = folder_info["path"]
                size_mb = round(size_bytes / (1024 * 1024), 2)
            else:
                size_bytes = size_mb = folder_path = ""

            writer.writerow({
                "item_id": display_item_id,
                "status": status,
                "in_portal": row_in_portal,
                "on_disk": on_disk,
                "title": title,
                "owner": owner,
                "item_type": type_,
                "folder_size_bytes": size_bytes,
                "folder_size_mb": size_mb,
                "folder_path": folder_path,
            })

    summary = (
        "Portal Content Directory Scan - Summary\n"
        "----------------------------------------------\n"
        f"  Platform             : {platform}\n"
        f"  Org Portal items     : {len(portal_ids)}\n"
        f"  Disk folders total   : {len(disk_ids)}\n"
        f"  MATCHED              : {count_matched}\n"
        f"  PORTAL_ONLY          : {count_portal_only}\n"
        f"  SYSTEM (excluded)    : {count_system}\n"
        f"  ORPHAN (disk-only)   : {count_orphan}\n"
        "----------------------------------------------\n"
        f"  Report file          : {output_csv}\n"
    )

    logger.info("Report written. Summary below.")
    for line in summary.rstrip("\n").split("\n"):
        logger.info("%s", line)

    return summary, {
        "portal_items": len(portal_ids),
        "disk_folders": len(disk_ids),
        "matched": count_matched,
        "portal_only": count_portal_only,
        "system_excluded": count_system,
        "orphan": count_orphan,
        "lookup_errors": 0,
    }


def build_and_write_report(items_by_id, disk_folders, output_csv,
                           session, portal_url, token_provider, org_users,
                           platform, workers=DEFAULT_WORKERS):
    summary, _stats = _build_and_write_report_detailed(
        items_by_id, disk_folders, output_csv, session, portal_url,
        token_provider, org_users, platform, workers=workers,
    )
    return summary


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Scan an ArcGIS Enterprise Portal and its content\\items directory "
            "to find orphan items and report per-folder sizes. PORTAL ONLY; "
            "requires organization-wide item and user visibility."
        ),
        epilog=r"""
Examples:
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org2.config.toml "D:\reports" ^
        --content-items-dir "\\big_servergis-f5e6\gis01\PRODPTL\content\items"
    python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml ^
        --tls-mode os-trust-store
""",
    )
    add_config_arg(parser)
    parser.add_argument(
        "output_path", nargs="?", default=None,
        help="Output path for the CSV (directory or .csv). If omitted, "
             "'output-dir' from the --config org toml is used.")
    parser.add_argument(
        "--content-items-dir", default=None,
        help="UNC/local path to the Portal content\\items directory. If "
             "omitted, taken from the --config org toml "
             "('content-items-dir').")
    add_common_portal_args(parser)
    add_email_args(parser)
    parser.add_argument(
        "--workers", type=_bounded_int("workers", 1, 64),
        default=DEFAULT_WORKERS,
        help=f"Concurrent threads for sizing/lookups (default: {DEFAULT_WORKERS}; "
             "allowed range 1-64).")

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES),
        default="standard",
        help="TLS trust strategy for both the verified direct REST session "
             "and the initial ArcGIS SDK authentication. 'standard' "
             "(default) uses the bundled public CA list; 'os-trust-store' "
             "uses the operating system certificate store and requires the "
             "'truststore' package; 'custom-ca-bundle' uses the PEM file "
             "given by --ca-bundle-path. Bulk catalog/user/item traffic "
             "deliberately bypasses the ArcGIS SDK and uses verified, "
             "per-worker REST sessions.",
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle. Required when --tls-mode is "
             "custom-ca-bundle; ignored for the other TLS modes.",
    )

    args = parser.parse_args(argv)
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        parser.error(
            "--ca-bundle-path is required when --tls-mode is "
            "custom-ca-bundle."
        )
    args.target = TARGET
    return args


def _gather_run_attachments(log_file_path, report_paths, output_dir,
                            logger_obj, configured_cap_mb):
    """Apply the configured cap to the log plus report files together."""
    try:
        log_mb = os.path.getsize(log_file_path) / (1024 * 1024)
    except OSError:
        log_mb = 0.0
    cap_mb = float(configured_cap_mb)
    if log_mb > cap_mb:
        logger_obj.warning(
            "Log file alone is %.2f MB, exceeding the %.2f MB email cap; "
            "no attachment will be added.", log_mb, cap_mb,
        )
        return [], (
            f"\nNOTE: The log file alone is {log_mb:.2f} MB, exceeding "
            f"the configured {cap_mb:.2f} MB email limit, so no files were "
            f"attached. The log remains on disk at:\n  {log_file_path}\n"
        )
    remaining_mb = max(0.0, cap_mb - log_mb)
    attachments, note = gather_attachments(
        log_file_path, report_paths, output_dir, logger_obj,
        cap_mb=remaining_mb, label="CSV",
    )
    if note:
        note += (
            f"Configured total email attachment ceiling: {cap_mb:.2f} MB; "
            f"log usage: {log_mb:.2f} MB.\n"
        )
    return attachments, note


def _send_abort(subject, reason, log_file_path, args,
                start_wallclock, start_perf):
    safe_reason = redact_token(str(reason))
    logger.error("Run ABORTED: %s", safe_reason)
    send_email(
        subject,
        f"Run ABORTED. {safe_reason}\n\n"
        + timing_block(start_wallclock, start_perf),
        [log_file_path],
        args.smtp_server,
        args.email_to,
        args.email_from,
        logger,
    )


def main(argv=None):
    global logger

    args = parse_args(argv)
    script_start = time.perf_counter()
    start_wallclock, start_perf = start_timer()
    base_timestamp = build_timestamp()

    finalize_config(args, required=REQUIRED_SETTINGS)

    platform = get_platform(args.portal_url)
    if platform != EXPECTED_PLATFORM:
        print(
            f"ERROR: This is a Portal-only script, but portal-url "
            f"{args.portal_url!r} was classified as {platform!r}.",
            file=sys.stderr,
        )
        return 2
    org_prefix = args.org_name_safe

    output_arg = args.output_path or get_config_value(
        "output-dir", args.target, path=args.config)
    if not output_arg:
        print("ERROR: No output path given and 'output-dir' is not set in "
              "the --config org toml.", file=sys.stderr)
        return 2

    content_items_dir = args.content_items_dir or get_config_value(
        "content-items-dir", args.target, path=args.config)
    if not content_items_dir:
        print("ERROR: No --content-items-dir given and 'content-items-dir' "
              "is not set in the --config org toml.", file=sys.stderr)
        return 2

    try:
        paths = _reserve_run_paths(
            output_arg, base_timestamp, org_prefix, platform
        )
    except Exception as exc:
        print(f"ERROR: Could not reserve output paths: {exc}", file=sys.stderr)
        return 1

    timestamp = paths["timestamp"]
    output_csv = paths["csv"]
    log_file_path = paths["log"]
    logger = setup_logging(log_file_path, "compare_content_dir")
    max_email_mb = get_max_email_mb(args.target, args.config)

    logger.info("=" * 70)
    logger.info("Portal Item Orphan Scan - run started")
    logger.info("=" * 70)
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s (hard-coded)", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: PRODUCTION")
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Content items directory: %s", content_items_dir)
    logger.info("Output CSV: %s", output_csv)
    logger.info("Log file: %s", log_file_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s | from: %s", args.email_to, args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Worker threads: %d", args.workers)
    logger.info("=" * 70)

    subject = subject_line(
        EMAIL_SUBJECT, timestamp, platform, org_name=args.org_name
    )
    tls_mode = _TLS_MODE_CHOICES[args.tls_mode]
    ca_bundle_path = (
        args.ca_bundle_path
        if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE
        else None
    )

    session = None
    session_pool = None
    try:
        logger.info("STEP 1/4: Establish verified TLS sessions and connect to Portal")
        session = build_secure_session(
            tls_mode,
            ca_bundle_path=ca_bundle_path,
            pool_connections=max(32, args.workers),
            pool_maxsize=max(32, args.workers * 2),
            retry_total=3,
            logger=logger,
        )
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, session, logger=logger
        )
        if not handshake_ok:
            raise ScanAbort(
                f"TLS handshake verification to {args.portal_url} failed: "
                f"{handshake_detail.get('error')}"
            )

        gis = connect_gis(
            args.portal_url,
            args.username,
            args.keyring_service,
            logger,
            verify_mode=tls_mode,
            ca_bundle_path=ca_bundle_path,
        )
        authenticated_portal_url = _authenticated_portal_base(
            gis, args.portal_url
        )
        _validate_portal_identity(args.portal_url, authenticated_portal_url)
        if get_platform(authenticated_portal_url) != EXPECTED_PLATFORM:
            raise ScanAbort(
                "The authenticated GIS connection is not an Enterprise Portal."
            )

        principal_name, principal_role, principal_basis = \
            _validate_inventory_principal(gis)
        logger.info(
            "Inventory visibility confirmed for %s (role=%s; %s).",
            principal_name, principal_role, principal_basis,
        )

        token_provider = provider_for_gis(gis, platform)
        logger.info("Token acquired via %s (token present: %s).",
                    type(token_provider).__name__, token_provider.is_present())
        org_id = _resolve_org_id(
            gis, session, authenticated_portal_url, token_provider
        )
        logger.info("Authenticated organization id: %s", org_id)

        # Build worker sessions serially before any worker starts, particularly
        # important when OS-trust-store mode modifies Python's SSL machinery.
        session_pool = WorkerSessionPool(
            tls_mode,
            ca_bundle_path,
            initial_session=session,
            capacity=args.workers,
        )

        logger.info("STEP 2/4: Retrieve Portal items and org users through the "
                    "verified REST session")
        items_by_id = get_portal_items(
            session, authenticated_portal_url, token_provider, org_id=org_id
        )
        org_users = get_org_usernames(
            session, authenticated_portal_url, token_provider, org_id=org_id
        )

        logger.info("STEP 3/4: Scan disk content directory (%d threads)",
                    args.workers)
        disk_folders, disk_stats = _get_disk_item_folders_detailed(
            content_items_dir, workers=args.workers
        )

        logger.info("STEP 4/4: Compare and write report")
        summary, report_stats = _build_and_write_report_detailed(
            items_by_id,
            disk_folders,
            output_csv,
            session,
            authenticated_portal_url,
            token_provider,
            org_users,
            platform,
            workers=args.workers,
            session_pool=session_pool,
        )

        result = "INCOMPLETE" if disk_stats["size_errors"] else "COMPLETE"
        exit_code = 1 if result == "INCOMPLETE" else 0
        total_elapsed = time.perf_counter() - script_start
        timing = timing_block(start_wallclock, start_perf)

        logger.info("=" * 70)
        logger.info("RUN SUMMARY")
        logger.info("Result: %s", result)
        logger.info("Organization: %s", args.org_name)
        logger.info("Platform: %s", platform)
        logger.info("Organization items: %d", report_stats["portal_items"])
        logger.info("Disk folders: %d", report_stats["disk_folders"])
        logger.info("Folder-size read errors: %d", disk_stats["size_errors"])
        logger.info("Output CSV: %s", output_csv)
        logger.info("Total runtime: %s", format_elapsed(total_elapsed))
        logger.info("=" * 70)
        for line in timing.rstrip("\n").split("\n"):
            logger.info("%s", line)

        if result == "INCOMPLETE":
            subject = "[INCOMPLETE] " + subject
        body = (
            f"Result: {result}\n"
            f"Organization: {args.org_name}\n\n"
            + summary
            + f"\nFolder-size read errors: {disk_stats['size_errors']}\n"
        )
        if result == "INCOMPLETE":
            body += (
                "WARNING: One or more files/directories could not be measured. "
                "Item-ID status comparisons are retained, but reported folder "
                "sizes may be incomplete.\n"
            )
        body += "\n" + timing

        attachments, note = _gather_run_attachments(
            log_file_path,
            [output_csv],
            os.path.dirname(output_csv),
            logger,
            max_email_mb,
        )
        body += note
        send_email(
            subject, body, attachments, args.smtp_server,
            args.email_to, args.email_from, logger,
        )
        return exit_code

    except Exception as exc:
        _send_abort(
            subject,
            redact_token(str(exc)),
            log_file_path,
            args,
            start_wallclock,
            start_perf,
        )
        return 1
    finally:
        if session_pool is not None:
            session_pool.close()
        elif session is not None:
            try:
                session.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
