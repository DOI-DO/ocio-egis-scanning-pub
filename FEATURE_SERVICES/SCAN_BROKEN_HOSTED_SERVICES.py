#!/usr/bin/env python3
"""Scan AGOL or Portal for broken hosted, federated, and registered
feature services and report only the non-functional ones via CSV and email.

The ordinary health test is unchanged: read the service root, read every
reported layer/table definition, and execute a returnCountOnly query against
each one.  Hardening is limited to catalog completeness, destination trust,
token refresh, response validation, bounded concurrency, and truthful run
status.

Common usage:
    python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal
    python SCAN_BROKEN_HOSTED_SERVICES.py --config org2.config.toml --target agol --no-test --workers 24
    python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal --no-test \\
        --sources hosted,federated
    python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \\
        --exclude-group-id "abc123def456,789ghi012jkl"
    python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \\
        --tls-mode os-trust-store

Every HTTP or HTTPS FeatureServer/MapServer item URL is checked, regardless
of whether its host matches the Portal host or appears in Portal federation.
The organization token is sent only to HTTPS Portal/federation destinations or
an exact --trusted-service-origin; every other destination (including private,
internal, external, and plain-HTTP registered services) is checked anonymously.
--anonymous-service-origin can force an otherwise trusted HTTPS origin to stay
anonymous.
"""

import argparse
import datetime
import importlib.util
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import List
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if os.path.isdir(os.path.join(_SCRIPT_DIR, "UTILITIES")):
    _PROJECT_ROOT = _SCRIPT_DIR
elif os.path.isdir(os.path.join(_PARENT_DIR, "UTILITIES")):
    _PROJECT_ROOT = _PARENT_DIR
else:
    _PROJECT_ROOT = _PARENT_DIR
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_EXPECTED_UTILITIES_DIR = os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
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
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
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
    add_exclude_group_arg,
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

EMAIL_SUBJECT_PREFIX = "Broken Service Scan"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

MAX_ITEMS = 2_000_000
TEST_MODE_ITEM_LIMIT = 250
DEFAULT_WORKERS = 12
REQUEST_TIMEOUT = 30
RETRY_TOTAL = 2
SEARCH_PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_ERROR_SAMPLES = 50
MAX_CATALOG_DIRECTORIES = 100_000
MAX_CATALOG_DEPTH = 16
AGOL_TRUSTED_SUFFIX = ".arcgis.com"

VALID_SOURCES = ("hosted", "federated", "registered")
ALL_FS_CLAUSE = 'type:"Feature Service"'

BROKEN_FIELDS = [
    "platform",
    "SOURCE",
    "title", "item_id", "service_url", "owner",
    "date_created", "date_modified",
    "Scope", "layer_name", "layer_id", "layer_url", "error",
    "Likely_Permission_Issue",
]

PERMISSION_ERROR_MARKERS = (
    "do not have permissions", "you do not have permission",
    "not authorized", "unauthorized", "access denied", "accessdenied",
    "forbidden", "permission denied", "not_authorized", "token required",
    "invalid token", "authentication token required",
)
PERMISSION_STATUS_CODES = ("401", "403", "498", "499")
INVALID_TOKEN_MARKERS = (
    "error 498", "error 499", "rest error 498", "rest error 499",
    "invalid token", "token is expired", "token expired",
)

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Enterprise Portal federation ids are opaque path-safe identifiers. Live
# deployments use both 32-hex ids and shorter mixed-case alphanumeric ids.
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_FEATURE_ENDPOINT_RE = re.compile(
    r"(?i)^.+/(?:FeatureServer|MapServer)(?:/\d+)?(?:/query)?/?$"
)
_PORTAL_PROXY_RE = re.compile(
    r"(?i)/sharing/servers/[A-Za-z0-9_-]{1,128}/rest/services/"
)

_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


class ScanAbort(RuntimeError):
    """Controlled failure that invalidates the complete inventory."""


class RestCallError(RuntimeError):
    """Sanitized direct-REST failure with classification metadata."""

    def __init__(self, message, *, tls_error=False, auth_error=False,
                 policy_error=False, transient_error=False,
                 transport_error=False, status_code=None, rest_code=None):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.policy_error = bool(policy_error)
        self.transient_error = bool(transient_error)
        self.transport_error = bool(transport_error)
        self.status_code = status_code
        self.rest_code = rest_code


@dataclass
class DiscoveryResult:
    errors: List[str] = field(default_factory=list)
    error_count: int = 0
    tls_errors: int = 0
    auth_errors: int = 0
    item_resolution_errors: int = 0
    catalog_errors: int = 0
    transport_errors: int = 0
    search_requests: int = 0
    selected_item_ids: int = 0
    excluded_items: int = 0

    def add_error(self, message, *, tls=False, auth=False, catalog=False,
                  transport=False):
        safe = redact_token(str(message))
        self.error_count += 1
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append(safe)
        if tls:
            self.tls_errors += 1
        if auth:
            self.auth_errors += 1
        if catalog:
            self.catalog_errors += 1
        if transport:
            self.transport_errors += 1


@dataclass
class ExclusionSet:
    """Excluded item ids plus normalized service URLs from those items."""

    item_ids: set = field(default_factory=set)
    service_keys: set = field(default_factory=set)

    def __contains__(self, item_id):
        return item_id in self.item_ids

    def __len__(self):
        return len(self.item_ids)


class WorkerSessionPool:
    """One pre-created requests.Session per worker under one TLS policy."""

    def __init__(self, mode, ca_bundle_path, initial_session=None, capacity=1):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._lock = Lock()
        self._assignments = {}
        self._next = 0
        self._sessions = []
        self._worker_sessions = []
        self._initial_session = initial_session
        self._initial_thread = get_ident() if initial_session is not None else None
        if initial_session is not None:
            self._sessions.append(initial_session)
        try:
            for _ in range(max(1, int(capacity))):
                session = build_secure_session(
                    mode,
                    ca_bundle_path=ca_bundle_path,
                    pool_connections=2,
                    pool_maxsize=4,
                    retry_total=RETRY_TOTAL,
                    logger=None,
                )
                self._worker_sessions.append(session)
                self._sessions.append(session)
        except Exception:
            self.close()
            raise

    def get(self):
        tid = get_ident()
        if self._initial_session is not None and tid == self._initial_thread:
            return self._initial_session
        with self._lock:
            existing = self._assignments.get(tid)
            if existing is not None:
                return existing
            session = self._worker_sessions[self._next % len(self._worker_sessions)]
            self._next += 1
            self._assignments[tid] = session
            return session

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


class TargetStore:
    """Disk-backed ordered target de-duplication matching dict insertion order."""

    def __init__(self, directory, logger):
        fd, path = tempfile.mkstemp(
            prefix=".broken_service_targets_", suffix=".sqlite", dir=directory
        )
        os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.path = path
        self.logger = logger
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE seen_items (item_id TEXT PRIMARY KEY)"
        )
        self._connection.execute(
            "CREATE TABLE targets ("
            "target_key TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL, "
            "payload TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE broken_rows ("
            "seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._target_count = 0
        self._broken_count = 0
        self._selected_item_count = 0
        self._pending = 0

    def mark_item_seen(self, item_id):
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO seen_items(item_id) VALUES (?)", (item_id,)
        )
        inserted = cursor.rowcount == 1
        if inserted:
            self._selected_item_count += 1
            self._pending += 1
            self._commit_if_needed()
        return inserted

    @property
    def selected_item_count(self):
        return self._selected_item_count

    def put_target(self, key, target, *, replace=True):
        rendered = json.dumps(
            target, ensure_ascii=False, separators=(",", ":"), default=str
        )
        row = self._connection.execute(
            "SELECT seq FROM targets WHERE target_key=?", (key,)
        ).fetchone()
        if row is not None:
            if replace:
                self._connection.execute(
                    "UPDATE targets SET payload=? WHERE target_key=?",
                    (rendered, key),
                )
                self._pending += 1
                self._commit_if_needed()
            return False
        self._connection.execute(
            "INSERT INTO targets(target_key, seq, payload) VALUES (?, ?, ?)",
            (key, self._target_count, rendered),
        )
        self._target_count += 1
        self._pending += 1
        self._commit_if_needed()
        return True

    def contains_target(self, key):
        row = self._connection.execute(
            "SELECT 1 FROM targets WHERE target_key=?", (key,)
        ).fetchone()
        return row is not None

    def __len__(self):
        return self._target_count

    def iter_targets(self, limit=None):
        self._connection.commit()
        sql = "SELECT payload FROM targets ORDER BY seq"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        cursor = self._connection.execute(sql, params)
        try:
            for (payload,) in cursor:
                target = json.loads(payload)
                if not isinstance(target, dict):
                    raise ScanAbort("Temporary target store contained malformed data.")
                yield target
        finally:
            cursor.close()

    def add_broken_rows(self, rows):
        for row in rows:
            rendered = json.dumps(
                row, ensure_ascii=False, separators=(",", ":"), default=str
            )
            self._connection.execute(
                "INSERT INTO broken_rows(seq, payload) VALUES (?, ?)",
                (self._broken_count, rendered),
            )
            self._broken_count += 1
            self._pending += 1
            self._commit_if_needed()

    @property
    def broken_count(self):
        return self._broken_count

    def iter_broken_rows(self):
        self._connection.commit()
        cursor = self._connection.execute(
            "SELECT payload FROM broken_rows ORDER BY seq"
        )
        try:
            for (payload,) in cursor:
                row = json.loads(payload)
                if not isinstance(row, dict):
                    raise ScanAbort(
                        "Temporary broken-row store contained malformed data."
                    )
                yield row
        finally:
            cursor.close()

    def broken_row_view(self):
        return BrokenRowView(self)

    def _commit_if_needed(self):
        if self._pending >= 1000:
            self._connection.commit()
            self._pending = 0

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
                    "Could not remove temporary target store %s: %s",
                    self.path, exc,
                )


