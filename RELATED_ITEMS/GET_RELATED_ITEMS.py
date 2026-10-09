#!/usr/bin/env python3
r"""
GET_RELATED_ITEMS.py

Catalogs relationships between data items (CSV, CSV Collection, KML, KML
Collection, File Geodatabase, Shapefile, Microsoft Excel) and the services,
apps, maps, scenes, surveys, and reports derived from them. Every catalog
search is scoped to the authenticated organization; the script refuses to run
unscoped. 

The successful-path relationship behavior is unchanged: every item is probed
with the same relationship types and directions, each returned relationship
produces one output row, and an item with no relationships produces one row
whose related-item columns are blank. Portal responses that say "Invalid
Relationship Type" for an unsupported type/direction pair are treated as an
empty probe, matching the historical behavior; all other probe failures still
mark the report incomplete.

Direct Portal/AGOL REST traffic uses an explicitly verified requests session.
Tokens are centrally held in a TokenProvider and exposed only while an outgoing
request is constructed. Direct REST responses, redirects, pagination, and
failure states are validated so a partial inventory cannot be reported as a
complete one.

Common usage:
    python GET_RELATED_ITEMS.py --config org1.config.toml --target portal
    python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --no-test
    python GET_RELATED_ITEMS.py --config org2.config.toml --target agol --no-test --workers 16
    python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --tls-mode os-trust-store
    python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --exclude-group-id abc123...
"""

import argparse
import datetime
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import Dict, List, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests


# Verify the expected project package before importing package code. Production
# scripts normally live one directory below the project root, but accepting a
# script placed directly in the root preserves the original layout flexibility.
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

_EXPECTED_UTILITIES_DIR = os.path.normcase(
    os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
)
_UTILITIES_SPEC = importlib.util.find_spec("UTILITIES")
_UTILITIES_ORIGIN = getattr(_UTILITIES_SPEC, "origin", None)
_ACTUAL_UTILITIES_DIR = (
    os.path.normcase(os.path.realpath(os.path.dirname(_UTILITIES_ORIGIN)))
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
    MAX_ATTACHMENTS_MB,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)


EMAIL_SUBJECT_PREFIX = "Related Items"

MAX_ITEMS_PER_TYPE = 2_000_000
TEST_MODE_ITEM_LIMIT = 20
DEFAULT_WORKERS = 16
HTTP_TIMEOUT = 30
SEARCH_PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_ERROR_SAMPLES = 50

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

ITEM_TYPES = [
    "CSV", "CSV Collection", "KML", "KML Collection",
    "File Geodatabase", "Shapefile", "Microsoft Excel",
]

# Probe order is intentionally unchanged. It is part of the report semantics.
RELATIONSHIP_PROBES = [
    ("OriginatesFrom", "reverse"),
    ("Data2Service", "forward"),
    ("Service2Data", "reverse"),
    ("Data2App", "reverse"),
    ("Data2App", "forward"),
    ("Data2Map", "reverse"),
    ("Data2Scene", "reverse"),
    ("Data2Scene", "forward"),
    ("Data2Survey", "reverse"),
    ("Data2Survey", "forward"),
    ("Data2SurveyAddIn", "reverse"),
    ("Data2SurveyAddIn", "forward"),
    ("Item2Report", "reverse"),
    ("Item2Report", "forward"),
    ("Survey2Data", "reverse"),
    ("Survey2Data", "forward"),
    ("Survey2Service", "reverse"),
    ("Survey2Service", "forward"),
]

OUTPUT_FIELDS = [
    "platform",
    "item_id", "title", "item_type", "owner", "item_size_mb",
    "date_created", "date_modified",
    "related_item_id", "related_title", "related_type", "related_owner",
    "relationship_type", "direction",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_INVALID_RELATIONSHIP_TYPE_RE = re.compile(
    r"(?i)\binvalid\s+relationship\s+type\b"
)
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


class ScanAbort(RuntimeError):
    """A controlled failure that invalidates the required inventory."""


class RestCallError(RuntimeError):
    """Sanitized direct-REST failure with structured diagnostics."""

    def __init__(self, message, *, tls_error=False, auth_error=False,
                 transient_error=False, transport_error=False,
                 status_code=None, rest_code=None):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.transient_error = bool(transient_error)
        self.transport_error = bool(transport_error)
        self.status_code = status_code
        self.rest_code = rest_code


@dataclass
class SearchDiagnostics:
    errors: List[str] = field(default_factory=list)
    failed_types: set = field(default_factory=set)
    request_count: int = 0
    excluded_items: int = 0
    tls_errors: int = 0
    auth_errors: int = 0
    transient_errors: int = 0
    transport_errors: int = 0

    def add_error(self, item_type, message):
        self.failed_types.add(item_type)
        safe = redact_token(str(message))
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append(f"{item_type}: {safe}")
        if isinstance(message, RestCallError):
            self.tls_errors += int(message.tls_error)
            self.auth_errors += int(message.auth_error)
            self.transient_errors += int(message.transient_error)
            self.transport_errors += int(message.transport_error)


@dataclass
class ItemOutcome:
    rows: List[Dict]
    errors: List[str]
    item_type: str
    relationships: int
    size_mb: float
    metadata_errors: int = 0
    probe_errors: int = 0
    tls_errors: int = 0
    auth_errors: int = 0
    transient_errors: int = 0
    transport_errors: int = 0


@dataclass
class RunDiagnostics:
    item_errors: int = 0
    metadata_errors: int = 0
    probe_errors: int = 0
    tls_errors: int = 0
    auth_errors: int = 0
    transient_errors: int = 0
    transport_errors: int = 0
    unexpected_worker_errors: int = 0
    error_samples: List[str] = field(default_factory=list)

    def add_outcome(self, item_id, outcome):
        if outcome.errors:
            self.item_errors += 1
            for message in outcome.errors:
                if len(self.error_samples) < MAX_ERROR_SAMPLES:
                    self.error_samples.append(
                        f"{item_id}: {redact_token(str(message))}"
                    )
        self.metadata_errors += outcome.metadata_errors
        self.probe_errors += outcome.probe_errors
        self.tls_errors += outcome.tls_errors
        self.auth_errors += outcome.auth_errors
        self.transient_errors += outcome.transient_errors
        self.transport_errors += outcome.transport_errors


class ItemStore:
    """Disk-backed item library preserving type and first-seen ordering."""

    def __init__(self, logger):
        fd, path = tempfile.mkstemp(prefix=".related_items_", suffix=".sqlite")
        os.close(fd)
        self.path = path
        self.logger = logger
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE items ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "item_type TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "UNIQUE(item_type, item_id))"
        )
        self._pending = 0
        logger.debug("Using temporary item library: %s", path)

    def put(self, item_type, item_id, item):
        payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO items(item_type,item_id,payload) VALUES (?,?,?)",
            (item_type, item_id, payload),
        )
        if cursor.rowcount == 0:
            # A dict assignment in the original code updated the value while
            # retaining first-insertion order. This UPDATE has the same effect.
            self._connection.execute(
                "UPDATE items SET payload=? WHERE item_type=? AND item_id=?",
                (payload, item_type, item_id),
            )
        self._pending += 1
        if self._pending >= 1000:
            self.commit()

    def commit(self):
        self._connection.commit()
        self._pending = 0

    def delete_type(self, item_type):
        self._connection.execute("DELETE FROM items WHERE item_type=?", (item_type,))
        self.commit()

    def remove_ids(self, item_type, item_ids):
        ids = list(item_ids or ())
        if not ids:
            return 0
        before = self.count_type(item_type)
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            self._connection.execute(
                f"DELETE FROM items WHERE item_type=? AND item_id IN ({placeholders})",
                (item_type, *batch),
            )
        self.commit()
        return before - self.count_type(item_type)

    def count_type(self, item_type):
        self.commit()
        row = self._connection.execute(
            "SELECT COUNT(*) FROM items WHERE item_type=?", (item_type,)
        ).fetchone()
        return int(row[0])

    def total(self):
        self.commit()
        row = self._connection.execute("SELECT COUNT(*) FROM items").fetchone()
        return int(row[0])

    def iter_items(self):
        self.commit()
        cursor = self._connection.execute(
            "SELECT item_type,item_id,payload FROM items ORDER BY seq"
        )
        try:
            for item_type, item_id, payload in cursor:
                item = json.loads(payload)
                if not isinstance(item, dict):
                    raise ScanAbort("Temporary item library contained malformed data.")
                yield item_type, item_id, item
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
                    "Could not remove temporary item library %s: %s",
                    self.path, exc,
                )


