#!/usr/bin/env python3
r"""
CORRELATE_SQLITE_REPLICAS.py 
 
Scans a Portal/AGOL org for "SQLite Geodatabase" items and reports, for each,
the feature-service replica it came from and whether that replica still exists
(MATCHED / ORPHANED / UNKNOWN_SERVICE / NO_SERVICE / NO_GUID). Read-only.

Examples:
    python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal
    python CORRELATE_SQLITE_REPLICAS.py --config org2.config.toml --target agol
    python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal --workers 48
    python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal --tls-mode os-trust-store
"""

import argparse
import csv
import datetime
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit

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
    epoch_to_date,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_target_arg,
    add_config_arg,
    subject_line,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
    DEFAULT_CAP,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)

EMAIL_SUBJECT_PREFIX = "SQLite GDB -> Replica Correlation"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

DEFAULT_WORKERS = 32
REQUEST_TIMEOUT = 60
HTTP_TIMEOUT = 60

DATE_FMT = "%Y/%m/%d"

# The item type we care about.
_SQLITE_ITEM_TYPE = "SQLite Geodatabase"

# Replica-record field aliases (detail endpoint shapes).
CREATED_KEYS = ["creationDate", "createdDate", "created", "creation_date", "dateCreated"]
LASTSYNC_KEYS = ["lastSyncDate", "lastSyncedDate", "lastSync", "last_sync_date",
                 "lastSyncTime", "lastSyncedTime"]
OWNER_KEYS = ["replicaOwner", "owner", "userName", "ownerName", "user", "creator"]
# On the per-replica DETAIL endpoint, replicaID/replicaGuid carry the GUID.
GUID_KEYS = ["replicaID", "replicaId", "replicaGuid", "replicaGUID",
             "guid", "GlobalID", "globalId", "globalid"]
NAME_KEYS = ["replicaName", "name"]

# A fully-normalized replica GUID is exactly 32 hex characters (after braces,
# dashes, and case have been stripped by normalize_guid()).
_NORMALIZED_GUID_RE = re.compile(r"^[0-9a-f]{32}$")

# typeKeyword that points at the parent service, e.g.
# "ReferenceURL#https://.../FeatureServer".
_REFERENCE_URL_PREFIX = "referenceurl#"

_TLS_MODE_MAP = {
    "standard-ca": VerifyMode.STANDARD_CA,
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

# ---------------------------------------------------------------------------
# Hardened discovery/request controls (mirrors ITEM_INVENTORY.py). These are
# local so changing this script does not alter helper behavior for other jobs.
# ---------------------------------------------------------------------------
SEARCH_PAGE_SIZE = 100
MAX_JSON_BYTES = 64 * 1024 * 1024
MB = 1024 * 1024
AGOL_TRUSTED_SUFFIX = ".arcgis.com"
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
_ORG_ID_RE = re.compile(r"^[0-9A-Za-z_-]+$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_SERVICE_ENDPOINT_RE = re.compile(
    r"(?i)^.+/(?:FeatureServer|MapServer)(?:/\d+)?(?:/replicas(?:/\d+)?)?/?$"
)
_PORTAL_PROXY_RE = re.compile(
    r"(?i)/sharing/servers/[0-9a-f]{32}/rest/services/"
)


# ==========================================================================
# Phase timing
# ==========================================================================
class PhaseTimer:
    def __init__(self, logger):
        self.logger = logger
        self.records = []

    def phase(self, name):
        return _PhaseCtx(self, name)

    def record(self, name, seconds):
        self.records.append((name, seconds))
        self.logger.info("PHASE DONE: %-45s %8.1fs", name, seconds)

    def log_summary(self):
        self.logger.info("=" * 70)
        self.logger.info("PHASE TIMING SUMMARY (largest first)")
        total = sum(s for _, s in self.records) or 1.0
        for name, secs in sorted(self.records, key=lambda r: r[1], reverse=True):
            self.logger.info("  %-45s %8.1fs  (%4.1f%%)",
                             name, secs, 100.0 * secs / total)
        self.logger.info("  %-45s %8.1fs", "TOTAL (sum of phases)", total)
        self.logger.info("=" * 70)


class _PhaseCtx:
    def __init__(self, timer, name):
        self.timer = timer
        self.name = name
        self.t0 = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        self.timer.logger.info("PHASE START: %s", self.name)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.timer.record(self.name, time.perf_counter() - self.t0)
        return False


# ==========================================================================
# Hardened REST error / scan-abort types
# ==========================================================================
class ScanAbort(RuntimeError):
    """Controlled failure that invalidates scan completeness."""


class RestCallError(RuntimeError):
    """Sanitized direct-REST failure with classification metadata."""

    def __init__(self, message, *, tls_error=False, auth_error=False):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)