class BrokenRowView:
    """Re-iterable, length-aware view accepted by write_dict_csv()."""

    def __init__(self, store):
        self._store = store

    def __len__(self):
        return self._store.broken_count

    def __iter__(self):
        return self._store.iter_broken_rows()


def _split_url(raw, allowed_schemes):
    text = str(raw or "").strip()
    if not text:
        raise RestCallError("URL is blank.", policy_error=True)
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise RestCallError(
            f"URL could not be parsed: {text!r}", policy_error=True
        ) from exc
    scheme = parts.scheme.lower()
    if scheme not in allowed_schemes:
        rendered = " or ".join(sorted(value.upper() for value in allowed_schemes))
        raise RestCallError(
            f"URL must use {rendered}: {text!r}", policy_error=True
        )
    if not parts.hostname:
        raise RestCallError(
            f"URL contains no hostname: {text!r}", policy_error=True
        )
    try:
        _ = parts.port
    except ValueError as exc:
        raise RestCallError(
            f"URL contains an invalid port: {text!r}", policy_error=True
        ) from exc
    if parts.username is not None or parts.password is not None:
        raise RestCallError(
            "URL must not contain user-info credentials.", policy_error=True
        )
    return parts


def _split_https_url(raw):
    return _split_url(raw, {"https"})


def _split_service_url(raw):
    return _split_url(raw, {"http", "https"})


def _origin_tuple_from_parts(parts):
    scheme = parts.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    return (scheme, (parts.hostname or "").lower().rstrip("."),
            parts.port or default_port)


def _validated_path(parts):
    if parts.query or parts.fragment:
        raise RestCallError(
            "Request URL must not contain a query or fragment.",
            policy_error=True,
        )
    raw = parts.path or ""
    lowered = raw.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise RestCallError(
            "Request URL contains an encoded slash or backslash.",
            policy_error=True,
        )
    decoded = unquote(raw)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError(
            "Request URL contains unsafe path characters.", policy_error=True
        )
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise RestCallError(
            "Request URL contains path traversal.", policy_error=True
        )
    return raw.rstrip("/")


def _normalize_portal_base(raw):
    parts = _split_https_url(raw)
    path = _validated_path(parts)
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def _portal_base_url(gis, configured_url=None):
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    if not raw:
        raw = configured_url
    if not raw:
        raise ScanAbort("The authenticated GIS object exposed no Portal URL.")
    return _normalize_portal_base(raw)


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


def _canonical_rest_root(raw):
    # Federation public/admin URLs can legitimately use a hostname different
    # from the Portal hostname. HTTP is retained for discovery compatibility,
    # but the organization token is never sent over HTTP.
    parts = _split_service_url(raw)
    path = _validated_path(parts)
    base_path = _normalize_federation_base_path(path)
    rest_path = (base_path + "/rest/services").replace("//", "/")
    return urlunsplit((parts.scheme.lower(), parts.netloc, rest_path, "", "")).rstrip("/")


class TrustedUrlPolicy:
    """Protect token destinations while retaining registered-service checks."""

    def __init__(self, portal_base, target, trusted_origins=None,
                 anonymous_origins=None, logger=None):
        self.portal_base = _normalize_portal_base(portal_base)
        self.target = str(target or "").lower()
        self.logger = logger
        portal_parts = _split_https_url(self.portal_base)
        self.portal_origin = _origin_tuple_from_parts(portal_parts)
        self.portal_path = (portal_parts.path or "").rstrip("/")
        self.trusted_origins = self._parse_exact_origins(
            trusted_origins, "--trusted-service-origin", allow_http=False
        )
        self.anonymous_origins = self._parse_exact_origins(
            anonymous_origins, "--anonymous-service-origin", allow_http=True
        )
        self.federation_roots = []
        self._dns_cache = {}
        self._dns_lock = Lock()

    @staticmethod
    def _parse_exact_origins(values, label, allow_http=False):
        result = set()
        for raw in values or []:
            for part in str(raw).split(","):
                text = part.strip()
                if not text:
                    continue
                pieces = (_split_service_url(text) if allow_http
                          else _split_https_url(text))
                if pieces.path not in ("", "/") or pieces.query or pieces.fragment:
                    scheme_label = "HTTP(S)" if allow_http else "HTTPS"
                    raise ScanAbort(
                        f"{label} must be an exact {scheme_label} origin without "
                        f"a path, query, or fragment: {text!r}"
                    )
                result.add(_origin_tuple_from_parts(pieces))
        return result

    def add_federation_base(self, raw):
        root = _canonical_rest_root(raw)
        parts = _split_service_url(root)
        entry = (_origin_tuple_from_parts(parts), _validated_path(parts))
        if entry not in self.federation_roots:
            self.federation_roots.append(entry)
        return root

    def portal_url(self, path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise RestCallError(
                "Portal REST path must be absolute.", policy_error=True
            )
        if "?" in path or "#" in path or "\\" in path:
            raise RestCallError(
                "Portal REST path contains unsafe characters.",
                policy_error=True,
            )
        if any(segment in (".", "..") for segment in path.split("/")):
            raise RestCallError(
                "Portal REST path contains traversal.", policy_error=True
            )
        allowed = ("/sharing/rest/", "/portaladmin/")
        if not any(path.startswith(prefix) for prefix in allowed):
            raise RestCallError(
                "Portal REST path is outside an approved API prefix.",
                policy_error=True,
            )
        return self.portal_base + path

    def validate_portal_url(self, raw):
        parts = _split_https_url(raw)
        path = _validated_path(parts)
        if _origin_tuple_from_parts(parts) != self.portal_origin:
            raise RestCallError(
                "Portal REST redirect changed origin.", policy_error=True
            )
        portal_path_lower = self.portal_path.lower()
        path_lower = path.lower()
        relative = (path[len(self.portal_path):]
                    if path_lower.startswith(portal_path_lower) else "")
        relative_lower = relative.lower()
        if not (relative_lower.startswith("/sharing/rest/")
                or relative_lower.startswith("/portaladmin/")):
            raise RestCallError(
                "Portal REST redirect left the approved API path.",
                policy_error=True,
            )
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def catalog_access(self, raw):
        parts = _split_service_url(raw)
        path = _validated_path(parts)
        origin = _origin_tuple_from_parts(parts)
        for registered_origin, root_path in self.federation_roots:
            if origin != registered_origin:
                continue
            if path.lower() == root_path.lower() or path.lower().startswith(
                    root_path.lower() + "/"):
                safe = urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))
                # Never expose the organization token on plain HTTP.
                mode = "token" if parts.scheme.lower() == "https" else "anonymous"
                return safe, mode
        raise RestCallError(
            "Catalog URL is outside the authenticated Portal federation.",
            policy_error=True,
        )

    def validate_catalog_redirect(self, raw, expected_mode):
        safe, mode = self.catalog_access(raw)
        if expected_mode == "anonymous":
            return safe
        if mode != expected_mode:
            raise RestCallError(
                "Catalog redirect changed the request authentication policy.",
                policy_error=True,
            )
        return safe

    def validate_catalog_url(self, raw):
        """Backward-compatible URL-only catalog validator."""
        return self.catalog_access(raw)[0]

    def _matches_federation(self, origin, path):
        for registered_origin, root_path in self.federation_roots:
            if origin != registered_origin:
                continue
            if path.lower() == root_path.lower() or path.lower().startswith(
                    root_path.lower() + "/"):
                return True
        return False

    def _is_public_destination(self, origin):
        _scheme, host, port = origin
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            return False
        cache_key = (host, port)
        with self._dns_lock:
            cached = self._dns_cache.get(cache_key)
            if cached is not None:
                return cached
            try:
                direct_ip = ipaddress.ip_address(host)
            except ValueError:
                direct_ip = None
            if direct_ip is not None:
                answer = bool(direct_ip.is_global)
                self._dns_cache[cache_key] = answer
                return answer
            try:
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                self._dns_cache[cache_key] = False
                return False
            addresses = set()
            for info in infos:
                sockaddr = info[4]
                if sockaddr:
                    addresses.add(sockaddr[0])
            answer = bool(addresses)
            for value in addresses:
                try:
                    if not ipaddress.ip_address(value).is_global:
                        answer = False
                        break
                except ValueError:
                    answer = False
                    break
            self._dns_cache[cache_key] = answer
            return answer

    def service_access(self, raw):
        parts = _split_service_url(raw)
        path = _validated_path(parts)
        if not _FEATURE_ENDPOINT_RE.match(path):
            raise RestCallError(
                "Service URL is not a FeatureServer/MapServer root, numeric "
                "sublayer, or sublayer query endpoint.", policy_error=True
            )
        origin = _origin_tuple_from_parts(parts)
        host = origin[1]
        force_anonymous = origin in self.anonymous_origins
        token_allowed = (
            parts.scheme.lower() == "https"
            and not force_anonymous
            and origin in self.trusted_origins
        )
        if (parts.scheme.lower() == "https" and not force_anonymous
                and self.target == "agol" and origin[2] == 443 and (
                    host == "arcgis.com" or host.endswith(AGOL_TRUSTED_SUFFIX))):
            token_allowed = True
        if (parts.scheme.lower() == "https" and not force_anonymous
                and origin == self.portal_origin
                and _PORTAL_PROXY_RE.search(path)):
            token_allowed = True
        if (parts.scheme.lower() == "https" and not force_anonymous
                and self._matches_federation(origin, path)):
            token_allowed = True

        # Every syntactically valid FeatureServer/MapServer destination is
        # contacted. Unknown/private/external/plain-HTTP origins are anonymous;
        # only authenticated federation/Portal/AGOL destinations receive a token.
        mode = "token" if token_allowed else "anonymous"
        safe = urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))
        return safe, mode

    def validate_service_redirect(self, raw, expected_mode):
        safe, mode = self.service_access(raw)
        if expected_mode == "anonymous":
            # Never add credentials merely because an anonymous endpoint
            # redirected to a destination that would otherwise permit a token.
            return safe
        if mode != expected_mode:
            raise RestCallError(
                "Service redirect changed the request authentication policy.",
                policy_error=True,
            )
        return safe

    def render_trusted(self):
        rendered = set()
        for scheme, host, port in self.trusted_origins:
            rendered.add(f"{scheme}://{host}" + (f":{port}" if port != 443 else ""))
        for (scheme, host, port), path in self.federation_roots:
            rendered.add(
                f"{scheme}://{host}" + (f":{port}" if port != 443 else "") + path
            )
        if self.target == "agol":
            rendered.add("https://*.arcgis.com")
        rendered.add(self.portal_base + "/sharing/servers/<id>/rest/services")
        return sorted(rendered)


