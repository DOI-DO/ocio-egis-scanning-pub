#!/usr/bin/env python3
"""
PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py

Inventories orphaned hosted services (FeatureServer/MapServer with no
matching Portal item of the same type) on the Portal hosting server.
Portal-only. Always runs a full scan and always sends the completion email.

The ordinary inventory behavior is preserved: enumerate every FeatureServer
and MapServer exposed by the authenticated Portal's hosting server, compare it
to the complete local Portal catalog by service type, scan Web Map JSON for
references, perform the existing service data check, and preserve the original
orphan-candidate report set. Authoritative same-type serviceItemId matches are
removed; indeterminate checks retain the original row behavior but make the run
INCOMPLETE. Hardening is otherwise limited to catalog completeness, authoritative
hosting-server identity, token refresh, bounded concurrency, response
validation, conservative handling of indeterminate checks, and truthful run
status.

The hosting server may use public, administrative, and direct-server URLs on
hosts that are different from the Portal host. All such URLs reported by the
authenticated Portal federation registry are supported. Tokens are sent only
to HTTPS Portal/hosting destinations established by that registry.

Usage examples:
    python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml
    python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --output-dir "D:\\reports"
    python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --workers 24
    python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --tls-mode os-trust-store
"""

from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import json
import os
import queue
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests


# ---------------------------------------------------------------------------
# Project-package discovery. Verify origin before package code is imported.
# ---------------------------------------------------------------------------
def _add_utilities_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    probe = here
    while True:
        candidate = os.path.join(probe, "UTILITIES")
        init_file = os.path.join(candidate, "__init__.py")
        if os.path.isdir(candidate) and os.path.isfile(init_file):
            if probe not in sys.path:
                sys.path.insert(0, probe)
            spec = importlib.util.find_spec("UTILITIES")
            origin = getattr(spec, "origin", None)
            actual = (
                os.path.realpath(os.path.dirname(origin)) if origin else ""
            )
            expected = os.path.realpath(candidate)
            if actual != expected:
                raise ImportError(
                    "Refusing to load UTILITIES from an unexpected location: "
                    f"{actual!r}; expected {expected!r}."
                )
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            raise ModuleNotFoundError(
                "Could not locate the 'UTILITIES' package by walking up from "
                f"{here!r}."
            )
        probe = parent


_PROJECT_ROOT = _add_utilities_to_path()

from UTILITIES import (  # noqa: E402
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    epoch_to_str,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_config_arg,
    subject_line,
    finalize_config,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    get_max_email_mb,
    provider_for_gis,
)
from UTILITIES.secure_session import (  # noqa: E402
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES.auth import redact_sensitive_data  # noqa: E402


SCRIPT_NAME = "PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES"
SUBJECT_PREFIX = "Orphan Hosted Service Inventory"
VERSION = "1.11"
TARGET = "portal"

DEFAULT_WEBMAP_WORKERS = 16
REQUEST_TIMEOUT = 60
SEARCH_PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_PORTAL_ITEMS = 2_000_000
MAX_HOSTED_SERVICES = 2_000_000
MAX_HOSTING_FOLDERS = 100_000
MAX_FOLDER_DEPTH = 32
MAX_ERROR_SAMPLES = 50

_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
_ITEM_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_AUTH_CODES = {401, 403, 498, 499}
_REDIRECT_CODES = {301, 302, 303, 307, 308}

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

REQUIRED_KEYS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

CSV_FIELDS = [
    "ServiceName", "ServiceType", "service_url", "item_id",
    "date_created", "date_modified", "ReferencingPortalItems", "DataCheck",
]

logger = None


# ---------------------------------------------------------------------------
# Exceptions and run diagnostics
# ---------------------------------------------------------------------------
class ScanAbort(RuntimeError):
    """Required inventory boundary failed; no complete orphan claim is safe."""


class RestCallError(RuntimeError):
    """Sanitized REST failure carrying machine-readable classification."""

    def __init__(self, message, *, status_code=None, rest_code=None,
                 tls_error=False, auth_error=False, transient_error=False,
                 transport_error=False, policy_error=False):
        super().__init__(redact_sensitive_data(str(message)))
        self.status_code = status_code
        self.rest_code = rest_code
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.transient_error = bool(transient_error)
        self.transport_error = bool(transport_error)
        self.policy_error = bool(policy_error)


@dataclass
class RunDiagnostics:
    reasons: List[str] = field(default_factory=list)
    error_count: int = 0
    webmap_errors: int = 0
    data_check_errors: int = 0
    item_detail_errors: int = 0
    catalog_warnings: int = 0
    tls_errors: int = 0
    auth_errors: int = 0
    transport_errors: int = 0
    transient_errors: int = 0
    same_type_item_matches: int = 0
    indeterminate_associations: int = 0

    def add(self, message, *, webmap=False, data=False, item=False,
            catalog=False, exc=None):
        safe = redact_sensitive_data(str(message))
        self.error_count += 1
        if len(self.reasons) < MAX_ERROR_SAMPLES:
            self.reasons.append(safe)
        if webmap:
            self.webmap_errors += 1
        if data:
            self.data_check_errors += 1
        if item:
            self.item_detail_errors += 1
        if catalog:
            self.catalog_warnings += 1
        if isinstance(exc, RestCallError):
            if exc.tls_error:
                self.tls_errors += 1
            if exc.auth_error:
                self.auth_errors += 1
            if exc.transport_error:
                self.transport_errors += 1
            if exc.transient_error:
                self.transient_errors += 1

    @property
    def incomplete(self):
        return self.error_count > 0


# ---------------------------------------------------------------------------
# CLI and small utility helpers
# ---------------------------------------------------------------------------
def _worker_count(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("workers must be an integer") from exc
    if not 1 <= number <= 64:
        raise argparse.ArgumentTypeError("workers must be between 1 and 64")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog=f"{SCRIPT_NAME}.py",
        description=(
            "Inventory orphaned hosted services on the Portal hosting server "
            "and write a reviewed-then-deletable CSV. PORTAL ONLY. Always "
            "runs a full scan and always sends the completion email."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {VERSION}")
    add_config_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    parser.add_argument(
        "--workers", type=_worker_count, default=DEFAULT_WEBMAP_WORKERS,
        help=f"Concurrent workers for reading Web Map JSON "
             f"(default: {DEFAULT_WEBMAP_WORKERS}; valid range 1-64).",
    )

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES),
        default="standard",
        help="TLS trust strategy for both the direct REST session and the "
             "ArcGIS SDK connection. 'standard' uses the public CA bundle; "
             "'os-trust-store' uses the operating-system trust store; "
             "'custom-ca-bundle' uses --ca-bundle-path.",
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="PEM CA bundle path; required with --tls-mode custom-ca-bundle.",
    )
    args = parser.parse_args(argv)
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        parser.error(
            "--ca-bundle-path is required when --tls-mode is "
            "custom-ca-bundle."
        )
    args.target = TARGET
    return args


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off", ""}:
            return False
    return False


def _canonical_item_id(value):
    text = str(value or "").strip()
    return text.lower() if _ITEM_ID_RE.fullmatch(text) else None


def _normalize_server_id(value):
    text = str(value or "").strip().strip("{}")
    compact = text.replace("-", "")
    if re.fullmatch(r"[0-9A-Fa-f]{32}", compact):
        return compact.lower()
    return text if _SERVER_ID_RE.fullmatch(text) else None


def _safe_error(exc):
    return redact_sensitive_data(f"{type(exc).__name__}: {exc}")


def _reserve_run_paths(output_dir, org_prefix, timestamp):
    """Reserve one collision-safe suffix for the log and CSV as a pair."""
    for index in range(1000):
        suffix = "" if index == 0 else f"_{index:02d}"
        run_stamp = timestamp + suffix
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{SCRIPT_NAME}_{run_stamp}.log"
        )
        csv_path = os.path.join(
            output_dir, f"{org_prefix}_ORPHAN_HOSTED_SERVICE_{run_stamp}.csv"
        )
        if os.path.exists(csv_path):
            continue
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(log_path, flags, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(fd)
            return run_stamp, log_path, csv_path
    raise RuntimeError("Could not reserve a unique output name after 1000 attempts.")


# ---------------------------------------------------------------------------
# URL normalization and service identity
# ---------------------------------------------------------------------------
def _default_port(scheme):
    return 443 if scheme.lower() == "https" else 80


def _origin(parts):
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if not scheme or not host:
        raise RestCallError("URL has no valid scheme/hostname.", policy_error=True)
    try:
        port = parts.port or _default_port(scheme)
    except ValueError as exc:
        raise RestCallError(f"URL has an invalid port: {exc}", policy_error=True)
    return scheme, host, port


def _safe_path(parts):
    if parts.username is not None or parts.password is not None:
        raise RestCallError("URL contains user-info credentials.", policy_error=True)
    if parts.query or parts.fragment:
        raise RestCallError("URL contains a query string or fragment.", policy_error=True)
    raw = parts.path or "/"
    lowered = raw.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise RestCallError(
            "URL path contains an encoded slash or backslash.", policy_error=True
        )
    decoded = unquote(raw)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError("URL path contains unsafe characters.", policy_error=True)
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        raise RestCallError("URL path contains traversal.", policy_error=True)
    normalized = "/" + "/".join(segment for segment in raw.split("/") if segment)
    return normalized.rstrip("/") or "/"


def _split_url(raw, *, require_https=False):
    text = str(raw or "").strip()
    if not text:
        raise RestCallError("URL is blank.", policy_error=True)
    parts = urlsplit(text if "://" in text else f"https://{text}")
    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise RestCallError(
            f"Unsupported URL scheme {scheme!r}; expected HTTP or HTTPS.",
            policy_error=True,
        )
    if require_https and scheme != "https":
        raise RestCallError("Credential-bearing URL is not HTTPS.", policy_error=True)
    _origin(parts)
    _safe_path(parts)
    return parts


def _normalize_portal_base(raw):
    parts = _split_url(raw, require_https=True)
    path = _safe_path(parts)
    if path.lower().endswith("/home"):
        path = path[:-5] or "/"
    return urlunsplit(("https", parts.netloc, path.rstrip("/"), "", ""))


def _normalize_server_base(raw):
    parts = _split_url(raw)
    path = _safe_path(parts)
    lower = path.lower()
    marker = lower.find("/rest/services")
    if marker >= 0:
        path = path[:marker]
    elif lower.endswith("/admin"):
        path = path[:-6]
    elif lower.endswith("/rest"):
        path = path[:-5]
    path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))