class RowStore:
    """Disk-backed output rows preserving completion-order insertion."""

    def __init__(self, logger):
        fd, path = tempfile.mkstemp(prefix=".related_rows_", suffix=".sqlite")
        os.close(fd)
        self.path = path
        self.logger = logger
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            "CREATE TABLE rows (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "payload TEXT NOT NULL)"
        )
        self._count = 0
        self._pending = 0
        logger.debug("Using temporary related-row store: %s", path)

    def add_many(self, rows):
        rendered = [
            (json.dumps(row, ensure_ascii=False, separators=(",", ":")),)
            for row in rows
        ]
        if not rendered:
            return
        self._connection.executemany(
            "INSERT INTO rows(payload) VALUES (?)", rendered
        )
        self._count += len(rendered)
        self._pending += len(rendered)
        if self._pending >= 1000:
            self.commit()

    def commit(self):
        self._connection.commit()
        self._pending = 0

    def __len__(self):
        return self._count

    def __iter__(self):
        self.commit()
        cursor = self._connection.execute("SELECT payload FROM rows ORDER BY seq")
        try:
            for (payload,) in cursor:
                row = json.loads(payload)
                if not isinstance(row, dict):
                    raise ScanAbort("Temporary row store contained malformed data.")
                yield row
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
                    "Could not remove temporary row store %s: %s", self.path, exc
                )


class WorkerSessionPool:
    """One pre-created requests.Session per worker under one TLS policy."""

    def __init__(self, mode, ca_bundle_path, initial_session, capacity):
        self._assignment_lock = Lock()
        self._initial_session = initial_session
        self._initial_thread = get_ident()
        self._sessions = [initial_session]
        self._worker_sessions = []
        self._assignments = {}
        self._next = 0
        try:
            # Build serially. OS trust-store setup modifies process-global SSL
            # behavior and must not race active request threads.
            for _ in range(max(1, int(capacity))):
                session = build_secure_session(
                    mode,
                    ca_bundle_path=ca_bundle_path,
                    pool_connections=2,
                    pool_maxsize=4,
                    retry_total=2,
                    logger=None,
                )
                self._worker_sessions.append(session)
                self._sessions.append(session)
        except Exception:
            self.close()
            raise

    def get(self):
        thread_id = get_ident()
        if thread_id == self._initial_thread:
            return self._initial_session
        with self._assignment_lock:
            assigned = self._assignments.get(thread_id)
            if assigned is not None:
                return assigned
            assigned = self._worker_sessions[self._next % len(self._worker_sessions)]
            self._next += 1
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
    """Compatibility adapter for helpers passed one existing Session."""

    def __init__(self, session):
        self._session = session

    def get(self):
        return self._session


def _split_https_url(raw):
    text = str(raw or "").strip()
    if not text:
        raise ScanAbort("Portal URL is blank.")
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
    return parts


def _validated_path(parts):
    raw_path = parts.path or ""
    lowered = raw_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise RestCallError("URL path contains an encoded slash or backslash.")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError("URL path contains a backslash or control character.")
    segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise RestCallError("URL path contains a traversal component.")
    return "/" + "/".join(segments) if segments else ""


def _origin_tuple(parts):
    return (
        parts.scheme.lower(),
        (parts.hostname or "").lower().rstrip("."),
        parts.port or 443,
    )


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


class PortalUrlPolicy:
    """Allow token-bearing calls only to the authenticated sharing REST API."""

    def __init__(self, portal_base):
        self.portal_base = _normalize_portal_base(portal_base)
        parts = _split_https_url(self.portal_base)
        self.origin = _origin_tuple(parts)
        self.portal_path = _validated_path(parts).rstrip("/")
        self.sharing_prefix = self.portal_path + "/sharing/rest/"

    def build(self, relative_path):
        value = str(relative_path or "")
        if not value.startswith("/"):
            value = "/" + value
        return self.validate(self.portal_base + value)

    def validate(self, raw_url):
        try:
            parts = _split_https_url(raw_url)
        except ScanAbort as exc:
            raise RestCallError(str(exc)) from exc
        if parts.query or parts.fragment:
            raise RestCallError("Portal REST URL must not contain a query or fragment.")
        path = _validated_path(parts)
        if _origin_tuple(parts) != self.origin:
            raise RestCallError("Portal REST URL changed the authenticated origin.")
        if not path.startswith(self.sharing_prefix):
            raise RestCallError(
                "Portal REST URL left the authenticated /sharing/rest/ boundary."
            )
        return urlunsplit(("https", parts.netloc, path, "", ""))