class ArcGISRestClient:
    """Strict JSON client with redirect, size, token, and origin controls."""

    def __init__(self, policy, session_pool, token_provider, gis, logger):
        self.policy = policy
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.gis = gis
        self.logger = logger
        self._refresh_lock = Lock()

    def portal_json(self, path, params=None, timeout=REQUEST_TIMEOUT):
        url = self.policy.portal_url(path)
        return self._request_with_refresh(
            url, params, timeout, self.policy.validate_portal_url, "token"
        )

    def catalog_json(self, url, params=None, timeout=REQUEST_TIMEOUT):
        safe, mode = self.policy.catalog_access(url)
        validator = lambda candidate: self.policy.validate_catalog_redirect(
            candidate, mode
        )
        return self._request_with_refresh(
            safe, params, timeout, validator, mode
        )

    def service_json(self, url, params=None, timeout=REQUEST_TIMEOUT):
        safe, mode = self.policy.service_access(url)
        validator = lambda candidate: self.policy.validate_service_redirect(
            candidate, mode
        )
        return self._request_with_refresh(
            safe, params, timeout, validator, mode
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

    def _request_with_refresh(self, url, params, timeout, validator, mode):
        token = self.token_provider.get() if mode == "token" else None
        try:
            return self._request_once(
                url, params, timeout, validator, mode, token
            )
        except RestCallError as exc:
            if mode != "token" or not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._request_once(
            url, params, timeout, validator, mode, refreshed
        )

    def _request_once(self, url, params, timeout, validator, mode, token):
        merged = {"f": "json"}
        if params:
            merged.update(params)
        merged.pop("token", None)
        if mode == "token":
            merged["token"] = token

        current = url
        response = None
        for redirect_index in range(MAX_REDIRECTS + 1):
            try:
                response = self.session_pool.get().get(
                    current,
                    params=merged,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.Timeout as exc:
                raise RestCallError(
                    f"timeout after {timeout}s", transport_error=True
                ) from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(
                    f"request failed: {exc}", transport_error=True
                ) from exc

            if not 300 <= response.status_code < 400:
                break
            location = response.headers.get("Location")
            status = response.status_code
            response.close()
            response = None
            if redirect_index >= MAX_REDIRECTS:
                raise RestCallError("Too many redirects.")
            if not location:
                raise RestCallError(
                    f"HTTP {status} redirect supplied no Location header."
                )
            # The endpoint-specific validator independently rechecks the
            # destination. Portal calls remain same-origin; service/catalog
            # redirects may move only to another destination already carrying
            # the same trusted/anonymous policy.
            current = validator(urljoin(current, location))

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
                    f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit",
                    policy_error=True,
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
                        f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit",
                        policy_error=True,
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
            raise RestCallError(
                f"request failed while reading: {exc}", transport_error=True
            ) from exc
        finally:
            response.close()

        body = b"".join(chunks)
        if not body:
            raise RestCallError(
                f"HTTP {status} {reason}: empty response body",
                auth_error=(mode == "token" and status in (401, 403, 498, 499)),
                transient_error=(status == 429),
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status,
            )
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestCallError(
                f"non-JSON response (HTTP {status})",
                auth_error=(mode == "token" and status in (401, 403, 498, 499)),
                transient_error=(status == 429),
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status,
            ) from exc
        if not isinstance(data, dict):
            raise RestCallError(
                "endpoint returned a non-object JSON response",
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status,
            )

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
                auth_error=(mode == "token" and (
                    code in {"401", "403", "498", "499"}
                    or bool(_AUTH_ERROR_RE.search(rendered))
                )),
                transient_error=(code == "429" or status == 429),
                transport_error=(
                    status in (408, 425) or status >= 500
                    or (code.isdigit() and int(code) >= 500)
                ),
                status_code=status,
                rest_code=code,
            )
        if status >= 400:
            raise RestCallError(
                f"HTTP {status} {reason}",
                auth_error=(mode == "token" and status in (401, 403, 498, 499)),
                transient_error=(status == 429),
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status,
            )
        if str(data.get("status", "")).strip().lower() == "error":
            messages = data.get("messages")
            rendered = f"endpoint returned status=error: {messages or 'no message supplied'}"
            raise RestCallError(
                rendered,
                auth_error=(mode == "token" and bool(_AUTH_ERROR_RE.search(rendered))),
                transient_error=("429" in rendered),
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
        stamp = timestamp if suffix == 0 else f"{timestamp}_{suffix:02d}"
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_broken_service_scan_{stamp}.log"
        )
        broken_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_BROKEN_SERVICES_{stamp}.csv"
        )
        if os.path.exists(broken_path):
            continue
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return {
            "timestamp": stamp,
            "log": log_path,
            "broken": broken_path,
        }
    raise RuntimeError("Could not reserve a unique output name after 999 collisions.")


def _expected_platform(target):
    return "AGOL" if str(target).lower() == "agol" else "PORTAL"


def _validate_target_platform(target, platform, portal_url):
    expected = _expected_platform(target)
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} requires platform {expected}, but portal-url "
            f"{portal_url!r} was classified as {platform}."
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


def _is_invalid_token_error(err_text):
    if not err_text or not isinstance(err_text, str):
        return False
    text = err_text.lower()
    return any(marker in text for marker in INVALID_TOKEN_MARKERS)


def _looks_like_permission_error(err_text):
    if not err_text or not isinstance(err_text, str):
        return False
    text = err_text.lower()
    for code in PERMISSION_STATUS_CODES:
        if f"http {code}" in text or f"error {code}" in text:
            return True
    return any(marker in text for marker in PERMISSION_ERROR_MARKERS)


def _is_terminal_auth_uncertainty(exc):
    if not isinstance(exc, RestCallError):
        return _is_invalid_token_error(str(exc))
    status = str(exc.status_code or "")
    rest = str(exc.rest_code or "")
    return (
        status in {"401", "498", "499"}
        or rest in {"401", "498", "499"}
        or _is_invalid_token_error(str(exc))
    )


def _requires_indeterminate(exc):
    return (
        isinstance(exc, RestCallError)
        and (exc.policy_error or exc.transient_error
             or _is_terminal_auth_uncertainty(exc))
    )


def _is_agol(platform):
    return (platform or "").upper() == "AGOL"


def _is_hosting_server(server):
    if not isinstance(server, dict) and not hasattr(server, "get"):
        return False
    if str(server.get("serverRole", "")).upper() == "HOSTING_SERVER":
        return True
    return _arcgis_bool(server.get("isHosted"))


def is_hosting_server(server):
    """Backward-compatible public wrapper."""
    return _is_hosting_server(server)


def server_site_name(server):
    if isinstance(server, dict) and server.get("name"):
        return server["name"]
    if hasattr(server, "get") and server.get("name"):
        return server.get("name")
    url = (server.get("url") if hasattr(server, "get") else "") or ""
    parsed = urlparse(url)
    host = parsed.netloc or url
    context = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
    return f"{host}/{context}" if context else host


def normalize_rest_root(server_url):
    """Preserve the original ordinary normalization contract."""
    if not server_url:
        return ""
    u = str(server_url).rstrip("/")
    lower = u.lower()
    if lower.endswith("/rest/services"):
        return u
    if "/rest/services" in lower:
        return u[:lower.find("/rest/services")] + "/rest/services"
    if lower.endswith("/rest"):
        return u + "/services"
    return u + "/rest/services"


def _root_match(service_url, root):
    try:
        service_parts = _split_service_url(service_url)
        root_parts = _split_service_url(root)
        service_path = _validated_path(service_parts).lower()
        root_path = _validated_path(root_parts).lower()
    except RestCallError:
        return False
    if _origin_tuple_from_parts(service_parts) != _origin_tuple_from_parts(root_parts):
        return False
    return service_path == root_path or service_path.startswith(root_path + "/")


def classify_source(service_url, has_hosted_keyword,
                    hosting_roots, federated_roots):
    for root in federated_roots:
        if root and _root_match(service_url, root):
            return "federated"
    for root in hosting_roots:
        if root and _root_match(service_url, root):
            return "hosted"
    if has_hosted_keyword:
        return "hosted"
    return "registered"


def _item_value(item, name, default=""):
    if item is None:
        return default
    if isinstance(item, dict):
        value = item.get(name, default)
    else:
        value = getattr(item, name, default)
    return default if value is None else value


def _item_dates(item):
    created = epoch_to_str(_item_value(item, "created", None),
                           fmt="%Y/%m/%d", utc=True)
    modified = epoch_to_str(_item_value(item, "modified", None),
                            fmt="%Y/%m/%d", utc=True)
    return created, modified


def _is_hosted_item(item):
    kws = _item_value(item, "typeKeywords", []) or []
    if isinstance(kws, str):
        kws = [kws]
    return any(str(kw or "").strip().lower() == "hosted service" for kw in kws)


def _snapshot_item(item):
    keywords = _item_value(item, "typeKeywords", []) or []
    if isinstance(keywords, tuple):
        keywords = list(keywords)
    return {
        "id": str(_item_value(item, "id", "") or ""),
        "title": _item_value(item, "title", "") or "",
        "owner": _item_value(item, "owner", "") or "",
        "created": _item_value(item, "created", None),
        "modified": _item_value(item, "modified", None),
        "typeKeywords": keywords,
        "url": _item_value(item, "url", "") or "",
    }


