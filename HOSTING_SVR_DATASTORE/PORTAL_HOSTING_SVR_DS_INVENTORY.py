#!/usr/bin/env python3
r"""
PORTAL_HOSTING_SVR_DS_INVENTORY.py

Read-only reconciliation of Portal hosted feature-service table references
against the PostgreSQL relational Data Store, with optional .msd
service-definition scanning. Portal-only. The live PRIMARY Data Store
machine is auto-detected on every run (HA-aware) unless --db-host forces a
specific host.

Authentication uses connect_gis() (always TLS-verified) for the arcgis SDK
connection. This script's own direct requests.Session (used for the
hosting-server admin API and item-existence REST calls) uses an explicit,
caller-chosen TLS verification strategy (--tls-mode), proven once at
startup via a real handshake check. OS trust is the default for this
Portal-only script so organizational CA chains trusted by Windows are used.
Tokens are centrally held in a TokenProvider (UTILITIES.auth) and exposed
only at the request-building boundary. Token-bearing REST traffic is limited
to the authenticated Portal and its registered hosting server, uses HTTPS, and
refuses redirects. The PostgreSQL session requires TLS and is forced read-only.

Usage:
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --force
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --workers 32 --no-lifecycle-probe
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --folder QA
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --arcgisinput "D:\\arcgissystem\\arcgisinput"
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --db-host datastore.example.com
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --domain-suffix example.com
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --tls-mode os-trust-store
    python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --db-sslmode verify-full --db-sslrootcert C:\\certs\\datastore-ca.pem

See readme.md for full documentation, column descriptions, and scenario
walkthroughs of every command-line option.
"""

import argparse
import csv
import glob
import ipaddress
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock, local
from urllib.parse import quote, urlsplit

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _UTILITIES  # noqa: E402

_EXPECTED_UTILITIES_DIR = os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_UTILITIES, "__file__", ""))
)
if os.path.normcase(_ACTUAL_UTILITIES_DIR) != os.path.normcase(_EXPECTED_UTILITIES_DIR):
    raise ImportError(
        "Refusing to import UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected {_EXPECTED_UTILITIES_DIR!r}."
    )

from UTILITIES import (  # noqa: E402
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    start_timer,
    timing_block,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_config_arg,
    subject_line,
    finalize_config,
    MAX_ATTACHMENTS_MB,
    provider_for_gis,
    redact_token,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES import config as _config  # noqa: E402
from UTILITIES.stack_discovery import short_hostname, looks_like_fqdn  # noqa: E402

try:
    import keyring
except ImportError:  # pragma: no cover - deployment dependency
    keyring = None

try:
    import requests
except ImportError:  # pragma: no cover - deployment dependency
    requests = None

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_AVAILABLE = True
except ImportError:  # pragma: no cover - deployment dependency
    psycopg2 = None
    _PSYCOPG2_AVAILABLE = False


EMAIL_SUBJECT_PREFIX = "Hosting Server / Data Store Inventory"

TARGET = "portal"

MB = 1024 * 1024

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

CSV_FIELDS = [
    "status", "table_name", "raw_table_name", "db_exists", "db_relkind",
    "db_owner", "db_size_pretty", "db_size_bytes", "service_is_view",
    "service_name", "service_type", "service_admin_url", "reference_source",
    "source_file", "layer_id", "layer_name", "kind", "portal_item_ids",
    "portal_items_exist", "portal_item_titles", "portal_item_missing",
    "possible_owning_service", "notes",
]

RELKIND = {"r": "table", "p": "partitioned table", "v": "view",
           "m": "materialized view", "f": "foreign table"}

IGNORE_RELNAME = re.compile(r"^i\d+$")

DEFAULT_VVIEW_SUFFIXES = ("_evw",)
TRACK_TABLE_SUFFIXES = ("__track_att", "__track_del", "__track")

CACHE_VERSION = 3
DEFAULT_WORKERS = 24
MAX_WORKERS = 64
MAX_IN_FLIGHT_FACTOR = 2
HTTP_TIMEOUT = 30
ADMIN_LIST_TIMEOUT = 180
DB_STATEMENT_TIMEOUT_MS = 300000
MAX_JSON_BYTES = 64 * MB
MAX_CACHE_BYTES = 128 * MB
MAX_MSD_FILES = 1000000
MAX_MSD_MEMBERS = 100000
MAX_MSD_MEMBER_BYTES = 256 * MB
MAX_MSD_TOTAL_BYTES = 2 * 1024 * MB
MAX_MSD_COMPRESSION_RATIO = 1000

_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:invalid|expired).*token|token.*(?:invalid|expired)|"
    r"authentication token|required token|not authenticated"
)
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


def _size_mb(size_bytes):
    if size_bytes in (None, ""):
        return ""
    try:
        return f"{float(size_bytes) / MB:.2f}"
    except (TypeError, ValueError):
        return ""


STAGE_CONFIG = "CONFIG"
STAGE_PORTAL = "PORTAL"
STAGE_DATASTORE = "DATASTORE_DISCOVERY"
STAGE_DB = "DATABASE"
STAGE_SERVICE = "SERVICE"


class StepError(Exception):
    def __init__(self, stage, msg):
        super().__init__(msg)
        self.stage = stage


class ArcGISRequestError(RuntimeError):
    def __init__(self, message, *, status=None, code=None, tls_error=False):
        super().__init__(redact_token(str(message)))
        self.status = status
        self.code = code
        self.tls_error = bool(tls_error)


class TokenManager:
    """Serialize refresh while normal request-time token reads remain cheap."""

    def __init__(self, provider, refresh_callback=None):
        self.provider = provider
        self.refresh_callback = refresh_callback
        self._refresh_lock = Lock()

    def get(self):
        return self.provider.get()

    def refresh_if_stale(self, token_used):
        with self._refresh_lock:
            current = self.provider.get()
            if current == token_used:
                if self.refresh_callback is not None:
                    try:
                        self.refresh_callback()
                    except Exception:
                        # The provider refresh below is still attempted. The
                        # request retry remains bounded to one attempt.
                        pass
                self.provider.refresh()
            return self.provider.get()


class WorkerSessionPool:
    """Give each worker thread its own Requests session and TLS policy."""

    def __init__(self, mode, ca_bundle_path):
        self.mode = mode
        self.ca_bundle_path = ca_bundle_path
        self._local = local()
        self._create_lock = Lock()
        self._sessions = []

    def get(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        # OS-trust-store injection is process-wide. Serialize construction so
        # two first-use worker threads cannot race that setup.
        with self._create_lock:
            session = build_secure_session(
                self.mode,
                ca_bundle_path=self.ca_bundle_path,
                pool_connections=2,
                pool_maxsize=4,
                retry_total=3,
                logger=None,
            )
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self):
        for session in self._sessions:
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