# ==========================================================================
# URL validation helpers (mirrors ITEM_INVENTORY.py)
# ==========================================================================
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


def _portal_base_url(gis, configured_url=None):
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    if not raw:
        raw = configured_url
    if not raw:
        raise ScanAbort("The authenticated GIS object exposed no Portal URL.")
    return _normalize_portal_base(str(raw))


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


# ==========================================================================
# Federation info (so Portal service origins are trusted for /replicas calls)
# ==========================================================================
@dataclass
class FederationInfo:
    federated_sites: set = field(default_factory=set)
    federated_guids: set = field(default_factory=set)
    hosting_site: Optional[str] = None
    records: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ==========================================================================
# Trusted URL policy (mirrors ITEM_INVENTORY.py)
# ==========================================================================
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
                "sublayer, or /replicas endpoint."
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


# ==========================================================================
# Worker session pool (mirrors ITEM_INVENTORY.py)
# ==========================================================================
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


# ==========================================================================
# Hardened REST client (mirrors ITEM_INVENTORY.py)
# ==========================================================================
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
        if not isinstance(data, (dict, list)):
            raise RestCallError("endpoint returned a non-object JSON response")

        if isinstance(data, dict):
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
        if isinstance(data, dict) and \
                str(data.get("status", "")).strip().lower() == "error":
            messages = data.get("messages")
            rendered = (
                f"endpoint returned status=error: "
                f"{messages or 'no message supplied'}"
            )
            raise RestCallError(
                rendered, auth_error=bool(_AUTH_ERROR_RE.search(rendered))
            )
        return data


# ==========================================================================
# Federation resolution (so Portal federated service origins are trusted)
# ==========================================================================
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
        server_id = srv.get("id")
        if server_id:
            info.federated_guids.add(str(server_id).lower())
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
                rejected_bases.append(f"{key}={redact_token(str(exc))}")
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
        info.records.append({"name": name, "server_id": server_id})

    logger.info("Resolved %d federation server record(s), %d server GUID(s).",
                len(info.records), len(info.federated_guids))
    logger.info("Trusted service destination(s): %s", policy.render_trusted())
    return info


# ==========================================================================
# Strict search paging helpers (mirrors ITEM_INVENTORY.py)
# ==========================================================================
def _search_results(data, label):
    if "results" in data:
        value = data.get("results")
    elif "items" in data:
        value = data.get("items")
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