def get_federation_servers(client, portal_url, token_provider, logger):
    """Read and validate the original Portal federation endpoint strictly."""
    _compatibility_inputs = (portal_url, token_provider)
    del _compatibility_inputs
    try:
        data = client.portal_json(
            "/portaladmin/federation/servers", timeout=REQUEST_TIMEOUT
        )
    except RestCallError as exc:
        logger.error("Could not read federation servers from Portal admin API: %s", exc)
        raise
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise RestCallError(
            "Portal federation response contained no valid 'servers' list."
        )
    if any(_mapping(server) is None for server in servers):
        raise RestCallError(
            "Portal federation response contained a non-object server record."
        )
    return [dict(_mapping(server)) for server in servers]


def enumerate_servers(client, portal_url, token_provider, logger):
    all_servers = get_federation_servers(
        client, portal_url, token_provider, logger
    )
    logger.info("Federated servers reported by Portal: %d", len(all_servers))
    servers = []
    errors = []
    for srv in all_servers:
        url = str(srv.get("url") or "").strip()
        site = server_site_name(srv)
        if not url:
            message = f"Federation server {site!r} has no public URL."
            logger.warning("SKIP %s", message)
            errors.append(message)
            continue
        role = "HOSTING_SERVER" if _is_hosting_server(srv) else "FEDERATED_SERVER"
        try:
            rest_root = client.policy.add_federation_base(url)
        except RestCallError as exc:
            message = f"Federation server {site!r} supplied an unsafe URL: {exc}"
            logger.warning("SKIP %s", message)
            errors.append(message)
            continue
        servers.append((site, role, url, rest_root, srv))
        logger.info("INCLUDE %-16s %s -> %s", role, site, rest_root)
    return servers, errors


def _normalize_server_id(value):
    # Portal federation ids are opaque and case-preserving. Live Enterprise
    # deployments commonly use shorter mixed-case ids (for example, 16
    # alphanumeric characters), while some deployments expose a UUID. Preserve
    # opaque ids exactly; retain the earlier UUID normalization for compatibility
    # with Portal proxy paths that use the compact 32-hex form.
    text = str(value or "").strip().strip("{}")
    compact = text.replace("-", "")
    if re.fullmatch(r"[0-9A-Fa-f]{32}", compact):
        return compact.lower()
    return text if _SERVER_ID_RE.fullmatch(text) else None


def build_server_lookup(client, portal_url, platform, token_provider, logger):
    hosting_roots = set()
    federated_roots = set()
    servers_info = []
    errors = []

    if _is_agol(platform):
        logger.info("Target is AGOL; skipping Portal federation server "
                    "enumeration. Using keyword classification instead.")
        return hosting_roots, federated_roots, servers_info, errors

    try:
        enumerated, federation_errors = enumerate_servers(
            client, portal_url, token_provider, logger
        )
    except RestCallError as exc:
        return hosting_roots, federated_roots, servers_info, [exc]
    errors.extend(federation_errors)

    for site, role, url, rest_root, raw in enumerated:
        is_hosting = role == "HOSTING_SERVER"
        roots = hosting_roots if is_hosting else federated_roots
        roots.add(rest_root)

        # Federation records may expose public, administrative, and server URLs
        # on different hostnames. Trust every syntactically valid URL reported by
        # the authenticated federation registry, while retaining the public URL
        # as the one used for direct catalog traversal.
        alternate_roots = set()
        for field_name in ("url", "serverUrl", "adminUrl"):
            candidate = str(raw.get(field_name) or "").strip()
            if not candidate:
                continue
            try:
                alternate_roots.add(client.policy.add_federation_base(candidate))
            except RestCallError as exc:
                logger.warning(
                    "Federation server %r supplied unusable %s %r: %s",
                    site, field_name, candidate, exc,
                )
        roots.update(alternate_roots)

        raw_server_id = str(raw.get("id") or "").strip()
        server_id = _normalize_server_id(raw_server_id)
        if raw_server_id:
            if server_id:
                proxy_root = (
                    client.policy.portal_base
                    + f"/sharing/servers/{server_id}/rest/services"
                )
                roots.add(proxy_root)
            else:
                errors.append(
                    f"Federation server {site!r} reported an invalid server "
                    f"id {raw_server_id!r}; Portal sharing-proxy URLs for "
                    "this server cannot be classified authoritatively."
                )

        servers_info.append({
            "name": site,
            "role": role,
            "url": url,
            "admin_url": raw.get("adminUrl") or url,
            "rest_root": rest_root,
            "rest_roots": sorted(alternate_roots | {rest_root}),
            "hosting": is_hosting,
            "server_id": server_id or None,
        })

    logger.info("Trusted service destination(s): %s",
                client.policy.render_trusted())
    return hosting_roots, federated_roots, servers_info, errors


def _search_results(data):
    if "results" in data:
        value = data.get("results")
    elif "items" in data:
        value = data.get("items")
    else:
        value = []
    if not isinstance(value, list):
        raise ScanAbort("Item search returned a malformed results list.")
    if any(not isinstance(entry, dict) for entry in value):
        raise ScanAbort("Item search returned a non-object result.")
    return value


def _search_total(data):
    value = data.get("total")
    if isinstance(value, bool):
        raise ScanAbort("Item search returned a Boolean total.")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(f"Item search returned an invalid total: {value!r}") from exc
    if total < 0:
        raise ScanAbort("Item search returned a negative total.")
    return total


def _search_next_start(data, fallback):
    value = data.get("nextStart")
    if value is None:
        return fallback
    if isinstance(value, bool):
        raise ScanAbort("Item search returned a Boolean nextStart.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"Item search returned an invalid nextStart: {value!r}"
        ) from exc


def _consume_item_bucket(client, query, org_id, enforce_org, store,
                         max_items, on_selected, discovery):
    params = {
        "q": query,
        "sortField": "modified",
        "sortOrder": "desc",
        "start": 1,
        "num": SEARCH_PAGE_SIZE,
    }
    first = client.portal_json(
        "/sharing/rest/search", params=params, timeout=REQUEST_TIMEOUT
    )
    discovery.search_requests += 1
    total = _search_total(first)
    if total >= SEARCH_CAP:
        return True, False

    data = first
    start = 1
    received = 0
    range_ids = set()
    while True:
        page_total = _search_total(data)
        if page_total != total:
            raise ScanAbort(
                f"Item-search total changed during paging ({total} -> {page_total})."
            )
        page = _search_results(data)
        if not page and received < total:
            raise ScanAbort(
                f"Item search returned an empty page after {received} of {total} results."
            )
        for result in page:
            item_id = str(result.get("id") or "").strip()
            if not item_id:
                raise ScanAbort("Item search returned an item without an id.")
            if item_id in range_ids:
                raise ScanAbort(
                    f"Item search repeated id {item_id!r} within one query."
                )
            range_ids.add(item_id)
            if enforce_org:
                explicit_org = result.get("orgId") or result.get("orgid")
                if (explicit_org not in (None, "")
                        and str(explicit_org).lower() != org_id.lower()):
                    raise ScanAbort(
                        f"Item search returned cross-organization item {item_id} "
                        f"(orgId={explicit_org!r}, expected {org_id!r})."
                    )
            if store.mark_item_seen(item_id):
                discovery.selected_item_ids += 1
                on_selected(item_id, result)
                if store.selected_item_count >= max_items:
                    return False, True
        received += len(page)
        if received > total:
            raise ScanAbort("Item search returned more records than its declared total.")
        next_start = _search_next_start(
            data, start + len(page) if page else -1
        )
        if next_start <= 0:
            if received != total:
                raise ScanAbort(
                    f"Item search ended after {received} of {total} results."
                )
            if len(range_ids) != total:
                raise ScanAbort(
                    f"Item-search paging mismatch: expected {total}, read "
                    f"{received}, unique {len(range_ids)}."
                )
            break
        if next_start <= start:
            raise ScanAbort("Item search pagination did not advance.")
        if next_start > SEARCH_CAP:
            raise ScanAbort("Item search pagination exceeded the API result cap.")
        start = next_start
        params["start"] = start
        data = client.portal_json(
            "/sharing/rest/search", params=params, timeout=REQUEST_TIMEOUT
        )
        discovery.search_requests += 1
    return False, False


def _enumerate_feature_items_strict(client, org_id, org_only, store,
                                    max_items, on_selected, logger, discovery):
    clause = ALL_FS_CLAUSE
    if org_only:
        clause = f"{clause} AND orgid:{org_id}"
        logger.info("Restricting item search to org-owned content (orgid:%s).",
                    org_id)
    else:
        logger.warning("--all-owners selected: item discovery is intentionally "
                       "not organization-scoped.")

    stopped = False

    def search_range(lo_ms, hi_ms):
        nonlocal stopped
        if stopped:
            return
        query = f"{clause} AND created:[{lo_ms} TO {hi_ms}]"
        hit_cap, reached_limit = _consume_item_bucket(
            client, query, org_id, org_only, store, max_items,
            on_selected, discovery,
        )
        if reached_limit:
            if max_items >= MAX_ITEMS:
                raise ScanAbort(
                    f"Full item discovery reached the {MAX_ITEMS:,}-item "
                    "safety ceiling; refusing to label a capped inventory complete."
                )
            stopped = True
            return
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
                f"reached the {SEARCH_CAP}-item cap."
            )

    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    cursor = _SEARCH_START
    while cursor < span_end and not stopped:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        search_range(
            int(cursor.timestamp() * 1000),
            int(next_month.timestamp() * 1000) - 1,
        )
        cursor = next_month
        if discovery.search_requests and discovery.search_requests % 20 == 0:
            logger.info(
                "  %d strict item-search request(s), %d selected item id(s) so far.",
                discovery.search_requests, store.selected_item_count,
            )
    logger.info(
        "Strict item discovery complete: %d request(s), %d selected item id(s)%s.",
        discovery.search_requests, store.selected_item_count,
        " (configured cap reached)" if stopped else "",
    )