def _rest_root(raw):
    base = _normalize_server_base(raw)
    parts = urlsplit(base)
    path = (parts.path.rstrip("/") + "/rest/services").replace("//", "/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _admin_root(raw):
    base = _normalize_server_base(raw)
    parts = urlsplit(base)
    if parts.scheme.lower() != "https":
        raise RestCallError(
            "Hosting administrative URL is not HTTPS; refusing to send the "
            "Portal token over plaintext HTTP.", policy_error=True
        )
    path = (parts.path.rstrip("/") + "/admin").replace("//", "/")
    return urlunsplit(("https", parts.netloc, path, "", ""))


def _path_segments(path):
    out = []
    for raw in str(path or "").split("/"):
        if not raw:
            continue
        value = unquote(raw)
        if "/" in value or "\\" in value or value in {".", ".."}:
            return []
        out.append(value)
    return out


def service_identity(url):
    """Return lower-case service-path/type identity, ignoring host and layer id."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url).strip() if "://" in str(url) else f"https://x/{url}")
        segments = _path_segments(parts.path)
    except Exception:
        return None
    lower = [segment.lower() for segment in segments]
    marker = None
    for index in range(len(lower) - 1):
        if lower[index] == "rest" and lower[index + 1] == "services":
            marker = index + 2
            break
    if marker is None:
        return None
    tail = lower[marker:]
    for index, segment in enumerate(tail):
        if segment in {"featureserver", "mapserver"}:
            return "/".join(tail[:index + 1])
    return None


def _root_parts(root):
    parts = _split_url(root)
    return _origin(parts), _safe_path(parts)


def _under_root(raw_url, root):
    try:
        parts = _split_url(raw_url)
        origin = _origin(parts)
        path = _safe_path(parts)
        root_origin, root_path = _root_parts(root)
    except RestCallError:
        return False
    if origin != root_origin:
        return False
    low_path = path.lower()
    low_root = root_path.lower()
    return low_path == low_root or low_path.startswith(low_root + "/")


@dataclass
class HostingContext:
    public_url: str
    public_rest_root: str
    admin_root: str
    admin_roots: List[str]
    service_roots: List[str]
    server_id: Optional[str]
    portal_proxy_root: Optional[str]
    name: str

    def service_key(self, url):
        if not url:
            return None
        if any(_under_root(url, root) for root in self.service_roots):
            return service_identity(url)
        if self.portal_proxy_root and _under_root(url, self.portal_proxy_root):
            return service_identity(url)
        return None

    def is_hosting_url(self, url):
        return bool(self.service_key(url))

    def ordered_request_roots(self):
        # Preserve the public URL for reporting, but prefer HTTPS federation
        # aliases for authenticated reads when the public URL is legacy HTTP.
        ordered = []
        for root in [self.public_rest_root] + list(self.service_roots):
            root = root.rstrip("/")
            if root not in ordered:
                ordered.append(root)
        https_roots = []
        other_roots = []
        for root in ordered:
            try:
                is_https = _split_url(root).scheme.lower() == "https"
            except RestCallError:
                is_https = False
            (https_roots if is_https else other_roots).append(root)
        if self.public_rest_root.rstrip("/") in https_roots:
            public = self.public_rest_root.rstrip("/")
            https_roots.remove(public)
            https_roots.insert(0, public)
        return https_roots + other_roots

    def service_request_urls(self, full_name, service_type):
        suffix = f"{str(full_name).strip('/')}/{service_type}"
        return [f"{root}/{suffix}" for root in self.ordered_request_roots()]


# ---------------------------------------------------------------------------
# Disk-backed Portal item catalog
# ---------------------------------------------------------------------------
class PortalItemStore:
    def __init__(self):
        fd, path = tempfile.mkstemp(prefix=".orphan_portal_items_", suffix=".sqlite")
        os.close(fd)
        self.path = path
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE items ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "item_id TEXT NOT NULL UNIQUE, item_type TEXT NOT NULL, "
            "payload TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX idx_items_type ON items(item_type)"
        )
        self.pending = 0
        self.count = 0

    def put(self, item):
        if not isinstance(item, dict):
            raise ScanAbort("Portal search returned a non-object item result.")
        item_id = _canonical_item_id(item.get("id"))
        if not item_id:
            raise ScanAbort("Portal search returned an item with an invalid id.")
        item_type = str(item.get("type") or "")
        payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO items(item_id,item_type,payload) VALUES (?,?,?)",
            (item_id, item_type, payload),
        )
        if cursor.rowcount == 1:
            self.count += 1
            if self.count > MAX_PORTAL_ITEMS:
                raise ScanAbort(
                    f"Portal catalog exceeded the {MAX_PORTAL_ITEMS:,}-item "
                    "safety ceiling; refusing to truncate."
                )
        else:
            self.connection.execute(
                "UPDATE items SET item_type=?, payload=? WHERE item_id=?",
                (item_type, payload, item_id),
            )
        self.pending += 1
        if self.pending >= 1000:
            self.connection.commit()
            self.pending = 0

    def __len__(self):
        return self.count

    def _flush(self):
        if self.pending:
            self.connection.commit()
            self.pending = 0

    def iter_all(self):
        self._flush()
        cursor = self.connection.execute(
            "SELECT payload FROM items ORDER BY seq"
        )
        for (payload,) in cursor:
            yield json.loads(payload)

    def iter_type(self, item_type):
        self._flush()
        cursor = self.connection.execute(
            "SELECT payload FROM items WHERE item_type=? ORDER BY seq",
            (item_type,),
        )
        for (payload,) in cursor:
            yield json.loads(payload)

    def count_type(self, item_type):
        self._flush()
        row = self.connection.execute(
            "SELECT COUNT(*) FROM items WHERE item_type=?", (item_type,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get(self, item_id):
        canonical = _canonical_item_id(item_id)
        if not canonical:
            return None
        self._flush()
        row = self.connection.execute(
            "SELECT payload FROM items WHERE item_id=?", (canonical,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        try:
            self.connection.close()
        finally:
            try:
                os.remove(self.path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# TLS-consistent sessions and strict REST client
# ---------------------------------------------------------------------------
class WorkerSessionPool:
    """Pre-create one Requests session per worker; lend one per active task."""

    def __init__(self, mode, ca_bundle_path, workers):
        self.sessions = []
        self.available = queue.Queue()
        try:
            for _ in range(workers):
                session = build_secure_session(
                    mode,
                    ca_bundle_path=ca_bundle_path,
                    pool_connections=2,
                    pool_maxsize=4,
                    retry_total=2,
                    logger=None,
                )
                self.sessions.append(session)
                self.available.put(session)
        except Exception:
            self.close()
            raise

    @contextmanager
    def borrow(self):
        session = self.available.get()
        try:
            yield session
        finally:
            self.available.put(session)

    def close(self):
        for session in self.sessions:
            try:
                session.close()
            except Exception:
                pass
        self.sessions.clear()


class TrustedUrlPolicy:
    """Allow Portal and authenticated hosting URLs, even on different hosts."""

    def __init__(self, portal_base):
        self.portal_base = _normalize_portal_base(portal_base)
        parts = _split_url(self.portal_base, require_https=True)
        self.portal_origin = _origin(parts)
        self.portal_path = _safe_path(parts).rstrip("/")
        self.admin_roots = []
        self.service_roots = []

    def add_admin_root(self, root):
        normalized = _admin_root(root)
        if normalized not in self.admin_roots:
            self.admin_roots.append(normalized)
        return normalized

    def add_service_root(self, root):
        normalized = _rest_root(root)
        if normalized not in self.service_roots:
            self.service_roots.append(normalized)
        return normalized

    def portal_url(self, relative):
        if not isinstance(relative, str) or not relative.startswith("/"):
            raise RestCallError("Portal API path must be absolute.", policy_error=True)
        if not (
            relative.startswith("/sharing/rest/")
            or relative.startswith("/portaladmin/")
        ):
            raise RestCallError(
                "Portal API path is outside /sharing/rest or /portaladmin.",
                policy_error=True,
            )
        candidate = self.portal_base + relative
        return self.validate_portal(candidate)

    def validate_portal(self, raw):
        parts = _split_url(raw, require_https=True)
        if _origin(parts) != self.portal_origin:
            raise RestCallError("Portal redirect changed origin.", policy_error=True)
        path = _safe_path(parts)
        base = self.portal_path.lower()
        lower = path.lower()
        if not lower.startswith(base + "/"):
            raise RestCallError("Portal URL left the configured Portal path.", policy_error=True)
        relative = path[len(self.portal_path):]
        if not (
            relative.lower().startswith("/sharing/rest/")
            or relative.lower().startswith("/portaladmin/")
        ):
            raise RestCallError("Portal URL left approved API paths.", policy_error=True)
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def validate_admin(self, raw):
        parts = _split_url(raw, require_https=True)
        path = _safe_path(parts)
        candidate = urlunsplit(("https", parts.netloc, path, "", ""))
        if not any(_under_root(candidate, root) for root in self.admin_roots):
            raise RestCallError(
                "Hosting admin URL is outside the authenticated federation record.",
                policy_error=True,
            )
        return candidate

    def service_access(self, raw):
        parts = _split_url(raw)
        path = _safe_path(parts)
        candidate = urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))
        if not any(_under_root(candidate, root) for root in self.service_roots):
            raise RestCallError(
                "Hosting service URL is outside the authenticated hosting-server "
                "federation record.", policy_error=True
            )
        mode = "token" if parts.scheme.lower() == "https" else "anonymous"
        return candidate, mode

    def validate_service_redirect(self, raw, expected_mode):
        candidate, mode = self.service_access(raw)
        if expected_mode == "anonymous":
            return candidate, "anonymous"
        if mode != "token":
            raise RestCallError(
                "Token-bearing service redirect would downgrade to plaintext HTTP.",
                policy_error=True,
            )
        return candidate, "token"


class ArcGISRestClient:
    def __init__(self, policy, session, token_provider, gis):
        self.policy = policy
        self.session = session
        self.token_provider = token_provider
        self.gis = gis
        self.refresh_lock = Lock()

    def portal_json(self, relative, params=None, *, session=None,
                    timeout=REQUEST_TIMEOUT):
        url = self.policy.portal_url(relative)
        return self._request(
            "GET", url, params=params, session=session, timeout=timeout,
            auth_mode="token",
            redirect_validator=lambda candidate, _mode: (
                self.policy.validate_portal(candidate), "token"
            ),
        )

    def admin_json(self, url, data=None, *, session=None,
                   timeout=REQUEST_TIMEOUT):
        safe = self.policy.validate_admin(url)
        return self._request(
            "POST", safe, data=data, session=session, timeout=timeout,
            auth_mode="token",
            redirect_validator=lambda candidate, _mode: (
                self.policy.validate_admin(candidate), "token"
            ),
        )

    def service_json(self, url, params=None, *, session=None,
                     timeout=REQUEST_TIMEOUT):
        safe, mode = self.policy.service_access(url)
        return self._request(
            "GET", safe, params=params, session=session, timeout=timeout,
            auth_mode=mode,
            redirect_validator=self.policy.validate_service_redirect,
        )

    def _refresh_token(self, failed_token):
        with self.refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return current
            try:
                properties = getattr(self.gis, "properties", None)
                if properties is not None and hasattr(properties, "get"):
                    properties.get("id")
            except Exception:
                pass
            self.token_provider.refresh()
            return self.token_provider.get()

    def _request(self, method, url, *, params=None, data=None, session=None,
                 timeout=REQUEST_TIMEOUT, auth_mode="token",
                 redirect_validator=None):
        session = session or self.session
        refreshed = False
        current_url = url
        current_mode = auth_mode
        redirects = 0

        while True:
            token = self.token_provider.get() if current_mode == "token" else None
            query = dict(params or {})
            body = dict(data or {})
            query.pop("token", None)
            body.pop("token", None)
            query.pop("f", None)
            body.pop("f", None)
            if method.upper() == "GET":
                query["f"] = "json"
                if token:
                    query["token"] = token
            else:
                body["f"] = "json"
                if token:
                    body["token"] = token

            try:
                response = session.request(
                    method,
                    current_url,
                    params=query if method.upper() == "GET" else None,
                    data=body if method.upper() != "GET" else None,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED for {current_url}: {exc}",
                    tls_error=True, transport_error=True,
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise RestCallError(
                    f"Request timed out after {timeout}s for {current_url}",
                    transient_error=True, transport_error=True,
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                raise RestCallError(
                    f"Connection failed for {current_url}: {exc}",
                    transient_error=True, transport_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(
                    f"Request failed for {current_url}: {exc}",
                    transport_error=True,
                ) from exc

            try:
                status = int(response.status_code)
                if status in _REDIRECT_CODES:
                    location = response.headers.get("Location")
                    if not location:
                        raise RestCallError(
                            f"HTTP {status} redirect from {current_url} had no Location."
                        )
                    if redirects >= MAX_REDIRECTS:
                        raise RestCallError(
                            f"Too many redirects while requesting {url}."
                        )
                    destination = urljoin(current_url, location)
                    if redirect_validator is None:
                        raise RestCallError("Redirects are not permitted for this request.")
                    current_url, validated_mode = redirect_validator(
                        destination, current_mode
                    )
                    # An anonymous request is never allowed to gain credentials.
                    current_mode = (
                        "anonymous" if current_mode == "anonymous" else validated_mode
                    )
                    redirects += 1
                    continue

                # HTTP authentication failures are actionable even when a
                # proxy returns HTML instead of ArcGIS JSON. Refresh before
                # attempting to parse that body, then retry exactly once.
                if (status in _AUTH_CODES and current_mode == "token"
                        and not refreshed):
                    self._refresh_token(token)
                    refreshed = True
                    continue

                try:
                    data_obj = _read_json_response(response, current_url)
                except RestCallError as parse_error:
                    if status >= 400:
                        raise RestCallError(
                            f"{current_url}: HTTP {status} "
                            f"{getattr(response, 'reason', '')}; "
                            f"response body was not valid JSON ({parse_error})",
                            status_code=status,
                            auth_error=(status in _AUTH_CODES),
                            transient_error=(status == 429 or status >= 500),
                        ) from parse_error
                    raise

                rest_code = _rest_code(data_obj)
                auth_failure = status in _AUTH_CODES or rest_code in _AUTH_CODES
                if auth_failure and current_mode == "token" and not refreshed:
                    self._refresh_token(token)
                    refreshed = True
                    continue

                if status >= 400:
                    message = _rest_error_text(data_obj) if rest_code is not None else (
                        f"HTTP {status} {getattr(response, 'reason', '')}".strip()
                    )
                    raise RestCallError(
                        f"{current_url}: {message}",
                        status_code=status,
                        rest_code=rest_code,
                        auth_error=auth_failure,
                        transient_error=(status == 429 or status >= 500),
                    )
                if rest_code is not None:
                    raise RestCallError(
                        f"{current_url}: {_rest_error_text(data_obj)}",
                        rest_code=rest_code,
                        auth_error=auth_failure,
                        transient_error=(rest_code == 429 or rest_code >= 500),
                    )
                if not isinstance(data_obj, dict):
                    raise RestCallError(
                        f"Expected a JSON object from {current_url}; got "
                        f"{type(data_obj).__name__}."
                    )
                return data_obj
            finally:
                try:
                    response.close()
                except Exception:
                    pass


def _read_json_response(response, url):
    content = bytearray()
    try:
        iterator = response.iter_content(chunk_size=64 * 1024)
    except AttributeError:
        iterator = [getattr(response, "content", b"")]
    for chunk in iterator:
        if not chunk:
            continue
        content.extend(chunk)
        if len(content) > MAX_JSON_BYTES:
            raise RestCallError(
                f"JSON response from {url} exceeded {MAX_JSON_BYTES // (1024 * 1024)} MiB."
            )
    encoding = getattr(response, "encoding", None) or "utf-8"
    try:
        text = bytes(content).decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        text = bytes(content).decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError as exc:
        snippet = text[:300].replace("\r", " ").replace("\n", " ")
        raise RestCallError(
            redact_sensitive_data(
                f"Non-JSON response from {url} (HTTP {response.status_code}; "
                f"Content-Type={response.headers.get('Content-Type', '')!r}; "
                f"first 300 chars={snippet!r})"
            )
        ) from exc


def _rest_code(data):
    if not isinstance(data, dict) or "error" not in data:
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return -1
    try:
        return int(error.get("code"))
    except (TypeError, ValueError):
        return -1


def _rest_error_text(data):
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return "unknown ArcGIS REST error"
    code = err.get("code", "?")
    message = err.get("message", "unknown error")
    details_value = err.get("details") or []
    if isinstance(details_value, (str, bytes)):
        details_value = [details_value]
    details = "; ".join(str(value) for value in details_value)
    text = f"REST error {code}: {message}"
    if details:
        text += f" ({details})"
    return redact_sensitive_data(text)


# Compatibility helpers retain the original call shapes for imported callers.
def _response_json(response, url):
    data = _read_json_response(response, url)
    if int(response.status_code) >= 400:
        raise RestCallError(
            f"{url}: HTTP {response.status_code} {getattr(response, 'reason', '')}"
        )
    return data


def make_admin_get(session, token_provider, client=None):
    def admin_get(url):
        if client is not None:
            return client.admin_json(url)
        response = session.post(
            url, data={"f": "json", **token_provider.auth_params()},
            timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True,
        )
        try:
            data = _response_json(response, url)
        finally:
            response.close()
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"{url}: {_rest_error_text(data)}")
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object from {url}.")
        return data
    return admin_get


def make_rest_get(session, token_provider, client=None):
    def rest_get(url, params=None):
        if client is not None:
            if _under_root(url, client.policy.portal_base):
                base = client.policy.portal_base
                relative = _safe_path(urlsplit(url))
                portal_path = client.policy.portal_path
                relative = relative[len(portal_path):]
                return client.portal_json(relative, params=params)
            return client.service_json(url, params=params)
        merged = {"f": "json"}
        if params:
            merged.update(params)
        merged.update(token_provider.auth_params())
        response = session.get(
            url, params=merged, timeout=REQUEST_TIMEOUT,
            allow_redirects=False, stream=True,
        )
        try:
            return _response_json(response, url)
        finally:
            response.close()
    return rest_get


# ---------------------------------------------------------------------------
# Strict Portal catalog enumeration
# ---------------------------------------------------------------------------
def _search_results(data):
    if "results" in data:
        value = data.get("results")
    elif "items" in data:
        value = data.get("items")
    else:
        raise ScanAbort("Portal search response contained no results array.")
    if not isinstance(value, list):
        raise ScanAbort("Portal search returned a malformed results array.")
    return value


def _strict_search_portal_items(client, query, cap):
    results = []
    seen_ids = set()
    start = 1
    expected_total = None
    pages = 0
    while len(results) < cap:
        pages += 1
        if pages > cap + 2:
            raise ScanAbort("Portal item search exceeded its pagination safety bound.")
        num = min(SEARCH_PAGE_SIZE, cap - len(results))
        data = client.portal_json(
            "/sharing/rest/search",
            params={
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": num,
            },
        )
        batch = _search_results(data)
        try:
            total = int(data.get("total"))
        except (TypeError, ValueError) as exc:
            raise ScanAbort("Portal search returned an invalid total.") from exc
        if total < 0:
            raise ScanAbort("Portal search returned a negative total.")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise ScanAbort(
                f"Portal search total changed during paging ({expected_total} -> {total})."
            )
        target = min(expected_total, cap)
        if not batch:
            if len(results) < target:
                raise ScanAbort(
                    "Portal search returned an empty page before its declared total."
                )
            break
        if len(batch) > num:
            raise ScanAbort(
                f"Portal search returned {len(batch)} rows after requesting {num}."
            )
        for item in batch:
            if not isinstance(item, dict):
                raise ScanAbort("Portal search returned a non-object item result.")
            item_id = _canonical_item_id(item.get("id"))
            if not item_id:
                raise ScanAbort("Portal search returned an item with an invalid id.")
            if item_id in seen_ids:
                raise ScanAbort(
                    f"Portal search repeated item id {item_id} while paging; "
                    "catalog completeness cannot be established."
                )
            seen_ids.add(item_id)
            results.append(item)
            if len(results) >= cap:
                break
        if len(results) >= target or len(results) >= cap:
            break
        raw_next = data.get("nextStart")
        try:
            next_start = int(raw_next)
        except (TypeError, ValueError) as exc:
            raise ScanAbort("Portal search returned an invalid nextStart.") from exc
        if next_start <= 0:
            raise ScanAbort(
                "Portal search terminated before its declared total was read."
            )
        if next_start <= start:
            raise ScanAbort(
                f"Portal search made no progress: start={start}, nextStart={next_start}."
            )
        start = next_start
    return results[:cap]


def search_portal_items(session, portal_url, token_provider, query, cap,
                        client=None):
    """Backward-compatible entry point; strict client used by the CLI path."""
    if client is not None:
        return _strict_search_portal_items(client, query, cap)
    search_url = portal_url.rstrip("/") + "/sharing/rest/search"
    results = []
    start = 1
    while len(results) < cap:
        num = min(SEARCH_PAGE_SIZE, cap - len(results))
        merged = {
            "f": "json", "q": query, "sortField": "modified",
            "sortOrder": "desc", "start": start, "num": num,
        }
        merged.update(token_provider.auth_params())
        response = session.get(
            search_url, params=merged, timeout=REQUEST_TIMEOUT,
            allow_redirects=False, stream=True,
        )
        try:
            data = _response_json(response, search_url)
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object from {search_url}.")
        if "error" in data:
            raise RuntimeError(_rest_error_text(data))
        batch = _search_results(data)
        if not batch:
            break
        results.extend(batch)
        raw_next = data.get("nextStart", -1)
        try:
            next_start = int(raw_next)
        except (TypeError, ValueError):
            break
        if next_start <= 0:
            break
        if next_start <= start:
            raise RuntimeError(
                f"Search pagination made no progress: start={start}, "
                f"nextStart={next_start}."
            )
        start = next_start
    return results[:cap]


def _strict_bucket_catalog(client, store, log):
    stats = {"requests": 0}
    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)

    def search_range(lo_ms, hi_ms):
        query = f"created:[{lo_ms} TO {hi_ms}]"
        stats["requests"] += 1
        results = _strict_search_portal_items(client, query, SEARCH_CAP)
        if len(results) >= SEARCH_CAP:
            if hi_ms <= lo_ms:
                raise ScanAbort(
                    f"Portal item bucket [{lo_ms} TO {hi_ms}] reached the "
                    f"{SEARCH_CAP:,}-item cap and cannot be subdivided; the "
                    "catalog cannot be proven complete."
                )
            middle = lo_ms + (hi_ms - lo_ms) // 2
            # For a two-millisecond inclusive range middle == lo_ms. That is
            # correct: recurse into the two one-millisecond leaves. Advancing
            # middle to lo+1 would resubmit the unchanged parent range forever.
            search_range(lo_ms, middle)
            search_range(middle + 1, hi_ms)
            return
        for item in results:
            store.put(item)
        if stats["requests"] % 20 == 0:
            log.info(
                "  %d strict catalog request(s), %d unique Portal item(s) so far.",
                stats["requests"], len(store),
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

    log.info(
        "Strict Portal catalog discovery complete: %d request(s), %d unique item(s).",
        stats["requests"], len(store),
    )
    return stats


# ---------------------------------------------------------------------------
# Authenticated Portal/hosting-server boundaries
# ---------------------------------------------------------------------------
def _authenticated_portal_url(gis, fallback):
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    return _normalize_portal_base(raw or fallback)


def _collect_self_properties(gis, client):
    data = {}
    endpoint_error = None
    try:
        candidate = client.portal_json("/sharing/rest/community/self")
        if isinstance(candidate, dict):
            data = candidate
        else:
            endpoint_error = "authenticated user endpoint returned malformed data"
    except Exception as exc:
        endpoint_error = _safe_error(exc)

    role = str(data.get("role") or "").strip().lower()
    privileges = data.get("privileges") or []
    if isinstance(privileges, str):
        privileges = [privileges]
    if not isinstance(privileges, (list, tuple, set)):
        privileges = []

    try:
        me = gis.users.me
    except Exception:
        me = None
    if me is not None:
        role = role or str(getattr(me, "role", "") or "").strip().lower()
        sdk_privileges = getattr(me, "privileges", None) or []
        if isinstance(sdk_privileges, str):
            sdk_privileges = [sdk_privileges]
        if isinstance(sdk_privileges, (list, tuple, set)):
            privileges = list(privileges) + list(sdk_privileges)

    privilege_set = {str(value).strip().lower() for value in privileges}
    standard_admin = role in {
        "org_admin", "account_admin", "administrator", "admin"
    }
    custom_view = "portal:admin:viewitems" in privilege_set
    if not (standard_admin or custom_view):
        detail = f" User-endpoint detail: {endpoint_error}." if endpoint_error else ""
        raise ScanAbort(
            "The authenticated account cannot be proven to have organization-wide "
            "item visibility. A standard organization administrator or a custom "
            "role with portal:admin:viewItems is required before orphan services "
            "can be classified safely." + detail
        )
    return data


def _hosting_context_from_federation(client, portal_url, log):
    data = client.portal_json("/portaladmin/federation/servers")
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise ScanAbort("Federation response contained no valid servers list.")
    hosting_records = []
    for server in servers:
        if not isinstance(server, dict):
            raise ScanAbort("Federation response contained a non-object server record.")
        role = str(server.get("serverRole") or "").strip().upper()
        if role == "HOSTING_SERVER" or _parse_bool(server.get("isHosted")):
            hosting_records.append(server)
    if not hosting_records:
        raise ScanAbort("No HOSTING_SERVER was found in the federation registry.")
    if len(hosting_records) > 1:
        distinct = {
            str(record.get("id") or record.get("url") or record.get("serverUrl") or "")
            for record in hosting_records
        }
        if len(distinct) > 1:
            raise ScanAbort(
                "The federation registry reported multiple distinct hosting servers; "
                "the inventory cannot choose one safely."
            )
    hosting = hosting_records[0]

    public_raw = ""
    public_base = ""
    public_rest = ""
    public_errors = []
    for field_name in ("url", "serverUrl", "adminUrl"):
        candidate = str(hosting.get(field_name) or "").strip()
        if not candidate:
            continue
        try:
            public_base = _normalize_server_base(candidate)
            public_rest = _rest_root(candidate)
            public_raw = candidate
            break
        except RestCallError as exc:
            public_errors.append(f"{field_name}: {exc}")
    if not public_raw:
        raise ScanAbort(
            "The hosting-server federation record has no usable public/server "
            "URL" + (f" ({'; '.join(public_errors)})" if public_errors else ".")
        )

    admin_roots = []
    admin_errors = []
    for field_name in ("adminUrl", "serverUrl", "url"):
        candidate = str(hosting.get(field_name) or "").strip()
        if not candidate:
            continue
        try:
            candidate_root = client.policy.add_admin_root(candidate)
        except RestCallError as exc:
            admin_errors.append(f"{field_name}: {exc}")
            continue
        if candidate_root not in admin_roots:
            admin_roots.append(candidate_root)
    if not admin_roots:
        raise ScanAbort(
            "The hosting-server federation record has no usable HTTPS admin "
            "URL" + (f" ({'; '.join(admin_errors)})" if admin_errors else ".")
        )
    admin_root = admin_roots[0]

    service_roots = []
    for field_name in ("url", "serverUrl", "adminUrl"):
        candidate = str(hosting.get(field_name) or "").strip()
        if not candidate:
            continue
        try:
            root = client.policy.add_service_root(candidate)
        except RestCallError as exc:
            log.warning(
                "Ignoring unusable hosting federation %s %r: %s",
                field_name, candidate, exc,
            )
            continue
        if root not in service_roots:
            service_roots.append(root)
    if public_rest not in service_roots:
        service_roots.insert(0, client.policy.add_service_root(public_raw))

    raw_server_id = hosting.get("id") or hosting.get("serverId")
    server_id = _normalize_server_id(raw_server_id)
    if raw_server_id and not server_id:
        raise ScanAbort(
            f"The hosting federation record supplied an unsafe server id "
            f"{raw_server_id!r}."
        )
    proxy_root = None
    if server_id:
        proxy_root = (
            portal_url.rstrip("/")
            + f"/sharing/servers/{server_id}/rest/services"
        )

    context = HostingContext(
        public_url=public_base,
        public_rest_root=public_rest,
        admin_root=admin_root,
        admin_roots=admin_roots,
        service_roots=service_roots,
        server_id=server_id,
        portal_proxy_root=proxy_root,
        name=str(hosting.get("name") or public_raw),
    )
    log.info("Hosting server public URL: %s", public_base)
    log.info("Hosting server admin URL candidate(s): %s", admin_roots)
    log.info("Hosting service root(s):              %s", service_roots)
    if proxy_root:
        log.info("Hosting Portal proxy root: %s", proxy_root)
    return context


# ---------------------------------------------------------------------------
# Hosting catalog and Portal item indexes
# ---------------------------------------------------------------------------
def _catalog_list(value, label):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScanAbort(f"Hosting catalog returned malformed {label}.")
    return value


def enumerate_hosted_services(client, hosting, log):
    services = []
    seen_folders = set()

    # Federation deployments commonly expose public, direct-server, and admin
    # aliases on different hosts. Try every authenticated HTTPS admin candidate
    # before declaring the hosting catalog unavailable.
    root_data = None
    root_errors = []
    for candidate_root in hosting.admin_roots:
        try:
            root_data = client.admin_json(candidate_root + "/services")
        except Exception as exc:
            root_errors.append(f"{candidate_root}: {_safe_error(exc)}")
            log.warning("Hosting admin candidate failed: %s (%s)",
                        candidate_root, _safe_error(exc))
            continue
        hosting.admin_root = candidate_root
        log.info("Selected reachable hosting admin URL: %s", candidate_root)
        break
    if root_data is None:
        raise ScanAbort(
            "Could not enumerate the hosting catalog through any federation "
            "admin URL. " + " | ".join(root_errors)
        )

    def scan_folder(folder="", depth=0, prefetched=None):
        if depth > MAX_FOLDER_DEPTH:
            raise ScanAbort(
                f"Hosting catalog exceeded the {MAX_FOLDER_DEPTH}-level folder "
                "depth safety ceiling."
            )
        normalized = str(folder or "").strip("/")
        folder_key = normalized.lower()
        if folder_key in seen_folders:
            return
        seen_folders.add(folder_key)
        if len(seen_folders) > MAX_HOSTING_FOLDERS:
            raise ScanAbort(
                f"Hosting catalog exceeded the {MAX_HOSTING_FOLDERS:,}-folder "
                "safety ceiling."
            )

        url = hosting.admin_root + "/services"
        if normalized:
            encoded = "/".join(quote(segment, safe="") for segment in normalized.split("/"))
            url += "/" + encoded
        data = prefetched if prefetched is not None else client.admin_json(url)

        for service in _catalog_list(data.get("services"), "services list"):
            if not isinstance(service, dict):
                raise ScanAbort("Hosting catalog returned a non-object service record.")
            service_type = str(service.get("type") or "")
            if service_type not in {"FeatureServer", "MapServer"}:
                continue
            service_name = str(service.get("serviceName") or "").strip()
            if not service_name:
                raise ScanAbort(
                    "Hosting catalog returned a FeatureServer/MapServer without a name."
                )
            folder_prefix = f"{normalized}/" if normalized else ""
            full_name = f"{folder_prefix}{service_name}"
            public_url = f"{hosting.public_rest_root}/{full_name}/{service_type}"
            request_urls = hosting.service_request_urls(full_name, service_type)
            key = service_identity(public_url)
            if not key:
                raise ScanAbort(
                    f"Could not derive a service identity for hosting catalog entry "
                    f"{full_name!r} ({service_type})."
                )
            services.append(
                (full_name, public_url, request_urls, service_type, key)
            )
            if len(services) > MAX_HOSTED_SERVICES:
                raise ScanAbort(
                    f"Hosting catalog exceeded the {MAX_HOSTED_SERVICES:,}-service "
                    "safety ceiling."
                )

        for child in _catalog_list(data.get("folders"), "folders list"):
            if not isinstance(child, str) or not child.strip():
                raise ScanAbort("Hosting catalog returned an invalid folder name.")
            child = child.strip("/")
            if child in {"System", "Utilities"}:
                continue
            if (normalized and
                    child.lower().startswith(normalized.lower() + "/")):
                # Some Server builds report a full folder path; the original
                # scanner passed that through directly.
                child_path = child
            else:
                child_path = f"{normalized}/{child}" if normalized else child
            scan_folder(child_path, depth + 1)

    scan_folder(prefetched=root_data)
    n_feature = sum(
        1 for _name, _public, _requests, kind, _key in services
        if kind == "FeatureServer"
    )
    n_map = sum(
        1 for _name, _public, _requests, kind, _key in services
        if kind == "MapServer"
    )
    log.info(
        "Hosted services found: %d (FeatureServer=%d, MapServer=%d)",
        len(services), n_feature, n_map,
    )
    return services


def _item_label(item):
    return (
        f"{item.get('title', '')} ({item.get('id', '')}) "
        f"[{item.get('type', '')}]"
    )


def _has_hosted_service_keyword(item):
    keywords = item.get("typeKeywords") if isinstance(item, dict) else None
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, (list, tuple, set)):
        return False
    return any(
        str(value or "").strip().lower() == "hosted service"
        for value in keywords
    )


def build_portal_indexes(store, hosting, diagnostics, log):
    # "hosting" keys are authoritative direct/proxy hosting URLs (plus the
    # ArcGIS Hosted Service keyword, which is how the original script recognized
    # aliases). "any" keys preserve the original host-independent service-path
    # index for reference reporting and for detecting an ambiguous same-name
    # service on another site.
    feature_hosting_keys = set()
    map_hosting_keys = set()
    feature_any_keys = set()
    map_any_keys = set()
    all_item_index: Dict[str, List[str]] = {}
    for item in store.iter_all():
        item_type = str(item.get("type") or "")
        url = item.get("url")
        any_key = service_identity(url)
        hosting_key = hosting.service_key(url)
        if any_key:
            # Preserve the original report's reference behavior: labels from
            # any Portal item with the same service path/type are shown, even
            # when the item uses an alternate URL alias.
            all_item_index.setdefault(any_key, []).append(_item_label(item))
            if item_type == "Feature Service":
                feature_any_keys.add(any_key)
            elif item_type == "Map Service":
                map_any_keys.add(any_key)

        authoritative_key = hosting_key
        if authoritative_key is None and _has_hosted_service_keyword(item):
            # A Hosted Service item may use a reverse-proxy alias not present in
            # every federation URL field. Preserve the original successful-path
            # match while serviceItemId verification handles ambiguous cases.
            authoritative_key = any_key

        if authoritative_key:
            if item_type == "Feature Service":
                feature_hosting_keys.add(authoritative_key)
            elif item_type == "Map Service":
                map_hosting_keys.add(authoritative_key)
        # The hosting catalog also has valid VectorTileServer, SceneServer,
        # ImageServer, and other endpoint types. They are outside this scanner's
        # FeatureServer/MapServer scope and must not make the run incomplete.
        elif (
            item_type in {"Feature Service", "Map Service"}
            and url
            and any(_under_root(url, root) for root in hosting.service_roots)
        ):
            diagnostics.add(
                f"Portal item {item.get('id')} [{item_type}] "
                f"{item.get('title', '')!r} points at the hosting server but "
                "its FeatureServer/MapServer URL could not be parsed: "
                f"{redact_sensitive_data(str(url))}",
                catalog=True,
            )

    log.info(
        "Feature Service items authoritatively matched to hosting: %d",
        len(feature_hosting_keys),
    )
    log.info(
        "Map Service items authoritatively matched to hosting:     %d",
        len(map_hosting_keys),
    )
    log.info("Web Map items in Portal:                             %d",
             store.count_type("Web Map"))
    return {
        "feature_hosting_keys": feature_hosting_keys,
        "map_hosting_keys": map_hosting_keys,
        "feature_any_keys": feature_any_keys,
        "map_any_keys": map_any_keys,
        "all_item_index": all_item_index,
    }


# ---------------------------------------------------------------------------
# Web Map reference scan
# ---------------------------------------------------------------------------
def iter_webmap_layers(data):
    if not isinstance(data, dict):
        return

    def walk(layers):
        for layer in layers or []:
            if not isinstance(layer, dict):
                continue
            yield layer
            yield from walk(layer.get("layers"))

    yield from walk(data.get("operationalLayers"))
    base_map = data.get("baseMap") or {}
    if isinstance(base_map, dict):
        yield from walk(base_map.get("baseMapLayers"))


def _bounded_results(executor, iterable, function, max_pending):
    iterator = iter(iterable)
    pending = set()

    def fill():
        while len(pending) < max_pending:
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending.add(executor.submit(function, item))

    fill()
    while pending:
        done, pending_remaining = wait(pending, return_when=FIRST_COMPLETED)
        pending = set(pending_remaining)
        for future in done:
            yield future.result()
        fill()


def scan_webmap_references(store, client, session_pool, workers,
                           diagnostics, log):
    total = store.count_type("Web Map")
    index: Dict[str, List[str]] = {}
    if not total:
        return index

    log.info(
        "Scanning %d Web Maps for embedded hosting-service URLs with %d worker(s) ...",
        total, workers,
    )

    def one(item):
        item_id = _canonical_item_id(item.get("id"))
        title = str(item.get("title") or "")
        if not item_id:
            return [], f"Web Map {item.get('id')!r} has an invalid item id."
        path = f"/sharing/rest/content/items/{quote(item_id, safe='')}/data"
        try:
            with session_pool.borrow() as worker_session:
                data = client.portal_json(path, session=worker_session)
        except Exception as exc:
            return [], (
                f"Could not read Web Map {title!r} ({item_id}): {_safe_error(exc)}"
            )
        label = f"{title} ({item_id}) [Web Map]"
        hits = []
        for layer in iter_webmap_layers(data):
            key = service_identity(layer.get("url"))
            if key:
                hits.append((key, label))
        return hits, None

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for hits, error in _bounded_results(
            executor, store.iter_type("Web Map"), one, max(1, workers * 2)
        ):
            done += 1
            if error:
                diagnostics.add(error, webmap=True)
            for key, label in hits:
                index.setdefault(key, []).append(label)
            if done % 100 == 0 or done == total:
                log.info("  Web Map scan progress: %d / %d read.", done, total)
    return index


# ---------------------------------------------------------------------------
# Orphan verification and data check
# ---------------------------------------------------------------------------
def _raw_service_item_id(info):
    if not isinstance(info, dict):
        return None
    properties = info.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    portal_item = info.get("portalItem")
    if not isinstance(portal_item, dict):
        portal_item = {}
    return (
        info.get("serviceItemId")
        or properties.get("serviceItemId")
        or portal_item.get("id")
    )


def _service_item_id(info):
    return _canonical_item_id(_raw_service_item_id(info))


def _item_dates_from_dict(item):
    if not isinstance(item, dict):
        return "", ""
    return (
        epoch_to_str(item.get("created"), fmt="%Y/%m/%d", utc=True),
        epoch_to_str(item.get("modified"), fmt="%Y/%m/%d", utc=True),
    )


def _get_portal_item(client, item_id):
    canonical = _canonical_item_id(item_id)
    if not canonical:
        raise RestCallError("Service returned an invalid serviceItemId.")
    path = f"/sharing/rest/content/items/{quote(canonical, safe='')}"
    try:
        data = client.portal_json(path)
    except RestCallError as exc:
        code = exc.rest_code if exc.rest_code is not None else exc.status_code
        if code in {400, 404}:
            return None, True
        raise
    returned = _canonical_item_id(data.get("id"))
    if returned != canonical:
        raise RestCallError(
            f"Portal item endpoint returned id {data.get('id')!r} instead of {canonical}."
        )
    return data, False


@dataclass
class CandidateResolution:
    keep_as_orphan: bool
    item_id: str = ""
    date_created: str = ""
    date_modified: str = ""
    root_info: Optional[dict] = None
    root_url: str = ""
    root_error: Optional[Exception] = None
    association_indeterminate: bool = False


def resolve_candidate_association(service_urls, service_type, store, client,
                                  diagnostics, ambiguous_same_type=False):
    if isinstance(service_urls, str):
        service_urls = [service_urls]
    service_urls = [str(value) for value in (service_urls or []) if value]
    if not service_urls:
        raise ValueError("No service URL was supplied for association verification.")
    display_url = service_urls[0]
    root = None
    root_url = ""
    failures = []
    for candidate_url in service_urls:
        try:
            root = client.service_json(candidate_url)
            root_url = candidate_url
            break
        except Exception as exc:
            failures.append((candidate_url, exc))
    if root is None:
        first_exc = failures[0][1] if failures else RestCallError("service read failed")
        rendered = " | ".join(
            f"{url}: {_safe_error(exc)}" for url, exc in failures[:3]
        )
        diagnostics.add(
            f"Could not verify the Portal-item association for {display_url} "
            f"through any hosting-server alias: {rendered}",
            item=True,
            exc=first_exc,
        )
        diagnostics.indeterminate_associations += 1
        # Preserve the original report set when the complete Portal URL index
        # had no same-type match: retain the URL-orphan candidate, but make the
        # run INCOMPLETE and expose the failed data check. If another same-type
        # item already has the same service path on an unrecognized origin, the
        # original suppressed the row, so keep it suppressed as indeterminate.
        return CandidateResolution(
            keep_as_orphan=not ambiguous_same_type,
            root_error=first_exc,
            association_indeterminate=True,
        )

    service_url = root_url
    raw_item_id = _raw_service_item_id(root)
    item_id = _canonical_item_id(raw_item_id)
    if raw_item_id not in (None, "") and not item_id:
        diagnostics.add(
            f"Service {service_url} returned malformed serviceItemId "
            f"{raw_item_id!r}; association is indeterminate.",
            item=True,
        )
        diagnostics.indeterminate_associations += 1
        return CandidateResolution(
            keep_as_orphan=not ambiguous_same_type,
            root_info=root,
            root_url=root_url,
            association_indeterminate=True,
        )
    if not item_id:
        if ambiguous_same_type:
            diagnostics.add(
                f"A same-type Portal item has the same service path as "
                f"{service_url}, but it uses a different/unrecognized origin "
                "and the hosting service returned no serviceItemId; association "
                "is indeterminate.",
                item=True,
            )
            diagnostics.indeterminate_associations += 1
            return CandidateResolution(
                keep_as_orphan=False,
                root_info=root,
                root_url=root_url,
                association_indeterminate=True,
            )
        return CandidateResolution(keep_as_orphan=True, root_info=root, root_url=root_url)

    item = store.get(item_id)
    confirmed_missing = False
    if item is None:
        try:
            item, confirmed_missing = _get_portal_item(client, item_id)
        except Exception as exc:
            diagnostics.add(
                f"Could not establish whether serviceItemId {item_id} exists for "
                f"{service_url}: {_safe_error(exc)}",
                item=True,
                exc=exc,
            )
            diagnostics.indeterminate_associations += 1
            return CandidateResolution(
                # Preserve the original URL-orphan row unless a same-type path
                # match already caused the original implementation to suppress
                # it. The INCOMPLETE result makes the uncertainty explicit.
                keep_as_orphan=not ambiguous_same_type,
                item_id=item_id,
                root_info=root,
                root_url=root_url,
                association_indeterminate=True,
            )
        if item is not None:
            diagnostics.add(
                f"Portal item {item_id} was returned by the item endpoint but was "
                "absent from the completed catalog index; catalog changed during "
                "the run.",
                catalog=True,
            )

    if item is not None:
        created, modified = _item_dates_from_dict(item)
        expected_type = "Feature Service" if service_type == "FeatureServer" else "Map Service"
        if str(item.get("type") or "") == expected_type:
            item_key = service_identity(item.get("url"))
            service_key = service_identity(service_url)
            if item_key and service_key and item_key != service_key:
                diagnostics.add(
                    f"Service {service_url} reports serviceItemId {item_id}, but "
                    f"that same-type Portal item points at a different service "
                    f"identity ({item_key}); association is indeterminate.",
                    item=True,
                )
                diagnostics.indeterminate_associations += 1
                return CandidateResolution(
                    keep_as_orphan=False,
                    item_id=item_id,
                    date_created=created,
                    date_modified=modified,
                    root_info=root,
                    root_url=root_url,
                    association_indeterminate=True,
                )
            diagnostics.same_type_item_matches += 1
            return CandidateResolution(
                keep_as_orphan=False,
                item_id=item_id,
                date_created=created,
                date_modified=modified,
                root_info=root,
                root_url=root_url,
            )
        return CandidateResolution(
            keep_as_orphan=True,
            item_id=item_id,
            date_created=created,
            date_modified=modified,
            root_info=root,
            root_url=root_url,
        )

    # Confirmed missing item: retain the stale serviceItemId for review.
    return CandidateResolution(
        keep_as_orphan=True,
        item_id=item_id if confirmed_missing else "",
        root_info=root,
        root_url=root_url,
    )


def check_service_data(client, service_url, service_type, root_info=None,
                       root_error=None):
    # Preserve the original data-check contract exactly: MapServer reports only
    # whether its root responds and how many layers it lists; FeatureServer
    # checks only the first listed layer with returnCountOnly.
    if root_error is not None:
        return f"Error: {_safe_error(root_error)}", root_error
    try:
        info = root_info if isinstance(root_info, dict) else client.service_json(service_url)
        layers = info.get("layers") or []
        if not isinstance(layers, list):
            raise RestCallError("service definition contained a malformed layers list")
        if not layers:
            return "NoLayers", None

        if service_type == "FeatureServer":
            first_layer = layers[0]
            if not isinstance(first_layer, dict):
                error = RestCallError("first layer descriptor was invalid")
                return "Error: first layer descriptor was invalid", error
            first_id = first_layer.get("id")
            if first_id is None or isinstance(first_id, bool):
                error = RestCallError("first layer has no id")
                return "Error: first layer has no id", error
            try:
                first_id = int(first_id)
            except (TypeError, ValueError):
                error = RestCallError("first layer has no numeric id")
                return "Error: first layer has no numeric id", error
            query_data = client.service_json(
                f"{service_url.rstrip('/')}/{first_id}/query",
                params={"where": "1=1", "returnCountOnly": "true"},
            )
            if "count" not in query_data:
                error = RestCallError("count query returned no count")
                return "Error: count query returned no count", error
            count = query_data.get("count")
            if isinstance(count, bool):
                raise ValueError("count was Boolean")
            count = int(count)
            if count < 0:
                raise ValueError("count was negative")
            return (f"HasData (count={count})" if count > 0
                    else "Empty (count=0)"), None

        return f"Responds ({len(layers)} layers)", None
    except Exception as exc:
        return f"Error: {_safe_error(exc)}", exc


def resolve_service_item_details(portal_url, rest_get, service_url, logger):
    """Backward-compatible tuple-returning helper retained for imported callers."""
    try:
        info = rest_get(service_url.rstrip("/"))
    except Exception as exc:
        logger.debug("Could not read service root for %s (%s).", service_url, exc)
        return "", "", ""
    item_id = _service_item_id(info) or ""
    if not item_id:
        return "", "", ""
    item_url = portal_url.rstrip("/") + f"/sharing/rest/content/items/{quote(item_id, safe='')}"
    try:
        item_data = rest_get(item_url)
        if not isinstance(item_data, dict) or _canonical_item_id(item_data.get("id")) != item_id:
            return item_id, "", ""
        created, modified = _item_dates_from_dict(item_data)
        return item_id, created, modified
    except Exception as exc:
        logger.debug("Could not resolve item %s for %s (%s).", item_id, service_url, exc)
        return item_id, "", ""


# ---------------------------------------------------------------------------
# Report construction
# ---------------------------------------------------------------------------
def _find_references(service_key, all_item_index, webmap_index):
    hits = list(all_item_index.get(service_key, [])) + list(webmap_index.get(service_key, []))
    return list(dict.fromkeys(hits))


def _write_report(rows, output_csv, log):
    written = write_dict_csv(
        rows,
        output_csv,
        fieldnames=CSV_FIELDS,
        logger=log,
        label="orphan service row",
    )
    if written:
        try:
            os.chmod(written, 0o600)
        except OSError:
            pass
        return written
    with open(output_csv, "w", newline="", encoding="utf-8") as output_handle:
        try:
            os.chmod(output_csv, 0o600)
        except OSError:
            pass
        csv.DictWriter(output_handle, fieldnames=CSV_FIELDS).writeheader()
    log.info("Wrote 0 orphan service row(s) to %s", output_csv)
    return output_csv


def _build_body(args, platform, run_stamp, result, output_csv, log_path,
                counts, diagnostics, timing, note="", error=None):
    lines = [
        SUBJECT_PREFIX,
        f"Organization: {args.org_name}",
        f"Config file: {args.config}",
        f"Platform: {platform}",
        f"Portal URL: {args.portal_url}",
        f"Result: {result}",
        f"Run timestamp: {run_stamp}",
    ]
    if error:
        lines.append(f"Reason: {redact_sensitive_data(str(error))}")
    lines.extend([
        f"Portal catalog items: {counts.get('portal_items', 0)}",
        f"Hosting services: {counts.get('hosted_services', 0)}",
        f"URL-orphan candidates: {counts.get('candidates', 0)}",
        f"Same-type serviceItemId matches removed: {diagnostics.same_type_item_matches}",
        f"Reported orphan rows: {counts.get('orphans', 0)}",
        f"Web Map read errors: {diagnostics.webmap_errors}",
        f"Data-check errors: {diagnostics.data_check_errors}",
        f"Item-detail errors: {diagnostics.item_detail_errors}",
        f"Catalog consistency warnings: {diagnostics.catalog_warnings}",
        f"TLS/CERT errors: {diagnostics.tls_errors}",
        f"Authentication errors: {diagnostics.auth_errors}",
        f"Transport errors: {diagnostics.transport_errors}",
        f"Throttling/transient errors: {diagnostics.transient_errors}",
        f"Indeterminate service-item associations: {diagnostics.indeterminate_associations}",
        f"Output CSV: {output_csv if os.path.exists(output_csv) else '(not written)'}",
        f"Log file: {log_path}",
        timing,
    ])
    if result == "INCOMPLETE":
        lines.extend([
            "",
            "WARNING: Reported orphan rows are retained, but one or more Web Map,",
            "service-data, or item-association checks were indeterminate. Missing",
            "references or omitted candidates must not be interpreted as definitive.",
        ])
    if note:
        lines.append(note)
    return redact_sensitive_data("\n".join(lines))




def _gather_attachments_with_total_cap(log_path, csv_paths, output_dir, log,
                                       cap_mb):
    """Apply the configured ceiling to log plus report, not report alone."""
    try:
        cap = float(cap_mb)
    except (TypeError, ValueError):
        cap = 20.0
    try:
        log_mb = os.path.getsize(log_path) / (1024 * 1024)
    except OSError:
        log_mb = 0.0
    if log_mb > cap:
        note = (
            f"\nNOTE: The log alone is {log_mb:.2f} MB, exceeding the "
            f"configured {cap:.2f} MB attachment ceiling. No files were "
            f"attached; the log remains at {log_path}.\n"
        )
        log.warning("Log file %.2f MB exceeds %.2f MB total attachment cap; "
                    "sending email without attachments.", log_mb, cap)
        return [], note
    remaining = max(0.0, cap - log_mb)
    log.debug(
        "Attachment cap accounting: configured=%.2f MB log=%.2f MB "
        "remaining-for-reports=%.2f MB.", cap, log_mb, remaining,
    )
    return gather_attachments(
        log_path, csv_paths, output_dir, log, cap_mb=remaining,
    )

# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def _run_inventory(args, log, output_csv):
    configured_platform = get_platform(args.portal_url)
    if configured_platform != "PORTAL":
        raise ScanAbort(
            f"This is a Portal-only script, but {args.portal_url!r} classified "
            f"as {configured_platform}."
        )

    mode = _TLS_MODE_CHOICES[args.tls_mode]
    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    main_session = None
    worker_pool = None
    store = None
    diagnostics = RunDiagnostics()
    counts = {
        "portal_items": 0, "hosted_services": 0,
        "candidates": 0, "orphans": 0,
    }

    try:
        log.info("STEP 1/5: Establish verified TLS sessions and connect to Portal")
        main_session = build_secure_session(
            mode,
            ca_bundle_path=ca_path,
            pool_connections=max(16, args.workers),
            pool_maxsize=max(32, args.workers * 2),
            retry_total=2,
            logger=log,
        )
        handshake_ok, detail = verify_tls_handshake(
            args.portal_url, main_session, logger=log, timeout=REQUEST_TIMEOUT
        )
        if not handshake_ok:
            raise ScanAbort(
                "TLS handshake verification failed: "
                + str(detail.get("error") or "unknown error")
            )
        gis = connect_gis(
            args.portal_url, args.username, args.keyring_service, log,
            verify_mode=mode, ca_bundle_path=ca_path,
        )
        portal_url = _authenticated_portal_url(gis, args.portal_url)
        authenticated_platform = get_platform(portal_url)
        if authenticated_platform != "PORTAL":
            raise ScanAbort(
                f"Authenticated GIS URL {portal_url!r} is not Enterprise Portal."
            )
        token_provider = provider_for_gis(gis, "PORTAL")
        log.info(
            "Token acquired via %s (token present: %s).",
            type(token_provider).__name__, token_provider.is_present(),
        )

        policy = TrustedUrlPolicy(portal_url)
        client = ArcGISRestClient(policy, main_session, token_provider, gis)
        _collect_self_properties(gis, client)

        # Fetch authenticated federation first so direct public/admin/server URLs
        # on different hosts are all accepted as one hosting-server identity.
        hosting = _hosting_context_from_federation(client, portal_url, log)

        log.info("STEP 2/5: Index complete Portal catalog through verified REST")
        store = PortalItemStore()
        _strict_bucket_catalog(client, store, log)
        counts["portal_items"] = len(store)

        indexes = build_portal_indexes(store, hosting, diagnostics, log)

        log.info("STEP 3/5: Enumerate every hosted FeatureServer/MapServer")
        hosted_services = enumerate_hosted_services(client, hosting, log)
        counts["hosted_services"] = len(hosted_services)

        log.info("STEP 4/5: Identify candidates and scan Web Map references")
        candidates = []
        for name, url, request_urls, service_type, key in hosted_services:
            if service_type == "FeatureServer":
                authoritative = key in indexes["feature_hosting_keys"]
                ambiguous_same_type = key in indexes["feature_any_keys"]
            else:
                authoritative = key in indexes["map_hosting_keys"]
                ambiguous_same_type = key in indexes["map_any_keys"]
            if not authoritative:
                candidates.append(
                    (name, url, request_urls, service_type, key,
                     ambiguous_same_type)
                )
        counts["candidates"] = len(candidates)
        log.info(
            "URL-orphan candidates requiring serviceItemId verification: %d "
            "(FeatureServer=%d, MapServer=%d)",
            len(candidates),
            sum(1 for _n, _u, _rs, kind, _k, _a in candidates
                if kind == "FeatureServer"),
            sum(1 for _n, _u, _rs, kind, _k, _a in candidates
                if kind == "MapServer"),
        )

        worker_pool = WorkerSessionPool(mode, ca_path, args.workers)
        webmap_index = scan_webmap_references(
            store, client, worker_pool, args.workers, diagnostics, log
        )

        log.info("STEP 5/5: Verify candidates, run data checks, and write report")
        rows = []
        for name, url, request_urls, service_type, key, ambiguous_same_type in candidates:
            resolution = resolve_candidate_association(
                request_urls, service_type, store, client, diagnostics,
                ambiguous_same_type=ambiguous_same_type,
            )
            if not resolution.keep_as_orphan:
                continue
            references = _find_references(
                key, indexes["all_item_index"], webmap_index
            )
            primary_request_url = (
                request_urls[0]
                if isinstance(request_urls, (list, tuple))
                else request_urls
            )
            data_text, data_error = check_service_data(
                client, (resolution.root_url or primary_request_url), service_type,
                root_info=resolution.root_info,
                # A failed association read does not suppress the original
                # independent data check; retrying here preserves the former
                # two-read behavior and can recover from a transient first call.
                root_error=None,
            )
            if data_error is not None:
                diagnostics.add(
                    f"Data check failed for {url}: {_safe_error(data_error)}",
                    data=True,
                    exc=data_error,
                )
            if resolution.association_indeterminate:
                data_text = (
                    "INDETERMINATE ITEM ASSOCIATION; " + data_text
                )
            log.info("  data-check [%s] %s: %s", service_type, name, data_text)
            rows.append({
                "ServiceName": name,
                "ServiceType": service_type,
                "service_url": url,
                "item_id": resolution.item_id,
                "date_created": resolution.date_created,
                "date_modified": resolution.date_modified,
                "ReferencingPortalItems": "; ".join(references),
                "DataCheck": redact_sensitive_data(data_text),
            })

        counts["orphans"] = len(rows)
        _write_report(rows, output_csv, log)

        log.info("=== Hosted services with no matching Portal item (by type) ===")
        if rows:
            for row in rows:
                flag = (
                    f"  <-- referenced by: {row['ReferencingPortalItems']}"
                    if row["ReferencingPortalItems"] else ""
                )
                log.info(
                    "[%s] %s\t%s\titem_id:%s\tDATA:%s%s",
                    row["ServiceType"], row["ServiceName"], row["service_url"],
                    row["item_id"] or "(none)", row["DataCheck"], flag,
                )
        else:
            log.info("None -- every hosted service has a matching Portal item of its type.")

        result = "INCOMPLETE" if diagnostics.incomplete else "COMPLETE"
        return result, counts, diagnostics, None
    finally:
        if worker_pool is not None:
            worker_pool.close()
        if store is not None:
            store.close()
        if main_session is not None:
            try:
                main_session.close()
            except Exception:
                pass


def main(argv=None):
    global logger
    args = parse_args(argv)
    finalize_config(args, required=REQUIRED_KEYS, logger=None)
    ensure_output_dir(args.output_dir)

    base_timestamp = build_timestamp()
    run_stamp, log_path, output_csv = _reserve_run_paths(
        args.output_dir, args.org_name_safe, base_timestamp
    )
    logger = setup_logging(log_path, SCRIPT_NAME)
    start_wallclock, start_perf = start_timer()
    max_email_mb = get_max_email_mb(args.target, args.config)
    platform = get_platform(args.portal_url)

    logger.info("=" * 70)
    logger.info("Orphan Hosted Service Inventory - run started")
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
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_path)
    logger.info("Output CSV: %s", output_csv)
    logger.info("Max email MB: %s", max_email_mb)
    logger.info("Worker threads: %d", args.workers)
    logger.info("=" * 70)

    result = "ABORTED"
    counts = {"portal_items": 0, "hosted_services": 0, "candidates": 0, "orphans": 0}
    diagnostics = RunDiagnostics()
    fatal_error = None

    try:
        result, counts, diagnostics, _unused = _run_inventory(
            args, logger, output_csv,
        )
    except Exception as exc:
        fatal_error = redact_sensitive_data(
            f"{type(exc).__name__}: {exc}"
        )
        logger.exception("Run aborted during orphan hosted-service inventory.")
        result = "ABORTED"

    timing = timing_block(start_wallclock, start_perf)
    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result: %s", result)
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Portal catalog items: %d", counts.get("portal_items", 0))
    logger.info("Hosting services: %d", counts.get("hosted_services", 0))
    logger.info("URL-orphan candidates: %d", counts.get("candidates", 0))
    logger.info("Same-type serviceItemId matches removed: %d",
                diagnostics.same_type_item_matches)
    logger.info("Reported orphan rows: %d", counts.get("orphans", 0))
    logger.info("Web Map errors: %d", diagnostics.webmap_errors)
    logger.info("Data-check errors: %d", diagnostics.data_check_errors)
    logger.info("Item-detail errors: %d", diagnostics.item_detail_errors)
    logger.info("Catalog consistency warnings: %d", diagnostics.catalog_warnings)
    logger.info("TLS/CERT errors: %d", diagnostics.tls_errors)
    logger.info("Authentication errors: %d", diagnostics.auth_errors)
    logger.info("Transport errors: %d", diagnostics.transport_errors)
    logger.info("Throttling/transient errors: %d", diagnostics.transient_errors)
    logger.info("Output CSV: %s", output_csv if os.path.exists(output_csv) else "(not written)")
    logger.info("=" * 70)

    if diagnostics.reasons:
        logger.warning("Incomplete-check reason(s), up to %d retained:", MAX_ERROR_SAMPLES)
        for reason in diagnostics.reasons:
            logger.warning("  %s", reason)

    csv_paths = [output_csv] if os.path.exists(output_csv) else []
    attachments, note = _gather_attachments_with_total_cap(
        log_path, csv_paths, args.output_dir, logger, max_email_mb,
    )
    body = _build_body(
        args, platform, run_stamp, result, output_csv, log_path,
        counts, diagnostics, timing, note=note, error=fatal_error,
    )
    subject = subject_line(
        SUBJECT_PREFIX, run_stamp, platform, org_name=args.org_name
    )
    if result != "COMPLETE":
        subject = f"[{result}] {subject}"
    send_email(
        subject=subject,
        body=body,
        attachments=attachments,
        smtp_server=args.smtp_server,
        email_to=args.email_to,
        email_from=args.email_from,
        logger=logger,
    )

    logger.info("%s finished with result %s", SCRIPT_NAME, result)
    return 0 if result == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