class PortalRestClient:
    """Strict Portal JSON client with token refresh and redirect controls."""

    def __init__(self, policy, session_pool, token_provider, gis=None):
        self.policy = policy
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.gis = gis
        self._refresh_lock = Lock()

    def get_json(self, path_or_url, params=None, timeout=HTTP_TIMEOUT):
        if str(path_or_url).startswith("http"):
            url = self.policy.validate(path_or_url)
        else:
            url = self.policy.build(path_or_url)
        token = self.token_provider.get()
        try:
            return self._request_once(url, params, timeout, token)
        except RestCallError as exc:
            if not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._request_once(url, params, timeout, refreshed)

    def _refresh_if_stale(self, failed_token):
        with self._refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return current
            if self.gis is not None:
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

    def _request_once(self, url, params, timeout, token):
        merged = {"f": "json"}
        if params:
            merged.update(params)
        # Callers cannot replace the provider token or output format.
        merged.pop("token", None)
        merged["f"] = "json"
        merged["token"] = token

        current_url = url
        response = None
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                response = self.session_pool.get().get(
                    current_url,
                    params=merged,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.Timeout as exc:
                raise RestCallError(
                    f"timeout after {timeout}s",
                    transient_error=True,
                    transport_error=True,
                ) from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                    transport_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(
                    f"request failed: {exc}", transport_error=True
                ) from exc

            status = int(response.status_code)
            if not 300 <= status < 400:
                break
            location = response.headers.get("Location")
            response.close()
            response = None
            if redirect_count >= MAX_REDIRECTS:
                raise RestCallError("Too many redirects for a token-bearing request.")
            if not location:
                raise RestCallError(
                    f"HTTP {status} redirect supplied no Location header."
                )
            current_url = self.policy.validate(urljoin(current_url, location))

        if response is None:
            raise RestCallError("Request did not produce a response.")

        status = int(response.status_code)
        reason = response.reason or ""
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
                    f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit"
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
                        f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        except RestCallError:
            raise
        except requests.exceptions.SSLError as exc:
            raise RestCallError(
                f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
                tls_error=True,
                transport_error=True,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RestCallError(
                f"request failed while reading: {exc}", transport_error=True
            ) from exc
        finally:
            response.close()

        body = b"".join(chunks)
        auth_status = status in (401, 403, 498, 499)
        transient_status = status == 429 or status >= 500
        if not body:
            raise RestCallError(
                f"HTTP {status} {reason}: empty response body",
                auth_error=auth_status,
                transient_error=transient_status,
                status_code=status,
            )
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestCallError(
                f"non-JSON response (HTTP {status})",
                auth_error=auth_status,
                transient_error=transient_status,
                status_code=status,
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
                transient_error=(code == "429" or code.startswith("5")),
                status_code=status,
                rest_code=code,
            )
        if status >= 400:
            raise RestCallError(
                f"HTTP {status} {reason}",
                auth_error=auth_status,
                transient_error=transient_status,
                status_code=status,
            )
        if str(data.get("status", "")).strip().lower() == "error":
            rendered = f"endpoint returned status=error: {data.get('messages') or 'no message supplied'}"
            raise RestCallError(
                rendered,
                auth_error=bool(_AUTH_ERROR_RE.search(rendered)),
            )
        return data


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


def _safe_item_id(value):
    text = str(value or "").strip()
    if not _ITEM_ID_RE.fullmatch(text):
        raise RestCallError(f"Invalid ArcGIS item id: {text!r}")
    return text


def _is_unsupported_relationship_probe(exc):
    """Identify Portal's canonical response for an unsupported probe pair.

    ArcGIS Enterprise versions do not all recognize every relationship type
    and direction in RELATIONSHIP_PROBES. They report an unsupported pair as
    a JSON REST error (commonly code 500) whose message is exactly "Invalid
    Relationship Type." That response means the pair is unavailable on this
    deployment, not that the item check failed. Transport, TLS, authentication,
    malformed-response, and every other REST error remain genuine failures.
    """
    if not isinstance(exc, RestCallError):
        return False
    if exc.tls_error or exc.auth_error or exc.transport_error:
        return False
    if str(exc.rest_code or "") not in {"400", "500"}:
        return False
    return bool(_INVALID_RELATIONSHIP_TYPE_RE.search(str(exc)))


def _relationship_probe_json(client, rel_path, rel_type, direction):
    """Run one relationship probe, normalizing unsupported pairs to empty."""
    try:
        return client.get_json(
            rel_path,
            params={"relationshipType": rel_type, "direction": direction},
        )
    except RestCallError as exc:
        if _is_unsupported_relationship_probe(exc):
            return {"relatedItems": []}
        raise


def _org_id_from_clause(org_clause):
    match = re.search(r"(?i)(?:^|\s)orgid:([A-Za-z0-9_-]+)", str(org_clause))
    if not match:
        raise ScanAbort("Organization clause did not contain an orgid value.")
    return _safe_org_id(match.group(1))


def portal_base_url(gis) -> str:
    """Preserve the original SDK Portal base, with the public URL as fallback."""
    try:
        value = gis._portal.url
    except Exception:
        value = None
    if not value:
        value = getattr(gis, "url", None)
    return _normalize_portal_base(value)


def resolve_org_scope(gis, logger):
    org_id = _safe_org_id(get_org_id(gis, logger))
    logger.info("Restricting all searches to org-owned content (orgid:%s).", org_id)
    return f"orgid:{org_id}"


def _validate_search_item(item, org_id):
    if not isinstance(item, dict):
        raise RestCallError("Portal search returned a non-object item entry.")
    item_id = _safe_item_id(item.get("id"))
    explicit_org = item.get("orgId") or item.get("orgid")
    if explicit_org is not None and str(explicit_org).strip() != org_id:
        raise RestCallError(
            f"Portal search returned cross-organization item {item_id}."
        )
    normalized = dict(item)
    normalized["id"] = item_id
    return normalized


def _strict_search_page(client, query, cap, org_id, page_size=SEARCH_PAGE_SIZE,
                        request_counter=None):
    results = []
    start = 1
    stable_total = None
    seen_page_ids = set()
    max_pages = max(4, cap + 2)
    pages = 0

    while len(results) < cap:
        pages += 1
        if pages > max_pages:
            raise RestCallError(
                f"Portal search exceeded {max_pages} pages; refusing to loop indefinitely."
            )
        num = min(page_size, cap - len(results))
        if request_counter is not None:
            request_counter[0] += 1
        data = client.get_json(
            "/sharing/rest/search",
            params={
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": num,
            },
        )

        raw_batch = data.get("results")
        if raw_batch is None:
            raw_batch = data.get("items")
        if raw_batch is None:
            raw_batch = []
        if not isinstance(raw_batch, list):
            raise RestCallError("Portal search returned a non-list results value.")

        raw_total = data.get("total")
        total = None
        if raw_total is not None:
            try:
                total = int(raw_total)
            except (TypeError, ValueError) as exc:
                raise RestCallError(
                    f"Portal search returned invalid total {raw_total!r}."
                ) from exc
            if total < 0:
                raise RestCallError("Portal search returned a negative total.")
            if stable_total is None:
                stable_total = total
            elif stable_total != total:
                raise RestCallError(
                    f"Portal search total changed during paging: {stable_total} -> {total}."
                )

        if not raw_batch:
            if total is not None and len(results) < min(total, cap):
                raise RestCallError(
                    "Portal search returned an empty page before its declared total."
                )
            break

        batch = []
        new_on_page = 0
        for raw in raw_batch:
            item = _validate_search_item(raw, org_id)
            item_id = item["id"]
            canonical_id = item_id.lower()
            batch.append(item)
            if canonical_id not in seen_page_ids:
                new_on_page += 1
                seen_page_ids.add(canonical_id)
        if new_on_page == 0:
            raise RestCallError("Portal search page made no item-id progress.")

        results.extend(batch)
        if len(results) >= cap:
            break
        if total is not None and len(results) >= total:
            break

        raw_next = data.get("nextStart")
        if raw_next is None:
            next_start = start + len(raw_batch)
        else:
            try:
                next_start = int(raw_next)
            except (TypeError, ValueError) as exc:
                raise RestCallError(
                    f"Portal search returned invalid nextStart {raw_next!r}."
                ) from exc
            if next_start <= 0:
                if total is not None and len(results) < min(total, cap):
                    raise RestCallError(
                        "Portal search ended before its declared total."
                    )
                break
        if next_start <= start:
            raise RestCallError(
                f"Portal search pagination made no progress: {start} -> {next_start}."
            )
        start = next_start

    return results[:cap]


def _month_windows():
    cursor = _SEARCH_START
    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    while cursor < span_end:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)
        yield int(cursor.timestamp() * 1000), int(nxt.timestamp() * 1000) - 1
        cursor = nxt


def _strict_bucketed_type_search(client, item_type, org_id, store, max_items,
                                 logger, request_counter):
    extra_clause = f'orgid:{org_id} AND type:"{item_type}"'

    def search_range(lo_ms, hi_ms):
        query = f"{extra_clause} AND created:[{lo_ms} TO {hi_ms}]"
        results = _strict_search_page(
            client, query, SEARCH_CAP, org_id,
            page_size=SEARCH_PAGE_SIZE,
            request_counter=request_counter,
        )
        if len(results) >= SEARCH_CAP:
            if hi_ms <= lo_ms:
                raise RestCallError(
                    f"Search range [{lo_ms} TO {hi_ms}] still reached the "
                    f"{SEARCH_CAP}-item cap and cannot be split further."
                )
            mid = lo_ms + (hi_ms - lo_ms) // 2
            search_range(lo_ms, mid)
            search_range(mid + 1, hi_ms)
            return
        for item in results:
            store.put(item_type, item["id"].lower(), item)
        if store.count_type(item_type) > max_items:
            raise RestCallError(
                f"Type {item_type!r} exceeded the {max_items}-item safety ceiling."
            )

    for lo_ms, hi_ms in _month_windows():
        search_range(lo_ms, hi_ms)


def _build_item_store(client, per_type_limit, test_mode, org_id, logger,
                      excluded_ids, store):
    diagnostics = SearchDiagnostics()
    logger.info("-" * 70)
    logger.info(
        "Building item library (%s) ...",
        f"TEST: up to {TEST_MODE_ITEM_LIMIT} per type"
        if test_mode else "FULL: all items per type",
    )

    for item_type in ITEM_TYPES:
        request_counter = [0]
        try:
            if test_mode:
                query = f'orgid:{org_id} AND type:"{item_type}"'
                items = _strict_search_page(
                    client,
                    query,
                    per_type_limit,
                    org_id,
                    page_size=per_type_limit,
                    request_counter=request_counter,
                )
                # The original de-duplicated before selecting the first sample.
                unique = {}
                for item in items:
                    unique.setdefault(item["id"].lower(), item)
                for item in list(unique.values())[:per_type_limit]:
                    store.put(item_type, item["id"].lower(), item)
                logger.info(
                    "TEST prescan | type '%-18s': %d item(s) sampled (target %d).",
                    item_type, store.count_type(item_type), per_type_limit,
                )
            else:
                _strict_bucketed_type_search(
                    client, item_type, org_id, store, per_type_limit, logger,
                    request_counter,
                )
                logger.info(
                    "FULL scan  | type '%-18s': found %d item(s) to scan.",
                    item_type, store.count_type(item_type),
                )

            removed = store.remove_ids(item_type, excluded_ids)
            diagnostics.excluded_items += removed
            if removed:
                logger.info(
                    "Excluded %d '%s' item(s) belonging to the requested "
                    "exclusion group(s); %d remain.",
                    removed, item_type, store.count_type(item_type),
                )
        except Exception as exc:
            store.delete_type(item_type)
            diagnostics.add_error(item_type, exc)
            logger.error(
                "Search FAILED for type '%s'; no partial rows from this type "
                "will be used: %s", item_type, redact_token(str(exc)),
            )
        finally:
            diagnostics.request_count += request_counter[0]

    logger.info("-" * 70)
    logger.info("Item library summary (%s):", "TEST" if test_mode else "FULL")
    for item_type in ITEM_TYPES:
        logger.info("  %-18s : %d item(s)", item_type, store.count_type(item_type))
    logger.info("  %-18s : %d item(s)", "TOTAL", store.total())
    logger.info("  Search requests      : %d", diagnostics.request_count)
    logger.info("-" * 70)
    return diagnostics


def _strict_excluded_item_ids(gis, raw_group_ids, logger):
    if not raw_group_ids:
        return set()
    group_ids = []
    for value in str(raw_group_ids).split(","):
        group_id = value.strip()
        if not group_id:
            continue
        if not _GROUP_ID_RE.fullmatch(group_id):
            raise ScanAbort(f"Invalid --exclude-group-id value: {group_id!r}")
        if group_id not in group_ids:
            group_ids.append(group_id)

    excluded = set()
    for group_id in group_ids:
        try:
            group = gis.groups.get(group_id)
        except Exception as exc:
            raise ScanAbort(
                f"Could not resolve requested exclusion group {group_id!r}: {exc}"
            ) from exc
        if group is None:
            raise ScanAbort(f"Requested exclusion group {group_id!r} was not found.")
        try:
            # Preserve the project's established no-argument Group.content()
            # behavior; only the failure policy is made strict.
            members = group.content()
        except Exception as exc:
            raise ScanAbort(
                f"Could not read requested exclusion group {group_id!r}: {exc}"
            ) from exc
        ids = {
            str(getattr(item, "id", "")).strip().lower()
            for item in members
            if getattr(item, "id", None)
        }
        excluded.update(ids)
        logger.info(
            "Exclusion group %s (%s) contributes %d item id(s).",
            group_id, getattr(group, "title", "?") or "?", len(ids),
        )
    logger.info("Resolved %d unique excluded item id(s).", len(excluded))
    return excluded


def _coerce_size(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    try:
        rendered = float(value)
    except (TypeError, ValueError):
        return None
    if rendered < 0:
        return None
    return rendered


def _item_metadata(client, item):
    errors = []
    size_bytes = _coerce_size(item.get("size"))
    created = item.get("created")
    modified = item.get("modified")
    needs_detail = size_bytes is None or size_bytes <= 0 or created is None or modified is None

    if needs_detail:
        item_id = _safe_item_id(item.get("id"))
        try:
            data = client.get_json(
                f"/sharing/rest/content/items/{quote(item_id, safe='')}"
            )
            returned_id = str(data.get("id") or "").strip().lower()
            if returned_id != item_id.lower():
                raise RestCallError(
                    f"item-detail endpoint returned id {returned_id!r} for {item_id}"
                )
            if size_bytes is None or size_bytes <= 0:
                detailed_size = _coerce_size(data.get("size"))
                if detailed_size is None:
                    errors.append("item detail did not contain a valid size")
                    size_bytes = 0.0
                else:
                    size_bytes = detailed_size
            if created is None:
                created = data.get("created")
                if created is None:
                    errors.append("item detail did not contain a created date")
            if modified is None:
                modified = data.get("modified")
                if modified is None:
                    errors.append("item detail did not contain a modified date")
        except RestCallError as exc:
            errors.append(exc)

    if size_bytes is None:
        size_bytes = 0.0
    size_mb = round(float(size_bytes) / (1024.0 * 1024.0), 2)
    return (
        size_mb,
        epoch_to_str(created, fmt="%Y/%m/%d", utc=True),
        epoch_to_str(modified, fmt="%Y/%m/%d", utc=True),
        errors,
    )


def _classify_rest_error(exc, outcome, metadata=False):
    if metadata:
        outcome.metadata_errors += 1
    else:
        outcome.probe_errors += 1
    if isinstance(exc, RestCallError):
        outcome.tls_errors += int(exc.tls_error)
        outcome.auth_errors += int(exc.auth_error)
        outcome.transient_errors += int(exc.transient_error)
        outcome.transport_errors += int(exc.transport_error)


def _process_item_detailed(client, platform, item):
    item_id = _safe_item_id(item.get("id"))
    item_title = item.get("title", "")
    item_type = item.get("type", "")
    owner = item.get("owner", "")
    outcome = ItemOutcome([], [], str(item_type), 0, 0.0)

    try:
        item_size_mb, date_created, date_modified, metadata_errors = _item_metadata(
            client, item
        )
    except Exception as exc:
        # This protects against a programming/shape failure while still allowing
        # the relationship probes to run from the valid search result.
        item_size_mb = 0.0
        date_created = epoch_to_str(item.get("created"), fmt="%Y/%m/%d", utc=True)
        date_modified = epoch_to_str(item.get("modified"), fmt="%Y/%m/%d", utc=True)
        metadata_errors = [f"item metadata processing failed: {exc}"]

    outcome.size_mb = item_size_mb
    for metadata_error in metadata_errors:
        if isinstance(metadata_error, RestCallError):
            message = f"item metadata lookup failed: {metadata_error}"
            _classify_rest_error(metadata_error, outcome, metadata=True)
        else:
            message = str(metadata_error)
            outcome.metadata_errors += 1
        outcome.errors.append(message)

    found = []
    rel_path = (
        f"/sharing/rest/content/items/{quote(item_id, safe='')}/relatedItems"
    )
    for rel_type, direction in RELATIONSHIP_PROBES:
        try:
            data = _relationship_probe_json(
                client, rel_path, rel_type, direction
            )
            related = data.get("relatedItems")
            if related is None:
                related = []
            if not isinstance(related, list):
                raise RestCallError(
                    f"{rel_type}/{direction} returned a non-list relatedItems value"
                )
            malformed = 0
            for related_item in related:
                if not isinstance(related_item, dict):
                    malformed += 1
                    continue
                found.append((rel_type, direction, related_item))
            if malformed:
                raise RestCallError(
                    f"{rel_type}/{direction} returned {malformed} malformed related item(s)"
                )
        except Exception as exc:
            safe = redact_token(str(exc))
            outcome.errors.append(f"{rel_type}/{direction} probe failed: {safe}")
            _classify_rest_error(exc, outcome, metadata=False)

    outcome.relationships = len(found)
    base = {
        "platform": platform,
        "item_id": item_id,
        "title": item_title,
        "item_type": item_type,
        "owner": owner,
        "item_size_mb": item_size_mb,
        "date_created": date_created,
        "date_modified": date_modified,
    }

    for rel_type, direction, related in found:
        outcome.rows.append({
            **base,
            "related_item_id": related.get("id", ""),
            "related_title": related.get("title", ""),
            "related_type": related.get("type", ""),
            "related_owner": related.get("owner", ""),
            "relationship_type": rel_type,
            "direction": direction,
        })

    # Preserve the original blank-row assertion when every available
    # relationship probe completed successfully. Unsupported type/direction
    # pairs were normalized to successful empty probes above; only a genuine
    # probe failure prevents a misleading "no relationships" row.
    if not found and outcome.probe_errors == 0:
        outcome.rows.append({
            **base,
            "related_item_id": "",
            "related_title": "",
            "related_type": "",
            "related_owner": "",
            "relationship_type": "",
            "direction": "",
        })

    return outcome


# ---------------------------------------------------------------------------
# Backward-compatible helper names. The CLI uses the detailed implementations
# above, while these wrappers retain the original callable shapes.
# ---------------------------------------------------------------------------

def _compat_client(session, base_url, token_provider, gis=None):
    return PortalRestClient(
        PortalUrlPolicy(base_url), FixedSessionPool(session), token_provider, gis
    )


def search_items_test_sample(gis, session, base_url, item_type, sample_size,
                             org_clause, logger):
    org_id = _org_id_from_clause(org_clause)
    client = _compat_client(session, base_url, provider_for_gis(gis, get_platform(base_url)), gis)
    query = f'{org_clause} AND type:"{item_type}"'
    items = _strict_search_page(
        client, query, sample_size, org_id, page_size=sample_size
    )
    unique = {}
    for item in items:
        unique.setdefault(item["id"], item)
    sample = list(unique.values())[:sample_size]
    logger.info("TEST prescan | type '%-18s': %d item(s) sampled (target %d).",
                item_type, len(sample), sample_size)
    return sample


def search_items_single_type(gis, session, base_url, item_type, max_items,
                             org_clause, logger):
    org_id = _org_id_from_clause(org_clause)
    client = _compat_client(session, base_url, provider_for_gis(gis, get_platform(base_url)), gis)
    store = ItemStore(logger)
    try:
        counter = [0]
        _strict_bucketed_type_search(
            client, item_type, org_id, store, max_items, logger, counter
        )
        items = [item for _t, _iid, item in store.iter_items()]
    finally:
        store.close()
    logger.info("FULL scan  | type '%-18s': found %d item(s) to scan.",
                item_type, len(items))
    return items


def build_item_library(gis, session, base_url, per_type_limit, test_mode,
                       org_clause, logger, excluded_ids):
    library = {}
    flat = []
    logger.info("-" * 70)
    logger.info(
        "Building item library (%s) ...",
        f"TEST: up to {TEST_MODE_ITEM_LIMIT} per type"
        if test_mode else "FULL: all items per type",
    )
    for item_type in ITEM_TYPES:
        try:
            if test_mode:
                items = search_items_test_sample(
                    gis, session, base_url, item_type, per_type_limit,
                    org_clause, logger
                )
            else:
                items = search_items_single_type(
                    gis, session, base_url, item_type, per_type_limit,
                    org_clause, logger
                )
        except Exception as exc:
            logger.error("Search FAILED for type '%s': %s", item_type, exc)
            items = []
        items = [
            item for item in items
            if item.get("id") not in excluded_ids
        ]
        library[item_type] = items
        flat.extend(items)
    logger.info("-" * 70)
    logger.info("Item library summary (%s):", "TEST" if test_mode else "FULL")
    for item_type in ITEM_TYPES:
        logger.info("  %-18s : %d item(s)",
                    item_type, len(library.get(item_type, [])))
    logger.info("  %-18s : %d item(s)", "TOTAL", len(flat))
    logger.info("-" * 70)
    return library, flat


def get_item_size_and_dates(session, base_url, token_provider, item
                            ) -> Tuple[float, str, str]:
    client = _compat_client(session, base_url, token_provider)
    size_mb, created, modified, errors = _item_metadata(client, item)
    if errors:
        raise RuntimeError("; ".join(str(value) for value in errors))
    return size_mb, created, modified


def related_items_probe(session, base_url, token_provider, item_id
                        ) -> List[Tuple[str, str, Dict]]:
    client = _compat_client(session, base_url, token_provider)
    safe_id = _safe_item_id(item_id)
    path = f"/sharing/rest/content/items/{quote(safe_id, safe='')}/relatedItems"
    results = []
    for rel_type, direction in RELATIONSHIP_PROBES:
        data = _relationship_probe_json(
            client, path, rel_type, direction
        )
        related = data.get("relatedItems") or []
        if not isinstance(related, list):
            raise RuntimeError(
                f"{rel_type}/{direction} returned malformed relatedItems"
            )
        for item in related:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"{rel_type}/{direction} returned a malformed related item"
                )
            results.append((rel_type, direction, item))
    return results


def process_one_item(session, base_url, token_provider, platform, item):
    client = _compat_client(session, base_url, token_provider)
    try:
        outcome = _process_item_detailed(client, platform, item)
    except Exception as exc:
        item_type = item.get("type", "") if isinstance(item, dict) else ""
        return [], f"{item.get('id') if isinstance(item, dict) else '?'}: {exc}", item_type, 0, 0.0
    if outcome.errors:
        # Preserve the original imported-helper failure contract. The hardened
        # CLI uses _process_item_detailed() directly so it can retain confirmed
        # partial relationships while marking the run incomplete.
        return ([], "; ".join(outcome.errors), outcome.item_type, 0, 0.0)
    return (outcome.rows, None, outcome.item_type, outcome.relationships,
            outcome.size_mb)


def write_csv(rows, path, platform, logger):
    """Retain the original list-based helper for imported callers."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows:
            row["platform"] = platform
            writer.writerow(row)
    logger.info("Wrote %d row(s) to %s", len(rows), path)


def _write_row_store(row_store, path, platform, logger):
    import csv
    directory = os.path.dirname(os.path.abspath(path))
    fd, temp_path = tempfile.mkstemp(prefix=".related_output_", suffix=".csv", dir=directory)
    os.close(fd)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with open(temp_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            for row in row_store:
                copied = dict(row)
                copied["platform"] = platform
                writer.writerow(copied)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    logger.info("Wrote %d row(s) to %s", len(row_store), path)


def _reserve_run_paths(output_dir, org_prefix, platform, timestamp):
    for suffix in range(1000):
        run_stamp = timestamp if suffix == 0 else f"{timestamp}_{suffix:02d}"
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_related_items_{run_stamp}.log"
        )
        csv_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_RELATED_ITEMS_{run_stamp}.csv"
        )
        if os.path.exists(csv_path):
            continue
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_stamp, log_path, csv_path
    raise RuntimeError("Could not reserve a unique output name after 999 collisions.")


def _combined_attachments(log_path, csv_paths, output_dir, logger, cap_mb):
    try:
        log_mb = os.path.getsize(log_path) / (1024.0 * 1024.0)
    except OSError:
        log_mb = 0.0
    if log_mb > cap_mb:
        note = (
            f"\nNOTE: The log alone is {log_mb:.2f} MB, exceeding the "
            f"{cap_mb:.2f} MB email attachment limit. No files were attached. "
            f"The log remains on disk at:\n  {log_path}\n"
        )
        logger.warning("Log file exceeds the configured email limit; attaching nothing.")
        return [], note
    remaining = max(0.0, float(cap_mb) - log_mb)
    logger.debug(
        "Attachment cap accounting: configured=%.2f MB log=%.2f MB "
        "remaining-for-reports=%.2f MB.", cap_mb, log_mb, remaining,
    )
    return gather_attachments(
        log_path, csv_paths, output_dir, logger, cap_mb=remaining
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Find related items for data items in AGOL or Portal, write them "
            "to a timestamped CSV, then email a report. All searches are "
            "scoped to the authenticated organization."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (fast 20-per-type prescan library). "
            "Pass --no-test to scan the entire organization.\n\n"
            "Examples:\n"
            "  python GET_RELATED_ITEMS.py --config org1.config.toml --target portal\n"
            "  python GET_RELATED_ITEMS.py --config org2.config.toml --target agol --no-test --workers 16\n"
            "  python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --tls-mode os-trust-store\n"
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
            "FULL mode: scan all items up to the per-type safety ceiling. "
            "If omitted, the script runs in TEST mode and builds the same "
            "20-per-type prescan sample."
        ),
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("workers", 1, 64),
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS}; range 1-64).",
    )

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help=(
            "How this script's direct REST sessions verify certificates: "
            "standard, os-trust-store, or custom-ca-bundle. The same mode is "
            "passed to connect_gis()."
        ),
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help=(
            "PEM CA bundle path. Required when --tls-mode is "
            "custom-ca-bundle; ignored otherwise."
        ),
    )
    args = parser.parse_args()
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        parser.error(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle."
        )
    return args


def main():
    args = parse_args()
    start_wallclock, start_perf = start_timer()
    raw_timestamp = build_timestamp()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(
        args.target, args.config, default=MAX_ATTACHMENTS_MB
    )
    platform = get_platform(args.portal_url)

    per_type_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS_PER_TYPE
    org_prefix = args.org_name_safe
    ensure_output_dir(args.output_dir)
    timestamp, log_file_path, csv_path = _reserve_run_paths(
        args.output_dir, org_prefix, platform, raw_timestamp
    )
    logger = setup_logging(log_file_path, "related_items")
    subject = subject_line(
        EMAIL_SUBJECT_PREFIX, timestamp, platform, org_name=args.org_name
    )

    logger.info("=" * 70)
    logger.info("Get Related Items - run started")
    logger.info("=" * 70)
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST (20 items per type, fast prescan)"
                if args.test else "PRODUCTION (full scan)")
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("Output CSV: %s", csv_path)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Items per type: %d", per_type_limit)
    logger.info("Worker threads: %d", args.workers)
    logger.info("Relationship probes per item: %d", len(RELATIONSHIP_PROBES))
    logger.info("Item types (%d): %s", len(ITEM_TYPES), ", ".join(ITEM_TYPES))
    logger.info("Exclude group id(s): %s", args.exclude_group_id or "(none)")
    logger.info("=" * 70)

    initial_session = None
    session_pool = None
    item_store = None
    row_store = None
    search_diagnostics = SearchDiagnostics()
    run_diagnostics = RunDiagnostics()
    per_type_found = {item_type: 0 for item_type in ITEM_TYPES}
    per_type_probed = {item_type: 0 for item_type in ITEM_TYPES}
    per_type_rels = {item_type: 0 for item_type in ITEM_TYPES}
    per_type_size = {item_type: 0.0 for item_type in ITEM_TYPES}
    processed = 0
    csv_written = False
    rows_written = 0
    result = "ABORTED"
    abort_reason = None

    try:
        _validate_target_platform(args.target, platform, args.portal_url)
        logger.info("STEP 1/4: Establish verified TLS sessions and connect")
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        ca_path = (
            args.ca_bundle_path
            if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
        )
        initial_session = build_secure_session(
            mode,
            ca_bundle_path=ca_path,
            pool_connections=max(16, args.workers),
            pool_maxsize=max(32, args.workers * 2),
            retry_total=2,
            logger=logger,
        )
        try:
            handshake_ok, handshake_detail = verify_tls_handshake(
                args.portal_url, initial_session, logger=logger
            )
        except Exception as exc:
            initial_session.close()
            raise ScanAbort(
                f"TLS handshake setup failed: {redact_token(str(exc))}"
            ) from exc
        if not handshake_ok:
            initial_session.close()
            raise ScanAbort(
                f"TLS handshake verification to {args.portal_url} failed: "
                f"{redact_token(str(handshake_detail.get('error')))}"
            )

        gis = connect_gis(
            args.portal_url,
            args.username,
            args.keyring_service,
            logger,
            verify_mode=mode,
            ca_bundle_path=ca_path,
        )
        authenticated_base = portal_base_url(gis)
        authenticated_platform = get_platform(authenticated_base)
        _validate_target_platform(args.target, authenticated_platform, authenticated_base)
        token_provider = provider_for_gis(gis, platform)
        logger.info(
            "Token acquired via %s (token present: %s).",
            type(token_provider).__name__, token_provider.is_present(),
        )

        session_pool = WorkerSessionPool(
            mode, ca_path, initial_session, capacity=args.workers
        )
        policy = PortalUrlPolicy(authenticated_base)
        client = PortalRestClient(policy, session_pool, token_provider, gis)

        logger.info("STEP 2/4: Resolve org scope and requested exclusions")
        org_id = _safe_org_id(get_org_id(gis, logger))
        logger.info("Restricting all searches to org-owned content (orgid:%s).", org_id)
        excluded_ids = _strict_excluded_item_ids(
            gis, args.exclude_group_id, logger
        )

        item_store = ItemStore(logger)
        row_store = RowStore(logger)

        logger.info("STEP 3/4: Build item library and probe relationships")
        search_diagnostics = _build_item_store(
            client,
            per_type_limit,
            args.test,
            org_id,
            logger,
            excluded_ids,
            item_store,
        )
        for item_type in ITEM_TYPES:
            per_type_found[item_type] = item_store.count_type(item_type)

        total_items = item_store.total()
        logger.info("Total items to process across all types: %d", total_items)

        def worker(record):
            _item_type, _item_id, item = record
            return _process_item_detailed(client, platform, item)

        if total_items:
            logger.info("Probing relationships with %d worker(s) ...", args.workers)
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for record, future in _bounded_future_results(
                        executor, item_store.iter_items(), worker,
                        max_in_flight=max(1, args.workers * 2)):
                    item_type, item_id, _item = record
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        run_diagnostics.unexpected_worker_errors += 1
                        safe = redact_token(str(exc))
                        if len(run_diagnostics.error_samples) < MAX_ERROR_SAMPLES:
                            run_diagnostics.error_samples.append(
                                f"{item_id}: unexpected worker failure: {safe}"
                            )
                        logger.warning(
                            "PLATFORM=%s | Item %s unexpected worker failure: %s",
                            platform, item_id, safe,
                        )
                        outcome = ItemOutcome([], [safe], item_type, 0, 0.0)

                    processed += 1
                    metric_type = outcome.item_type
                    if metric_type in per_type_probed:
                        per_type_probed[metric_type] += 1
                        per_type_rels[metric_type] += outcome.relationships
                        per_type_size[metric_type] += outcome.size_mb
                    row_store.add_many(outcome.rows)
                    run_diagnostics.add_outcome(item_id, outcome)
                    for message in outcome.errors:
                        logger.warning(
                            "PLATFORM=%s | Item %s incomplete: %s",
                            platform, item_id, redact_token(str(message)),
                        )
                    if processed % 50 == 0 or processed == total_items:
                        logger.info(
                            "PLATFORM=%s | Progress: %d / %d items processed "
                            "(%d row(s), %d item error(s)).",
                            platform, processed, total_items, len(row_store),
                            run_diagnostics.item_errors,
                        )
        else:
            logger.warning("No items found. A header-only CSV will be written.")

        total_size = sum(per_type_size.values())
        logger.info("-" * 70)
        logger.info("PER-TYPE SCAN CONFIRMATION "
                    "(found / probed / relationships / size MB):")
        for item_type in ITEM_TYPES:
            flag = "" if per_type_found[item_type] else "   <-- none found"
            logger.info(
                "  %-18s : found %3d | probed %3d | relationships %4d | "
                "size %12.2f MB%s",
                item_type, per_type_found[item_type],
                per_type_probed[item_type], per_type_rels[item_type],
                per_type_size[item_type], flag,
            )
        logger.info("  %-18s : %38s size %12.2f MB", "TOTAL", "", total_size)
        logger.info("-" * 70)

        logger.info("STEP 4/4: Write report")
        _write_row_store(row_store, csv_path, platform, logger)
        rows_written = len(row_store)
        csv_written = True

        incomplete = bool(
            search_diagnostics.failed_types
            or run_diagnostics.item_errors
            or processed != total_items
        )
        result = "INCOMPLETE" if incomplete else "COMPLETE"

    except Exception as exc:
        abort_reason = redact_token(str(exc))
        result = "ABORTED"
        logger.exception("Run aborted: %s", abort_reason)
    finally:
        if row_store is not None:
            try:
                row_store.close()
            except Exception:
                pass
        if item_store is not None:
            try:
                item_store.close()
            except Exception:
                pass
        if session_pool is not None:
            session_pool.close()
        elif initial_session is not None:
            try:
                initial_session.close()
            except Exception:
                pass

    total_size = sum(per_type_size.values())

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result: %s", result)
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "PRODUCTION")
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Items processed: %d", processed)
    logger.info("Rows written: %d", rows_written)
    logger.info("Search type failures: %d", len(search_diagnostics.failed_types))
    logger.info("Items with incomplete checks: %d", run_diagnostics.item_errors)
    logger.info("Metadata errors: %d", run_diagnostics.metadata_errors)
    logger.info("Relationship probe errors: %d", run_diagnostics.probe_errors)
    logger.info("Unexpected worker errors: %d",
                run_diagnostics.unexpected_worker_errors)
    logger.info("TLS/CERT errors: %d",
                search_diagnostics.tls_errors + run_diagnostics.tls_errors)
    logger.info("Authentication errors: %d",
                search_diagnostics.auth_errors + run_diagnostics.auth_errors)
    logger.info("Transient errors: %d",
                search_diagnostics.transient_errors + run_diagnostics.transient_errors)
    logger.info("Transport errors: %d",
                search_diagnostics.transport_errors + run_diagnostics.transport_errors)
    logger.info("Total size (MB): %.2f", total_size)
    logger.info("Output CSV: %s", csv_path if csv_written else "(NOT WRITTEN)")
    if abort_reason:
        logger.info("Abort reason: %s", abort_reason)
    logger.info("=" * 70)

    all_samples = list(search_diagnostics.errors) + list(run_diagnostics.error_samples)
    if all_samples:
        logger.warning("Incomplete-check reason(s), up to %d retained:", MAX_ERROR_SAMPLES)
        for sample in all_samples[:MAX_ERROR_SAMPLES]:
            logger.warning("  %s", sample)

    per_type_lines = "\n".join(
        f"  {item_type:<18}: found {per_type_found[item_type]:>3} | "
        f"probed {per_type_probed[item_type]:>3} | "
        f"relationships {per_type_rels[item_type]:>4} | "
        f"size {per_type_size[item_type]:>12.2f} MB"
        for item_type in ITEM_TYPES
    )
    per_type_lines += (
        f"\n  {'TOTAL':<18}: {'':>38} size {total_size:>12.2f} MB"
    )

    body = redact_token(
        f"Get Related Items\n"
        f"Result: {result}\n"
        f"Organization: {args.org_name}\n"
        f"Config file: {args.config}\n"
        f"Platform: {platform}\n"
        f"Mode: {'TEST (20 items per type)' if args.test else 'PRODUCTION (full scan)'}\n"
        f"TLS mode: {args.tls_mode}\n"
        f"Username: {args.username}\n"
        f"Excluded group(s): {args.exclude_group_id or '(none)'}\n\n"
        f"Items processed: {processed}\n"
        f"Rows written: {rows_written}\n"
        f"Search type failures: {len(search_diagnostics.failed_types)}\n"
        f"Items with incomplete checks: {run_diagnostics.item_errors}\n"
        f"Metadata errors: {run_diagnostics.metadata_errors}\n"
        f"Relationship probe errors: {run_diagnostics.probe_errors}\n"
        f"Unexpected worker errors: "
        f"{run_diagnostics.unexpected_worker_errors}\n"
        f"TLS/CERT errors: "
        f"{search_diagnostics.tls_errors + run_diagnostics.tls_errors}\n"
        f"Authentication errors: "
        f"{search_diagnostics.auth_errors + run_diagnostics.auth_errors}\n"
        f"Total size (MB): {total_size:.2f}\n"
        f"Output CSV: {csv_path if csv_written else '(NOT WRITTEN)'}\n"
        + (f"Abort reason: {abort_reason}\n" if abort_reason else "")
        + ("\nWARNING: This report is incomplete. Absence of a relationship "
           "row must not be interpreted as proof that none exists.\n"
           if result == "INCOMPLETE" else "")
        + f"\nPer-type scan confirmation:\n{per_type_lines}\n\n"
        + timing_block(start_wallclock, start_perf)
    )

    attachments, note = _combined_attachments(
        log_file_path,
        [csv_path] if csv_written else [],
        args.output_dir,
        logger,
        max_email_mb,
    )
    body = redact_token(body + note)
    final_subject = subject if result == "COMPLETE" else f"{subject} ({result})"
    send_email(
        final_subject, body, attachments, args.smtp_server,
        args.email_to, args.email_from, logger,
    )

    logger.info("=" * 70)
    logger.info("Get Related Items finished with result %s", result)
    logger.info("=" * 70)
    if result == "ABORTED" or not csv_written:
        logger.error(
            "Get Related Items did not complete; returning exit status 1."
        )
        return 1
    if result == "INCOMPLETE":
        logger.warning(
            "Get Related Items completed with contained search/item "
            "error(s); returning exit status 0 because the workflow ran "
            "to completion. The report remains marked INCOMPLETE because "
            "some relationship checks could not be fully resolved."
        )
    else:
        logger.info("Get Related Items completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