def search_feature_service_items(gis, base_url, max_items, org_only, logger):
    """Backward-compatible strict item search returning ``(Item, hosted)``.

    The main workflow streams directly into ``TargetStore``. This wrapper keeps
    the original helper name/signature and return shape for any local caller
    that imports it, while using the same strict organization/paging contract.
    """
    portal_base = _normalize_portal_base(base_url or _portal_base_url(gis))
    platform = get_platform(portal_base)
    target = "agol" if platform == "AGOL" else "portal"
    policy = TrustedUrlPolicy(portal_base, target, logger=logger)

    class _SdkSessionPool:
        def get(self):
            return gis._con._session

    provider = provider_for_gis(gis, platform)
    client = ArcGISRestClient(
        policy, _SdkSessionPool(), provider, gis, logger
    )
    org_id = get_org_id(gis, logger=logger)
    if org_only:
        org_id = _safe_org_id(org_id)
    else:
        org_id = str(org_id or "").strip()

    discovery = DiscoveryResult()
    selected_ids = []
    store = TargetStore(tempfile.gettempdir(), logger)
    try:
        _enumerate_feature_items_strict(
            client,
            org_id,
            org_only,
            store,
            max_items,
            lambda item_id, _raw: selected_ids.append(item_id),
            logger,
            discovery,
        )
    finally:
        store.close()

    tagged = []
    for item_id in selected_ids:
        try:
            item = gis.content.get(item_id)
        except Exception as exc:
            logger.warning(
                "Could not resolve item %s: %s",
                item_id, redact_token(str(exc)),
            )
            continue
        if item is not None:
            tagged.append((item, _is_hosted_item(item)))
    return tagged


def _parse_group_ids(raw):
    if not raw:
        return []
    result = []
    seen = set()
    for value in str(raw).split(","):
        group_id = value.strip()
        if not group_id or group_id in seen:
            continue
        if not _GROUP_ID_RE.fullmatch(group_id):
            raise ScanAbort(f"Invalid --exclude-group-id value: {group_id!r}")
        seen.add(group_id)
        result.append(group_id)
    return result


