#!/usr/bin/env python3
r"""
ITEM_INVENTORY.py

Inventories every item, and optionally every group, in one authenticated
ArcGIS Online or ArcGIS Enterprise organization. Writes three CSVs: the main
item/group inventory, a feature-service-only inventory (with service kind and
last edit date), and an item-type count summary.

Tokens are centrally held in a TokenProvider and exposed only while an
outgoing request is constructed. Token-bearing requests are restricted to the
authenticated Portal/AGOL origin and trusted ArcGIS service origins; rendered
errors are redacted before logs or notifications are written.

Usage:
    python ITEM_INVENTORY.py --config org1.config.toml --target agol
    python ITEM_INVENTORY.py --config org2.config.toml --target portal --no-test
    python ITEM_INVENTORY.py --config org1.config.toml --target agol --no-groups --max-items 1000
    python ITEM_INVENTORY.py --config org2.config.toml --target portal --with-groups
    python ITEM_INVENTORY.py --config org1.config.toml --target agol --no-test --no-edit-tracking
    python ITEM_INVENTORY.py --config org1.config.toml --target agol --tls-mode os-trust-store
    python ITEM_INVENTORY.py --help
"""

import argparse
import datetime
import html
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
# Production scripts in this project normally live one directory below the
# project root, but accepting a script placed directly in the root preserves
# the original Python import behavior and makes the origin check layout-safe.
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

# Import only after the spec origin has been verified, so a shadow package does
# not get an opportunity to execute before the location check.
from UTILITIES import (
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    get_org_id,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_target_arg,
    add_test_flag,
    add_config_arg,
    subject_line,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    write_dict_csv,
    epoch_to_date,
    DEFAULT_CAP,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)
from UTILITIES.restutils import get_json
from UTILITIES.emailer import _total_mb, MB
from UTILITIES.stack_discovery import site_name_from_url

EMAIL_SUBJECT_PREFIX = "Item Inventory"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

DEFAULT_WORKERS = 12
REQUEST_TIMEOUT = 60
GROUP_PAGE_SIZE = 100
TEST_MODE_ITEM_LIMIT = 250

NO_EDITOR_TRACKING = "no editor tracking found"
NO_RECORDS_TO_TRACK = "no records to track"
REGISTERED_SKIP_MSG = "Editor tracking detection not available for registered services"
EDIT_DATE_DISPLAY_FMT = "%Y/%m/%d"
EDIT_TIMEOUT_MIN = 15.0
EDIT_TIMEOUT_MAX = 45.0
EDIT_TIMEOUT_MULTIPLIER = 4.0
_HOSTED_FS_TYPE = "Feature Service"

DETAIL_FIELD_MAP = [
    ("id", "item_id"),
    ("owner", "owner"),
    ("created", "date_created"),
    ("modified", "date_modified"),
    ("title", "title"),
    ("type", "item_type"),
    ("description", "description"),
    ("tags", "tags"),
    ("snippet", "summary"),
    ("accessInformation", "acknowledgments"),
    ("licenseInfo", "terms_of_use"),
    ("numViews", "numViews"),
    ("protected", "protected"),
    ("access", "access"),
]

_DATE_CSV_FIELDS = {"date_created", "date_modified"}

OUTPUT_FIELDS = ["platform"] + [csv for _json, csv in DETAIL_FIELD_MAP]

FS_OUTPUT_FIELDS = [
    "platform", "record_kind", "item_id", "owner", "date_created",
    "date_modified", "title", "item_type", "last_fs_edited", "rest_url",
]

SUMMARY_OUTPUT_FIELDS = ["item_type", "count"]

_TAG_RE = re.compile(r"<[^>]+>")
_LEADING_TAGS_RE = re.compile(r"^(?:\s*<[^>]+>\s*)+")
_TRAILING_TAGS_RE = re.compile(r"(?:\s*<[^>]+>\s*)+$")

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


# ---------------------------------------------------------------------------
# Hardened discovery/request controls. These are local so changing this script
# does not alter helper behavior for other production jobs.
# ---------------------------------------------------------------------------
SEARCH_PAGE_SIZE = 100
GROUP_SEARCH_CAP = 10000
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ERROR_SAMPLES = 50
AGOL_TRUSTED_SUFFIX = ".arcgis.com"
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
_GROUP_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_ORG_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_SERVICE_ENDPOINT_RE = re.compile(
    r"(?i)^.+/(?:FeatureServer|MapServer)(?:/\d+)?(?:/query)?/?$"
)
_PORTAL_PROXY_RE = re.compile(
    r"(?i)/sharing/servers/[0-9a-f]{32}/rest/services/"
)


class ScanAbort(RuntimeError):
    """Controlled failure that invalidates inventory completeness."""


class RestCallError(RuntimeError):
    """Sanitized direct-REST failure with classification metadata."""

    def __init__(self, message, *, tls_error=False, auth_error=False):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)


@dataclass
class FederationInfo:
    federated_sites: set = field(default_factory=set)
    federated_guids: set = field(default_factory=set)
    hosting_site: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class ItemStore:
    """Disk-backed item store preserving the original sorted-id selection."""

    def __init__(self, logger, directory=None):
        fd, path = tempfile.mkstemp(
            prefix=".item_inventory_", suffix=".sqlite", dir=directory
        )
        os.close(fd)
        self.path = path
        self.logger = logger
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE items (item_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._pending = 0
        self._count = 0
        logger.debug("Using temporary item catalog store: %s", path)

    def put(self, item_id, item):
        rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO items(item_id, payload) VALUES (?, ?)",
            (item_id, rendered),
        )
        if cursor.rowcount == 1:
            self._count += 1
        else:
            self._connection.execute(
                "UPDATE items SET payload=? WHERE item_id=?",
                (rendered, item_id),
            )
        self._pending += 1
        if self._pending >= 1000:
            self._connection.commit()
            self._pending = 0

    def __len__(self):
        return self._count

    def selected(self, limit=None):
        self._connection.commit()
        self._pending = 0
        sql = "SELECT item_id, payload FROM items ORDER BY item_id"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        cursor = self._connection.execute(sql, params)
        try:
            for item_id, payload in cursor:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ScanAbort("Temporary item store contained malformed data.")
                yield item_id, value
        finally:
            cursor.close()

    def close(self):
        try:
            self._connection.close()
        finally:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.logger.warning(
                    "Could not remove temporary item store %s: %s",
                    self.path, exc,
                )


class WorkerSessionPool:
    """One pre-created requests.Session per worker under one TLS policy.

    Sessions are constructed serially on the main thread before any worker is
    started. That avoids sharing mutable Session state and avoids applying an
    OS-trust-store SSL patch while another thread may already be using ssl.
    """

    def __init__(self, mode, ca_bundle_path, initial_session=None, capacity=1):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._assignment_lock = Lock()
        self._sessions = []
        self._assignments = {}
        self._next_session = 0
        self._initial_session = initial_session
        self._initial_thread = get_ident() if initial_session is not None else None
        if initial_session is not None:
            self._sessions.append(initial_session)

        self._worker_sessions = []
        try:
            for _ in range(max(1, int(capacity))):
                session = build_secure_session(
                    self._mode,
                    ca_bundle_path=self._ca_bundle_path,
                    pool_connections=2,
                    pool_maxsize=4,
                    retry_total=3,
                    logger=None,
                )
                self._worker_sessions.append(session)
                self._sessions.append(session)
        except Exception:
            self.close()
            raise

    def get(self):
        thread_id = get_ident()
        if (self._initial_session is not None
                and thread_id == self._initial_thread):
            return self._initial_session
        with self._assignment_lock:
            assigned = self._assignments.get(thread_id)
            if assigned is not None:
                return assigned
            # ThreadPoolExecutor never has more simultaneously live workers than
            # its configured maximum, which is the capacity supplied by main().
            # Modulo is only a defensive fallback if a library replaces a dead
            # worker thread after all pre-created sessions have been assigned.
            assigned = self._worker_sessions[
                self._next_session % len(self._worker_sessions)
            ]
            self._next_session += 1
            self._assignments[thread_id] = assigned
            return assigned

    def close(self):
        seen = set()
        for session in self._sessions:
            if id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()
        self._worker_sessions.clear()
        self._assignments.clear()


class FixedSessionPool:
    """Compatibility adapter for functions passed an existing Session."""

    def __init__(self, session):
        self._session = session

    def get(self):
        return self._session