# ==========================================================================
# Disk-backed item store (mirrors ITEM_INVENTORY.py)
# ==========================================================================
class ItemStore:
    """Disk-backed item store preserving a sorted-id selection."""

    def __init__(self, logger, directory=None):
        fd, path = tempfile.mkstemp(
            prefix=".sqlite_correlate_", suffix=".sqlite", dir=directory
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

    def selected(self):
        self._connection.commit()
        self._pending = 0
        cursor = self._connection.execute(
            "SELECT item_id, payload FROM items ORDER BY item_id"
        )
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


# ==========================================================================
# Discover SQLite Geodatabase items (replaces the old CSV reader)
# ==========================================================================
def discover_sqlite_items(client, gis, logger, phases, temp_dir=None):
    """
    Enumerate every item in the org via strict date-range bucketing (the same
    logic ITEM_INVENTORY.py uses), then keep only rows whose type is
    'SQLite Geodatabase'. Returns a list of dicts shaped like the rows the old
    CSV reader produced, so the rest of the pipeline is unchanged.
    """
    org_id = _safe_org_id(get_org_id(gis, logger=logger))
    logger.info("Scoping item search to organization id: %s", org_id)

    items = []
    store = ItemStore(logger, directory=temp_dir)
    try:
        with phases.phase("STEP 0b: enumerate org items (strict bucketing)"):
            logger.info("Enumerating all organization items via strict "
                        "date-range bucketing ...")
            _enumerate_items_strict(client, org_id, store, logger)
            total = len(store)
            logger.info("Strict search found %d unique item(s).", total)

        with phases.phase("STEP 0c: filter to SQLite Geodatabase items"):
            scanned = 0
            for item_id, item in store.selected():
                scanned += 1
                item_type = str(item.get("type") or "")
                if item_type != _SQLITE_ITEM_TYPE:
                    continue
                items.append({
                    "platform": "",
                    "item_id": item_id,
                    "owner": item.get("owner") or "",
                    "date_created": epoch_to_date(item.get("created")),
                    "date_modified": epoch_to_date(item.get("modified")),
                    "title": item.get("title") or item.get("name") or "",
                    "item_type": item_type or _SQLITE_ITEM_TYPE,
                    "access": item.get("access") or "",
                })
            logger.info(
                "Identified %d SQLite Geodatabase item(s) out of %d scanned "
                "item(s).", len(items), scanned)
    finally:
        store.close()
    return items


# ==========================================================================
# GUID + parent-service extraction
# ==========================================================================
def first_non_empty(d, keys):
    if not isinstance(d, dict):
        return ""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return ""


def normalize_guid(raw):
    """Lowercase, strip braces/dashes/whitespace so two spellings of the same
    GUID compare equal. Returns '' if there's nothing usable."""
    if raw in (None, "", []):
        return ""
    s = str(raw).strip().strip("{}").replace("-", "").lower()
    return s if s else ""


def is_guid(normalized):
    """True if a NORMALIZED value is a real 32-hex GUID."""
    return bool(_NORMALIZED_GUID_RE.match(normalized or ""))


def parent_service_from_item(item_json):
    """
    Resolve the parent FeatureServer URL for a SQLite replica item.

    Prefers the "ReferenceURL#<url>" typeKeyword (authoritative pointer back to
    the service the replica came from); falls back to item.properties.url. The
    returned URL is trimmed to end at '/FeatureServer' (no trailing slash).
    Returns '' if neither source yields a FeatureServer URL.
    """
    def _clean(u):
        u = (u or "").strip().rstrip("/")
        return u if u.lower().endswith("/featureserver") else ""

    for kw in (item_json.get("typeKeywords") or []):
        if isinstance(kw, str) and kw.lower().startswith(_REFERENCE_URL_PREFIX):
            cand = _clean(kw[kw.index("#") + 1:])
            if cand:
                return cand

    props = item_json.get("properties") or {}
    return _clean(props.get("url"))


def replica_guid_from_item(item_json):
    """
    The replica GUID that links this SQLite item to a live replica:
    item.properties.replicaID. Returns the NORMALIZED guid, or '' if absent /
    not a real GUID.
    """
    props = item_json.get("properties") or {}
    guid = normalize_guid(props.get("replicaID"))
    return guid if is_guid(guid) else ""


def service_name_from_url(service_url):
    if not service_url:
        return ""
    u = service_url.rstrip("/")
    if u.lower().endswith("/featureserver"):
        u = u[:-len("/FeatureServer")]
    parts = u.split("/services/", 1)
    if len(parts) > 1:
        return parts[1]
    return urlparse(u).path.rstrip("/").split("/")[-1] if u else ""


def server_site_from_url(service_url):
    """Best-effort 'host/context' label from a service URL, for the report."""
    if not service_url:
        return ""
    parsed = urlparse(service_url)
    host = parsed.netloc or ""
    segs = [s for s in (parsed.path or "").split("/") if s]
    return f"{host}/{segs[0]}" if segs else host


# ==========================================================================
# Per-item resolution: item JSON -> parent service + replica GUID
# ==========================================================================
def fetch_item_json(client, item_id):
    """GET /sharing/rest/content/items/<id>?f=json through the hardened client.
    Returns dict or {'__error__': ...}."""
    safe_id = str(item_id or "").strip()
    if not safe_id or any(ch in safe_id for ch in "/\\?#"):
        return {"__error__": f"unsafe item id: {item_id!r}"}
    try:
        return client.portal_json(f"/sharing/rest/content/items/{safe_id}")
    except RestCallError as exc:
        return {"__error__": str(exc)}


def resolve_items_detail(client, sqlite_items, workers, logger, phases):
    """
    For each SQLite item, fetch its item JSON and extract the parent service
    URL and the replica GUID. Returns a dict keyed by item_id:
        {item_id: {"parent_service": str, "replica_guid": str,
                   "item_error": str}}
    """
    detail = {}
    done = 0
    lock = Lock()
    ids = [it["item_id"] for it in sqlite_items if it.get("item_id")]

    with phases.phase("STEP 1: read item JSON (parent service + replica GUID)"):
        logger.info("Fetching item JSON for %d item(s) with %d worker(s) ...",
                    len(ids), workers)

        def work(item_id):
            data = fetch_item_json(client, item_id)
            if not isinstance(data, dict) or "__error__" in data or "error" in data:
                err = (data or {}).get("__error__") or \
                      (data.get("error") if isinstance(data, dict) else None) or \
                      "could not read item JSON"
                return item_id, {"parent_service": "", "replica_guid": "",
                                 "item_error": redact_token(str(err))}
            return item_id, {
                "parent_service": parent_service_from_item(data),
                "replica_guid": replica_guid_from_item(data),
                "item_error": "",
            }

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed(pool.submit(work, i) for i in ids):
                item_id, info = fut.result()
                detail[item_id] = info
                with lock:
                    done += 1
                    if done % 100 == 0 or done == len(ids):
                        logger.info("Item JSON: %d / %d read.", done, len(ids))

    with_guid = sum(1 for v in detail.values() if v["replica_guid"])
    with_svc = sum(1 for v in detail.values() if v["parent_service"])
    logger.info("Item JSON resolved: %d item(s); %d with a replica GUID, "
                "%d with a parent service URL.", len(detail), with_guid, with_svc)
    return detail


# ==========================================================================
# Per-service replica lookup (only the services the items point at)
# ==========================================================================
def list_replica_ids(client, service_url):
    """
    Return (entries, error) from <service>/replicas.

    entries is the raw replica list (each typically {'replicaName':...,
    'replicaID': <int>}); error is a non-empty string ONLY when the service
    itself could not be read (unreachable/404/auth/etc.). A reachable service
    with zero replicas returns ([], "") -- distinct from an unreachable one."""
    url = service_url.rstrip("/") + "/replicas"
    try:
        data = client.service_json(url)
    except RestCallError as exc:
        return [], str(exc)
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        reps = data.get("replicas")
        if isinstance(reps, list):
            return reps, ""
        # A dict with neither error nor a replicas list: treat as empty but
        # reachable (some builds return {} for a service with no replicas).
        return [], ""
    return [], "unexpected response shape from /replicas"


def fetch_replica_detail(client, service_url, replica_id):
    """GET <service>/replicas/<id>; returns the detail dict (whose replicaID is
    the GUID form) or {}."""
    rid = str(replica_id)
    if not re.fullmatch(r"[0-9A-Za-z._-]+", rid):
        return {}
    url = f"{service_url.rstrip('/')}/replicas/{rid}"
    try:
        data = client.service_json(url)
    except RestCallError:
        return {}
    return data if isinstance(data, dict) else {}


def build_service_replica_index(client, service_url, logger):
    """
    For ONE service, return (index, status) where:
      index  = {normalized_guid: replica-info dict} for every live replica,
               resolving each replica's GUID via its detail endpoint.
      status = {"reachable": bool, "error": str, "live_count": int}
    """
    index = {}
    entries, err = list_replica_ids(client, service_url)
    if err:
        return index, {"reachable": False, "error": err, "live_count": 0}

    for rep in entries:
        if not isinstance(rep, dict):
            continue
        rid = rep.get("replicaID", rep.get("replicaId", rep.get("id")))
        if rid is None:
            continue
        # The listing's replicaID is usually an int; the GUID comes from detail.
        guid = normalize_guid(rid)
        detail = {}
        if not is_guid(guid):
            detail = fetch_replica_detail(client, service_url, rid)
            guid = normalize_guid(first_non_empty(detail, GUID_KEYS))
        if not is_guid(guid):
            continue
        src = detail or rep
        index[guid] = {
            "service_url": service_url,
            "service_name": service_name_from_url(service_url),
            "server_site": server_site_from_url(service_url),
            "replica_id": str(rid),
            "replica_guid": guid,
            "replica_name": first_non_empty(src, NAME_KEYS) or "",
            "replica_owner": str(first_non_empty(src, OWNER_KEYS) or ""),
            "replica_created": epoch_to_date(
                first_non_empty(src, CREATED_KEYS), fmt=DATE_FMT, logger=logger),
            "replica_last_sync": epoch_to_date(
                first_non_empty(src, LASTSYNC_KEYS), fmt=DATE_FMT, logger=logger),
        }
    return index, {"reachable": True, "error": "", "live_count": len(index)}


def build_replica_indexes(client, detail_by_item, workers, logger, phases):
    """
    Build per-service replica lookups for the DISTINCT set of parent services
    the items point at -- not the whole deployment.

    Returns (indexes, statuses).
    """
    distinct_services = sorted({
        v["parent_service"] for v in detail_by_item.values()
        if v.get("parent_service")
    })
    indexes = {}
    statuses = {}
    done = 0
    lock = Lock()

    with phases.phase("STEP 2: list replicas on referenced services"):
        logger.info("Resolving replicas for %d distinct parent service(s) "
                    "with %d worker(s) ...", len(distinct_services), workers)

        def work(su):
            idx, status = build_service_replica_index(client, su, logger)
            return su, idx, status

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed(pool.submit(work, su) for su in distinct_services):
                su, idx, status = fut.result()
                indexes[su] = idx
                statuses[su] = status
                with lock:
                    done += 1
                    if done % 25 == 0 or done == len(distinct_services):
                        logger.info("Service replicas: %d / %d service(s).",
                                    done, len(distinct_services))

    total_live = sum(len(idx) for idx in indexes.values())
    unreachable = [su for su, s in statuses.items() if not s["reachable"]]
    logger.info("Live replicas found across %d referenced service(s): %d "
                "(with a usable GUID).", len(indexes), total_live)
    if unreachable:
        logger.warning(
            "%d of %d referenced service(s) could NOT be read; items on these "
            "are reported as UNKNOWN_SERVICE (NOT confirmed orphans). First few: %s",
            len(unreachable), len(distinct_services),
            ", ".join(unreachable[:5]) + ("..." if len(unreachable) > 5 else ""))
    return indexes, statuses


# ==========================================================================
# Correlation
# ==========================================================================
OUTPUT_FIELDS = [
    "sqlite_item_id", "sqlite_title", "sqlite_owner",
    "sqlite_date_created", "sqlite_date_modified", "sqlite_access",
    "parent_service_url", "item_replica_guid", "match_status",
    "service_reachable", "service_live_replica_count", "detail",
    "server_site", "service_name", "service_url",
    "replica_id", "replica_name", "replica_owner",
    "replica_created", "replica_last_sync",
]


def correlate(sqlite_items, detail_by_item, indexes, statuses, logger):
    """
    Join each item's replica GUID against the replica index of its OWN parent
    service.
    """
    rows = []
    matched = orphaned = unknown_service = no_guid = no_service = read_err = 0

    for it in sqlite_items:
        iid = it.get("item_id", "")
        info = detail_by_item.get(iid, {})
        guid = info.get("replica_guid", "")
        service = info.get("parent_service", "")
        item_error = info.get("item_error", "")
        status = statuses.get(service, {}) if service else {}
        reachable = status.get("reachable")
        live_count = status.get("live_count")

        base = {
            "sqlite_item_id": iid,
            "sqlite_title": it.get("title", ""),
            "sqlite_owner": it.get("owner", ""),
            "sqlite_date_created": it.get("date_created", ""),
            "sqlite_date_modified": it.get("date_modified", ""),
            "sqlite_access": it.get("access", ""),
            "parent_service_url": service,
            "item_replica_guid": guid,
            "match_status": "",
            "service_reachable": "" if reachable is None else ("yes" if reachable else "no"),
            "service_live_replica_count": "" if live_count is None else live_count,
            "detail": "",
            "server_site": "", "service_name": "", "service_url": "",
            "replica_id": "", "replica_name": "", "replica_owner": "",
            "replica_created": "", "replica_last_sync": "",
        }

        if item_error:
            read_err += 1

        if not guid:
            base["match_status"] = "NO_GUID"
            base["detail"] = (
                "Item JSON had no usable properties.replicaID"
                + (f" (item read error: {item_error})" if item_error
                   else "; this item may not be a sync-download replica.")
            )
            no_guid += 1
        elif not service:
            base["match_status"] = "NO_SERVICE"
            base["detail"] = (
                "Item has a replica GUID but no parent FeatureServer URL "
                "(no ReferenceURL# typeKeyword and no properties.url); cannot "
                "determine which service to check."
            )
            no_service += 1
        else:
            base["service_name"] = service_name_from_url(service)
            base["service_url"] = service
            if reachable is False:
                base["match_status"] = "UNKNOWN_SERVICE"
                base["detail"] = (
                    f"Parent service could not be read ({status.get('error', 'unknown error')}); "
                    f"cannot confirm whether replica {guid} still exists. "
                    f"NOT a confirmed orphan -- re-check the service before any cleanup."
                )
                unknown_service += 1
            else:
                hit = (indexes.get(service) or {}).get(guid)
                if hit:
                    base["match_status"] = "MATCHED"
                    base["server_site"] = hit["server_site"]
                    base["service_name"] = hit["service_name"]
                    base["service_url"] = hit["service_url"]
                    base["replica_id"] = hit["replica_id"]
                    base["replica_name"] = hit["replica_name"]
                    base["replica_owner"] = hit["replica_owner"]
                    base["replica_created"] = hit["replica_created"]
                    base["replica_last_sync"] = hit["replica_last_sync"]
                    base["detail"] = (
                        f"Live replica {guid} present on the parent service "
                        f"(replica id {hit['replica_id']})."
                    )
                    matched += 1
                else:
                    base["match_status"] = "ORPHANED"
                    base["detail"] = (
                        f"Parent service is reachable and has "
                        f"{live_count} live replica(s), but none with GUID "
                        f"{guid}. The replica appears to have been deleted; "
                        f"this SQLite item is dangling (safe-to-clean "
                        f"candidate -- verify first)."
                    )
                    orphaned += 1

        rows.append(base)

    logger.info("Correlation complete: %d MATCHED, %d ORPHANED, "
                "%d UNKNOWN_SERVICE, %d NO_SERVICE, %d NO_GUID "
                "(of %d SQLite item(s); %d item JSON read error(s)).",
                matched, orphaned, unknown_service, no_service, no_guid,
                len(sqlite_items), read_err)
    return rows, matched, orphaned, unknown_service, no_service, no_guid


def write_csv(rows, path, logger):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d correlation row(s) to %s", len(rows), path)


# ==========================================================================
# CLI
# ==========================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan a Portal/AGOL org for SQLite Geodatabase items and "
                    "correlate each to the live feature-service replica it came "
                    "from, flagging orphans whose replica no longer exists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml "
            "--target portal\n"
            "  python CORRELATE_SQLITE_REPLICAS.py --config org2.config.toml "
            "--target agol\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Concurrent worker threads (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--trusted-service-origin", action="append", default=[],
        metavar="HTTPS_ORIGIN",
        help="Optional repeatable exact HTTPS origin allowed to receive the "
             "Portal token for FeatureServer /replicas calls. Normally "
             "unnecessary because Portal federation metadata and AGOL domains "
             "are trusted automatically.",
    )
    parser.add_argument(
        "--tls-mode", choices=sorted(set(_TLS_MODE_MAP.keys())),
        default="standard-ca",
        help="TLS verification strategy: 'standard-ca' (default), "
             "'os-trust-store', or 'custom-ca-bundle' (requires "
             "--ca-bundle-path).",
    )
    parser.add_argument(
        "--ca-bundle-path", default=None,
        help="Path to a PEM CA bundle. Required when --tls-mode is "
             "custom-ca-bundle.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(args.target, args.config)

    tls_mode = _TLS_MODE_MAP[args.tls_mode]
    if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE and not args.ca_bundle_path:
        print("--tls-mode custom-ca-bundle requires --ca-bundle-path.",
              file=sys.stderr)
        sys.exit(2)

    platform = get_platform(args.portal_url)
    org_prefix = args.org_name_safe
    csv_base_name = f"{org_prefix}_{platform}_SQLITE_REPLICA_CORRELATION"

    ensure_output_dir(args.output_dir)

    log_name = f"{platform.lower()}_sqlite_replica_correlation"
    log_file_path = os.path.join(
        args.output_dir, f"{org_prefix}_{log_name}_{timestamp}.log")
    csv_path = os.path.join(args.output_dir, f"{csv_base_name}_{timestamp}.csv")

    logger = setup_logging(log_file_path, log_name)
    logger.info("=" * 70)
    logger.info("SQLite GDB -> Replica Correlation - run started")
    logger.info("=" * 70)
    logger.info("Organization       : %s", args.org_name)
    logger.info("Platform           : %s", platform)
    logger.info("Target             : %s", args.target)
    logger.info("Config path        : %s", args.config)
    logger.info("Portal URL         : %s", args.portal_url)
    logger.info("Username           : %s", args.username)
    logger.info("Keyring service    : %s", args.keyring_service)
    logger.info("Output directory   : %s", args.output_dir)
    logger.info("Log file           : %s", log_file_path)
    logger.info("Output CSV         : %s", csv_path)
    logger.info("SMTP server        : %s", args.smtp_server)
    logger.info("Email to           : %s", args.email_to)
    logger.info("TLS mode           : %s", args.tls_mode)
    logger.info("Worker threads     : %d", args.workers)
    logger.info("=" * 70)

    phases = PhaseTimer(logger)
    subject = subject_line(EMAIL_SUBJECT_PREFIX, timestamp, platform,
                           org_name=args.org_name)

    def abort(reason):
        safe = redact_token(str(reason))
        logger.error("Run ABORTED: %s", safe)
        send_email(subject,
                   f"Run ABORTED. {safe}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    # --- STEP 0: connect ---
    try:
        with phases.phase("STEP 0: connect"):
            ca_path = (args.ca_bundle_path
                       if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE else None)
            gis = connect_gis(args.portal_url, args.username,
                              args.keyring_service, logger,
                              verify_mode=tls_mode, ca_bundle_path=ca_path)
    except Exception as exc:
        abort(f"Could not connect: {exc}")

    try:
        token_provider = provider_for_gis(gis, platform)
    except Exception as exc:
        abort(f"Could not initialize the token provider: {exc}")
    logger.info("Token acquired via %s (token present: %s).",
                token_provider.__class__.__name__, token_provider.is_present())

    # Build the primary (main-thread) secure session and verify TLS.
    try:
        session = build_secure_session(
            tls_mode, ca_bundle_path=ca_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2), retry_total=3,
            logger=logger)
    except Exception as exc:
        abort(f"Could not establish TLS verification strategy "
              f"{args.tls_mode!r}: {exc}")

    try:
        authenticated_base = _portal_base_url(gis, args.portal_url)
    except Exception as exc:
        abort(f"Authenticated portal base resolution failed: {exc}")

    ok, handshake_detail = verify_tls_handshake(
        authenticated_base, session, logger=logger)
    if not ok:
        abort(f"TLS handshake/verification to {authenticated_base} failed: "
              f"{handshake_detail.get('error') if isinstance(handshake_detail, dict) else handshake_detail}")

    # --- Trusted URL policy + federation (so service origins are trusted) ---
    try:
        policy = TrustedUrlPolicy(
            authenticated_base, args.target,
            args.trusted_service_origin, logger)
    except Exception as exc:
        abort(f"Could not establish trusted service URL policy: {exc}")

    if platform == "PORTAL":
        try:
            fed = _resolve_federation_info(gis, policy, logger)
            for failure in fed.errors:
                logger.warning("Federation metadata issue: %s", failure)
        except Exception as exc:
            logger.warning("Could not process Portal federation metadata: %s",
                           redact_token(str(exc)))

    rows = []
    csv_written = False
    run_error = None
    sqlite_items = []
    matched = orphaned = unknown_service = no_service = no_guid = 0

    try:
        session_pool = WorkerSessionPool(
            tls_mode, ca_path, initial_session=session, capacity=args.workers)
    except Exception as exc:
        abort(f"Could not initialize worker HTTP sessions: {exc}")

    client = ArcGISRestClient(policy, session_pool, token_provider, gis, logger)

    try:
        # --- STEP 0b/0c: discover SQLite Geodatabase items (no CSV input) ---
        sqlite_items = discover_sqlite_items(
            client, gis, logger, phases, temp_dir=args.output_dir)

        if not sqlite_items:
            logger.warning("No SQLite Geodatabase items found in the org; "
                           "nothing to correlate.")

        # --- STEP 1: read each item's JSON for parent svc + replica GUID ---
        detail_by_item = resolve_items_detail(
            client, sqlite_items, args.workers, logger, phases)

        # --- STEP 2: list replicas ONLY on the referenced services ---
        indexes, statuses = build_replica_indexes(
            client, detail_by_item, args.workers, logger, phases)

        # --- STEP 3: correlate ---
        with phases.phase("STEP 3: correlate SQLite items"):
            (rows, matched, orphaned, unknown_service, no_service,
             no_guid) = correlate(
                sqlite_items, detail_by_item, indexes, statuses, logger)
    except Exception as exc:
        run_error = redact_token(str(exc))
        logger.exception("Correlation FAILED: %s", exc)
    finally:
        session_pool.close()

    orphans_csv_path = os.path.join(
        args.output_dir, f"{csv_base_name}_ORPHANS_{timestamp}.csv")
    orphans_written = False
    orphan_rows = [r for r in rows if r["match_status"] == "ORPHANED"]
    try:
        with phases.phase("write CSV"):
            write_csv(rows, csv_path, logger)
        csv_written = True
    except Exception as exc:
        logger.error("Failed to write CSV: %s", exc)

    # Separate orphans-only CSV for quick cleanup review (only confirmed
    # orphans -- UNKNOWN_SERVICE rows are deliberately excluded).
    if orphan_rows:
        try:
            write_csv(orphan_rows, orphans_csv_path, logger)
            orphans_written = True
        except Exception as exc:
            logger.error("Failed to write orphans CSV: %s", exc)
    else:
        logger.info("No confirmed ORPHANED items; orphans-only CSV not written.")

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("=" * 70)
    logger.info("Organization              : %s", args.org_name)
    logger.info("Platform                  : %s", platform)
    logger.info("SQLite items discovered   : %d", len(sqlite_items))
    logger.info("  MATCHED (live replica)  : %d", matched)
    logger.info("  ORPHANED (no live repl) : %d", orphaned)
    logger.info("  UNKNOWN_SERVICE         : %d", unknown_service)
    logger.info("  NO_SERVICE (no parent)  : %d", no_service)
    logger.info("  NO_GUID (no replicaID)  : %d", no_guid)
    logger.info("Status                    : %s",
                "SUCCESS" if run_error is None else f"FAILED ({run_error})")
    logger.info("Output CSV                : %s",
                csv_path if csv_written else "(NOT WRITTEN)")
    logger.info("Orphans-only CSV          : %s",
                orphans_csv_path if orphans_written else "(none)")
    logger.info("=" * 70)
    phases.log_summary()

    status_line = ("Status        : SUCCESS\n" if run_error is None
                   else f"Status        : FAILED ({run_error})\n")
    phase_lines = "\n".join(
        f"  {name:<40} {secs:8.1f}s"
        for name, secs in sorted(phases.records, key=lambda r: r[1], reverse=True)
    )
    body = (
        f"SQLite Geodatabase -> Feature Service Replica Correlation\n"
        f"Organization      : {args.org_name}\n"
        f"Platform          : {platform}\n"
        f"Target            : {args.target}\n"
        f"Run timestamp     : {timestamp}\n"
        f"Item source       : live org scan (SQLite Geodatabase items)\n"
        f"{status_line}\n"
        f"SQLite items discovered   : {len(sqlite_items)}\n"
        f"  MATCHED (live replica)  : {matched}\n"
        f"  ORPHANED (no live repl) : {orphaned}\n"
        f"  UNKNOWN_SERVICE         : {unknown_service}\n"
        f"  NO_SERVICE (no parent)  : {no_service}\n"
        f"  NO_GUID (no replicaID)  : {no_guid}\n"
        f"Output CSV                : {csv_path if csv_written else '(NOT WRITTEN)'}\n\n"
        f"MATCHED    = the parent service still has a live replica with this\n"
        f"             item's replica GUID; service/replica columns are filled.\n"
        f"ORPHANED   = the item's replica GUID was found, and its parent\n"
        f"             service was reachable, but that service has no replica\n"
        f"             with the GUID (safe-to-clean candidate -- verify first).\n"
        f"UNKNOWN_SERVICE = the parent service could not be read; NOT a\n"
        f"             confirmed orphan -- re-check before any cleanup.\n"
        f"NO_SERVICE = the item JSON had a replica GUID but no parent\n"
        f"             FeatureServer URL to check it against.\n"
        f"NO_GUID    = the item JSON had no usable properties.replicaID; the\n"
        f"             item could not be correlated (review manually).\n\n"
        f"SQLite items are discovered by scanning the organization (strict\n"
        f"orgid + created date-range bucketing) and keeping only items whose\n"
        f"type is 'SQLite Geodatabase' -- no input CSV is required.\n\n"
        f"Correlation key: item.properties.replicaID (from each item's JSON),\n"
        f"matched against the GUID of each live replica on the item's parent\n"
        f"service (resolved via <FeatureServer>/replicas/<id>). The item TITLE\n"
        f"token is NOT the replica GUID and is not used for matching.\n\n"
        f"Phase timing (largest first):\n{phase_lines}\n\n"
        f"{timing_block(start_wallclock, start_perf)}\n"
        f"See the attached log file and CSV for full detail.\n"
    )

    attachments, note = gather_attachments(
        log_file_path, [csv_path] if csv_written else [],
        args.output_dir, logger, cap_mb=max_email_mb)
    body += note

    send_email(subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    logger.info("Success" if run_error is None else "Completed with errors")
    sys.exit(0 if (csv_written and run_error is None) else 1)


if __name__ == "__main__":
    main()