def _resolve_exclusions_strict(gis, raw, logger):
    group_ids = _parse_group_ids(raw)
    if not group_ids:
        return ExclusionSet()
    excluded_ids = set()
    excluded_service_keys = set()
    for group_id in group_ids:
        try:
            group = gis.groups.get(group_id)
        except Exception as exc:
            raise ScanAbort(
                f"Could not resolve requested exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        if group is None:
            raise ScanAbort(
                f"Requested exclusion group {group_id!r} was not found."
            )
        try:
            members = group.content()
        except Exception as exc:
            raise ScanAbort(
                f"Could not read requested exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        group_item_ids = set()
        group_service_keys = set()
        for item in members:
            item_id = str(getattr(item, "id", "") or "").strip()
            if item_id:
                group_item_ids.add(item_id)
            service_url = str(getattr(item, "url", "") or "").strip()
            if service_url:
                group_service_keys.add(_target_key(service_url))
        excluded_ids.update(group_item_ids)
        excluded_service_keys.update(group_service_keys)
        logger.info(
            "--exclude-group-id: group %r (%s) contributes %d excluded "
            "item id(s) and %d service URL(s).",
            group_id, getattr(group, "title", "?") or "?",
            len(group_item_ids), len(group_service_keys),
        )
    logger.info(
        "Exclusion group(s) resolved: %d unique item id(s), %d unique service "
        "URL(s).",
        len(excluded_ids), len(excluded_service_keys),
    )
    return ExclusionSet(excluded_ids, excluded_service_keys)


def _safe_catalog_segment(value, label, allow_slash=False):
    text = str(value or "").strip()
    if not text:
        raise RestCallError(f"Catalog {label} is blank.")
    if "\\" in text or any(ord(ch) < 32 for ch in text):
        raise RestCallError(f"Catalog {label} contains unsafe characters.")
    pieces = text.split("/") if allow_slash else [text]
    if any(piece in ("", ".", "..") for piece in pieces):
        raise RestCallError(f"Catalog {label} contains an unsafe path segment.")
    return "/".join(quote(unquote(piece), safe="") for piece in pieces)


def _contextual_rest_error(context, exc):
    if not isinstance(exc, RestCallError):
        return RestCallError(f"{context}: {exc}")
    return RestCallError(
        f"{context}: {exc}",
        tls_error=exc.tls_error,
        auth_error=exc.auth_error,
        policy_error=exc.policy_error,
        transient_error=exc.transient_error,
        transport_error=exc.transport_error,
        status_code=exc.status_code,
        rest_code=exc.rest_code,
    )


def walk_server_feature_services(client, server_url, logger, max_items,
                                 on_service=None):
    """Walk one registered Server catalog in established depth-first order.

    When ``on_service`` is omitted, the backward-compatible return value is a
    list of service URLs.  Internal full scans supply a callback so even a very
    large federated catalog is streamed directly into the disk-backed target
    store rather than accumulated in memory.
    """
    found = []
    errors = []
    emitted = 0
    stop = False
    try:
        base = client.policy.validate_catalog_url(server_url)
    except RestCallError as exc:
        return found, [_contextual_rest_error("unsafe catalog root", exc)]
    base = base.rstrip("/")
    visited = set()

    def emit(url):
        nonlocal emitted, stop
        emitted += 1
        if on_service is None:
            found.append(url)
            keep_going = True
        else:
            keep_going = on_service(url) is not False
        if emitted >= max_items or not keep_going:
            stop = True

    def read_dir(dir_url, depth):
        nonlocal stop
        if stop:
            return
        if depth > MAX_CATALOG_DEPTH:
            errors.append(
                f"Federated catalog exceeded depth {MAX_CATALOG_DEPTH}: {dir_url}"
            )
            return
        try:
            safe_dir = client.policy.validate_catalog_url(dir_url)
        except RestCallError as exc:
            errors.append(_contextual_rest_error(
                "unsafe catalog directory", exc
            ))
            return
        key = safe_dir.lower()
        if key in visited:
            return
        if len(visited) >= MAX_CATALOG_DIRECTORIES:
            errors.append(
                f"Federated catalog exceeded {MAX_CATALOG_DIRECTORIES} directories."
            )
            stop = True
            return
        visited.add(key)
        try:
            data = client.catalog_json(safe_dir, timeout=REQUEST_TIMEOUT)
        except RestCallError as exc:
            errors.append(_contextual_rest_error(
                f"Federated catalog read failed ({safe_dir})", exc
            ))
            return

        services = data.get("services", [])
        folders = data.get("folders", [])
        if not isinstance(services, list) or not isinstance(folders, list):
            errors.append(f"Federated catalog returned malformed lists: {safe_dir}")
            return
        for service in services:
            if stop:
                return
            if not isinstance(service, dict):
                errors.append(
                    f"Federated catalog returned a non-object service: {safe_dir}"
                )
                continue
            if str(service.get("type") or "") != "FeatureServer":
                continue
            try:
                name = _safe_catalog_segment(
                    service.get("name"), "service name", allow_slash=True
                )
            except RestCallError as exc:
                errors.append(f"{safe_dir}: {exc}")
                continue
            emit(f"{base}/{name}/FeatureServer")
        for folder in folders:
            if stop:
                return
            try:
                name = _safe_catalog_segment(folder, "folder name")
            except RestCallError as exc:
                errors.append(f"{safe_dir}: {exc}")
                continue
            read_dir(f"{base}/{name}", depth + 1)

    read_dir(base, 0)
    logger.info("Federated catalog %s -> %d feature service(s).",
                base, emitted)
    return found, errors


def _make_broken_row(source, title, item_id, service_url, owner,
                     date_created, date_modified, scope, err,
                     lname="", lid="", lurl=""):
    safe_err = redact_token(str(err))
    return {
        "SOURCE": source,
        "title": title,
        "item_id": item_id,
        "service_url": service_url,
        "owner": owner,
        "date_created": date_created,
        "date_modified": date_modified,
        "Scope": scope,
        "layer_name": lname,
        "layer_id": lid,
        "layer_url": lurl,
        "error": safe_err,
        "Likely_Permission_Issue": _looks_like_permission_error(safe_err),
    }


def _new_stats_delta():
    return {
        "services_checked": 1,
        "services_ok": 0,
        "services_broken": 0,
        "services_indeterminate": 0,
        "services_permission_flagged": 0,
        "services_skipped_invalid_token": 0,
        "layers_checked": 0,
        "layers_ok": 0,
        "layers_broken": 0,
        "layers_permission_flagged": 0,
        "service_root_failures": 0,
        "src_hosted": 0,
        "src_federated": 0,
        "src_registered": 0,
        "tls_failures": 0,
        "auth_failures": 0,
        "policy_failures": 0,
        "transient_failures": 0,
        "transport_failures": 0,
        "error_samples": [],
    }


def _mark_indeterminate(delta, message, exc=None, confirmed_rows=None):
    delta["services_indeterminate"] += 1
    safe = redact_token(str(message))
    delta["error_samples"].append(safe)
    if confirmed_rows and not delta.get("services_broken"):
        # A prior layer failure already proved the service broken even though a
        # later check was indeterminate. Preserve that confirmed finding while
        # also marking the overall service check incomplete.
        delta["services_broken"] = 1
    if isinstance(exc, RestCallError):
        if exc.tls_error:
            delta["tls_failures"] += 1
        if exc.auth_error:
            delta["auth_failures"] += 1
        if exc.policy_error:
            delta["policy_failures"] += 1
        if exc.transient_error:
            delta["transient_failures"] += 1
        if exc.transport_error:
            delta["transport_failures"] += 1
    return list(confirmed_rows or []), delta


def _record_reported_uncertainty(delta, message, exc):
    """Retain a broken observation but make the run status incomplete."""
    if not delta.get("services_indeterminate"):
        delta["services_indeterminate"] = 1
    safe = redact_token(str(message))
    if safe not in delta["error_samples"]:
        delta["error_samples"].append(safe)
    if exc.tls_error:
        delta["tls_failures"] += 1
    if exc.transport_error:
        delta["transport_failures"] += 1



def _validated_sublayers(root):
    sublayers = []
    for key, kind in (("layers", "layer"), ("tables", "table")):
        value = root.get(key, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            raise ValueError(f"service root returned malformed {key} list")
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError(f"service root returned a non-object {kind}")
            layer_id = entry.get("id")
            if isinstance(layer_id, bool):
                raise ValueError(f"service root returned Boolean {kind} id")
            try:
                normalized_id = int(layer_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"service root returned invalid {kind} id {layer_id!r}"
                ) from exc
            if normalized_id < 0:
                raise ValueError(
                    f"service root returned negative {kind} id {normalized_id}"
                )
            sublayers.append((normalized_id, entry.get("name"), kind))
    return sublayers


def _validated_count(data):
    if not isinstance(data, dict) or "count" not in data:
        raise ValueError("query returned no 'count' field")
    value = data.get("count")
    if isinstance(value, bool):
        raise ValueError("query returned a Boolean count")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"query returned an invalid count: {value!r}") from exc
    if count < 0:
        raise ValueError(f"query returned a negative count: {count}")
    return count


def _check_service(target, client, hosting_roots, federated_roots):
    delta = _new_stats_delta()
    broken_rows = []

    item = target.get("item")
    svc_url = str(target.get("service_url") or "").rstrip("/")
    has_hosted_keyword = bool(target.get("has_hosted_keyword"))
    source = classify_source(
        svc_url, has_hosted_keyword, hosting_roots, federated_roots
    )
    delta[f"src_{source}"] += 1

    if item is not None:
        date_created, date_modified = _item_dates(item)
        title = _item_value(item, "title", "")
        item_id = _item_value(item, "id", "")
        owner = _item_value(item, "owner", "")
    else:
        date_created = date_modified = ""
        title = svc_url.rsplit("/", 2)[-2] if "/" in svc_url else svc_url
        item_id = owner = ""

    if not svc_url:
        delta["services_broken"] += 1
        delta["service_root_failures"] += 1
        broken_rows.append(_make_broken_row(
            source, title, item_id, svc_url, owner,
            date_created, date_modified, "SERVICE",
            "item has no service URL (nothing to query)",
        ))
        return broken_rows, delta

    try:
        root = client.service_json(svc_url, timeout=REQUEST_TIMEOUT)
    except RestCallError as exc:
        full_error = f"service root failed: {exc}"
        if _requires_indeterminate(exc):
            delta["services_skipped_invalid_token"] += int(
                _is_terminal_auth_uncertainty(exc)
            )
            return _mark_indeterminate(delta, full_error, exc)
        if exc.tls_error or exc.transport_error:
            _record_reported_uncertainty(delta, full_error, exc)
        delta["services_broken"] += 1
        delta["service_root_failures"] += 1
        if _looks_like_permission_error(full_error):
            delta["services_permission_flagged"] += 1
        broken_rows.append(_make_broken_row(
            source, title, item_id, svc_url, owner,
            date_created, date_modified, "SERVICE", full_error, lurl=svc_url,
        ))
        return broken_rows, delta

    try:
        sublayers = _validated_sublayers(root)
    except ValueError as exc:
        delta["services_broken"] += 1
        delta["service_root_failures"] += 1
        broken_rows.append(_make_broken_row(
            source, title, item_id, svc_url, owner,
            date_created, date_modified, "SERVICE", str(exc), lurl=svc_url,
        ))
        return broken_rows, delta

    if not sublayers:
        delta["services_broken"] += 1
        broken_rows.append(_make_broken_row(
            source, title, item_id, svc_url, owner,
            date_created, date_modified, "SERVICE",
            "service root returned no layers or tables", lurl=svc_url,
        ))
        return broken_rows, delta

    layers_broken = 0
    for layer_id, layer_name, kind in sublayers:
        delta["layers_checked"] += 1
        layer_url = f"{svc_url}/{layer_id}"
        try:
            client.service_json(layer_url, timeout=REQUEST_TIMEOUT)
        except RestCallError as exc:
            full_error = f"{kind} metadata failed: {exc}"
            if _requires_indeterminate(exc):
                return _mark_indeterminate(
                    delta, full_error, exc, confirmed_rows=broken_rows
                )
            if exc.tls_error or exc.transport_error:
                _record_reported_uncertainty(delta, full_error, exc)
            layers_broken += 1
            delta["layers_broken"] += 1
            if _looks_like_permission_error(full_error):
                delta["layers_permission_flagged"] += 1
            broken_rows.append(_make_broken_row(
                source, title, item_id, svc_url, owner,
                date_created, date_modified, kind.upper(), full_error,
                layer_name or "", layer_id, layer_url,
            ))
            continue

        try:
            count_data = client.service_json(
                f"{layer_url}/query",
                params={"where": "1=1", "returnCountOnly": "true"},
                timeout=REQUEST_TIMEOUT,
            )
            _validated_count(count_data)
        except RestCallError as exc:
            full_error = f"{kind} query {exc}"
            if _requires_indeterminate(exc):
                return _mark_indeterminate(
                    delta, full_error, exc, confirmed_rows=broken_rows
                )
            if exc.tls_error or exc.transport_error:
                _record_reported_uncertainty(delta, full_error, exc)
            layers_broken += 1
            delta["layers_broken"] += 1
            if _looks_like_permission_error(full_error):
                delta["layers_permission_flagged"] += 1
            broken_rows.append(_make_broken_row(
                source, title, item_id, svc_url, owner,
                date_created, date_modified, kind.upper(), full_error,
                layer_name or "", layer_id, layer_url,
            ))
            continue
        except ValueError as exc:
            full_error = f"{kind} query {exc}"
            layers_broken += 1
            delta["layers_broken"] += 1
            broken_rows.append(_make_broken_row(
                source, title, item_id, svc_url, owner,
                date_created, date_modified, kind.upper(), full_error,
                layer_name or "", layer_id, layer_url,
            ))
            continue

        delta["layers_ok"] += 1

    if delta["services_indeterminate"]:
        if broken_rows and not delta.get("services_broken"):
            delta["services_broken"] = 1
        return broken_rows, delta
    if layers_broken == 0:
        delta["services_ok"] += 1
    else:
        delta["services_broken"] += 1
    return broken_rows, delta


def _target_key(url, item_id=None):
    normalized = str(url or "").rstrip("/").lower()
    if normalized:
        return normalized
    return f"__nourl__{item_id or ''}"


def build_targets(gis, client, max_items, org_only, sources,
                  federated_hosts_info, hosting_roots, federated_roots,
                  logger, excluded_ids, target_store, discovery):
    want_item_catalog = bool({"hosted", "registered"} & set(sources))

    if want_item_catalog:
        org_id = get_org_id(gis, logger=logger)
        if org_only:
            org_id = _safe_org_id(org_id)
        else:
            org_id = str(org_id or "").strip()

        def on_selected(item_id, raw_result):
            # The strict pager supplies the search record for compatibility;
            # item resolution intentionally uses the SDK just as the original did.
            _search_record = raw_result
            del _search_record
            if item_id in excluded_ids:
                discovery.excluded_items += 1
                return
            try:
                item = gis.content.get(item_id)
            except Exception as exc:
                discovery.item_resolution_errors += 1
                discovery.add_error(
                    f"Could not resolve feature service item {item_id}: "
                    f"{redact_token(str(exc))}"
                )
                return
            if item is None:
                discovery.item_resolution_errors += 1
                discovery.add_error(
                    f"Feature service item {item_id} could not be resolved."
                )
                return
            snapshot = _snapshot_item(item)
            has_keyword = _is_hosted_item(snapshot)
            url = str(snapshot.get("url") or "")
            source = classify_source(
                url, has_keyword, hosting_roots, federated_roots
            )
            if source not in sources:
                return
            target_store.put_target(
                _target_key(url, item_id),
                {
                    "service_url": url,
                    "item": snapshot,
                    "has_hosted_keyword": has_keyword,
                },
                replace=True,
            )

        _enumerate_feature_items_strict(
            client, org_id, org_only, target_store, max_items,
            on_selected, logger, discovery,
        )
        logger.info(
            "Resolved item-backed targets: %d target(s), %d item resolution "
            "error(s), %d excluded item(s).",
            len(target_store), discovery.item_resolution_errors,
            discovery.excluded_items,
        )

    if "federated" in sources and len(target_store) < max_items:
        for server in federated_hosts_info:
            if server.get("hosting"):
                continue
            def add_catalog_target(url):
                key = _target_key(url)
                excluded_service_keys = getattr(
                    excluded_ids, "service_keys", set()
                )
                if key in excluded_service_keys:
                    discovery.excluded_items += 1
                    return len(target_store) < max_items
                if not target_store.contains_target(key):
                    target_store.put_target(
                        key,
                        {"service_url": url, "item": None,
                         "has_hosted_keyword": False},
                        replace=False,
                    )
                return len(target_store) < max_items

            _unused_urls, errors = walk_server_feature_services(
                client,
                server.get("rest_root", ""),
                logger,
                max_items,
                on_service=add_catalog_target,
            )
            for error in errors:
                discovery.add_error(
                    error,
                    catalog=True,
                    tls=isinstance(error, RestCallError) and error.tls_error,
                    auth=isinstance(error, RestCallError) and error.auth_error,
                    transport=(
                        isinstance(error, RestCallError)
                        and error.transport_error
                    ),
                )
            if len(target_store) >= max_items:
                break

    if max_items >= MAX_ITEMS and len(target_store) >= MAX_ITEMS:
        raise ScanAbort(
            f"Full target discovery reached the {MAX_ITEMS:,}-service safety "
            "ceiling; refusing to label a capped inventory complete."
        )
    logger.info("Assembled %d unique scan target(s) from sources: %s",
                len(target_store), ", ".join(sorted(sources)))


def _merge_delta(stats, delta, incomplete_errors):
    for key, value in delta.items():
        if key == "error_samples":
            for message in value:
                if len(incomplete_errors) < MAX_ERROR_SAMPLES:
                    incomplete_errors.append(message)
            continue
        stats[key] = stats.get(key, 0) + value


def scan_services(gis, client, platform, output_path, max_items, workers,
                  org_only, sources, logger, excluded_ids, target_store,
                  discovery, hosting_roots, federated_roots,
                  servers_info):
    build_targets(
        gis, client, max_items, org_only, sources, servers_info,
        hosting_roots, federated_roots, logger, excluded_ids,
        target_store, discovery,
    )

    stats = {
        "items_found": len(target_store),
        "services_checked": 0,
        "services_ok": 0,
        "services_broken": 0,
        "services_indeterminate": 0,
        "services_permission_flagged": 0,
        "services_skipped_invalid_token": 0,
        "layers_checked": 0,
        "layers_ok": 0,
        "layers_broken": 0,
        "layers_permission_flagged": 0,
        "service_root_failures": 0,
        "src_hosted": 0,
        "src_federated": 0,
        "src_registered": 0,
        "tls_failures": 0,
        "auth_failures": 0,
        "policy_failures": 0,
        "transient_failures": 0,
        "transport_failures": 0,
    }
    incomplete_errors = list(discovery.errors[:MAX_ERROR_SAMPLES])

    logger.info("Health-checking %d service(s) with %d worker(s) ...",
                len(target_store), workers)

    def worker(target):
        return _check_service(
            target, client, hosting_roots, federated_roots
        )

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for target, future in _bounded_future_results(
                executor, target_store.iter_targets(max_items), worker,
                max_in_flight=max(1, workers * 2)):
            try:
                rows, delta = future.result()
            except Exception as exc:
                delta = _new_stats_delta()
                source = classify_source(
                    target.get("service_url", ""),
                    bool(target.get("has_hosted_keyword")),
                    hosting_roots,
                    federated_roots,
                )
                delta[f"src_{source}"] = 1
                delta["services_indeterminate"] = 1
                delta["error_samples"] = [
                    redact_token(
                        f"unexpected scan exception for "
                        f"{target.get('service_url', '')}: {exc}"
                    )
                ]
                rows = []
            _merge_delta(stats, delta, incomplete_errors)
            target_store.add_broken_rows(rows)
            done += 1
            if done % 100 == 0 or done == len(target_store):
                logger.info(
                    "Progress: %d / %d service(s) processed, %d broken, "
                    "%d indeterminate.",
                    done, len(target_store), stats["services_broken"],
                    stats["services_indeterminate"],
                )

    broken_csv = None
    broken_row_view = target_store.broken_row_view()
    if len(broken_row_view):
        broken_csv = write_dict_csv(
            broken_row_view, output_path, fieldnames=BROKEN_FIELDS,
            logger=logger, label="broken row",
            force_values={"platform": platform},
        )
        if not broken_csv:
            raise ScanAbort("Broken-service rows existed but the report was not written.")

    stats["rows_written"] = len(broken_row_view)
    stats["rows_permission_flagged"] = sum(
        1 for row in broken_row_view if row.get("Likely_Permission_Issue")
    )
    stats["discovery_errors"] = discovery.error_count
    stats["item_resolution_errors"] = discovery.item_resolution_errors
    stats["catalog_errors"] = discovery.catalog_errors
    stats["tls_failures"] += discovery.tls_errors
    stats["auth_failures"] += discovery.auth_errors
    stats["transport_failures"] += discovery.transport_errors
    stats["search_requests"] = discovery.search_requests
    stats["excluded_items"] = discovery.excluded_items

    logger.info(
        "Scan diagnostics: targets=%d checked=%d ok=%d broken=%d "
        "indeterminate=%d | layers_checked=%d layers_broken=%d | "
        "src hosted=%d federated=%d registered=%d | rows=%d",
        stats["items_found"], stats["services_checked"], stats["services_ok"],
        stats["services_broken"], stats["services_indeterminate"],
        stats["layers_checked"], stats["layers_broken"],
        stats["src_hosted"], stats["src_federated"],
        stats["src_registered"], len(broken_row_view),
    )
    return {
        "broken_csv": broken_csv,
        "stats": stats,
        "incomplete_errors": incomplete_errors,
    }




def _sources_arg(value):
    raw = str(value or "")
    unknown = [
        token.strip() for token in raw.split(",")
        if token.strip() and token.strip().lower() not in VALID_SOURCES
    ]
    if unknown:
        raise argparse.ArgumentTypeError(
            "unknown source value(s): " + ", ".join(repr(v) for v in unknown)
            + "; expected hosted, federated, and/or registered"
        )
    return raw

def _parse_sources(raw, logger):
    if not raw:
        return set(VALID_SOURCES)
    if logger is not None:
        logger.debug("Parsing requested service sources: %s", raw)
    picked = set()
    for token in str(raw).split(","):
        source = token.strip().lower()
        if not source:
            continue
        if source not in VALID_SOURCES:
            raise ScanAbort(
                f"Unknown source {source!r}; expected one or more of "
                f"{', '.join(VALID_SOURCES)}."
            )
        picked.add(source)
    return picked or set(VALID_SOURCES)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Scan AGOL or Portal for non-functional feature services and "
            "report only the broken ones via CSV and email."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (scans only the first 250 services in "
            "the script's established discovery order). Pass --no-test to "
            "scan everything.\n\n"
            "Examples:\n"
            "  python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml "
            "--target portal\n"
            "  python SCAN_BROKEN_HOSTED_SERVICES.py --config org2.config.toml "
            "--target agol --no-test --workers 24\n"
            "  python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml "
            "--target portal --sources hosted,federated\n"
            "  python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml "
            "--target portal --tls-mode os-trust-store\n"
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
        help_text=(
            "FULL mode: scan all feature services. If omitted, the script "
            "runs in test mode and scans only 250 services."
        ),
    )
    parser.add_argument(
        "--all-owners",
        dest="org_only",
        action="store_false",
        help=(
            "Scan every reachable feature service item, not just "
            "organization-owned content. This intentionally removes the "
            "orgid search clause."
        ),
    )
    parser.set_defaults(org_only=True)
    parser.add_argument(
        "--sources",
        default=None,
        type=_sources_arg,
        help=(
            "Comma-separated categories: hosted, federated, registered. "
            "Default: all three."
        ),
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("--workers", 1, 64),
        default=DEFAULT_WORKERS,
        help=(
            f"Number of concurrent worker threads, 1-64 "
            f"(default: {DEFAULT_WORKERS})."
        ),
    )
    parser.add_argument(
        "--trusted-service-origin",
        action="append",
        default=[],
        metavar="HTTPS_ORIGIN",
        help=(
            "Advanced override, repeatable. Permit an exact HTTPS origin "
            "outside the authenticated Portal federation to receive the "
            "organization token for service checks. Supply only an origin "
            "such as https://server.example.com, with no path."
        ),
    )
    parser.add_argument(
        "--anonymous-service-origin",
        action="append",
        default=[],
        metavar="HTTP_OR_HTTPS_ORIGIN",
        help=(
            "Advanced override, repeatable. Force an exact HTTP or HTTPS "
            "origin to be checked anonymously even if Portal federation would "
            "otherwise allow the organization token. All other untrusted "
            "service origins are already checked anonymously by default."
        ),
    )

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES),
        default="standard",
        help=(
            "TLS verification strategy: 'standard' (default), "
            "'os-trust-store', or 'custom-ca-bundle'."
        ),
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help=(
            "Path to a PEM CA bundle. Required when --tls-mode is "
            "custom-ca-bundle."
        ),
    )
    return parser.parse_args(argv)