def _split_https_url(raw):
    text = str(raw or "").strip()
    if not text:
        raise ScanAbort("URL is blank.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ScanAbort(f"URL could not be parsed: {text!r}") from exc
    if parts.scheme.lower() != "https":
        raise ScanAbort(f"URL must use HTTPS: {text!r}")
    if not parts.hostname:
        raise ScanAbort(f"URL contains no hostname: {text!r}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ScanAbort(f"URL contains an invalid port: {text!r}") from exc
    if parts.username is not None or parts.password is not None:
        raise ScanAbort("URL must not contain user-info credentials.")
    return parts


def _origin_tuple_from_parts(parts):
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or 443
    return scheme, host, port


def _origin_tuple(raw):
    try:
        return _origin_tuple_from_parts(_split_https_url(raw))
    except Exception:
        return None


def _normalize_portal_base(raw):
    parts = _split_https_url(raw)
    if parts.query or parts.fragment:
        raise ScanAbort("Portal URL must not contain a query or fragment.")
    try:
        path = _validated_path(parts)
    except RestCallError as exc:
        raise ScanAbort(f"Portal URL is unsafe: {exc}") from exc
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def _portal_base_url(gis, configured_url=None):
    # Prefer the SDK's public URL property. The private _portal.url member is
    # retained only as a compatibility fallback for older ArcGIS releases.
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    if not raw:
        raw = configured_url
    if not raw:
        raise ScanAbort("The authenticated GIS object exposed no Portal URL.")
    return _normalize_portal_base(str(raw))


def _normalize_federation_base_path(path):
    normalized = "/" + "/".join(
        segment for segment in str(path or "").split("/") if segment
    )
    lower = normalized.lower()
    marker = lower.find("/rest/services")
    if marker >= 0:
        normalized = normalized[:marker]
    elif lower.endswith("/admin"):
        normalized = normalized[:-6]
    elif lower.endswith("/rest"):
        normalized = normalized[:-5]
    return normalized.rstrip("/")


def _validated_path(parts):
    if parts.query or parts.fragment:
        raise RestCallError("Request URL must not contain a query or fragment.")
    raw_path = parts.path or ""
    lowered_raw = raw_path.lower()
    if "%2f" in lowered_raw or "%5c" in lowered_raw:
        raise RestCallError("Request URL contains an encoded slash or backslash.")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError("Request URL contains unsafe path characters.")
    segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise RestCallError("Request URL contains path traversal.")
    return raw_path.rstrip("/")


class TrustedUrlPolicy:
    """Decide which ArcGIS service URLs may receive the organization token."""

    def __init__(self, portal_base, target, explicit_origins, logger):
        self.portal_base = _normalize_portal_base(portal_base)
        self.target = str(target).lower()
        self.logger = logger
        self.portal_origin = _origin_tuple(self.portal_base)
        portal_parts = _split_https_url(self.portal_base)
        self.portal_path = (portal_parts.path or "").rstrip("/")
        self.explicit_origins = set()
        self.federation_bases = []
        for raw in explicit_origins or []:
            parts = _split_https_url(raw)
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ScanAbort(
                    "--trusted-service-origin must be an exact HTTPS origin "
                    f"without a path, query, or fragment: {raw!r}"
                )
            self.explicit_origins.add(_origin_tuple_from_parts(parts))

    def add_federation_base(self, raw):
        if not raw:
            return
        parts = _split_https_url(str(raw))
        if parts.query or parts.fragment:
            raise ScanAbort(
                f"Federation URL contains a query or fragment: {raw!r}"
            )
        origin = _origin_tuple_from_parts(parts)
        _validated_path(parts)
        base_path = _normalize_federation_base_path(parts.path)
        entry = (origin, base_path)
        if entry not in self.federation_bases:
            self.federation_bases.append(entry)

    def validate_portal_path(self, path):
        if not isinstance(path, str) or not path.startswith("/sharing/rest/"):
            raise RestCallError(
                "Portal REST path must begin with '/sharing/rest/'."
            )
        if "?" in path or "#" in path or "\\" in path:
            raise RestCallError("Portal REST path contains unsafe characters.")
        if any(part in (".", "..") for part in path.split("/")):
            raise RestCallError("Portal REST path contains traversal.")
        return self.portal_base + path

    def validate_portal_url(self, raw):
        try:
            parts = _split_https_url(raw)
        except ScanAbort as exc:
            raise RestCallError(str(exc)) from exc
        path = _validated_path(parts)
        if _origin_tuple_from_parts(parts) != self.portal_origin:
            raise RestCallError("Portal REST redirect changed origin.")
        required_prefix = self.portal_path + "/sharing/rest/"
        if not path.startswith(required_prefix):
            raise RestCallError(
                "Portal REST redirect left the authenticated sharing/rest path."
            )
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def validate_service_url(self, raw):
        try:
            parts = _split_https_url(raw)
        except ScanAbort as exc:
            raise RestCallError(str(exc)) from exc
        path = _validated_path(parts)
        if not _SERVICE_ENDPOINT_RE.match(path):
            raise RestCallError(
                "Service URL is not a FeatureServer/MapServer root, numeric "
                "sublayer, or sublayer query endpoint."
            )
        if "/rest/services/" not in path.lower():
            raise RestCallError("Service URL is not under an ArcGIS REST services path.")
        origin = _origin_tuple_from_parts(parts)
        host = origin[1]
        allowed = origin in self.explicit_origins
        if self.target == "agol" and host.endswith(AGOL_TRUSTED_SUFFIX):
            allowed = True
        if origin == self.portal_origin and _PORTAL_PROXY_RE.search(path):
            allowed = True
        for registered_origin, base_path in self.federation_bases:
            if origin != registered_origin:
                continue
            expected = (base_path + "/rest/services/").lower()
            if path.lower().startswith(expected):
                allowed = True
                break
        if not allowed:
            raise RestCallError(
                "Refusing to send the organization token to an unregistered "
                f"service origin: {parts.scheme}://{parts.netloc}"
            )
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def render_trusted(self):
        rendered = set()
        for scheme, host, port in self.explicit_origins:
            rendered.add(f"{scheme}://{host}" + (f":{port}" if port != 443 else ""))
        for (scheme, host, port), path in self.federation_bases:
            rendered.add(
                f"{scheme}://{host}" + (f":{port}" if port != 443 else "") + path
            )
        if self.target == "agol":
            rendered.add("https://*.arcgis.com")
        rendered.add(self.portal_base + "/sharing/servers/<id>/rest/services")
        return sorted(rendered)


class ArcGISRestClient:
    """Strict JSON client with size, origin, redirect, and token controls."""

    def __init__(self, policy, session_pool, token_provider, gis, logger):
        self.policy = policy
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.gis = gis
        self.logger = logger
        self._refresh_lock = Lock()

    def portal_json(self, path, params=None, timeout=REQUEST_TIMEOUT):
        url = self.policy.validate_portal_path(path)
        return self._request_with_refresh(
            url, params, timeout, self.policy.validate_portal_url
        )

    def service_json(self, url, params=None, timeout=REQUEST_TIMEOUT):
        safe_url = self.policy.validate_service_url(url)
        return self._request_with_refresh(
            safe_url, params, timeout, self.policy.validate_service_url
        )

    def _refresh_if_stale(self, failed_token):
        with self._refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return current
            try:
                _ = self.gis.properties.get("name")
            except Exception:
                pass
            try:
                self.token_provider.refresh()
                refreshed = self.token_provider.get()
            except Exception as exc:
                raise RestCallError(
                    f"token refresh failed: {exc}", auth_error=True
                ) from exc
            if not refreshed:
                raise RestCallError(
                    "token refresh returned no token", auth_error=True
                )
            return refreshed

    def _request_with_refresh(self, url, params, timeout, redirect_validator):
        token = self.token_provider.get()
        try:
            return self._request_once(
                url, params, timeout, token, redirect_validator
            )
        except RestCallError as exc:
            if not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._request_once(
            url, params, timeout, refreshed, redirect_validator
        )

    def _request_once(self, url, params, timeout, token, redirect_validator):
        merged = {"f": "json"}
        if params:
            merged.update(params)
        merged.pop("token", None)
        merged["token"] = token

        original_origin = _origin_tuple(url)
        current_url = url
        response = None
        for redirect_count in range(4):
            try:
                response = self.session_pool.get().get(
                    current_url,
                    params=merged,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.Timeout as exc:
                raise RestCallError(f"timeout after {timeout}s") from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(f"request failed: {exc}") from exc

            status = response.status_code
            if not 300 <= status < 400:
                break
            location = response.headers.get("Location")
            response.close()
            response = None
            if redirect_count >= 3:
                raise RestCallError(
                    "Too many redirects for a token-bearing request."
                )
            if not location:
                raise RestCallError(
                    f"HTTP {status} redirect supplied no Location header."
                )
            candidate = urljoin(current_url, location)
            candidate = redirect_validator(candidate)
            if _origin_tuple(candidate) != original_origin:
                raise RestCallError(
                    "Refusing a cross-origin redirect for a token-bearing request."
                )
            current_url = candidate

        if response is None:
            raise RestCallError("Request did not produce a response.")

        status = response.status_code
        reason = response.reason
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except (TypeError, ValueError) as exc:
                response.close()
                raise RestCallError(
                    f"invalid Content-Length header: {declared!r}"
                ) from exc
            if declared_size < 0 or declared_size > MAX_JSON_BYTES:
                response.close()
                raise RestCallError(
                    f"JSON response exceeded the {MAX_JSON_BYTES // MB} MB limit"
                )

        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    raise RestCallError(
                        f"JSON response exceeded the {MAX_JSON_BYTES // MB} MB limit"
                    )
                chunks.append(chunk)
        except RestCallError:
            raise
        except requests.exceptions.SSLError as exc:
            raise RestCallError(
                f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
                tls_error=True,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RestCallError(f"request failed while reading: {exc}") from exc
        finally:
            response.close()

        body = b"".join(chunks)
        if not body:
            raise RestCallError(
                f"HTTP {status} {reason}: empty response body",
                auth_error=status in (401, 403, 498, 499),
            )
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestCallError(
                f"non-JSON response (HTTP {status})",
                auth_error=status in (401, 403, 498, 499),
            ) from exc
        if not isinstance(data, dict):
            raise RestCallError("endpoint returned a non-object JSON response")

        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code", "?"))
            message = str(error.get("message") or "unknown REST error")
            details_value = error.get("details")
            if isinstance(details_value, list):
                details = "; ".join(str(value) for value in details_value)
            elif details_value:
                details = str(details_value)
            else:
                details = ""
            rendered = f"REST error {code}: {message}"
            if details:
                rendered += f" ({details})"
            raise RestCallError(
                rendered,
                auth_error=(code in {"401", "403", "498", "499"}
                            or bool(_AUTH_ERROR_RE.search(rendered))),
            )
        if status >= 400:
            raise RestCallError(
                f"HTTP {status} {reason}",
                auth_error=status in (401, 403, 498, 499),
            )
        if str(data.get("status", "")).strip().lower() == "error":
            messages = data.get("messages")
            rendered = (
                f"endpoint returned status=error: "
                f"{messages or 'no message supplied'}"
            )
            raise RestCallError(
                rendered, auth_error=bool(_AUTH_ERROR_RE.search(rendered))
            )
        return data

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

    for _ in range(max(1, max_in_flight)):
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


def _reserve_run_paths(output_dir, org_prefix, platform, timestamp):
    for suffix in range(1000):
        run_stamp = timestamp if suffix == 0 else f"{timestamp}_{suffix:02d}"
        log_path = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_item_inventory_{run_stamp}.log",
        )
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return {
            "timestamp": run_stamp,
            "log": log_path,
            "main": os.path.join(
                output_dir,
                f"{org_prefix}_{platform}_ITEM_INVENTORY_{run_stamp}.csv",
            ),
            "feature": os.path.join(
                output_dir,
                f"{org_prefix}_{platform}_FEATURE_SERVICE_INVENTORY_{run_stamp}.csv",
            ),
            "summary": os.path.join(
                output_dir,
                f"{org_prefix}_{platform}_ITEM_TYPE_SUMMARY_{run_stamp}.csv",
            ),
        }
    raise RuntimeError("Could not reserve a unique output name after 999 collisions.")


def _expected_platform(target):
    return "AGOL" if str(target).lower() == "agol" else "PORTAL"


def _validate_target_platform(target, platform, portal_url):
    expected = _expected_platform(target)
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} requires platform {expected}, but "
            f"portal-url {portal_url!r} was classified as {platform}."
        )


def _safe_org_id(value):
    text = str(value or "").strip()
    if not text or not _ORG_ID_RE.fullmatch(text):
        raise ScanAbort(f"Invalid or unavailable organization id: {text!r}")
    return text


def _mapping(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "get"):
        return value
    try:
        converted = dict(value)
    except Exception:
        converted = None
    return converted if isinstance(converted, dict) else None


def _arcgis_bool(value):
    """Parse ordinary ArcGIS Boolean representations without bool("false")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "", "none", "null"}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return bool(value)


def _resolve_federation_info(gis, policy, logger):
    info = FederationInfo()
    try:
        payload = gis.admin.federation.servers
        payload_map = _mapping(payload)
        if payload_map is None:
            raise ValueError("federation payload was not dictionary-like")
        servers = payload_map.get("servers", [])
        if not isinstance(servers, (list, tuple)):
            raise ValueError("federation 'servers' value was not a list")
        servers = list(servers)
    except Exception as exc:
        info.errors.append(
            "Could not read Portal federation metadata: "
            + redact_token(str(exc))
        )
        return info

    for raw in servers:
        srv = _mapping(raw)
        if srv is None:
            info.errors.append("Federation metadata contained a non-object server record.")
            continue
        name = srv.get("name") or srv.get("id") or "UnknownServer"
        admin_url = srv.get("adminUrl") or srv.get("url")
        server_id = srv.get("id")
        is_hosting = (_arcgis_bool(srv.get("isHosted"))
                      or "hosting" in str(name).lower())
        # Preserve the original inventory's classification rule: hosting_site
        # was derived from the hosting admin URL's first path segment, while a
        # hosting entry was excluded from the federated-site set.
        url_site_name = site_name_from_url(admin_url)
        classification_site = "HOSTING" if is_hosting else url_site_name
        if is_hosting:
            info.hosting_site = url_site_name or "HOSTING"
        elif url_site_name:
            info.federated_sites.add(str(url_site_name).lower())
        if server_id:
            info.federated_guids.add(str(server_id).lower())
        record = {
            "name": name,
            "admin_url": admin_url,
            "site_name": classification_site,
            "server_id": server_id,
            "is_hosting": is_hosting,
        }
        info.records.append(record)
        usable_base = False
        rejected_bases = []
        for key in ("url", "adminUrl", "serverUrl"):
            raw_url = srv.get(key)
            if not raw_url:
                continue
            try:
                policy.add_federation_base(raw_url)
                usable_base = True
            except Exception as exc:
                rejected_bases.append(
                    f"{key}={redact_token(str(exc))}"
                )
        if not usable_base:
            detail = ("; ".join(rejected_bases)
                      if rejected_bases else "no URL values supplied")
            info.errors.append(
                f"Federation server {name!r} supplied no usable HTTPS service "
                f"base ({detail})."
            )
        elif rejected_bases:
            logger.warning(
                "Ignoring unsafe alternate federation URL(s) for %r because "
                "the same record supplied a usable HTTPS service base: %s",
                name, "; ".join(rejected_bases),
            )

    logger.info(
        "Resolved %d federation server record(s): hosting site=%s, "
        "%d federated site(s), %d server GUID(s).",
        len(info.records), info.hosting_site,
        len(info.federated_sites), len(info.federated_guids),
    )
    logger.info("Trusted service destination(s): %s", policy.render_trusted())
    return info


def _search_results(data, label):
    if "results" in data:
        value = data.get("results")
    elif "items" in data:
        value = data.get("items")
    elif label == "group" and "groups" in data:
        value = data.get("groups")
    else:
        value = []
    if not isinstance(value, list):
        raise ScanAbort(f"{label.capitalize()} search returned a malformed results list.")
    if any(not isinstance(entry, dict) for entry in value):
        raise ScanAbort(f"{label.capitalize()} search returned a non-object result.")
    return value


def _search_total(data, label):
    value = data.get("total")
    if isinstance(value, bool):
        raise ScanAbort(f"{label.capitalize()} search returned a Boolean total.")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"{label.capitalize()} search returned an invalid total: {value!r}"
        ) from exc
    if total < 0:
        raise ScanAbort(f"{label.capitalize()} search returned a negative total.")
    return total


def _search_next_start(data, fallback, label):
    value = data.get("nextStart")
    if value is None:
        return fallback
    if isinstance(value, bool):
        raise ScanAbort(f"{label.capitalize()} search returned a Boolean nextStart.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"{label.capitalize()} search returned an invalid nextStart: {value!r}"
        ) from exc


def _item_bucket(client, query, org_id, store, cap=DEFAULT_CAP):
    params = {
        "q": query,
        "sortField": "modified",
        "sortOrder": "desc",
        "start": 1,
        "num": SEARCH_PAGE_SIZE,
    }
    first = client.portal_json("/sharing/rest/search", params=params)
    total = _search_total(first, "item")
    if total >= cap:
        return True

    data = first
    start = 1
    received = 0
    bucket_ids = set()
    while True:
        page_total = _search_total(data, "item")
        if page_total != total:
            raise ScanAbort(
                f"Item-search total changed during paging ({total} -> {page_total})."
            )
        page = _search_results(data, "item")
        if not page and received < total:
            raise ScanAbort(
                f"Item search returned an empty page after {received} of {total} results."
            )
        for item in page:
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                raise ScanAbort("Item search returned an item without an id.")
            if item_id in bucket_ids:
                raise ScanAbort(
                    f"Item search repeated id {item_id!r} within one query."
                )
            bucket_ids.add(item_id)
            explicit_org = item.get("orgId") or item.get("orgid")
            if (explicit_org not in (None, "")
                    and str(explicit_org).lower() != org_id.lower()):
                raise ScanAbort(
                    f"Item search returned cross-organization item {item_id} "
                    f"(orgId={explicit_org!r}, expected {org_id!r})."
                )
            store.put(item_id, item)
        received += len(page)
        if received > total:
            raise ScanAbort("Item search returned more records than its declared total.")
        next_start = _search_next_start(
            data, start + len(page) if page else -1, "item"
        )
        if next_start <= 0:
            if received != total:
                raise ScanAbort(
                    f"Item search ended after {received} of {total} results."
                )
            if len(bucket_ids) != total:
                raise ScanAbort(
                    f"Item-search paging mismatch: expected {total}, read "
                    f"{received}, unique {len(bucket_ids)}."
                )
            break
        if next_start <= start:
            raise ScanAbort("Item search pagination did not advance.")
        if next_start > cap:
            raise ScanAbort("Item search pagination exceeded the API result cap.")
        start = next_start
        params["start"] = start
        data = client.portal_json("/sharing/rest/search", params=params)
    return False


def _enumerate_items_strict(client, org_id, store, logger):
    query_count = 0

    def search_range(lo_ms, hi_ms):
        nonlocal query_count
        query_count += 1
        query = f"orgid:{org_id} AND created:[{lo_ms} TO {hi_ms}]"
        hit_cap = _item_bucket(client, query, org_id, store)
        if hit_cap and hi_ms > lo_ms:
            mid = lo_ms + (hi_ms - lo_ms) // 2
            if mid <= lo_ms:
                mid = lo_ms + 1
            search_range(lo_ms, mid)
            search_range(mid + 1, hi_ms)
            return
        if hit_cap:
            raise ScanAbort(
                f"A one-millisecond item-search bucket [{lo_ms} TO {hi_ms}] "
                f"reached the {DEFAULT_CAP}-item cap."
            )
        if query_count % 20 == 0:
            logger.info(
                "  %d strict item-search request(s), %d unique item(s) so far.",
                query_count, len(store),
            )

    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    cursor = _SEARCH_START
    while cursor < span_end:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        search_range(
            int(cursor.timestamp() * 1000),
            int(next_month.timestamp() * 1000) - 1,
        )
        cursor = next_month
    logger.info(
        "Strict item search completed: %d request(s), %d unique item(s).",
        query_count, len(store),
    )


def _group_page(client, query, start):
    return client.portal_json(
        "/sharing/rest/community/groups",
        params={
            "q": query,
            "sortField": "created",
            "sortOrder": "asc",
            "num": GROUP_PAGE_SIZE,
            "start": start,
        },
    )


def _consume_group_pages(client, query, org_id, first, seen, cap=GROUP_SEARCH_CAP):
    total = _search_total(first, "group")
    if total >= cap:
        return True
    data = first
    start = 1
    received = 0
    range_ids = set()
    while True:
        if _search_total(data, "group") != total:
            raise ScanAbort("Group-search total changed during paging.")
        page = _search_results(data, "group")
        if not page and received < total:
            raise ScanAbort(
                f"Group search returned an empty page after {received} of {total} results."
            )
        for group in page:
            group_id = str(group.get("id") or "").strip()
            if not group_id or not _GROUP_ID_RE.fullmatch(group_id):
                raise ScanAbort(f"Group search returned an invalid id: {group_id!r}")
            if group_id in range_ids:
                raise ScanAbort(
                    f"Group search repeated id {group_id!r} within one query."
                )
            range_ids.add(group_id)
            explicit_org = group.get("orgId") or group.get("orgid")
            if (explicit_org not in (None, "")
                    and str(explicit_org).lower() != org_id.lower()):
                raise ScanAbort(
                    f"Group search returned cross-organization group {group_id} "
                    f"(orgId={explicit_org!r}, expected {org_id!r})."
                )
            if group_id not in seen:
                seen[group_id] = group
        received += len(page)
        next_start = _search_next_start(
            data, start + len(page) if page else -1, "group"
        )
        if next_start <= 0:
            if received != total:
                raise ScanAbort(
                    f"Group search ended after {received} of {total} results."
                )
            break
        if next_start <= start:
            raise ScanAbort("Group search pagination did not advance.")
        if next_start > cap:
            raise ScanAbort("Group search pagination exceeded the API result cap.")
        start = next_start
        data = _group_page(client, query, start)
    if received != total or len(range_ids) != total:
        raise ScanAbort(
            f"Group-search paging mismatch: expected {total}, read {received}, "
            f"unique {len(range_ids)}."
        )
    return False


def _enumerate_groups_strict(client, org_id, logger):
    seen = {}
    base_query = f"orgid:{org_id}"
    first = _group_page(client, base_query, 1)
    if not _consume_group_pages(client, base_query, org_id, first, seen):
        logger.info("Discovered %d unique organization group(s).", len(seen))
        return list(seen.values())

    seen.clear()
    request_count = 1

    def search_range(lo_ms, hi_ms):
        nonlocal request_count
        query = f"orgid:{org_id} AND created:[{lo_ms} TO {hi_ms}]"
        first_page = _group_page(client, query, 1)
        request_count += 1
        hit_cap = _consume_group_pages(
            client, query, org_id, first_page, seen
        )
        if hit_cap and hi_ms > lo_ms:
            mid = lo_ms + (hi_ms - lo_ms) // 2
            if mid <= lo_ms:
                mid = lo_ms + 1
            search_range(lo_ms, mid)
            search_range(mid + 1, hi_ms)
            return
        if hit_cap:
            raise ScanAbort(
                f"A one-millisecond group-search bucket [{lo_ms} TO {hi_ms}] "
                f"reached the {GROUP_SEARCH_CAP}-group cap."
            )

    end_ms = int(
        (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)).timestamp()
        * 1000
    )
    search_range(0, end_ms)
    logger.info(
        "Discovered %d unique organization group(s) using strict bucketing.",
        len(seen),
    )
    return list(seen.values())


def _strip_html(text, full=True):
    if not isinstance(text, str):
        return text
    if full:
        cleaned = _TAG_RE.sub(" ", text)
    else:
        cleaned = _LEADING_TAGS_RE.sub("", text)
        cleaned = _TRAILING_TAGS_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned)
    return " ".join(cleaned.split())


def _clean_cell(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(_strip_html(str(v)) for v in value if v is not None)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _strip_html(value)
    return value


def _row_from_detail(platform, record_kind, detail):
    row = {"platform": platform}
    for json_field, csv_field in DETAIL_FIELD_MAP:
        raw = detail.get(json_field) if isinstance(detail, dict) else None
        if csv_field in _DATE_CSV_FIELDS:
            row[csv_field] = epoch_to_date(raw)
        else:
            row[csv_field] = _clean_cell(raw)
    if record_kind == "group":
        row["item_type"] = "Group"
    row["_record_kind"] = record_kind
    return row


class AdaptiveTimeout:
    def __init__(self, floor=EDIT_TIMEOUT_MIN, ceiling=EDIT_TIMEOUT_MAX,
                multiplier=EDIT_TIMEOUT_MULTIPLIER, window=50):
        self._floor = floor
        self._ceiling = ceiling
        self._multiplier = multiplier
        self._window = window
        self._lock = Lock()
        self._samples = []

    def current_timeout(self):
        with self._lock:
            if not self._samples:
                return self._floor
            avg = sum(self._samples) / len(self._samples)
        return max(self._floor, min(self._ceiling, avg * self._multiplier))

    def record(self, elapsed_seconds):
        with self._lock:
            self._samples.append(elapsed_seconds)
            if len(self._samples) > self._window:
                self._samples.pop(0)


def format_edit_date(epoch_ms):
    import datetime
    try:
        dt = datetime.datetime.utcfromtimestamp(int(epoch_ms) / 1000.0)
        return dt.strftime(EDIT_DATE_DISPLAY_FMT)
    except Exception:
        return NO_EDITOR_TRACKING


def _is_hosted_feature_service(item_dict):
    if not isinstance(item_dict, dict):
        return False
    return item_dict.get("type") == _HOSTED_FS_TYPE


_SHARING_SERVERS_GUID_RE = re.compile(
    r"/sharing/servers/([0-9a-fA-F]{32})/rest/services/", re.IGNORECASE)


def _guid_from_sharing_servers_url(url):
    if not url:
        return None
    m = _SHARING_SERVERS_GUID_RE.search(url)
    return m.group(1).lower() if m else None



def _classify_fs_kind(item_dict, platform, federated_sites=None,
                      hosting_site=None, federated_guids=None):
    if not isinstance(item_dict, dict):
        return "Registered"
    raw_keywords = item_dict.get("typeKeywords") or []
    if isinstance(raw_keywords, str):
        type_keywords = [raw_keywords]
    elif isinstance(raw_keywords, (list, tuple, set)):
        type_keywords = raw_keywords
    else:
        type_keywords = [raw_keywords]
    tk_lower = {str(t).lower() for t in type_keywords}
    if "hosted service" in tk_lower:
        return "Hosted"
    if platform == "AGOL":
        return "Registered"
    url = item_dict.get("url") or ""

    guid = _guid_from_sharing_servers_url(url)
    if guid and federated_guids and guid in federated_guids:
        return "Federated"

    site = (site_name_from_url(url) or "").lower()
    if hosting_site and site == hosting_site.lower():
        return "Hosted"
    if federated_sites and site and site in federated_sites:
        return "Federated"
    return "Registered"

def _extract_edit_date_field(layer_defn):
    if not isinstance(layer_defn, dict):
        return None
    efi = layer_defn.get("editFieldsInfo")
    if not isinstance(efi, dict):
        return None
    claimed = efi.get("editDateField")
    if not isinstance(claimed, str) or not claimed.strip():
        return None
    claimed = claimed.strip()

    fields = layer_defn.get("fields")
    if not isinstance(fields, list) or not fields:
        return None

    claimed_lower = claimed.lower()
    for f in fields:
        if not isinstance(f, dict):
            continue
        real_name = f.get("name")
        if isinstance(real_name, str) and real_name.lower() == claimed_lower:
            return real_name
    return None


def _service_editor_tracking_enabled(service_defn):
    if not isinstance(service_defn, dict):
        return False
    eti = service_defn.get("editorTrackingInfo")
    if isinstance(eti, dict):
        return _arcgis_bool(eti.get("enableEditorTracking"))
    return False



def _get_json_timed(session, url, token_provider, timeout, params=None):
    start = time.perf_counter()
    if isinstance(session, ArcGISRestClient):
        try:
            data = session.service_json(url, params=params, timeout=timeout)
            err = None
        except RestCallError as exc:
            data, err = None, str(exc)
    else:
        data, err = get_json(session, url, token_provider.get(), timeout=timeout,
                             params=params)
    elapsed = time.perf_counter() - start
    return data, err, elapsed


def detect_last_fs_edited(session, service_url, token_provider,
                          timeout_tracker, skip_log, item_id, item_title,
                          logger):
    if not service_url:
        return NO_EDITOR_TRACKING, False

    root_url = service_url.rstrip("/")
    errors = []

    timeout = timeout_tracker.current_timeout()
    data, err, elapsed = _get_json_timed(session, root_url, token_provider,
                                         timeout)
    if (err or not isinstance(data, dict)) and "timeout" in str(err).lower():
        retry_timeout = min(EDIT_TIMEOUT_MAX, timeout * 2)
        if logger is not None:
            logger.info("  [edit-tracking] %s (%s): service definition "
                        "fetch timed out at %.1fs, retrying once at "
                        "%.1fs ...", item_title, item_id, timeout,
                        retry_timeout)
        data, err, elapsed = _get_json_timed(session, root_url,
                                             token_provider, retry_timeout)
    if err or not isinstance(data, dict):
        msg = redact_token(f"service definition fetch failed: {err}")
        skip_log.append({
            "item_id": item_id, "title": item_title, "service_url": root_url,
            "reason": msg,
        })
        if logger is not None:
            logger.warning("  [edit-tracking] %s (%s): %s (%.1fs, "
                           "timeout=%.1fs)", item_title, item_id, msg,
                           elapsed, timeout)
        return msg, True
    timeout_tracker.record(elapsed)

    if not _service_editor_tracking_enabled(data):
        return NO_EDITOR_TRACKING, False

    sublayer_ids = []
    malformed_sections = []
    for key in ("layers", "tables"):
        container = data.get(key) or []
        if not isinstance(container, list):
            malformed_sections.append(key)
            continue
        for lyr in container:
            if not isinstance(lyr, dict):
                malformed_sections.append(key)
                continue
            raw_id = lyr.get("id")
            if isinstance(raw_id, bool):
                malformed_sections.append(key)
                continue
            try:
                layer_id = int(raw_id)
            except (TypeError, ValueError):
                malformed_sections.append(key)
                continue
            if layer_id < 0:
                malformed_sections.append(key)
                continue
            sublayer_ids.append(layer_id)

    if malformed_sections:
        msg = ("service returned malformed layer/table descriptors in: "
               + ", ".join(sorted(set(malformed_sections))))
        skip_log.append({
            "item_id": item_id, "title": item_title,
            "service_url": root_url, "reason": msg,
        })
        return msg, True

    if not sublayer_ids:
        msg = ("editorTrackingInfo.enableEditorTracking=true but the "
               "service reported no layers/tables to check")
        skip_log.append({
            "item_id": item_id, "title": item_title, "service_url": root_url,
            "reason": msg,
        })
        return msg, True

    tracked = []
    for layer_id in sublayer_ids:
        layer_url = f"{root_url}/{layer_id}"
        timeout = timeout_tracker.current_timeout()
        lyr_data, lyr_err, lyr_elapsed = _get_json_timed(
            session, layer_url, token_provider, timeout)
        if lyr_err or not isinstance(lyr_data, dict):
            msg = redact_token(
                f"layer {layer_id} definition fetch failed: {lyr_err}")
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": layer_url,
                "reason": msg,
            })
            if logger is not None:
                logger.warning("  [edit-tracking] %s (%s): layer %s "
                               "definition fetch failed (%.1fs, "
                               "timeout=%.1fs): %s", item_title, item_id,
                               layer_id, lyr_elapsed, timeout, msg)
            continue
        timeout_tracker.record(lyr_elapsed)

        fld = _extract_edit_date_field(lyr_data)
        if fld:
            tracked.append((layer_id, fld))

    if not tracked:
        base_msg = ("editorTrackingInfo.enableEditorTracking=true but no "
                    "sublayer had an editDateField that validated against "
                    "its own live fields[] (stale/mismatched metadata)")
        skip_log.append({
            "item_id": item_id, "title": item_title, "service_url": root_url,
            "reason": base_msg,
        })
        combined = "; ".join(errors + [base_msg]) if errors else base_msg
        return combined, True

    best_epoch = None
    any_query_failed = False
    any_layer_had_records = False
    for layer_id, edit_date_field in tracked:
        layer_url = f"{root_url}/{layer_id}/query"
        timeout = timeout_tracker.current_timeout()
        params = {
            "where": "1=1",
            "outFields": edit_date_field,
            "returnGeometry": "false",
            "orderByFields": f"{edit_date_field} DESC",
            "resultRecordCount": 1,
        }
        data, err, elapsed = _get_json_timed(session, layer_url,
                                             token_provider, timeout,
                                             params=params)
        if err or not isinstance(data, dict):
            any_query_failed = True
            msg = redact_token(
                f"layer {layer_id} (field={edit_date_field}) edit-date "
                f"query failed: {err}")
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": layer_url,
                "reason": msg,
            })
            if logger is not None:
                logger.warning("  [edit-tracking] %s (%s): edit-date query "
                               "failed for layer %s (field=%s) (%.1fs, "
                               "timeout=%.1fs): %s", item_title, item_id,
                               layer_id, edit_date_field, elapsed, timeout,
                               msg)
            continue
        timeout_tracker.record(elapsed)

        if "features" not in data or not isinstance(data.get("features"), list):
            any_query_failed = True
            msg = (f"layer {layer_id} (field={edit_date_field}) edit-date "
                   "query returned a malformed features list")
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": layer_url, "reason": msg,
            })
            continue
        feats = data.get("features") or []
        if not feats:
            continue
        any_layer_had_records = True
        first_feature = feats[0]
        if not isinstance(first_feature, dict):
            any_query_failed = True
            msg = f"layer {layer_id} edit-date query returned a malformed feature"
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": layer_url, "reason": msg,
            })
            continue
        attrs = first_feature.get("attributes")
        if not isinstance(attrs, dict):
            any_query_failed = True
            msg = f"layer {layer_id} edit-date query returned malformed attributes"
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": layer_url, "reason": msg,
            })
            continue
        val = attrs.get(edit_date_field)
        if val is None:
            continue
        try:
            val = int(val)
        except (TypeError, ValueError):
            continue
        if best_epoch is None or val > best_epoch:
            best_epoch = val

    if best_epoch is not None:
        date_str = format_edit_date(best_epoch)
        if date_str == NO_EDITOR_TRACKING:
            msg = "edit-date query returned an invalid epoch value"
            errors.append(msg)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": root_url, "reason": msg,
            })
        if errors:
            combined = "; ".join([date_str] + errors)
            skip_log.append({
                "item_id": item_id, "title": item_title,
                "service_url": root_url,
                "reason": ("partial failure: a valid edit date was found "
                           "on another sublayer, but " + "; ".join(errors)),
            })
            return combined, True
        return date_str, False

    if not any_layer_had_records and not any_query_failed:
        return NO_RECORDS_TO_TRACK, False

    if errors:
        return "; ".join(errors), True

    return NO_EDITOR_TRACKING, False


def build_fs_rows(items_by_id, platform, federated_sites=None,
                  hosting_site=None, federated_guids=None):
    fs_rows = []
    for item_id, item_dict in items_by_id.items():
        if not _is_hosted_feature_service(item_dict):
            continue
        kind = _classify_fs_kind(item_dict, platform, federated_sites,
                                 hosting_site, federated_guids)
        raw_url = item_dict.get("url") or ""
        fs_rows.append({
            "platform": platform,
            "record_kind": kind,
            "item_id": item_id,
            "owner": _clean_cell(item_dict.get("owner")),
            "date_created": epoch_to_date(item_dict.get("created")),
            "date_modified": epoch_to_date(item_dict.get("modified")),
            "title": _clean_cell(item_dict.get("title")),
            "item_type": _HOSTED_FS_TYPE,
            "last_fs_edited": "",
            "rest_url": redact_token(str(raw_url)),
            "_item_id": item_id,
        })
    return fs_rows


def resolve_last_fs_edited_for_rows(fs_rows, session, token_provider,
                                    workers, logger):
    candidates = []
    skip_log = []
    for row in fs_rows:
        if row["record_kind"] == "Registered":
            row["last_fs_edited"] = REGISTERED_SKIP_MSG
            continue
        service_url = row.get("rest_url")
        if not service_url:
            message = "Feature Service item has no 'url' in its search result"
            row["last_fs_edited"] = message
            skip_log.append({
                "item_id": row.get("_item_id", ""),
                "title": row.get("title", ""),
                "service_url": "",
                "reason": message,
            })
            continue
        candidates.append(row)

    if not candidates:
        logger.info("No Hosted/Federated Feature Service items with a "
                    "usable service URL found; skipping editor-tracking "
                    "detection.")
        return skip_log

    logger.info("Detecting editor tracking / last-edit date for %d Feature "
                "Service item(s), %d worker(s) ...", len(candidates), workers)

    timeout_tracker = AdaptiveTimeout()

    def worker(row):
        local_skips = []
        result, _had_skip = detect_last_fs_edited(
            session, row["rest_url"], token_provider, timeout_tracker,
            local_skips, row["_item_id"], row["title"], logger)
        return result, local_skips

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for row, future in _bounded_future_results(
                executor, candidates, worker, max_in_flight=max(1, workers * 2)):
            try:
                result, local_skips = future.result()
            except Exception as exc:
                message = redact_token(f"editor-tracking worker failed: {exc}")
                row["last_fs_edited"] = message
                local_skips = [{
                    "item_id": row.get("_item_id", ""),
                    "title": row.get("title", ""),
                    "service_url": row.get("rest_url", ""),
                    "reason": message,
                }]
            else:
                row["last_fs_edited"] = result
            skip_log.extend(local_skips)
            done += 1
            if done % 200 == 0 or done == len(candidates):
                logger.info("  editor-tracking detection: %d / %d Feature "
                            "Service item(s) processed ...", done,
                            len(candidates))

    logger.info("Editor-tracking detection complete: %d Feature Service "
                "item(s) checked, %d skip(s) logged.", len(candidates),
                len(skip_log))
    return skip_log

def build_summary_rows(item_rows, fs_rows):
    counts = {}
    for row in item_rows:
        it = row.get("item_type") or "(unknown)"
        if it == _HOSTED_FS_TYPE:
            continue
        counts[it] = counts.get(it, 0) + 1
    for row in fs_rows:
        label = f"Feature Service ({row['record_kind']})"
        counts[label] = counts.get(label, 0) + 1
    return [{"item_type": k, "count": v} for k, v in sorted(counts.items())]



def inventory_items(gis, base_url, session, platform, max_items, logger,
                    token_provider=None, temp_dir=None):
    org_id = _safe_org_id(get_org_id(gis, logger=logger))
    logger.info("Scoping item search to organization id: %s", org_id)

    if isinstance(session, ArcGISRestClient):
        client = session
    else:
        provider = token_provider or provider_for_gis(gis, platform)
        policy = TrustedUrlPolicy(base_url, platform.lower(), [], logger)
        client = ArcGISRestClient(
            policy, FixedSessionPool(session), provider, gis, logger
        )

    store = ItemStore(logger, directory=temp_dir)
    try:
        logger.info("Enumerating all organization items via strict date-range "
                    "bucketing ...")
        _enumerate_items_strict(client, org_id, store, logger)
        total = len(store)
        logger.info("Strict search found %d unique item(s).", total)

        if max_items is not None and total > max_items:
            logger.info("TEST/cap mode: limiting from %d to the first %d "
                        "item(s), preserving the original sorted-id rule.",
                        total, max_items)

        items_by_id = {}
        rows = []
        for item_id, item in store.selected(max_items):
            items_by_id[item_id] = item
            rows.append(_row_from_detail(platform, "item", item))
        logger.info("Mapped %d item detail record(s) to CSV rows.", len(rows))
        return rows, items_by_id
    finally:
        store.close()



def inventory_groups(gis, base_url, session, token_provider, platform, logger):
    org_id = _safe_org_id(get_org_id(gis, logger=logger))
    logger.info("Scoping group inventory to organization id: %s", org_id)

    if isinstance(session, ArcGISRestClient):
        client = session
    else:
        policy = TrustedUrlPolicy(base_url, platform.lower(), [], logger)
        client = ArcGISRestClient(
            policy, FixedSessionPool(session), token_provider, gis, logger
        )

    groups = _enumerate_groups_strict(client, org_id, logger)
    rows = [_row_from_detail(platform, "group", group) for group in groups]
    logger.info("Mapped %d group detail record(s) to CSV rows.", len(rows))
    return rows

def gather_all_or_zip(log_file_path, csv_paths, output_dir, logger, cap_mb,
                      zip_name):
    written = [p for p in (csv_paths or []) if p]
    attachments = [log_file_path]
    if not written:
        return attachments, ""

    total_mb = _total_mb([log_file_path] + written)
    if total_mb <= cap_mb:
        attachments.extend(written)
        logger.info("Attaching %d CSV(s) individually (%.2f MB total).",
                   len(written), total_mb)
        return attachments, ""

    zip_path = os.path.join(output_dir, zip_name)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zf:
            for p in written:
                zf.write(p, arcname=os.path.basename(p))
    except Exception as exc:
        logger.warning("Could not create combined zip %s: %s", zip_path, exc)
        note = (f"\nNOTE: Output CSVs totaled {total_mb:.2f} MB, exceeding "
               f"the {cap_mb} MB limit, and a combined zip could not be "
               f"created. Files remain on disk in:\n{output_dir}\n")
        return attachments, note

    zip_mb = _total_mb([zip_path])
    if zip_mb <= cap_mb:
        attachments.append(zip_path)
        logger.info("Zipped all %d CSV(s): %.2f MB -> %.2f MB, attaching "
                   "the zip instead of individual files.", len(written),
                   total_mb, zip_mb)
        return attachments, ""

    logger.warning("Zipped CSVs still %.2f MB (> %d MB cap); NOT attaching. "
                  "Files remain on disk in: %s", zip_mb, cap_mb, output_dir)
    note = (f"\nNOTE: Output CSVs totaled {total_mb:.2f} MB ({zip_mb:.2f} MB "
           f"even zipped), exceeding the {cap_mb} MB email attachment "
           f"limit. They remain on disk here:\n"
           + "\n".join(f"  {p}" for p in written) + "\n")
    return attachments, note



def build_email_body(platform, org_name, args, item_count, group_count,
                     fs_count, csv_written, csv_path, fs_csv_written,
                     fs_csv_path, summary_csv_written, summary_csv_path,
                     timing, attach_note, edit_skip_log,
                     result_label="COMPLETE", incomplete_reasons=None):
    incomplete_reasons = list(incomplete_reasons or [])
    lines = [
        f"{EMAIL_SUBJECT_PREFIX} for platform [{platform}].",
        "",
        f"Result: {result_label}",
        f"Organization: {org_name}",
        f"Target: {args.target}",
        f"Run mode: {'TEST' if args.test else 'PRODUCTION'}",
        f"TLS mode: {args.tls_mode}",
        f"Item cap: "
        f"{(args.max_items if args.max_items > 0 else 'none (--max-items <= 0)') if args.max_items is not None else ('250 (test default)' if args.test else 'none')}",
        f"Groups included: {'yes' if (not args.no_groups and (not args.test or args.with_groups)) else 'no'}",
        f"Items/groups inventoried: {item_count + group_count}",
        f"Items inventoried: {item_count}",
        f"Groups inventoried: {group_count}",
        f"Total main-inventory rows: {item_count + group_count}",
        f"Feature services inventoried: {fs_count}",
        f"Main inventory CSV: {csv_path if csv_written else '(NOT WRITTEN)'}",
        f"Feature service CSV: {fs_csv_path if fs_csv_written else '(NOT WRITTEN)'}",
        f"Item-type summary CSV: {summary_csv_path if summary_csv_written else '(NOT WRITTEN)'}",
        "",
        timing,
    ]
    if incomplete_reasons:
        lines.extend([
            "",
            "WARNING: This run is incomplete. Absence from a report is not "
            "proof that the item/group was absent or fully inspected.",
        ])
        for reason in incomplete_reasons[:MAX_ERROR_SAMPLES]:
            lines.append("  - " + redact_token(str(reason)))
        if len(incomplete_reasons) > MAX_ERROR_SAMPLES:
            lines.append(
                f"  ... and {len(incomplete_reasons) - MAX_ERROR_SAMPLES} more "
                "(see the log)."
            )
    if attach_note:
        lines.append(attach_note)

    if edit_skip_log:
        lines.append("")
        lines.append("=" * 70)
        lines.append(
            f"WARNING: {len(edit_skip_log)} editor-tracking lookup(s) "
            "could not be fully resolved. See last_fs_edited in the "
            "feature service CSV for each affected row.")
        lines.append("=" * 70)
        max_shown = 50
        for i, skip in enumerate(edit_skip_log[:max_shown], start=1):
            safe_item_id = redact_token(str(skip.get("item_id", "")))
            safe_title = redact_token(str(skip.get("title", "")))
            safe_reason = redact_token(str(skip.get("reason", "")))
            lines.append(
                f"{i}. item_id={safe_item_id} title={safe_title!r}\n"
                f"     reason: {safe_reason}")
        if len(edit_skip_log) > max_shown:
            lines.append(
                f"... and {len(edit_skip_log) - max_shown} more (see the "
                "attached log for the full list).")

    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inventory every item (and every group) on Portal or "
                    "AGOL, writing a main inventory CSV, a feature-service "
                    "CSV, and an item-type count CSV, then email them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (first 250 items, groups skipped).\n\n"
            "Examples:\n"
            "  python ITEM_INVENTORY.py --config org1.config.toml "
            "--target agol\n"
            "  python ITEM_INVENTORY.py --config org2.config.toml "
            "--target portal --no-test\n"
            "  python ITEM_INVENTORY.py --config org1.config.toml "
            "--target agol --tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    add_test_flag(
        parser,
        help_text="Run in PRODUCTION mode (default is TEST mode). TEST tags "
                  "the subject [TEST], inventories only the first 250 items, "
                  "and skips groups unless --with-groups is given.")
    parser.add_argument("--max-items", type=int, default=None,
                        help="Cap the item inventory to at most this many items.")
    parser.add_argument("--no-groups", action="store_true",
                        help="Do NOT inventory groups (items only).")
    parser.add_argument("--with-groups", action="store_true",
                        help="Force the group inventory ON even in TEST mode.")
    parser.add_argument(
        "--workers", type=_bounded_int("workers", 1, 64),
        default=DEFAULT_WORKERS,
        help=f"Connection-pool size / concurrency (1-64; default: "
             f"{DEFAULT_WORKERS}).")
    parser.add_argument("--no-edit-tracking", dest="edit_tracking",
                        action="store_false",
                        help="Skip editor-tracking / last-edit-date "
                             "detection entirely.")
    parser.set_defaults(edit_tracking=True)
    parser.add_argument(
        "--trusted-service-origin", action="append", default=[],
        metavar="HTTPS_ORIGIN",
        help="Optional repeatable exact HTTPS origin allowed to receive the "
             "Portal token for FeatureServer/MapServer editor-tracking calls. "
             "Normally unnecessary because Portal federation metadata and "
             "AGOL domains are trusted automatically.")

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode", choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="TLS verification strategy for this script's own HTTP "
             "session: standard, os-trust-store, or custom-ca-bundle.")
    tls_group.add_argument(
        "--ca-bundle-path", default=None,
        help="PEM CA bundle path. Required when --tls-mode is "
             "custom-ca-bundle.")
    return parser.parse_args()

def _resolve_item_cap(args, logger):
    if args.max_items is not None:
        if args.max_items <= 0:
            logger.info("--max-items <= 0 -> no cap.")
            return None
        logger.info("Item cap: %d (explicit --max-items).", args.max_items)
        return args.max_items
    if args.test:
        logger.info("Item cap: %d (TEST mode default).", TEST_MODE_ITEM_LIMIT)
        return TEST_MODE_ITEM_LIMIT
    logger.info("Item cap: none (PRODUCTION mode).")
    return None


def _resolve_include_groups(args, logger):
    if args.no_groups:
        logger.info("Groups: skipped (--no-groups).")
        return False
    if args.test and not args.with_groups:
        logger.info("Groups: skipped (TEST default).")
        return False
    logger.info("Groups: included.")
    return True



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

    detected_platform = get_platform(args.portal_url)
    try:
        _validate_target_platform(args.target, detected_platform, args.portal_url)
    except ScanAbort as exc:
        print(f"Run ABORTED. {redact_token(str(exc))}", file=sys.stderr)
        sys.exit(1)
    platform = detected_platform
    org_prefix = args.org_name_safe

    ensure_output_dir(args.output_dir)
    try:
        paths = _reserve_run_paths(
            args.output_dir, org_prefix, platform, timestamp
        )
    except Exception as exc:
        print(f"Run ABORTED. Could not reserve output paths: {exc}",
              file=sys.stderr)
        sys.exit(1)
    timestamp = paths["timestamp"]
    log_file_path = paths["log"]
    csv_path = paths["main"]
    fs_csv_path = paths["feature"]
    summary_csv_path = paths["summary"]

    logger = setup_logging(log_file_path, "item_inventory")
    logger.info("=" * 70)
    logger.info("Item Inventory Scan - run started")
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "PRODUCTION")
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s", args.email_to)
    logger.info("Email from: %s", args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Worker/pool size: %d", args.workers)
    logger.info("Editor-tracking detection: %s",
                "enabled" if args.edit_tracking else "disabled")
    logger.info("=" * 70)

    subject = subject_line(EMAIL_SUBJECT_PREFIX, timestamp, platform,
                           org_name=args.org_name)
    if args.test:
        subject = "[TEST] " + subject

    def abort(reason):
        safe = redact_token(str(reason))
        logger.error("Run ABORTED: %s", safe)
        send_email(
            subject,
            f"Run ABORTED. {safe}\n\n"
            + timing_block(start_wallclock, start_perf),
            [log_file_path], args.smtp_server, args.email_to,
            args.email_from, logger,
        )
        raise SystemExit(1)

    max_items = _resolve_item_cap(args, logger)
    include_groups = _resolve_include_groups(args, logger)

    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2), retry_total=3,
            logger=logger)
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, session, logger=logger)
    except Exception as exc:
        abort(
            f"Could not establish TLS verification strategy "
            f"{args.tls_mode!r}: {exc}"
        )
    if not handshake_ok:
        abort(
            f"TLS handshake verification to {args.portal_url} failed: "
            f"{handshake_detail.get('error')}"
        )

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          verify_mode=mode, ca_bundle_path=ca_path)
    except Exception as exc:
        abort(f"Could not connect to portal: {exc}")

    try:
        authenticated_base = _portal_base_url(gis, args.portal_url)
        _validate_target_platform(
            args.target, get_platform(authenticated_base), authenticated_base
        )
    except Exception as exc:
        abort(f"Authenticated target validation failed: {exc}")

    try:
        token_provider = provider_for_gis(gis, platform)
    except Exception as exc:
        abort(f"Could not initialize the token provider: {exc}")
    logger.info("Token acquired via %s (token present: %s).",
                type(token_provider).__name__, token_provider.is_present())

    try:
        policy = TrustedUrlPolicy(
            authenticated_base, args.target,
            args.trusted_service_origin, logger,
        )
    except Exception as exc:
        abort(f"Could not establish trusted service URL policy: {exc}")

    incomplete_reasons = []
    federation = FederationInfo()
    if platform == "PORTAL":
        try:
            federation = _resolve_federation_info(gis, policy, logger)
        except Exception as exc:
            message = ("Could not process Portal federation metadata: "
                       + redact_token(str(exc)))
            logger.exception("%s", message)
            federation.errors.append(message)
        if federation.errors:
            incomplete_reasons.extend(federation.errors)
            for failure in federation.errors:
                logger.warning("Federation metadata issue: %s", failure)

    try:
        session_pool = WorkerSessionPool(
            mode, ca_path, initial_session=session, capacity=args.workers
        )
    except Exception as exc:
        abort(f"Could not initialize worker HTTP sessions: {exc}")
    client = ArcGISRestClient(
        policy, session_pool, token_provider, gis, logger
    )

    item_rows = []
    group_rows = []
    fs_rows = []
    items_by_id = {}
    edit_skip_log = []

    try:
        try:
            item_rows, items_by_id = inventory_items(
                gis, authenticated_base, client, platform, max_items, logger,
                token_provider=token_provider, temp_dir=args.output_dir)
        except Exception as exc:
            abort(f"Fatal error during item inventory: {exc}")

        if include_groups:
            try:
                group_rows = inventory_groups(
                    gis, authenticated_base, client, token_provider,
                    platform, logger)
            except Exception as exc:
                message = "Group inventory failed: " + redact_token(str(exc))
                logger.exception("%s", message)
                incomplete_reasons.append(message)

        try:
            fs_rows = build_fs_rows(
                items_by_id, platform, federation.federated_sites,
                federation.hosting_site, federation.federated_guids)
        except Exception as exc:
            abort(f"Could not build the Feature Service inventory: {exc}")

        if args.edit_tracking and fs_rows:
            try:
                edit_skip_log = resolve_last_fs_edited_for_rows(
                    fs_rows, client, token_provider, args.workers, logger)
            except Exception as exc:
                message = ("Editor-tracking detection failed unexpectedly: "
                           + redact_token(str(exc)))
                logger.exception("%s", message)
                incomplete_reasons.append(message)
        else:
            for row in fs_rows:
                if row["record_kind"] == "Registered":
                    row["last_fs_edited"] = REGISTERED_SKIP_MSG
    finally:
        session_pool.close()

    if edit_skip_log:
        incomplete_reasons.append(
            f"{len(edit_skip_log)} editor-tracking lookup(s) were incomplete"
        )

    all_item_rows = item_rows + group_rows
    for row in all_item_rows:
        row.pop("_record_kind", None)

    try:
        written_csv = write_dict_csv(
            all_item_rows, csv_path, fieldnames=OUTPUT_FIELDS, logger=logger,
            label="inventory row", force_values={"platform": platform})
        for row in fs_rows:
            row.pop("_item_id", None)
        written_fs_csv = write_dict_csv(
            fs_rows, fs_csv_path, fieldnames=FS_OUTPUT_FIELDS, logger=logger,
            label="feature service row")
        summary_rows = build_summary_rows(all_item_rows, fs_rows)
        written_summary_csv = write_dict_csv(
            summary_rows, summary_csv_path, fieldnames=SUMMARY_OUTPUT_FIELDS,
            logger=logger, label="item-type summary row")
    except Exception as exc:
        abort(f"Could not write inventory output: {exc}")

    csv_written = written_csv is not None
    fs_csv_written = written_fs_csv is not None
    summary_csv_written = written_summary_csv is not None
    if not csv_written:
        incomplete_reasons.append("Main inventory CSV was not written")
    if not fs_csv_written:
        incomplete_reasons.append("Feature Service inventory CSV was not written")
    if not summary_csv_written:
        incomplete_reasons.append("Item-type summary CSV was not written")

    result_label = "INCOMPLETE" if incomplete_reasons else "COMPLETE"

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result: %s", result_label)
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "PRODUCTION")
    logger.info("Items: %d", len(item_rows))
    logger.info("Groups: %d", len(group_rows))
    logger.info("Total main-inventory rows: %d", len(all_item_rows))
    logger.info("Feature services: %d", len(fs_rows))
    logger.info("Editor-tracking skip(s): %d", len(edit_skip_log))
    logger.info("Incomplete reason(s): %d", len(incomplete_reasons))
    logger.info("Main CSV: %s", csv_path if csv_written else "(NOT WRITTEN)")
    logger.info("Feature service CSV: %s",
                fs_csv_path if fs_csv_written else "(NOT WRITTEN)")
    logger.info("Summary CSV: %s",
                summary_csv_path if summary_csv_written else "(NOT WRITTEN)")
    logger.info("=" * 70)

    if edit_skip_log:
        logger.warning("Full editor-tracking skip list (%d):", len(edit_skip_log))
        for skip in edit_skip_log:
            logger.warning("  item_id=%s title=%r reason=%s",
                           skip["item_id"], skip["title"],
                           redact_token(skip["reason"]))

    timing = timing_block(start_wallclock, start_perf)
    csv_outputs = [p for p in
                   [written_csv, written_fs_csv, written_summary_csv] if p]
    zip_name = (
        f"{org_prefix}_{platform}_ITEM_INVENTORY_{timestamp}_attachments.zip"
    )
    attachments, note = gather_all_or_zip(
        log_file_path, csv_outputs, args.output_dir, logger,
        cap_mb=(max_email_mb if max_email_mb is not None else 20),
        zip_name=zip_name)

    body = build_email_body(
        platform, args.org_name, args,
        item_count=len(item_rows), group_count=len(group_rows),
        fs_count=len(fs_rows), csv_written=csv_written, csv_path=csv_path,
        fs_csv_written=fs_csv_written, fs_csv_path=fs_csv_path,
        summary_csv_written=summary_csv_written,
        summary_csv_path=summary_csv_path, timing=timing, attach_note=note,
        edit_skip_log=edit_skip_log, result_label=result_label,
        incomplete_reasons=incomplete_reasons)

    email_subject = subject
    if result_label == "INCOMPLETE":
        email_subject += " (INCOMPLETE)"
    logger.info("Emailing results to configured recipient(s) ...")
    send_email(email_subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    if result_label == "INCOMPLETE":
        logger.warning(
            "Run completed INCOMPLETE with %d reason(s); returning exit "
            "status 0 because the inventory workflow ran to completion. "
            "The report remains marked INCOMPLETE because some details "
            "could not be fully inventoried.",
            len(incomplete_reasons),
        )
    else:
        logger.info("Success")
    sys.exit(0)

if __name__ == "__main__":
    main()