class CacheRunLock:
    """Cross-process lock protecting the stable incremental cache."""

    def __init__(self, path):
        self.path = path
        self._handle = None
        self._backend = None

    def acquire(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        handle = None
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(fd, "r+b")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                self._backend = ("nt", msvcrt)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._backend = ("posix", fcntl)
            self._handle = handle
        except OSError as exc:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            raise StepError(
                STAGE_CONFIG,
                "Another inventory process appears to hold the incremental "
                f"cache lock {self.path!r}. Refusing concurrent cache use.",
            ) from exc

    def release(self):
        if self._handle is None:
            return
        try:
            backend, module = self._backend
            if backend == "nt":
                self._handle.seek(0)
                module.locking(self._handle.fileno(), module.LK_UNLCK, 1)
            else:
                module.flock(self._handle.fileno(), module.LOCK_UN)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        self._handle = None
        self._backend = None


class ArcGISJsonClient:
    """Strict, token-refreshing client for trusted Portal/admin origins."""

    def __init__(self, session_getter, token_manager, trusted_origins):
        self._session_getter = session_getter
        self._tokens = token_manager
        self._trusted_origins = set(trusted_origins)

    def add_trusted_base(self, base_url):
        self._trusted_origins.add(_origin(base_url))

    @staticmethod
    def _rest_error(data):
        if not isinstance(data, dict):
            return None
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or "unknown ArcGIS REST error"
            details = "; ".join(str(v) for v in (err.get("details") or []))
            text = f"REST error {code}: {message}"
            if details:
                text += f" ({details})"
            return code, text
        if str(data.get("status", "")).strip().lower() == "error":
            messages = data.get("messages") or data.get("message") or \
                "unknown ArcGIS admin error"
            if isinstance(messages, (list, tuple)):
                messages = "; ".join(str(v) for v in messages)
            return data.get("code"), f"ArcGIS admin error: {messages}"
        return None

    @staticmethod
    def _is_auth_error(status, code, message):
        if status in {401, 403, 498, 499}:
            return True
        if str(code).strip() in {"401", "403", "498", "499"}:
            return True
        return bool(_AUTH_ERROR_RE.search(str(message or "")))

    def _validate_url(self, url):
        try:
            parsed = urlsplit(str(url or "").strip())
            port = parsed.port or 443
        except (TypeError, ValueError) as exc:
            raise ArcGISRequestError(f"Could not parse request URL: {exc}") from exc
        if parsed.scheme.lower() != "https":
            raise ArcGISRequestError("Token-bearing REST requests must use HTTPS")
        if not parsed.hostname:
            raise ArcGISRequestError("REST request URL has no hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ArcGISRequestError(
                "REST request URL contains user-info credentials"
            )
        if parsed.fragment:
            raise ArcGISRequestError("REST request URL contains a fragment")
        origin = (parsed.hostname.lower().rstrip("."), port)
        if origin not in self._trusted_origins:
            raise ArcGISRequestError(
                f"REST request origin https://{origin[0]}:{origin[1]} is not trusted"
            )
        return parsed.geturl()

    def request(self, method, url, *, params=None, data=None,
                timeout=HTTP_TIMEOUT):
        checked_url = self._validate_url(url)
        for attempt in range(2):
            token = self._tokens.get()
            extra_params = dict(params or {})
            extra_data = dict(data or {})
            for reserved in ("token", "f"):
                extra_params.pop(reserved, None)
                extra_data.pop(reserved, None)
            query = {"f": "json", "token": token}
            form = None
            if method.upper() == "GET":
                query.update(extra_params)
            else:
                form = {"f": "json", "token": token}
                form.update(extra_data)
                query = extra_params or None
            try:
                response = self._session_getter().request(
                    method.upper(), checked_url,
                    params=query,
                    data=form,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.SSLError as exc:
                raise ArcGISRequestError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise ArcGISRequestError(
                    f"Request timed out after {timeout}s"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise ArcGISRequestError(f"REST request failed: {exc}") from exc

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                try:
                    response.close()
                except Exception:
                    pass
                raise ArcGISRequestError(
                    f"Unexpected HTTP {response.status_code} redirect"
                    + (f" to {location}" if location else ""),
                    status=response.status_code,
                )

            if attempt == 0 and response.status_code in {401, 403, 498, 499}:
                try:
                    response.close()
                except Exception:
                    pass
                self._tokens.refresh_if_stale(token)
                continue

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_JSON_BYTES:
                        try:
                            response.close()
                        except Exception:
                            pass
                        raise ArcGISRequestError(
                            f"REST response exceeds the {MAX_JSON_BYTES} byte limit",
                            status=response.status_code,
                        )
                except ValueError:
                    pass
            try:
                chunks = []
                total = 0
                iterator = getattr(response, "iter_content", None)
                if callable(iterator):
                    for chunk in iterator(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_JSON_BYTES:
                            raise ArcGISRequestError(
                                f"REST response exceeds the {MAX_JSON_BYTES} "
                                "byte limit", status=response.status_code
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                else:  # lightweight test doubles / older adapters
                    content = response.content or b""
                    if len(content) > MAX_JSON_BYTES:
                        raise ArcGISRequestError(
                            f"REST response exceeds the {MAX_JSON_BYTES} byte limit",
                            status=response.status_code,
                        )
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                parsed_data = json.loads(content.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as exc:
                if response.status_code >= 400:
                    raise ArcGISRequestError(
                        f"HTTP {response.status_code} {response.reason}",
                        status=response.status_code,
                    ) from exc
                raise ArcGISRequestError(
                    f"REST endpoint returned non-JSON data "
                    f"(HTTP {response.status_code})"
                ) from exc

            rest_error = self._rest_error(parsed_data)
            code = rest_error[0] if rest_error else None
            message = rest_error[1] if rest_error else None
            if response.status_code >= 400 and message is None:
                message = f"HTTP {response.status_code} {response.reason}"
            if message is not None:
                if attempt == 0 and self._is_auth_error(
                        response.status_code, code, message):
                    self._tokens.refresh_if_stale(token)
                    continue
                raise ArcGISRequestError(
                    message, status=response.status_code, code=code
                )
            if not isinstance(parsed_data, dict):
                raise ArcGISRequestError(
                    "REST endpoint returned JSON that was not an object",
                    status=response.status_code,
                )
            return parsed_data
        raise ArcGISRequestError("Authentication retry failed")

    def get(self, url, *, timeout=HTTP_TIMEOUT, **params):
        return self.request("GET", url, params=params, timeout=timeout)

    def post(self, url, *, timeout=HTTP_TIMEOUT, **data):
        return self.request("POST", url, data=data, timeout=timeout)


@dataclass(frozen=True)
class IdentifierPart:
    value: str
    quoted: bool = False


def _parse_identifier_path(value):
    """Parse dot-qualified PostgreSQL identifiers, honoring doubled quotes."""
    text = str(value or "").strip()
    if not text:
        return []
    parts = []
    chars = []
    in_quotes = False
    part_quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_quotes:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    chars.append('"')
                    index += 2
                    continue
                in_quotes = False
            else:
                chars.append(char)
            index += 1
            continue
        if char == '"':
            if "".join(chars).strip():
                return []
            in_quotes = True
            part_quoted = True
        elif char == ".":
            rendered = "".join(chars).strip()
            if not rendered:
                return []
            parts.append(IdentifierPart(rendered, part_quoted))
            chars = []
            part_quoted = False
        else:
            chars.append(char)
        index += 1
    if in_quotes:
        return []
    rendered = "".join(chars).strip()
    if not rendered:
        return []
    parts.append(IdentifierPart(rendered, part_quoted))
    return parts[-2:]


def _identifier_value(part):
    return part.value if part.quoted else part.value.lower()


def normalize_table(name):
    """Preserve legacy lower-case output while parsing quoted dots correctly."""
    if not name:
        return ""
    parts = _parse_identifier_path(name)
    if parts:
        return ".".join(_identifier_value(part).lower() for part in parts)
    # Backward-compatible fallback for malformed identifiers.
    fallback = str(name).replace('"', "").split(".")
    if len(fallback) >= 2:
        fallback = fallback[-2:]
    return ".".join(part.strip().lower() for part in fallback if part.strip())


def _schema_from_raw(raw_name, normalized_name=""):
    parts = _parse_identifier_path(raw_name)
    if len(parts) == 2:
        return _identifier_value(parts[0])
    normalized = str(normalized_name or "")
    if "." in normalized:
        return normalized.split(".", 1)[0]
    return None


def _bounded_futures(function, inputs, workers):
    """Yield (input, future) while limiting queued work to about 2x workers."""
    iterator = iter(inputs)
    max_pending = max(workers, workers * MAX_IN_FLIGHT_FACTOR)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending = {}

        def fill():
            while len(pending) < max_pending:
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                pending[executor.submit(function, value)] = value

        fill()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                value = pending.pop(future)
                yield value, future
            fill()


def _normalize_https_base(value, label, *, strip_home=False,
                          strip_server_suffixes=False):
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port or 443
    except (TypeError, ValueError) as exc:
        raise StepError(STAGE_CONFIG, f"{label} is invalid: {exc}") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise StepError(STAGE_CONFIG, f"{label} must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise StepError(STAGE_CONFIG, f"{label} must not contain user-info")
    if parsed.query or parsed.fragment:
        raise StepError(
            STAGE_CONFIG, f"{label} must not contain a query or fragment"
        )
    path = (parsed.path or "").rstrip("/")
    if strip_home and path.lower().endswith("/home"):
        path = path[:-len("/home")]
    if strip_server_suffixes:
        for suffix in ("/admin", "/rest"):
            if path.lower().endswith(suffix):
                path = path[:-len(suffix)]
                break
    host = parsed.hostname.lower().rstrip(".")
    try:
        rendered_host = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        rendered_host = host
    rendered_port = "" if port == 443 else f":{port}"
    return f"https://{rendered_host}{rendered_port}{path}"


def _origin(value):
    parsed = urlsplit(value)
    return parsed.hostname.lower().rstrip("."), parsed.port or 443


def _server_base_key(value):
    parsed = urlsplit(value)
    return (
        parsed.hostname.lower().rstrip("."),
        parsed.port or 443,
        (parsed.path or "").rstrip("/").lower(),
    )


def _mapping_value(obj, key, default=None):
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    try:
        getter = getattr(obj, "get", None)
        if callable(getter):
            return getter(key, default)
    except Exception:
        pass
    try:
        return getattr(obj, key)
    except Exception:
        return default


def _federation_entries(gis, portal_client, portal_base, logger):
    entries = None
    sdk_error = None
    try:
        raw = gis.admin.federation.servers
        if callable(raw):
            raw = raw()
        if isinstance(raw, (list, tuple)):
            entries = list(raw)
        else:
            candidate = _mapping_value(raw, "servers")
            if isinstance(candidate, (list, tuple)):
                entries = list(candidate)
    except Exception as exc:
        sdk_error = redact_token(str(exc))

    if entries is None:
        try:
            data = portal_client.get(
                f"{portal_base}/portaladmin/federation/servers"
            )
            candidate = data.get("servers")
            if isinstance(candidate, list):
                entries = candidate
        except Exception as exc:
            detail = redact_token(str(exc))
            if sdk_error:
                detail = f"SDK: {sdk_error}; REST fallback: {detail}"
            raise StepError(
                STAGE_PORTAL,
                f"Could not read Portal federation metadata: {detail}",
            ) from exc
    if not isinstance(entries, list):
        raise StepError(
            STAGE_PORTAL,
            "Portal federation metadata did not contain a server list",
        )
    logger.info("Portal federation registry returned %d server entry/entries.",
                len(entries))
    return entries


def _validate_registered_hosting_server(gis, portal_client, portal_base,
                                        configured_hosting_url, logger):
    wanted = _server_base_key(configured_hosting_url)
    entries = _federation_entries(gis, portal_client, portal_base, logger)
    matched = None
    matched_urls = []
    for entry in entries:
        candidates = []
        for key in ("url", "adminUrl", "serverUrl"):
            value = _mapping_value(entry, key)
            if not value:
                continue
            try:
                normalized = _normalize_https_base(
                    value, f"federated server {key}",
                    strip_server_suffixes=True,
                )
            except StepError as exc:
                logger.warning("Ignoring invalid federation URL: %s", exc)
                continue
            candidates.append(normalized)
        if any(_server_base_key(value) == wanted for value in candidates):
            matched = entry
            matched_urls = candidates
            break
    if matched is None:
        raise StepError(
            STAGE_PORTAL,
            "Configured hosting-url is not an exact public/admin base in the "
            "authenticated Portal federation registry. Refusing to send the "
            f"Portal token to {configured_hosting_url}.",
        )
    role = str(
        _mapping_value(matched, "serverRole", "")
        or _mapping_value(matched, "role", "")
    ).strip().upper()
    if role and role not in {"HOSTING_SERVER", "HOSTING"}:
        raise StepError(
            STAGE_PORTAL,
            f"Configured hosting-url belongs to federation role {role!r}, "
            "not the hosting server.",
        )
    server_name = (_mapping_value(matched, "name")
                   or _mapping_value(matched, "id") or "unknown")
    logger.info("Validated hosting URL against federation server %r (%s).",
                server_name, ", ".join(matched_urls))


def _normalize_hostname(value, label):
    raw = str(value or "").strip().rstrip(".")
    if not raw:
        raise StepError(STAGE_CONFIG, f"{label} is blank")
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        ipaddress.ip_address(raw)
        return raw
    except ValueError:
        pass
    labels = raw.split(".")
    if any(not _HOST_LABEL_RE.fullmatch(part) for part in labels):
        raise StepError(STAGE_CONFIG, f"{label} is not a valid hostname: {value!r}")
    return raw


def _normalize_domain_suffix(value):
    suffix = str(value or "").strip().strip(".")
    if not suffix:
        return None
    return _normalize_hostname(suffix, "domain-suffix").lower()


def _reserve_run_paths(output_dir, org_prefix, platform, timestamp):
    for sequence in range(1000):
        run_stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_ds_inventory_{run_stamp}.log"
        )
        csv_path = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_DATASTORE_INVENTORY_{run_stamp}.csv",
        )
        if os.path.exists(csv_path):
            continue
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_stamp, log_path, csv_path
    raise StepError(
        STAGE_CONFIG,
        "Could not reserve a unique output name after 1,000 attempts",
    )


def _discover_data_store(client, hosting_url, logger):
    """Strict Data Store discovery without logging the raw admin response."""
    data = client.post(
        hosting_url.rstrip("/") + "/admin/data/findItems",
        ancestorPath="/enterpriseDatabases",
    )
    items_raw = data.get("items")
    if not isinstance(items_raw, list):
        raise StepError(
            STAGE_DATASTORE,
            "Data Store findItems response did not contain an 'items' list",
        )

    items = []
    candidate_primaries = []
    any_ambiguous = False
    all_fqdns = []
    for item_data in items_raw:
        if not isinstance(item_data, Mapping):
            logger.warning("Ignoring malformed Data Store registration entry.")
            continue
        item_path = str(_mapping_value(item_data, "path", "") or "")
        item_name = item_path.rstrip("/").rsplit("/", 1)[-1] or item_path
        info = _mapping_value(item_data, "info", {}) or {}
        machines = _mapping_value(info, "machines", []) if info else []
        if not isinstance(machines, list) or not machines:
            # Ordinary enterprise database registrations can share this path
            # but do not describe ArcGIS Data Store machines. Preserve them in
            # diagnostics without treating them as an HA ambiguity.
            items.append({
                "name": item_name,
                "path": item_path,
                "type": _mapping_value(item_data, "type"),
                "primary": None,
                "standby": [],
                "all_machines": [],
                "ambiguous": False,
            })
            continue

        all_names = []
        primary_names = []
        standby = []
        role_present = False
        for machine in machines:
            if not isinstance(machine, Mapping):
                continue
            name = _mapping_value(machine, "name")
            if not name:
                continue
            name = str(name).strip()
            role = str(_mapping_value(machine, "role", "") or "").upper()
            all_names.append(name)
            role_present = role_present or bool(role)
            if role == "PRIMARY":
                primary_names.append(name)
            elif role == "STANDBY":
                standby.append(name)

        item_ambiguous = False
        primary = None
        if len(primary_names) == 1:
            primary = primary_names[0]
        elif len(primary_names) > 1:
            item_ambiguous = True
        elif len(all_names) == 1 and not role_present:
            primary = all_names[0]
        else:
            item_ambiguous = True

        if primary:
            candidate_primaries.append(primary)
        if item_ambiguous:
            any_ambiguous = True
        all_fqdns.extend(all_names)
        items.append({
            "name": item_name,
            "path": item_path,
            "type": _mapping_value(item_data, "type"),
            "primary": primary,
            "standby": standby,
            "all_machines": all_names,
            "ambiguous": item_ambiguous,
        })
        logger.info(
            "Data Store item %r: machines=%s primary=%s standby=%s "
            "ambiguous=%s",
            item_name, all_names or "none", primary, standby or "none",
            item_ambiguous,
        )

    distinct = {name.lower(): name for name in candidate_primaries}
    if len(distinct) > 1:
        any_ambiguous = True
    resolved = next(iter(distinct.values())) if len(distinct) == 1 else None
    return {
        "host": resolved,
        "fqdns": all_fqdns,
        "items": items,
        "ambiguous": any_ambiguous,
        "error": None,
    }

def find_table_names(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "tableName" and isinstance(v, str) and v:
                yield v
            else:
                yield from find_table_names(v)
    elif isinstance(node, list):
        for item in node:
            yield from find_table_names(item)


def modified_marker(lifecycle_json):
    best = 0

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            for k, v in node.items():
                if (isinstance(v, (int, float)) and not isinstance(v, bool)
                        and "modif" in k.lower()):
                    best = max(best, int(v))
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(lifecycle_json)
    return best


def parse_service_tables(j):
    jp = j.get("jsonProperties", {}) or {}
    svc_is_view = bool(jp.get("isView"))
    rows, seen = [], set()

    def entries():
        for kind, container in (("layer", jp.get("layers")),
                                ("table", jp.get("tables")),
                                ("layer", j.get("layers")),
                                ("table", j.get("tables"))):
            if isinstance(container, dict):
                for e in container.values():
                    yield kind, e
            elif isinstance(container, list):
                for e in container:
                    yield kind, e

    for kind, e in entries():
        if not isinstance(e, dict):
            continue
        ali = e.get("adminLayerInfo", {}) or {}
        raw = ali.get("tableName") or next(iter(find_table_names(e)), None)
        if not raw:
            continue
        norm = normalize_table(raw)
        key = (norm, e.get("id"), kind)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "raw_table_name": raw,
            "table_name": norm,
            "_db_schema": _schema_from_raw(raw, norm),
            "is_view": bool(svc_is_view or e.get("isView")
                            or "viewLayerDefinition" in json.dumps(ali)),
            "layer_id": e.get("id"),
            "layer_name": e.get("name"),
            "kind": kind,
        })

    if not rows:
        for raw in dict.fromkeys(find_table_names(j)):
            norm = normalize_table(raw)
            rows.append({"raw_table_name": raw, "table_name": norm,
                         "_db_schema": _schema_from_raw(raw, norm),
                         "is_view": svc_is_view, "layer_id": None,
                         "layer_name": None, "kind": "layer"})
    return rows


def extract_portal_items(j):
    ids = []
    pp = j.get("portalProperties") or {}
    for item in (pp.get("portalItems") or []):
        value = item.get("itemID") if isinstance(item, dict) else item
        if value is None or value == "":
            continue
        ids.append(value if isinstance(value, str) else str(value))
    return list(dict.fromkeys(ids))


def extract_companion_tables(node, out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower().endswith("tableinfo") and isinstance(v, dict):
                for nk in ("name", "viewName"):
                    val = v.get(nk)
                    if isinstance(val, str) and val:
                        out.append(val)
            extract_companion_tables(v, out)
    elif isinstance(node, list):
        for item in node:
            extract_companion_tables(item, out)
    return out


def _admin_services_path(folder, service=None, suffix=None):
    parts = ["admin", "services"]
    if folder:
        parts.append(quote(folder, safe=""))
    if service:
        parts.append(quote(service, safe="."))
    if suffix:
        parts.append(quote(suffix, safe=""))
    return "/".join(parts)


class Admin:
    def __init__(self, hosting_url, token_provider, session, logger,
                 timeout=HTTP_TIMEOUT, list_timeout=ADMIN_LIST_TIMEOUT,
                 client=None):
        self.base = hosting_url.rstrip("/")
        self.token_provider = token_provider
        self.session = session
        self.logger = logger
        self.timeout = timeout
        self.list_timeout = list_timeout
        self.client = client

    def get(self, path, timeout=None, **params):
        url = f"{self.base}/{path.lstrip('/')}"
        if self.client is not None:
            return self.client.get(url, timeout=timeout or self.timeout, **params)
        params.update(f="json", token=self.token_provider.get())
        response = self.session.get(
            url, params=params, timeout=timeout or self.timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and (
                data.get("status") == "error" or "error" in data):
            raise RuntimeError(
                redact_token(f"{path}: {data.get('messages') or data.get('error')}")
            )
        if not isinstance(data, dict):
            raise RuntimeError(f"{path}: response was not a JSON object")
        return data

    def list_feature_services(self, folder):
        path = _admin_services_path(folder)
        data = self.get(path, timeout=self.list_timeout)
        services = data.get("services")
        if not isinstance(services, list):
            raise ArcGISRequestError(
                "Admin services response did not contain a 'services' list"
            )
        output = []
        for entry in services:
            if not isinstance(entry, Mapping):
                raise ArcGISRequestError(
                    "Admin services response contained a malformed service entry"
                )
            if _mapping_value(entry, "type") != "FeatureServer":
                continue
            name = _mapping_value(entry, "serviceName")
            if not isinstance(name, str) or not name.strip():
                raise ArcGISRequestError(
                    "Admin services response contained a FeatureServer without "
                    "a valid serviceName"
                )
            output.append(f"{name.strip()}.FeatureServer")
        return output

    def lifecycle_modified(self, folder, svc):
        try:
            return modified_marker(
                self.get(_admin_services_path(folder, svc, "lifecycleinfos"))
            )
        except Exception as exc:
            self.logger.debug(
                "lifecycleinfos read failed for %s (%s); treating as unknown "
                "and refetching.", svc, redact_token(str(exc))
            )
            return 0


def check_portal_item(portal_url, item_id, token_provider, cache, session,
                      client=None):
    if item_id in cache:
        return cache[item_id]
    result = {
        "exists": None, "title": None, "owner": None, "type": None,
        "error": None, "tls_error": False,
    }
    rendered_id = str(item_id or "").strip()
    if not _ITEM_ID_RE.fullmatch(rendered_id):
        result["error"] = f"invalid Portal item id {item_id!r}"
        cache[item_id] = result
        return result
    url = (f"{portal_url.rstrip('/')}/sharing/rest/content/items/"
           f"{quote(rendered_id, safe='')}")
    try:
        if client is not None:
            data = client.get(url)
        else:
            response = session.get(
                url,
                params={"f": "json", "token": token_provider.get()},
                timeout=HTTP_TIMEOUT,
                allow_redirects=False,
            )
            response.raise_for_status()
            data = response.json()
    except ArcGISRequestError as exc:
        text = str(exc).lower()
        if exc.status == 404 or (
                str(exc.code) in {"400", "404"}
                and "does not exist" in text
                and "inaccessible" not in text):
            result["exists"] = False
        else:
            result["error"] = str(exc)
            result["tls_error"] = exc.tls_error
    except Exception as exc:
        result["error"] = redact_token(str(exc))
        result["tls_error"] = bool(
            requests is not None
            and isinstance(exc, requests.exceptions.SSLError)
        )
    else:
        if isinstance(data, dict) and str(data.get("id") or "").lower() == \
                rendered_id.lower():
            result.update(
                exists=True,
                title=data.get("title"),
                owner=data.get("owner"),
                type=data.get("type"),
            )
        else:
            result["error"] = "item endpoint returned an unexpected response shape"
    cache[item_id] = result
    return result


def _usable_service_cache_record(prior):
    if not isinstance(prior, dict):
        return False
    rows = prior.get("table_refs")
    companions = prior.get("companion_raw", [])
    item_ids = prior.get("portal_item_ids", [])
    return (
        isinstance(rows, list)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("table_name"), str)
            and bool(row.get("table_name"))
            for row in rows
        )
        and isinstance(companions, list)
        and all(isinstance(value, str) for value in companions)
        and isinstance(item_ids, list)
        and all(isinstance(value, str) for value in item_ids)
    )


def collect_services(admin, portal_url, folder, token_provider, cache, force,
                     logger, session, debug=False, workers=DEFAULT_WORKERS,
                     use_lifecycle_probe=True, client=None,
                     return_stats=False):
    services = admin.list_feature_services(folder)
    display_folder = folder or "/"
    logger.info("%d feature service(s) found in /%s.", len(services),
                display_folder)

    svc_cache = cache.setdefault("services", {})
    if not isinstance(svc_cache, dict):
        logger.warning("Service cache had an invalid shape; resetting it.")
        svc_cache = {}
        cache["services"] = svc_cache

    layer_rows, svc_items, companion_map = [], {}, {}
    live = {service.rsplit(".", 1)[0] for service in services}
    cache_lock = Lock()
    per_service = {}
    counters = {
        "services_total": len(services),
        "refetched": 0,
        "reused": 0,
        "failed": 0,
        "stale_cache_reused": 0,
        "item_checks": 0,
        "item_check_errors": 0,
        "tls_errors": 0,
    }

    def process_service(service):
        service_name = service.rsplit(".", 1)[0]
        service_path = _admin_services_path(folder, service)
        admin_url = f"{admin.base}/{service_path}"
        with cache_lock:
            raw_prior = svc_cache.get(service_name)
            prior = raw_prior if _usable_service_cache_record(raw_prior) else None

        can_reuse = False
        modified = 0
        if prior and not force and use_lifecycle_probe:
            modified = admin.lifecycle_modified(folder, service)
            prior_modified = prior.get("modified", 0)
            can_reuse = (
                isinstance(prior_modified, (int, float))
                and not isinstance(prior_modified, bool)
                and bool(modified)
                and int(modified) == int(prior_modified)
            )

        state = "refetched"
        error = None
        if can_reuse:
            rows = [dict(row) for row in prior["table_refs"]]
            companions_raw = list(prior.get("companion_raw", []))
            ids = list(prior.get("portal_item_ids", []))
            state = "reused"
        else:
            try:
                data = admin.get(service_path)
                rows = parse_service_tables(data)
                companions_raw = extract_companion_tables(data)
                ids = extract_portal_items(data)
                if use_lifecycle_probe:
                    modified = admin.lifecycle_modified(folder, service)
                with cache_lock:
                    svc_cache[service_name] = {
                        "modified": modified,
                        "table_refs": [dict(row) for row in rows],
                        "companion_raw": list(companions_raw),
                        "portal_item_ids": list(ids),
                    }
            except Exception as exc:
                error = redact_token(str(exc))
                if prior is not None and not force:
                    rows = [dict(row) for row in prior["table_refs"]]
                    companions_raw = list(prior.get("companion_raw", []))
                    ids = list(prior.get("portal_item_ids", []))
                    state = "stale_cache"
                else:
                    rows, companions_raw, ids = [], [], []
                    state = "failed"

        stale = state == "stale_cache"
        for row in rows:
            row["_from_stale_cache"] = stale
        record = {
            "rows": rows,
            "companions_raw": companions_raw,
            "ids": ids,
            "admin_url": admin_url,
            "note": (f"fetch failed: {error}" if error else None),
            "state": state,
        }
        return service_name, record, error

    logger.info("Collecting %d service(s) with %d worker(s) ...",
                len(services), workers)
    done = 0
    for service, future in _bounded_futures(process_service, services, workers):
        try:
            service_name, record, error = future.result()
        except Exception as exc:
            service_name = service.rsplit(".", 1)[0]
            error = redact_token(f"unexpected worker failure: {exc}")
            record = {
                "rows": [], "companions_raw": [], "ids": [],
                "admin_url": f"{admin.base}/{_admin_services_path(folder, service)}",
                "note": f"fetch failed: {error}", "state": "failed",
            }
        per_service[service_name] = record
        state = record["state"]
        if state == "refetched":
            counters["refetched"] += 1
        elif state == "reused":
            counters["reused"] += 1
        else:
            counters["failed"] += 1
            if state == "stale_cache":
                counters["stale_cache_reused"] += 1
                logger.warning(
                    "Service %s: live admin JSON read failed (%s); using the "
                    "previous cached references and marking the run incomplete.",
                    service, error,
                )
            else:
                logger.warning("Service %s: admin JSON read failed: %s",
                               service, error)
        if debug and state in {"refetched", "reused"}:
            logger.debug("  ~ %s: %d table(s), %d portal item(s)%s",
                         service_name, len(record["rows"]), len(record["ids"]),
                         " (cached)" if state == "reused" else "")
        done += 1
        if done % 200 == 0 or done == len(services):
            logger.info("  collected %d / %d service(s) ...", done,
                        len(services))

    all_ids = sorted({
        item_id for record in per_service.values()
        for item_id in record["ids"]
    })
    counters["item_checks"] = len(all_ids)
    logger.info("Checking %d unique portal item id(s) with %d worker(s) ...",
                len(all_ids), workers)

    item_cache = {}

    def check_id(item_id):
        # Each ID is checked once. Use a private compatibility cache so worker
        # threads never mutate the shared result dictionary.
        return check_portal_item(
            portal_url, item_id, token_provider, {}, session, client=client
        )

    for item_id, future in _bounded_futures(check_id, all_ids, workers):
        try:
            info = future.result()
        except Exception as exc:
            info = {
                "exists": None, "title": None, "owner": None, "type": None,
                "error": redact_token(str(exc)), "tls_error": False,
            }
        item_cache[item_id] = info
        if info.get("error"):
            counters["item_check_errors"] += 1
            counters["tls_errors"] += int(bool(info.get("tls_error")))
            logger.warning("Portal item %s could not be checked: %s",
                           item_id, info["error"])

    for service_name in sorted(per_service):
        record = per_service[service_name]
        rows = record["rows"]
        companions_raw = record["companions_raw"]
        ids = record["ids"]
        admin_url = record["admin_url"]

        if record.get("note") and not rows and not ids:
            svc_items[service_name] = {
                "ids": [], "exist": "n/a", "admin_url": admin_url,
                "titles": [], "missing": [], "note": record["note"],
            }
            continue

        primary = {row["table_name"] for row in rows}
        for raw in companions_raw:
            normalized = normalize_table(raw)
            if normalized and normalized not in primary:
                entry = companion_map.setdefault(
                    normalized,
                    {"services": set(), "raw": raw,
                     "_db_schema": _schema_from_raw(raw, normalized),
                     "_stale_services": set()},
                )
                entry["services"].add(service_name)
                if record.get("state") == "stale_cache":
                    entry["_stale_services"].add(service_name)

        titles, missing, statuses = [], [], []
        for item_id in ids:
            info = item_cache.get(item_id, {
                "exists": None, "title": None, "error": "not checked"
            })
            statuses.append(info["exists"])
            if info["exists"]:
                titles.append(info.get("title") or "")
            elif info["exists"] is False:
                missing.append(item_id)
        if not ids:
            existence = "n/a"
        elif all(status is True for status in statuses):
            existence = "yes"
        elif all(status is False for status in statuses):
            existence = "no"
        elif any(status is None for status in statuses):
            existence = "unknown"
        else:
            existence = "partial"
        svc_items[service_name] = {
            "ids": ids, "exist": existence, "admin_url": admin_url,
            "titles": [title for title in titles if title],
            "missing": missing,
        }
        if record.get("note"):
            svc_items[service_name]["note"] = record["note"]

        for row in rows:
            row["service_name"] = service_name
            row["service_type"] = "FeatureServer"
            layer_rows.append(row)

    for dead in [name for name in list(svc_cache) if name not in live]:
        del svc_cache[dead]

    companion_map = {
        key: {
            "services": sorted(value["services"]),
            "raw": value["raw"],
            "_db_schema": value.get("_db_schema"),
            "_stale_services": sorted(value.get("_stale_services", set())),
        }
        for key, value in companion_map.items()
    }
    logger.info(
        "  services: refetched %d, cached %d, failed %d (%d stale-cache "
        "fallback); Portal item checks: %d total, %d error(s)",
        counters["refetched"], counters["reused"], counters["failed"],
        counters["stale_cache_reused"], counters["item_checks"],
        counters["item_check_errors"],
    )
    if return_stats:
        return layer_rows, svc_items, companion_map, counters
    return layer_rows, svc_items, companion_map

_SERVICE_TYPES = ("MapServer", "FeatureServer", "ImageServer", "GPServer",
                  "GeocodeServer", "GeometryServer", "SceneServer",
                  "VectorTileServer", "StreamServer")
_SVC_DIR_RE = re.compile(r"^(.+)\.(" + "|".join(_SERVICE_TYPES) + r")$", re.I)

_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tif", ".tiff",
             ".dll", ".exe", ".pdf")


def service_from_msd_path(path):
    parts = re.split(r"[\\/]+", path)
    for i, comp in enumerate(parts):
        m = _SVC_DIR_RE.match(comp)
        if m:
            folder = parts[i - 1] if i > 0 else ""
            return m.group(1), m.group(2), folder
    return "", "", ""


def tables_in_text(text, db_prefix, schemas):
    found = set()
    ident = r"[A-Za-z0-9_]+"
    if db_prefix:
        for m in re.finditer(rf"{re.escape(db_prefix)}\.({ident})\.({ident})", text, re.I):
            found.add(f"{m.group(1)}.{m.group(2)}".lower())
    for sch in schemas:
        for m in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(sch)}\.({ident})", text, re.I):
            found.add(f"{sch}.{m.group(1)}".lower())
    return found


_FEATUREDATASET_RE = re.compile(
    r'featuredataset\s*["\']?\s*[:=>]\s*["\']?'
    r'([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){1,2})',
    re.I)


def feature_datasets_in_text(text):
    return {normalize_table(m.group(1)) for m in _FEATUREDATASET_RE.finditer(text)}


def tables_in_msd(path, db_prefix, schemas):
    found, datasets = set(), set()
    total_uncompressed = 0
    total_read = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MSD_MEMBERS:
            raise RuntimeError(
                f"archive has {len(infos)} members, exceeding the "
                f"{MAX_MSD_MEMBERS} member limit"
            )
        for info in infos:
            name = info.filename
            if name.lower().endswith(_SKIP_EXT) or info.is_dir():
                continue
            if info.file_size > MAX_MSD_MEMBER_BYTES:
                raise RuntimeError(
                    f"archive member {name!r} is {info.file_size} bytes, "
                    f"exceeding the {MAX_MSD_MEMBER_BYTES} byte limit"
                )
            total_uncompressed += max(0, int(info.file_size))
            if total_uncompressed > MAX_MSD_TOTAL_BYTES:
                raise RuntimeError(
                    f"archive uncompressed size exceeds the "
                    f"{MAX_MSD_TOTAL_BYTES} byte limit"
                )
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_MSD_COMPRESSION_RATIO:
                    raise RuntimeError(
                        f"archive member {name!r} has suspicious compression "
                        f"ratio {ratio:.1f}:1"
                    )
            try:
                with archive.open(info) as member:
                    raw = member.read(MAX_MSD_MEMBER_BYTES + 1)
            except Exception as exc:
                raise RuntimeError(
                    f"could not read archive member {name!r}: {exc}"
                ) from exc
            if len(raw) > MAX_MSD_MEMBER_BYTES:
                raise RuntimeError(
                    f"archive member {name!r} exceeded the read limit"
                )
            total_read += len(raw)
            if total_read > MAX_MSD_TOTAL_BYTES:
                raise RuntimeError(
                    f"archive data read exceeds the {MAX_MSD_TOTAL_BYTES} "
                    "byte aggregate limit"
                )
            text = raw.decode("utf-8", "ignore")
            found |= tables_in_text(text, db_prefix, schemas)
            datasets |= feature_datasets_in_text(text)
    return found - datasets


def scan_arcgisinput(paths, hosting_url, db_prefix, schemas, cache, force,
                     logger, debug=False, workers=DEFAULT_WORKERS,
                     return_stats=False):
    stats = {
        "paths_requested": len(paths or []),
        "paths_unavailable": 0,
        "files_total": 0,
        "reused": 0,
        "rescanned": 0,
        "errors": 0,
        "unrecognized": 0,
    }
    if not paths:
        return ([], stats) if return_stats else []

    msd_files = []
    seen_file_keys = set()
    seen_bases = set()
    for base in paths:
        rendered_base = os.path.normcase(os.path.abspath(str(base)))
        if rendered_base in seen_bases:
            continue
        seen_bases.add(rendered_base)
        if not os.path.isdir(base):
            stats["paths_unavailable"] += 1
            logger.warning("arcgisinput path not found / not accessible: %s", base)
            continue
        for msd in glob.iglob(os.path.join(base, "**", "*.msd"), recursive=True):
            key = os.path.normcase(os.path.abspath(msd))
            if key in seen_file_keys:
                continue
            seen_file_keys.add(key)
            msd_files.append(msd)
            if len(msd_files) > MAX_MSD_FILES:
                raise RuntimeError(
                    f"arcgisinput contains more than {MAX_MSD_FILES} .msd "
                    "files; refusing an unbounded scan"
                )
    stats["files_total"] = len(msd_files)
    logger.info("%d .msd archive(s) to inspect under arcgisinput.", len(msd_files))

    msd_cache = cache.setdefault("msd", {})
    if not isinstance(msd_cache, dict):
        logger.warning(".msd cache had an invalid shape; resetting it.")
        msd_cache = {}
        cache["msd"] = msd_cache
    params = {"db_prefix": db_prefix or "", "schemas": sorted(schemas)}
    if force or cache.get("msd_scan_params") != params:
        msd_cache.clear()
        cache["msd_scan_params"] = params

    cache_lock = Lock()

    def scan_one(msd):
        service_name, service_type, folder = service_from_msd_path(msd)
        if not service_name:
            return {"state": "unrecognized", "path": msd}
        cache_key = os.path.normcase(os.path.abspath(msd))
        try:
            stat = os.stat(msd)
            mtime_ns = stat.st_mtime_ns
            file_size = stat.st_size
        except OSError as exc:
            return {"state": "error", "path": msd,
                    "error": redact_token(str(exc))}
        with cache_lock:
            prior = msd_cache.get(cache_key)
        if (isinstance(prior, dict)
                and prior.get("mtime_ns") == mtime_ns
                and prior.get("size") == file_size
                and isinstance(prior.get("tables"), list)):
            return {
                "state": "reused", "path": msd,
                "service_name": service_name, "service_type": service_type,
                "folder": folder, "tables": list(prior["tables"]),
            }
        try:
            tables = sorted(tables_in_msd(msd, db_prefix, schemas))
        except Exception as exc:
            return {"state": "error", "path": msd,
                    "error": redact_token(str(exc))}
        with cache_lock:
            msd_cache[cache_key] = {
                "path": msd,
                "mtime_ns": mtime_ns,
                "size": file_size,
                "svc_name": service_name,
                "svc_type": service_type,
                "folder": folder,
                "tables": tables,
            }
        return {
            "state": "rescanned", "path": msd,
            "service_name": service_name, "service_type": service_type,
            "folder": folder, "tables": tables,
        }

    results = []
    done = 0
    for msd, future in _bounded_futures(scan_one, msd_files, workers):
        try:
            result = future.result()
        except Exception as exc:
            result = {"state": "error", "path": msd,
                      "error": redact_token(str(exc))}
        state = result["state"]
        if state == "error":
            stats["errors"] += 1
            logger.warning("  ! .msd %s: %s", result["path"], result["error"])
        elif state == "unrecognized":
            stats["unrecognized"] += 1
        else:
            stats[state] += 1
            results.append(result)
        done += 1
        if done % 200 == 0 or done == len(msd_files):
            logger.info("  scanned %d / %d .msd file(s) ...", done,
                        len(msd_files))

    references = {}
    for result in results:
        for table_name in result["tables"]:
            entry = references.setdefault(
                (result["service_name"], result["service_type"], table_name),
                {"folder": result["folder"], "sources": set()},
            )
            entry["sources"].add(result["path"])
        if debug and result["tables"]:
            logger.debug(
                "  ~ %s.%s: %d table ref(s)%s",
                result["service_name"], result["service_type"],
                len(result["tables"]),
                " (cached)" if result["state"] == "reused" else "",
            )

    for dead in [key for key in list(msd_cache) if key not in seen_file_keys]:
        del msd_cache[dead]

    base = hosting_url.rstrip("/")
    output = []
    for (service_name, service_type, table_name), entry in references.items():
        folder_prefix = f"{entry['folder']}/" if entry["folder"] else ""
        output.append({
            "service_name": service_name,
            "service_type": service_type,
            "folder": entry["folder"],
            "table_name": table_name,
            "_db_schema": _schema_from_raw(table_name, table_name),
            "source_file": sorted(entry["sources"])[0],
            "n_sources": len(entry["sources"]),
            "admin_url": (
                f"{base}/admin/services/{folder_prefix}"
                f"{service_name}.{service_type}"
            ),
        })
    logger.info(
        "  .msd: rescanned %d, cached %d, errored %d, unavailable paths %d",
        stats["rescanned"], stats["reused"], stats["errors"],
        stats["paths_unavailable"],
    )
    if return_stats:
        return output, stats
    return output

def fetch_db_relations(conn, schemas):
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            SELECT n.nspname AS schema, c.relname AS relname,
                   c.relkind AS relkind, r.rolname AS owner,
                   pg_total_relation_size(c.oid) AS size_bytes
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_roles r ON r.oid = c.relowner
            WHERE c.relkind IN ('r','p','v','m','f')
              AND n.nspname = ANY(%(schemas)s);
        """, {"schemas": list(schemas)})
        output = {}
        for row in cursor.fetchall():
            if IGNORE_RELNAME.match(row["relname"]):
                continue
            key = f"{row['schema']}.{row['relname']}".lower()
            record = dict(row)
            record["size_pretty"] = _size_mb(record.get("size_bytes"))
            output[key] = record
        return output
    finally:
        cursor.close()

def owning_service_for_companion(orphan_name, mapped_names):
    best = None
    for m in mapped_names:
        if orphan_name.startswith(m + "_") or orphan_name.startswith(m + "__"):
            if best is None or len(m) > len(best):
                best = m
    if best is None:
        return None, None
    return best, orphan_name[len(best):].lstrip("_")


def versioned_view_base(name, suffixes, referenced):
    for suf in suffixes:
        if suf and name.endswith(suf):
            base = name[:-len(suf)]
            if base in referenced:
                return base, suf
    return None, None


def build_rows(layer_rows, svc_items, db_relations, companion_map, msd_refs,
               vview_suffixes, reference_inventory_complete=True):
    rows = []
    referenced = set()
    live_referenced = set()
    ref_owner = {}
    layer_table_names = {lr["table_name"] for lr in layer_rows}

    def remember_owner(tname, row):
        ref_owner.setdefault(tname, {k: row.get(k, "") for k in
                                     ("service_name", "service_type",
                                      "service_admin_url", "reference_source")})

    def svc_cols(service_name):
        si = svc_items.get(service_name, {})
        return {
            "service_name": service_name,
            "service_type": "FeatureServer",
            "service_admin_url": si.get("admin_url", ""),
            "reference_source": "admin_api",
            "portal_item_ids": ";".join(si.get("ids", [])),
            "portal_items_exist": si.get("exist", "n/a"),
            "portal_item_titles": ";".join(si.get("titles", [])),
            "portal_item_missing": ";".join(si.get("missing", [])),
        }

    def mapped_or_missing(row, tname, extra_missing_note=""):
        db = db_relations.get(tname)
        if db:
            row.update(status="MAPPED_OK", db_exists="yes",
                       db_relkind=RELKIND.get(db["relkind"], db["relkind"]),
                       db_owner=db["owner"], db_size_pretty=db["size_pretty"],
                       db_size_bytes=db["size_bytes"])
            row.setdefault("notes", "")
        else:
            row.update(status="MISSING_DB_RELATION", db_exists="no",
                       db_relkind="", db_owner="", db_size_pretty="",
                       db_size_bytes="")
            row["notes"] = ("Service references a table/view that is absent from "
                            "the database. " + (row.get("notes") or "")
                            + extra_missing_note).strip()
        return row

    for lr in layer_rows:
        tname = lr["table_name"]
        referenced.add(tname)
        if not lr.get("_from_stale_cache"):
            live_referenced.add(tname)
        row = {
            "table_name": tname,
            "raw_table_name": lr.get("raw_table_name", ""),
            "service_is_view": lr.get("is_view", ""),
            "layer_id": lr.get("layer_id", ""),
            "layer_name": lr.get("layer_name", ""),
            "kind": lr.get("kind", ""),
            "possible_owning_service": "",
        }
        row.update(svc_cols(lr.get("service_name", "")))
        row = mapped_or_missing(row, tname)
        if lr.get("_from_stale_cache"):
            if row.get("status") == "MISSING_DB_RELATION":
                row["status"] = "INDETERMINATE_SERVICE_REFERENCE"
            row["notes"] = (
                "Live service JSON could not be read; this reference came "
                "from the previous cache and must be revalidated. "
                + (row.get("notes") or "")
            ).strip()
        rows.append(row)
        remember_owner(tname, row)

    for name in sorted(companion_map):
        if name in referenced:
            continue
        referenced.add(name)
        info = companion_map[name]
        services = info["services"]
        stale_services_for_ref = set(info.get("_stale_services") or [])
        if any(service not in stale_services_for_ref for service in services):
            live_referenced.add(name)
        primary_svc = services[0]
        row = {
            "table_name": name,
            "raw_table_name": info.get("raw", ""),
            "service_is_view": "",
            "layer_id": "", "layer_name": "", "kind": "attachment",
            "possible_owning_service": "",
        }
        row.update(svc_cols(primary_svc))
        if len(services) > 1:
            row["service_name"] = ";".join(services)
        row = mapped_or_missing(row, name)
        stale_services = info.get("_stale_services") or []
        if stale_services and row.get("status") == "MISSING_DB_RELATION":
            row["status"] = "INDETERMINATE_SERVICE_REFERENCE"
        note = ("Attachment/companion table referenced in the service JSON "
                f"(*TableInfo) of {';'.join(services)}.")
        if stale_services:
            note += (" Live JSON could not be read for cached service(s): "
                     + ";".join(stale_services) + ".")
        row["notes"] = (note + " " + (row.get("notes") or "")).strip()
        rows.append(row)
        remember_owner(name, row)

    for ref in (msd_refs or []):
        tname = ref["table_name"]
        referenced.add(tname)
        live_referenced.add(tname)
        src = ref["source_file"]
        if ref.get("n_sources", 1) > 1:
            src += f" (+{ref['n_sources'] - 1} more)"
        row = {
            "table_name": tname, "raw_table_name": "",
            "service_is_view": "",
            "service_name": ref["service_name"],
            "service_type": ref["service_type"],
            "service_admin_url": ref.get("admin_url", ""),
            "reference_source": "msd",
            "source_file": src,
            "layer_id": "", "layer_name": "", "kind": "",
            "portal_item_ids": "", "portal_items_exist": "n/a",
            "portal_item_titles": "", "portal_item_missing": "",
            "possible_owning_service": "",
        }
        row = mapped_or_missing(row, tname)
        note = (f"Referenced in the service definition (.msd) of "
                f"{ref['service_name']}.{ref['service_type']}.")
        row["notes"] = (note + " " + (row.get("notes") or "")).strip()
        rows.append(row)
        remember_owner(tname, row)

    for name in sorted(db_relations):
        if name in referenced:
            continue
        db = db_relations[name]

        vbase, vsuf = versioned_view_base(name, vview_suffixes, referenced)
        if vbase:
            owner = ref_owner.get(vbase, {})
            row = {
                "table_name": name, "raw_table_name": "",
                "service_is_view": "",
                "service_name": owner.get("service_name", ""),
                "service_type": owner.get("service_type", ""),
                "service_admin_url": owner.get("service_admin_url", ""),
                "reference_source": owner.get("reference_source", ""),
                "layer_id": "", "layer_name": "", "kind": "versioned view",
                "portal_item_ids": "", "portal_items_exist": "n/a",
                "portal_item_titles": "", "portal_item_missing": "",
                "possible_owning_service": "",
            }
            row = mapped_or_missing(row, name)
            if (not reference_inventory_complete
                    and vbase not in live_referenced):
                row["status"] = "INDETERMINATE_DB_RELATION"
                row["notes"] = (
                    f"Looks like a versioned/editor view ('{vsuf}') of cached "
                    f"base table {vbase}, but the live service reference could "
                    "not be revalidated in this run."
                )
            else:
                row["notes"] = (
                    f"Versioned/editor view ('{vsuf}') of mapped base table "
                    f"{vbase}."
                ).strip()
            rows.append(row)
            continue

        tbase, tsuf = versioned_view_base(name, TRACK_TABLE_SUFFIXES, referenced)
        if tbase:
            owner = ref_owner.get(tbase, {})
            row = {
                "table_name": name, "raw_table_name": "",
                "service_is_view": "",
                "service_name": owner.get("service_name", ""),
                "service_type": owner.get("service_type", ""),
                "service_admin_url": owner.get("service_admin_url", ""),
                "reference_source": owner.get("reference_source", ""),
                "layer_id": "", "layer_name": "", "kind": "track table",
                "portal_item_ids": "", "portal_items_exist": "n/a",
                "portal_item_titles": "", "portal_item_missing": "",
                "possible_owning_service": "",
            }
            row = mapped_or_missing(row, name)
            if (not reference_inventory_complete
                    and tbase not in live_referenced):
                row["status"] = "INDETERMINATE_DB_RELATION"
                row["notes"] = (
                    f"Looks like an editor-tracking/archive table ('{tsuf}') "
                    f"of cached base table {tbase}, but the live service "
                    "reference could not be revalidated in this run."
                )
            else:
                row["notes"] = (
                    f"Editor-tracking/archive table ('{tsuf}') of mapped base "
                    f"table {tbase}."
                ).strip()
            rows.append(row)
            continue

        owner_svc, suffix = owning_service_for_companion(name, layer_table_names)
        base_services = sorted({lr["service_name"] for lr in layer_rows
                                if lr["table_name"] == owner_svc}) if owner_svc else []
        row = {
            "table_name": name, "raw_table_name": "",
            "db_exists": "yes",
            "db_relkind": RELKIND.get(db["relkind"], db["relkind"]),
            "db_owner": db["owner"], "db_size_pretty": db["size_pretty"],
            "db_size_bytes": db["size_bytes"],
            "service_is_view": "", "service_name": "", "service_type": "",
            "service_admin_url": "",
            "layer_id": "", "layer_name": "", "kind": "",
            "portal_item_ids": "", "portal_items_exist": "n/a",
            "portal_item_titles": "", "portal_item_missing": "",
            "possible_owning_service": "",
        }
        if owner_svc:
            row["service_admin_url"] = svc_items.get(base_services[0], {}).get("admin_url", "") \
                if base_services else ""
            if (not reference_inventory_complete
                    and owner_svc not in live_referenced):
                row["status"] = "INDETERMINATE_DB_RELATION"
                row["notes"] = (
                    f"Name resembles a companion (suffix '_{suffix}') of "
                    f"cached table {owner_svc}, but the live service reference "
                    "could not be revalidated in this run."
                )
            else:
                row["status"] = "COMPANION_DB_RELATION"
                row["notes"] = (
                    f"Likely companion (suffix '_{suffix}') of mapped table "
                    f"{owner_svc} - inferred by naming, NOT declared in the "
                    "service JSON. Verify before any cleanup."
                )
        else:
            if reference_inventory_complete:
                row["status"] = "ORPHAN_DB_RELATION"
                row["notes"] = (
                    "DB relation in a managed schema referenced by no service "
                    "(no FeatureServer JSON and no scanned .msd). Verify "
                    "before any cleanup."
                )
            else:
                row["status"] = "INDETERMINATE_DB_RELATION"
                row["notes"] = (
                    "No live service/.msd reference was found, but one or more "
                    "reference sources failed during this run, so orphan status "
                    "cannot be confirmed. Re-run after resolving the errors."
                )
        rows.append(row)

    return rows


def _fresh_cache(context=None):
    cache = {"version": CACHE_VERSION, "services": {}, "msd": {}}
    if context is not None:
        cache["context"] = dict(context)
    return cache


def load_cache(path, logger, context=None):
    if not path or not os.path.exists(path):
        return _fresh_cache(context)
    try:
        if os.path.islink(path):
            raise ValueError("cache path is a symbolic link")
        size = os.path.getsize(path)
        if size > MAX_CACHE_BYTES:
            raise ValueError(
                f"cache is {size} bytes, exceeding the {MAX_CACHE_BYTES} byte limit"
            )
        with open(path, encoding="utf-8") as handle:
            cache = json.load(handle)
        if not isinstance(cache, dict):
            raise ValueError("cache root is not an object")
        if cache.get("version") != CACHE_VERSION:
            logger.info("Cache %s has a different version; starting fresh.", path)
            return _fresh_cache(context)
        if context is not None and cache.get("context") != dict(context):
            logger.info(
                "Cache %s belongs to a different Portal/hosting/folder/.msd "
                "context; starting fresh.", path
            )
            return _fresh_cache(context)
        if not isinstance(cache.get("services", {}), dict):
            raise ValueError("cache 'services' value is not an object")
        if not isinstance(cache.get("msd", {}), dict):
            raise ValueError("cache 'msd' value is not an object")
        cache.setdefault("services", {})
        cache.setdefault("msd", {})
        if context is not None:
            cache["context"] = dict(context)
        logger.info("Loaded incremental cache: %s", path)
        return cache
    except Exception as exc:
        logger.warning(
            "Could not safely read cache %s (%s); starting fresh.",
            path, redact_token(str(exc)),
        )
        return _fresh_cache(context)


def save_cache(path, cache, logger):
    if not path:
        return
    temp_path = None
    temp_fd = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=os.path.basename(path) + ".tmp.", dir=directory
        )
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = None
            json.dump(cache, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        cache_size = os.path.getsize(temp_path)
        if cache_size > MAX_CACHE_BYTES:
            raise ValueError(
                f"serialized cache is {cache_size} bytes, exceeding the "
                f"{MAX_CACHE_BYTES} byte limit"
            )
        os.replace(temp_path, path)
        temp_path = None
        logger.info("Wrote incremental cache: %s", path)
    except Exception as exc:
        logger.warning(
            "Could not write incremental cache %s: %s",
            path, redact_token(str(exc)),
        )
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def get_keyring_password(service, user, env_fallback, label, logger):
    password = None
    if keyring is not None:
        try:
            logger.info("Retrieving %s password from keyring service '%s' for "
                        "user '%s'.", label, service, user)
            password = keyring.get_password(service, user)
        except Exception as exc:
            logger.warning("keyring lookup for %s failed (%s); will try the "
                           "environment variable %s.", label, exc, env_fallback)
    else:
        logger.warning("The 'keyring' package is not installed; falling back to "
                       "the %s environment variable for the %s password.",
                       env_fallback, label)

    if not password:
        password = os.getenv(env_fallback)
        if password:
            logger.info("Using %s password from environment variable %s.",
                        label, env_fallback)

    if not password:
        raise StepError(
            STAGE_CONFIG,
            f"No {label} password found. Store it in keyring (service "
            f"'{service}', username '{user}') or set the {env_fallback} "
            f"environment variable.")
    return password


def resolve_extra(args, key, target, default=None):
    attr = key.replace("-", "_")
    cli_val = getattr(args, attr, None)
    if cli_val is not None and not (isinstance(cli_val, str) and cli_val.strip() == ""):
        return cli_val
    val = _config.get_config_value(key, target, path=args.config)
    if val is not None and not (isinstance(val, str) and val.strip() == ""):
        return val
    return default


def resolve_domain_suffix(args, target):
    cli_value = getattr(args, "domain_suffix", None)
    if cli_value is not None and str(cli_value).strip():
        return str(cli_value).strip().strip("."), "explicit --domain-suffix"

    config_value = _config.get_config_value(
        "domain-suffix", target, path=args.config)
    if config_value is not None and str(config_value).strip():
        return (str(config_value).strip().strip("."),
                "config key 'domain-suffix'")

    configured_host = _config.get_config_value(
        "db-host", target, path=args.config)
    if configured_host is not None and str(configured_host).strip():
        host = str(configured_host).strip().rstrip(".")

        try:
            ipaddress.ip_address(host)
        except ValueError:
            _short, separator, suffix = host.partition(".")
            if separator and suffix:
                return (suffix.strip("."),
                        "derived from config key 'db-host'")

    return (None,
            "not supplied by --domain-suffix/config 'domain-suffix' and "
            "not derivable from config 'db-host'")


def resolve_db_host_fqdn(machine_name, domain_suffix, logger=None):
    if not machine_name:
        return machine_name
    suffix = _normalize_domain_suffix(domain_suffix)
    checked = _normalize_hostname(machine_name, "Data Store machine name")
    try:
        ipaddress.ip_address(checked)
        is_ip = True
    except ValueError:
        is_ip = False

    if is_ip or "." in checked:
        if suffix and not is_ip and not checked.lower().endswith("." + suffix):
            raise StepError(
                STAGE_DATASTORE,
                f"Auto-discovered Data Store host {checked!r} is outside "
                f"the configured domain suffix {suffix!r}.",
            )
        if logger is not None:
            logger.info(
                "  [db-host] discovered machine name '%s' is already a full "
                "hostname/address; using it as-is.", checked
            )
        return checked

    short = _normalize_hostname(short_hostname(checked), "Data Store short hostname")
    fqdn = f"{short}.{suffix}" if suffix else short
    fqdn = _normalize_hostname(fqdn, "resolved Data Store host")
    if logger is not None:
        logger.info(
            "  [db-host] discovered machine name '%s' has no domain; "
            "built FQDN '%s' using domain-suffix='%s'.",
            machine_name, fqdn, suffix,
        )
    return fqdn


def _bounded_integer(value, *, minimum, maximum, label):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _worker_count(value):
    return _bounded_integer(
        value, minimum=1, maximum=MAX_WORKERS, label="--workers"
    )


def _db_port(value):
    return _bounded_integer(
        value, minimum=1, maximum=65535, label="--db-port"
    )


def _service_folder(value):
    rendered = str(value or "").strip()
    if rendered in {"", "/"}:
        return ""
    if any(ch in rendered for ch in ("/", "\\")) or rendered in {".", ".."}:
        raise argparse.ArgumentTypeError(
            "--folder must be one ArcGIS Server folder name, or '/' for root"
        )
    if any(ord(ch) < 32 for ch in rendered):
        raise argparse.ArgumentTypeError("--folder contains a control character")
    return rendered


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only inventory / reconciliation of hosted feature "
                    "services against the relational Data Store. Portal-only; "
                    "the Data Store's live primary is auto-detected on every "
                    "run (HA-aware) unless --db-host is given. Authentication "
                    "and TLS verification follow the same pattern as every "
                    "other script in this project (connect_gis() + "
                    "--tls-mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Always runs a FULL scan of every feature service.\n\n"
            "Examples:\n"
            "  python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config "
            "Org.config.toml\n"
            "  python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config "
            "Org.config.toml --force\n"
            "  python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config "
            "Org.config.toml --db-host datastore.example.com\n"
            "  python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config "
            "Org.config.toml --tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)

    grp_host = parser.add_argument_group("Hosting server (config fallback)")
    grp_host.add_argument(
        "--hosting-url", default=None,
        help="Hosting GIS Server admin URL. If omitted, taken from the "
             "--config org toml ('hosting-url').")
    grp_host.add_argument(
        "--folder", type=_service_folder, default="Hosted",
        help="Admin services folder to inventory (default: Hosted; use '/' "
             "for the root folder).")
    grp_host.add_argument(
        "--arcgisinput", action="append", default=None,
        help="Path to the hosting server's arcgisinput folder (repeatable). "
             "If omitted, taken from the --config org toml ('arcgisinput').")
    grp_host.add_argument(
        "--db-prefix", default=None,
        help="Database name used to anchor db.schema.table matches in .msd "
             "files.")
    grp_host.add_argument(
        "--vview-suffix", action="append", default=[],
        help="Versioned/editor-view suffix (repeatable; default: "
             f"{' '.join(DEFAULT_VVIEW_SUFFIXES)}).")

    grp_db = parser.add_argument_group(
        "Data Store / database (auto-detected primary; config fallback for "
        "non-host settings)")
    grp_db.add_argument(
        "--db-host", default=None,
        help="EXPLICIT OVERRIDE: force this Data Store host, bypassing live "
             "primary auto-detection entirely.")
    grp_db.add_argument(
        "--domain-suffix", default=None,
        help="Optional override for the domain suffix appended to a "
             "discovered SHORT Data Store machine name. If omitted, "
             "read from config key 'domain-suffix'; when that key is "
             "absent, derive it from the config 'db-host' FQDN. "
             "There is no built-in domain suffix.")
    grp_db.add_argument(
        "--db-port", type=_db_port, default=None,
        help="Data Store port (default from config, else 9876).")
    grp_db.add_argument(
        "--db-sslmode", choices=("require", "verify-ca", "verify-full"),
        default=None,
        help="PostgreSQL TLS mode (default from config, else 'require'). "
             "Use verify-full with --db-sslrootcert for hostname validation.")
    grp_db.add_argument(
        "--db-sslrootcert", default=None,
        help="CA file for PostgreSQL verify-ca/verify-full. Optional for "
             "sslmode=require.")
    grp_db.add_argument(
        "--dbname", default=None,
        help="Database name. If omitted, taken from the --config org toml.")
    grp_db.add_argument(
        "--db-user", default=None,
        help="DB admin username. If omitted, taken from the "
             "--config org toml ('db-user'). There is no built-in "
             "username.")
    grp_db.add_argument(
        "--keyring-service-db", default=None,
        help="Keyring service name for the DB password. If omitted, "
             "taken from the --config org toml "
             "('keyring-service-db'). There is no built-in service "
             "name.")

    grp_misc = parser.add_argument_group("Cache / performance / debugging")
    grp_misc.add_argument(
        "--force", action="store_true",
        help="Ignore the incremental cache: refetch every service JSON and "
             "re-scan every .msd.")
    grp_misc.add_argument(
        "--workers", type=_worker_count, default=DEFAULT_WORKERS,
        help=f"Concurrent worker threads (default: {DEFAULT_WORKERS}).")
    grp_misc.add_argument(
        "--no-lifecycle-probe", dest="lifecycle_probe", action="store_false",
        help="Skip the per-service lifecycleinfos round-trip.")
    parser.set_defaults(lifecycle_probe=True)
    grp_misc.add_argument(
        "--debug", action="store_true",
        help="Verbose per-service / per-.msd debug logging.")

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="os-trust-store",
        help="How this script's own HTTP session and the arcgis SDK "
             "connection verify TLS certificates. 'os-trust-store' "
             "(default): verify against the operating system's native "
             "certificate store (requires the 'truststore' package) -- "
             "appropriate for an internal Enterprise Portal signed by an "
             "organizational CA already trusted by Windows. 'standard': "
             "use certifi's bundled public CA list. 'custom-ca-bundle': "
             "verify against an explicit PEM file passed via "
             "--ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")

    return parser.parse_args()


def _coerce_db_port(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StepError(STAGE_CONFIG, f"Invalid db-port {value!r}") from exc
    if not 1 <= parsed <= 65535:
        raise StepError(STAGE_CONFIG, "db-port must be between 1 and 65535")
    return parsed


def _resolve_arcgisinput_paths(args):
    if args.arcgisinput:
        raw_values = list(args.arcgisinput)
    else:
        configured = resolve_extra(args, "arcgisinput", args.target)
        if isinstance(configured, (list, tuple, set)):
            raw_values = list(configured)
        elif configured:
            raw_values = [configured]
        else:
            raw_values = []
    output = []
    seen = set()
    for value in raw_values:
        rendered = str(value or "").strip()
        if not rendered:
            continue
        key = os.path.normcase(os.path.abspath(rendered))
        if key in seen:
            continue
        seen.add(key)
        output.append(rendered)
    return output


def _cache_context(portal_base, hosting_url, folder, db_prefix,
                   arcgisinput_paths, lifecycle_probe):
    return {
        "portal": portal_base.lower(),
        "hosting": hosting_url.lower(),
        "folder": folder,
        "db_prefix": str(db_prefix or "").lower(),
        "arcgisinput": sorted(
            os.path.normcase(os.path.abspath(path))
            for path in arcgisinput_paths
        ),
        "lifecycle_probe": bool(lifecycle_probe),
    }


def _handshake_or_raise(url, session, logger, stage):
    try:
        ok, detail = verify_tls_handshake(url, session, logger=logger)
    except Exception as exc:
        raise StepError(
            stage,
            f"TLS handshake setup/check for {url} failed: "
            f"{redact_token(str(exc))}",
        ) from exc
    if not ok:
        raise StepError(
            stage,
            f"TLS handshake verification to {url} FAILED: "
            f"{redact_token(str(detail.get('error')))}",
        )


def _db_connect_kwargs(host, port, dbname, user, password, sslmode,
                       sslrootcert):
    kwargs = {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "connect_timeout": 10,
        "sslmode": sslmode,
        "options": (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"
        ),
    }
    if sslrootcert:
        kwargs["sslrootcert"] = sslrootcert
    return kwargs


def _run_inventory(args, timestamp, start_wallclock, start_perf, platform,
                   log_file_path, inventory_csv, logger, subject):
    if platform != "PORTAL":
        raise StepError(
            STAGE_CONFIG,
            "This is a Portal-only inventory. The configured portal-url "
            f"classified as {platform!r}, not 'PORTAL'.",
        )
    if requests is None:
        raise StepError(
            STAGE_CONFIG, "The 'requests' package is required but not installed."
        )
    if not _PSYCOPG2_AVAILABLE:
        raise StepError(
            STAGE_CONFIG,
            "The 'psycopg2' package is required for the Data Store snapshot "
            "but is not installed. Install psycopg2-binary or psycopg2.",
        )

    portal_base = _normalize_https_base(
        args.portal_url, "portal-url", strip_home=True
    )
    raw_hosting_url = resolve_extra(args, "hosting-url", args.target)
    if not raw_hosting_url:
        raise StepError(
            STAGE_CONFIG,
            "Missing --hosting-url (config key 'hosting-url').",
        )
    hosting_url = _normalize_https_base(
        raw_hosting_url, "hosting-url", strip_server_suffixes=True
    )

    dbname = resolve_extra(args, "dbname", args.target)
    db_user = resolve_extra(args, "db-user", args.target)
    db_keyring = resolve_extra(args, "keyring-service-db", args.target)
    missing = []
    if not dbname:
        missing.append("--dbname (config 'dbname')")
    if not db_user:
        missing.append("--db-user (config 'db-user')")
    if not db_keyring:
        missing.append("--keyring-service-db (config 'keyring-service-db')")
    if missing:
        raise StepError(
            STAGE_CONFIG,
            "Missing required Data Store setting(s): " + ", ".join(missing),
        )

    db_port = _coerce_db_port(
        resolve_extra(args, "db-port", args.target, default=9876)
    )
    sslmode = str(
        resolve_extra(args, "db-sslmode", args.target, default="require")
        or "require"
    ).strip().lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise StepError(
            STAGE_CONFIG,
            "db-sslmode must be one of: require, verify-ca, verify-full",
        )
    sslrootcert = resolve_extra(args, "db-sslrootcert", args.target)
    if sslrootcert:
        sslrootcert = os.path.abspath(str(sslrootcert))
        if not os.path.isfile(sslrootcert):
            raise StepError(
                STAGE_CONFIG,
                f"PostgreSQL db-sslrootcert file not found: {sslrootcert}",
            )
    if sslmode in {"verify-ca", "verify-full"} and not sslrootcert:
        logger.warning(
            "db-sslmode=%s was selected without db-sslrootcert; libpq's "
            "configured/default root certificate location will be used.",
            sslmode,
        )

    domain_suffix, domain_suffix_source = resolve_domain_suffix(args, args.target)
    domain_suffix = _normalize_domain_suffix(domain_suffix)
    db_prefix = (
        args.db_prefix
        or resolve_extra(args, "db-prefix", args.target)
        or dbname
    )
    arcgisinput_paths = _resolve_arcgisinput_paths(args)

    logger.info("Resolved hosting URL: %s", hosting_url)
    logger.info("Service folder: /%s", args.folder or "")
    logger.info("arcgisinput path(s): %s", arcgisinput_paths or "(none)")
    logger.info("DB port: %d", db_port)
    logger.info("DB TLS mode: %s", sslmode)
    logger.info("Domain suffix: %s (%s)", domain_suffix or "(none)",
                domain_suffix_source)

    mode = _TLS_MODE_CHOICES[args.tls_mode]
    ca_path = (
        args.ca_bundle_path
        if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    )
    try:
        handshake_session = build_secure_session(
            mode,
            ca_bundle_path=ca_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2),
            retry_total=3,
            logger=logger,
        )
    except Exception as exc:
        raise StepError(
            STAGE_CONFIG,
            f"Could not establish TLS verification strategy "
            f"{args.tls_mode!r}: {redact_token(str(exc))}",
        ) from exc

    session_pool = None
    run_lock = None
    db_password = None
    try:
        _handshake_or_raise(portal_base, handshake_session, logger, STAGE_PORTAL)
        try:
            gis = connect_gis(
                args.portal_url,
                args.username,
                args.keyring_service,
                logger,
                verify_mode=mode,
                ca_bundle_path=ca_path,
            )
        except Exception as exc:
            raise StepError(
                STAGE_PORTAL,
                f"Could not connect to Portal: {redact_token(str(exc))}",
            ) from exc

        authenticated_platform = get_platform(getattr(gis, "url", args.portal_url))
        if authenticated_platform != "PORTAL":
            raise StepError(
                STAGE_PORTAL,
                "The authenticated GIS connection is not an Enterprise Portal "
                f"connection (detected {authenticated_platform!r}).",
            )

        token_provider = provider_for_gis(gis, platform)
        token_manager = TokenManager(
            token_provider,
            refresh_callback=lambda: gis.properties.get("name"),
        )
        logger.info(
            "Token acquired via %s (token present: %s).",
            type(token_provider).__name__, token_provider.is_present(),
        )

        session_pool = WorkerSessionPool(mode, ca_path)
        client = ArcGISJsonClient(
            session_pool.get, token_manager, {_origin(portal_base)}
        )
        _validate_registered_hosting_server(
            gis, client, portal_base, hosting_url, logger
        )
        client.add_trusted_base(hosting_url)
        _handshake_or_raise(
            hosting_url, handshake_session, logger, STAGE_SERVICE
        )

        cache_path = os.path.join(
            args.output_dir,
            f"{args.org_name_safe}_{platform}_inventory_cache.json",
        )
        run_lock = CacheRunLock(cache_path + ".lock")
        run_lock.acquire()
        logger.info("Acquired incremental-cache run lock: %s",
                    cache_path + ".lock")
        cache_context = _cache_context(
            portal_base, hosting_url, args.folder, db_prefix,
            arcgisinput_paths, args.lifecycle_probe,
        )
        if args.force:
            cache = _fresh_cache(cache_context)
            logger.info("--force: ignoring incremental cache.")
        else:
            cache = load_cache(cache_path, logger, context=cache_context)

        logger.info("Detecting live Data Store topology (primary/HA-aware) ...")
        if args.db_host:
            db_host = _normalize_hostname(
                args.db_host, "explicit --db-host"
            )
            logger.warning(
                "--db-host was explicitly given (%r); live primary discovery "
                "is bypassed for this run.", db_host,
            )
        else:
            try:
                discovery = _discover_data_store(client, hosting_url, logger)
            except ArcGISRequestError as exc:
                raise StepError(
                    STAGE_DATASTORE,
                    f"Could not discover Data Store topology: {exc}",
                ) from exc
            machine_items = [
                item for item in discovery["items"] if item.get("all_machines")
            ]
            if not machine_items:
                raise StepError(
                    STAGE_DATASTORE,
                    "The hosting server reported no ArcGIS Data Store machine "
                    "registration under /enterpriseDatabases.",
                )
            if discovery.get("ambiguous") or not discovery.get("host"):
                details = "\n".join(
                    f"  - {item['name']}: machines={item['all_machines']} "
                    f"primary={item['primary']} standby={item['standby']} "
                    f"ambiguous={item['ambiguous']}"
                    for item in machine_items
                )
                raise StepError(
                    STAGE_DATASTORE,
                    "Could not determine exactly one live PRIMARY Data Store "
                    "machine; refusing to guess.\n" + details,
                )
            machine = discovery["host"]
            if not looks_like_fqdn(machine) and not domain_suffix:
                raise StepError(
                    STAGE_CONFIG,
                    f"Data Store discovery returned short hostname {machine!r}, "
                    "but no domain suffix could be resolved.",
                )
            db_host = resolve_db_host_fqdn(machine, domain_suffix, logger=logger)

        logger.info(
            "Resolved DB connection target: %s@%s:%s/%s (keyring service %r)",
            db_user, db_host, db_port, dbname, db_keyring,
        )

        admin = Admin(
            hosting_url, token_provider, session_pool.get(), logger,
            client=client,
        )
        logger.info("Collecting FeatureServers from /%s ...", args.folder or "")
        try:
            layer_rows, svc_items, companion_map, service_stats = collect_services(
                admin,
                portal_base,
                args.folder,
                token_provider,
                cache,
                args.force,
                logger,
                session_pool.get(),
                args.debug,
                workers=args.workers,
                use_lifecycle_probe=args.lifecycle_probe,
                client=client,
                return_stats=True,
            )
        except Exception as exc:
            raise StepError(
                STAGE_SERVICE,
                f"Could not enumerate/read hosting FeatureServers: "
                f"{redact_token(str(exc))}",
            ) from exc
        logger.info(
            "Collected %d layer/table mapping(s) across %d service(s).",
            len(layer_rows), len(svc_items),
        )
        save_cache(cache_path, cache, logger)

        schemas = {
            schema
            for row in layer_rows
            for schema in [row.get("_db_schema")]
            if schema
        }
        schemas |= {
            str(row["table_name"]).split(".", 1)[0]
            for row in layer_rows
            if not row.get("_db_schema") and "." in str(row.get("table_name", ""))
        }
        schemas |= {
            value.get("_db_schema")
            for value in companion_map.values()
            if value.get("_db_schema")
        }
        schemas |= {
            str(name).split(".", 1)[0]
            for name, value in companion_map.items()
            if not value.get("_db_schema") and "." in str(name)
        }

        logger.info("Scanning arcgisinput .msd service definitions ...")
        try:
            msd_refs, msd_stats = scan_arcgisinput(
                arcgisinput_paths,
                hosting_url,
                db_prefix,
                schemas,
                cache,
                args.force,
                logger,
                args.debug,
                workers=args.workers,
                return_stats=True,
            )
        except Exception as exc:
            raise StepError(
                STAGE_SERVICE,
                f"Unexpected .msd scan failure: {redact_token(str(exc))}",
            ) from exc
        for ref in msd_refs:
            schema = ref.get("_db_schema")
            if schema:
                schemas.add(schema)
            elif "." in ref.get("table_name", ""):
                schemas.add(ref["table_name"].split(".", 1)[0])
        save_cache(cache_path, cache, logger)

        reference_inventory_complete = (
            service_stats["failed"] == 0
            and msd_stats["errors"] == 0
            and msd_stats["paths_unavailable"] == 0
            and msd_stats["unrecognized"] == 0
        )
        if not schemas:
            logger.warning("No managed schemas were identified; DB snapshot skipped.")
            db_relations = {}
        else:
            logger.info("Snapshotting DB schema(s): %s", ", ".join(sorted(schemas)))
            connection = None
            try:
                db_password = get_keyring_password(
                    db_keyring, db_user, "PGPASSWORD", "database", logger
                )
                connection = psycopg2.connect(**_db_connect_kwargs(
                    db_host, db_port, dbname, db_user, db_password,
                    sslmode, sslrootcert,
                ))
                connection.set_session(readonly=True, autocommit=True)
                db_relations = fetch_db_relations(connection, schemas)
            except StepError:
                raise
            except psycopg2.OperationalError as exc:
                raise StepError(
                    STAGE_DB,
                    f"Could not connect securely/read-only to PostgreSQL "
                    f"{db_user}@{db_host}:{db_port}/{dbname}: "
                    f"{redact_token(str(exc).strip())}",
                ) from exc
            except psycopg2.Error as exc:
                raise StepError(
                    STAGE_DB,
                    f"Catalog query failed: {redact_token(str(exc).strip())}",
                ) from exc
            except Exception as exc:
                raise StepError(
                    STAGE_DB,
                    f"Unexpected DB connection/setup error: "
                    f"{redact_token(str(exc))}",
                ) from exc
            finally:
                db_password = None
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            logger.info(
                "%d relation(s) found in scanned schema(s).", len(db_relations)
            )

        vview_suffixes = [
            suffix if suffix.startswith("_") else "_" + suffix
            for suffix in (args.vview_suffix or DEFAULT_VVIEW_SUFFIXES)
        ]
        rows = build_rows(
            layer_rows,
            svc_items,
            db_relations,
            companion_map,
            msd_refs,
            vview_suffixes,
            reference_inventory_complete=reference_inventory_complete,
        )

        try:
            with open(inventory_csv, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
            logger.info("Wrote %d row(s) to %s", len(rows), inventory_csv)
        except OSError as exc:
            raise StepError(
                STAGE_CONFIG,
                f"Could not write output CSV {inventory_csv}: "
                f"{redact_token(str(exc))}",
            ) from exc

        save_cache(cache_path, cache, logger)
        counts = Counter(row["status"] for row in rows)
        msd_mapped = sum(
            1 for row in rows if row.get("reference_source") == "msd"
        )
        bad_items = sum(
            1 for info in svc_items.values()
            if info.get("exist") in {"no", "partial"}
        )
        unknown_items = sum(
            1 for info in svc_items.values() if info.get("exist") == "unknown"
        )
        incomplete_reasons = []
        if service_stats["failed"]:
            incomplete_reasons.append(
                f"{service_stats['failed']} service definition error(s)"
            )
        if service_stats["item_check_errors"]:
            incomplete_reasons.append(
                f"{service_stats['item_check_errors']} Portal item check error(s)"
            )
        if msd_stats["paths_unavailable"]:
            incomplete_reasons.append(
                f"{msd_stats['paths_unavailable']} unavailable arcgisinput path(s)"
            )
        if msd_stats["errors"]:
            incomplete_reasons.append(
                f"{msd_stats['errors']} unreadable .msd file(s)"
            )
        if msd_stats["unrecognized"]:
            incomplete_reasons.append(
                f"{msd_stats['unrecognized']} .msd file(s) outside a "
                "recognizable service directory"
            )
        indeterminate_rows = (
            counts.get("INDETERMINATE_DB_RELATION", 0)
            + counts.get("INDETERMINATE_SERVICE_REFERENCE", 0)
        )
        incomplete = bool(incomplete_reasons or indeterminate_rows)
        result_label = "INCOMPLETE" if incomplete else "COMPLETE"

        logger.info("=" * 70)
        logger.info("RUN SUMMARY")
        logger.info("Result: %s", result_label)
        logger.info("Organization: %s", args.org_name)
        logger.info("Platform: %s", platform)
        logger.info("Mode: FULL")
        logger.info("TLS mode: %s", args.tls_mode)
        logger.info("DB TLS mode: %s", sslmode)
        logger.info(
            "DB host used: %s (%s)", db_host,
            "explicit --db-host override" if args.db_host
            else "auto-detected live primary",
        )
        logger.info("Rows written: %d", len(rows))
        logger.info("MAPPED_OK: %d (%d via .msd)",
                    counts.get("MAPPED_OK", 0), msd_mapped)
        for status in (
            "MISSING_DB_RELATION", "ORPHAN_DB_RELATION",
            "COMPANION_DB_RELATION", "INDETERMINATE_DB_RELATION",
            "INDETERMINATE_SERVICE_REFERENCE",
        ):
            logger.info("%s: %d", status, counts.get(status, 0))
        logger.info("Services with missing/partial Portal items: %d", bad_items)
        logger.info("Services with unknown Portal item status: %d", unknown_items)
        if incomplete_reasons:
            logger.warning("Incomplete because: %s", "; ".join(incomplete_reasons))
        logger.info("Inventory CSV: %s", inventory_csv)
        if schemas:
            logger.info("Read-only controls were enforced for the DB session.")
        else:
            logger.info("No DB session was opened because no schemas were discovered.")
        logger.info("=" * 70)

        body = (
            f"{EMAIL_SUBJECT_PREFIX}\n"
            f"Result: {result_label}\n"
            f"Organization: {args.org_name}\n"
            f"Platform: {platform}\n"
            f"Run timestamp: {timestamp}\n"
            f"Mode: FULL\n"
            f"Portal URL: {portal_base}\n"
            f"Hosting URL: {hosting_url}\n"
            f"DB: {db_user}@{db_host}:{db_port}/{dbname}\n"
            f"DB host source: "
            f"{'explicit --db-host override' if args.db_host else 'auto-detected LIVE PRIMARY (HA-aware)'}\n"
            f"TLS mode: {args.tls_mode}\n"
            f"DB TLS mode: {sslmode}\n"
            f"Folder: /{args.folder}\n\n"
            f"Rows written: {len(rows)}\n"
            f"MAPPED_OK: {counts.get('MAPPED_OK', 0)} ({msd_mapped} via .msd)\n"
            f"MISSING_DB_RELATION: {counts.get('MISSING_DB_RELATION', 0)}\n"
            f"ORPHAN_DB_RELATION: {counts.get('ORPHAN_DB_RELATION', 0)}\n"
            f"COMPANION_DB_RELATION: {counts.get('COMPANION_DB_RELATION', 0)}\n"
            f"INDETERMINATE_DB_RELATION: {counts.get('INDETERMINATE_DB_RELATION', 0)}\n"
            f"INDETERMINATE_SERVICE_REFERENCE: {counts.get('INDETERMINATE_SERVICE_REFERENCE', 0)}\n"
            f"Services with missing/partial Portal item(s): {bad_items}\n"
            f"Services with unknown Portal item status: {unknown_items}\n"
        )
        if incomplete:
            body += (
                "\nWARNING: This inventory is incomplete. Confirmed rows are "
                "retained, but an absent service reference must not be treated "
                "as proof that a DB relation is orphaned.\n"
            )
            if incomplete_reasons:
                body += "Reasons: " + "; ".join(incomplete_reasons) + "\n"
        body += (
            f"\nInventory CSV: {inventory_csv}\n\n"
            f"{timing_block(start_wallclock, start_perf)}\n"
            "READ-ONLY: no changes were made to services, Portal, or the DB.\n"
            "See the attached log and CSV for full detail.\n"
        )

        attachments = [log_file_path]
        try:
            total_mb = os.path.getsize(inventory_csv) / MB
        except OSError:
            total_mb = 0.0
        if total_mb <= MAX_ATTACHMENTS_MB:
            attachments.append(inventory_csv)
            logger.info("Attaching inventory CSV to email (%.2f MB).", total_mb)
        else:
            logger.warning(
                "Inventory CSV is %.2f MB, exceeding the %d MB limit; it "
                "will not be attached but remains at %s.",
                total_mb, MAX_ATTACHMENTS_MB, inventory_csv,
            )
            body += (
                f"\nNOTE: The inventory CSV is {total_mb:.2f} MB and was not "
                f"attached. It remains at:\n{inventory_csv}\n"
            )

        completion_subject = (
            subject + " (INCOMPLETE)" if incomplete else subject
        )
        send_email(
            completion_subject, body, attachments, args.smtp_server,
            args.email_to, args.email_from, logger,
        )
        logger.info(result_label)
        if incomplete:
            logger.warning(
                "Inventory workflow completed with incomplete detail(s); "
                "returning exit status 0 because the scheduled task ran "
                "to completion. The report remains marked INCOMPLETE."
            )
        return 0
    finally:
        db_password = None
        if run_lock is not None:
            run_lock.release()
        if session_pool is not None:
            session_pool.close()
        try:
            handshake_session.close()
        except Exception:
            pass


def main():
    args = parse_args()
    args.target = TARGET
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    start_wallclock, start_perf = start_timer()
    finalize_config(args, required=REQUIRED_SETTINGS)
    platform = get_platform(args.portal_url)
    timestamp = build_timestamp()

    try:
        os.makedirs(args.output_dir, exist_ok=True)
        run_stamp, log_file_path, inventory_csv = _reserve_run_paths(
            args.output_dir, args.org_name_safe, platform, timestamp
        )
    except Exception as exc:
        sys.stderr.write(
            f"Run ABORTED. Could not prepare output paths in "
            f"{args.output_dir!r}: {redact_token(str(exc))}\n"
        )
        raise SystemExit(1)

    logger = setup_logging(log_file_path, "ds_inventory")
    subject = subject_line(
        EMAIL_SUBJECT_PREFIX, run_stamp, platform, org_name=args.org_name
    )
    logger.info("=" * 70)
    logger.info("Hosting Server / Data Store Inventory - run started")
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: FULL (all services)")
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Portal user: %s (keyring %r)", args.username,
                args.keyring_service)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("Inventory CSV: %s", inventory_csv)
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Force (no cache): %s", bool(args.force))
    logger.info("Worker threads: %d", args.workers)
    logger.info("=" * 70)

    try:
        exit_code = _run_inventory(
            args, run_stamp, start_wallclock, start_perf, platform,
            log_file_path, inventory_csv, logger, subject,
        )
    except StepError as exc:
        safe = redact_token(str(exc))
        logger.error("[%s] %s", exc.stage, safe)
        body = (
            f"{EMAIL_SUBJECT_PREFIX}\n"
            f"Organization: {args.org_name}\n"
            f"Platform: {platform}\n"
            f"Run timestamp: {run_stamp}\n"
            f"Result: ABORTED during {exc.stage}\n\n"
            f"Reason:\n{safe}\n\n"
            f"{timing_block(start_wallclock, start_perf)}\n"
            "See the attached log for full detail.\n"
        )
        send_email(
            subject + " (ABORTED)", body, [log_file_path],
            args.smtp_server, args.email_to, args.email_from, logger,
        )
        exit_code = 1
    except Exception as exc:
        safe = redact_token(str(exc))
        logger.exception("Unexpected fatal error: %s", safe)
        body = (
            f"{EMAIL_SUBJECT_PREFIX}\n"
            f"Organization: {args.org_name}\n"
            f"Platform: {platform}\n"
            f"Run timestamp: {run_stamp}\n"
            "Result: ABORTED during unexpected processing\n\n"
            f"Reason:\n{safe}\n\n"
            f"{timing_block(start_wallclock, start_perf)}\n"
            "See the attached log for the sanitized traceback.\n"
        )
        send_email(
            subject + " (ABORTED)", body, [log_file_path],
            args.smtp_server, args.email_to, args.email_from, logger,
        )
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