def _send_abort_email(subject, reason, log_path, args,
                      start_wallclock, start_perf, logger):
    safe_reason = redact_token(str(reason))
    body = (
        "Broken Service Scan\n"
        "Result: ABORTED\n\n"
        f"Reason: {safe_reason}\n\n"
        f"{timing_block(start_wallclock, start_perf)}\n"
    )
    send_email(
        "[ABORTED] " + subject,
        body,
        [log_path],
        args.smtp_server,
        args.email_to,
        args.email_from,
        logger,
    )


def _build_completion_body(result_label, platform, org_name, timestamp,
                           args, sources, stats, broken_csv, timing,
                           incomplete_errors):
    mode = "TEST (250 services)" if args.test else "FULL"
    lines = [
        "Broken Service Scan (hosted / federated / registered)",
        f"Result        : {result_label}",
        f"Organization  : {org_name}",
        f"Platform      : {platform}",
        f"Run timestamp : {timestamp}",
        f"Mode          : {mode}",
        f"TLS mode      : {args.tls_mode}",
        f"Org-only scan : {bool(args.org_only)}",
        f"Sources       : {', '.join(sorted(sources)) or '(none applicable)'}",
        f"Username      : {args.username}",
        f"Excluded group(s): {args.exclude_group_id or '(none)'}",
        "",
        f"Scan targets          : {stats.get('items_found', 0)}",
        f"Services processed    : {stats.get('services_checked', 0)}",
        f"Services OK           : {stats.get('services_ok', 0)}",
        f"Services broken       : {stats.get('services_broken', 0)}",
        f"Services indeterminate: {stats.get('services_indeterminate', 0)}",
        f"Layers checked        : {stats.get('layers_checked', 0)}",
        f"Layers broken         : {stats.get('layers_broken', 0)}",
        f"Broken rows reported  : {stats.get('rows_written', 0)}",
        f"Broken report         : {broken_csv or '(none broken)'}",
        "",
        f"Item-resolution errors: {stats.get('item_resolution_errors', 0)}",
        f"Catalog errors        : {stats.get('catalog_errors', 0)}",
        f"TLS/CERT errors       : {stats.get('tls_failures', 0)}",
        f"Authentication errors : {stats.get('auth_failures', 0)}",
        f"URL-policy blocks     : {stats.get('policy_failures', 0)}",
        f"Throttling/transient  : {stats.get('transient_failures', 0)}",
        f"Transport failures    : {stats.get('transport_failures', 0)}",
    ]
    if result_label == "INCOMPLETE":
        lines.extend([
            "",
            "WARNING: Confirmed broken rows are retained, but one or more "
            "services or discovery stages could not be conclusively checked. "
            "Broken and indeterminate service counts can overlap when one layer "
            "was confirmed broken and another could not be checked. Absence "
            "from the broken report is not proof that every omitted "
            "service is functional.",
        ])
    if incomplete_errors:
        lines.extend(["", "Representative incomplete-check reason(s):"])
        for message in incomplete_errors[:10]:
            lines.append("  - " + redact_token(str(message)))
        remaining = len(incomplete_errors) - min(len(incomplete_errors), 10)
        if remaining > 0:
            lines.append(f"  ...and {remaining} additional reason(s); see log.")
    lines.extend(["", timing, "", "See the attached log and report for detail."])
    return "\n".join(lines) + "\n"


def _log_run_summary(result_label, args, platform, sources, stats, broken_csv,
                     incomplete_errors, logger):
    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result                : %s", result_label)
    logger.info("Organization          : %s", args.org_name)
    logger.info("Platform              : %s", platform)
    logger.info("Mode                  : %s", "TEST" if args.test else "FULL")
    logger.info("TLS mode              : %s", args.tls_mode)
    logger.info("Org-only scan         : %s", bool(args.org_only))
    logger.info("Sources               : %s",
                ", ".join(sorted(sources)) or "(none applicable)")
    logger.info("Scan targets          : %d", stats.get("items_found", 0))
    logger.info("Services processed    : %d", stats.get("services_checked", 0))
    logger.info("Services OK           : %d", stats.get("services_ok", 0))
    logger.info("Services broken       : %d", stats.get("services_broken", 0))
    logger.info("Services indeterminate: %d",
                stats.get("services_indeterminate", 0))
    logger.info("Layers checked        : %d", stats.get("layers_checked", 0))
    logger.info("Layers broken         : %d", stats.get("layers_broken", 0))
    logger.info("Broken rows           : %d", stats.get("rows_written", 0))
    logger.info("Permission-flag rows  : %d",
                stats.get("rows_permission_flagged", 0))
    logger.info("Item-resolution errors: %d",
                stats.get("item_resolution_errors", 0))
    logger.info("Catalog errors        : %d", stats.get("catalog_errors", 0))
    logger.info("TLS/CERT errors       : %d", stats.get("tls_failures", 0))
    logger.info("Authentication errors : %d", stats.get("auth_failures", 0))
    logger.info("URL-policy blocks     : %d", stats.get("policy_failures", 0))
    logger.info("Throttling/transient  : %d",
                stats.get("transient_failures", 0))
    logger.info("Transport failures    : %d",
                stats.get("transport_failures", 0))
    logger.info("Broken report         : %s", broken_csv or "(none broken)")
    logger.info("=" * 70)
    if incomplete_errors:
        logger.warning("Incomplete-check reason(s), up to %d retained:",
                       MAX_ERROR_SAMPLES)
        for message in incomplete_errors:
            logger.warning("  %s", redact_token(str(message)))


def main(argv=None):
    args = parse_args(argv)
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle.",
            file=sys.stderr,
        )
        return 2

    start_wallclock, start_perf = start_timer()
    base_timestamp = build_timestamp()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(args.target, args.config)
    platform = get_platform(args.portal_url)
    per_run_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS

    ensure_output_dir(args.output_dir)
    paths = _reserve_run_paths(
        args.output_dir, args.org_name_safe, platform, base_timestamp
    )
    timestamp = paths["timestamp"]
    logger = setup_logging(paths["log"], "broken_service_scan")
    sources = _parse_sources(args.sources, logger)
    if _is_agol(platform) and "federated" in sources:
        sources.discard("federated")
        logger.info("Federated source is not applicable to AGOL; ignoring it.")

    subject = subject_line(
        EMAIL_SUBJECT_PREFIX, timestamp, platform, org_name=args.org_name
    )

    logger.info("=" * 70)
    logger.info("Broken Service Scan - run started")
    logger.info("Organization      : %s", args.org_name)
    logger.info("Target            : %s", args.target)
    logger.info("Platform          : %s", platform)
    logger.info("Mode              : %s",
                "TEST (250 services)" if args.test else "FULL")
    logger.info("Org-only scan     : %s", bool(args.org_only))
    logger.info("Sources           : %s",
                ", ".join(sorted(sources)) or "(none applicable)")
    logger.info("Portal URL        : %s", args.portal_url)
    logger.info("Username          : %s", args.username)
    logger.info("Keyring service   : %s", args.keyring_service)
    logger.info("TLS mode          : %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path    : %s", args.ca_bundle_path)
    logger.info("Output directory  : %s", args.output_dir)
    logger.info("Log file          : %s", paths["log"])
    logger.info("Max email MB      : %s", max_email_mb)
    logger.info("Worker threads    : %d", args.workers)
    logger.info("Request timeout   : %ds", REQUEST_TIMEOUT)
    logger.info("Exclude group ID  : %s",
                args.exclude_group_id or "(none)")
    logger.info("Trusted override origins   : %s",
                args.trusted_service_origin or "(none)")
    logger.info("Anonymous override origins : %s",
                args.anonymous_service_origin or "(none)")
    logger.info("=" * 70)

    session_pool = None
    target_store = None
    try:
        _validate_target_platform(args.target, platform, args.portal_url)
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        ca_path = (
            args.ca_bundle_path
            if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
        )

        # Keep CA parsing and SSLContext construction inside this controlled
        # boundary; verify_tls_handshake() builds its context before its own
        # socket-level try/except.
        session = build_secure_session(
            mode,
            ca_bundle_path=args.ca_bundle_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2),
            retry_total=RETRY_TOTAL,
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
            verify_mode=mode,
            ca_bundle_path=ca_path,
        )
        authenticated_base = _portal_base_url(gis, args.portal_url)
        authenticated_platform = get_platform(authenticated_base)
        _validate_target_platform(
            args.target, authenticated_platform, authenticated_base
        )
        if authenticated_platform != platform:
            raise ScanAbort(
                "The authenticated GIS platform differs from the configured "
                f"platform ({authenticated_platform} != {platform})."
            )

        tune_gis_session(
            gis, args.workers, timeout=REQUEST_TIMEOUT,
            retry_total=RETRY_TOTAL,
        )
        token_provider = provider_for_gis(gis, platform)
        logger.info(
            "Token acquired via %s (token present: %s).",
            type(token_provider).__name__, token_provider.is_present(),
        )

        policy = TrustedUrlPolicy(
            authenticated_base,
            args.target,
            trusted_origins=args.trusted_service_origin,
            anonymous_origins=args.anonymous_service_origin,
            logger=logger,
        )
        session_pool = WorkerSessionPool(
            mode,
            ca_path,
            initial_session=session,
            capacity=args.workers,
        )
        client = ArcGISRestClient(
            policy, session_pool, token_provider, gis, logger
        )

        excluded_ids = _resolve_exclusions_strict(
            gis, args.exclude_group_id, logger
        )
        discovery = DiscoveryResult()
        hosting_roots, federated_roots, servers_info, federation_errors = (
            build_server_lookup(
                client, authenticated_base, platform, token_provider, logger
            )
        )
        for message in federation_errors:
            discovery.add_error(
                message,
                catalog=True,
                tls=isinstance(message, RestCallError) and message.tls_error,
                auth=isinstance(message, RestCallError) and message.auth_error,
                transport=(
                    isinstance(message, RestCallError)
                    and message.transport_error
                ),
            )

        target_store = TargetStore(tempfile.gettempdir(), logger)
        result = scan_services(
            gis,
            client,
            platform,
            paths["broken"],
            per_run_limit,
            args.workers,
            bool(args.org_only),
            sources,
            logger,
            excluded_ids,
            target_store,
            discovery,
            hosting_roots,
            federated_roots,
            servers_info,
        )
        stats = result["stats"]
        broken_csv = result["broken_csv"]
        incomplete_errors = result["incomplete_errors"]

        incomplete = bool(
            discovery.error_count
            or stats.get("services_indeterminate", 0)
            or stats.get("tls_failures", 0)
            or stats.get("item_resolution_errors", 0)
            or stats.get("catalog_errors", 0)
            or stats.get("transport_failures", 0)
        )
        result_label = "INCOMPLETE" if incomplete else "COMPLETE"
        _log_run_summary(
            result_label, args, platform, sources, stats, broken_csv,
            incomplete_errors, logger,
        )

        timing = timing_block(start_wallclock, start_perf)
        body = _build_completion_body(
            result_label,
            platform,
            args.org_name,
            timestamp,
            args,
            sources,
            stats,
            broken_csv,
            timing,
            incomplete_errors,
        )
        attachments, note = gather_attachments(
            paths["log"],
            [broken_csv] if broken_csv else [],
            args.output_dir,
            logger,
            cap_mb=max_email_mb,
        )
        body += note
        mail_subject = (
            "[INCOMPLETE] " + subject
            if result_label == "INCOMPLETE" else subject
        )
        send_email(
            mail_subject,
            body,
            attachments,
            args.smtp_server,
            args.email_to,
            args.email_from,
            logger,
        )
        logger.info("%s", result_label)
        if incomplete:
            logger.warning(
                "Broken Service Scan completed with contained discovery/"
                "service error(s); returning exit status 0 because the "
                "scan ran to completion. The report remains marked "
                "INCOMPLETE."
            )
        else:
            logger.info(
                "Broken Service Scan completed; returning exit status 0."
            )
        return 0

    except Exception as exc:
        safe = redact_token(str(exc))
        logger.exception("Run ABORTED: %s", safe)
        try:
            _send_abort_email(
                subject, safe, paths["log"], args,
                start_wallclock, start_perf, logger,
            )
        except Exception as email_exc:
            logger.error(
                "Could not send abort notification: %s",
                redact_token(str(email_exc)),
            )
        return 1
    finally:
        if target_store is not None:
            target_store.close()
        if session_pool is not None:
            session_pool.close()


if __name__ == "__main__":
    sys.exit(main())
