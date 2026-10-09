#!/usr/bin/env python3
r"""
FEATURE_CLASS_NAME_SCAN.py

Read-only scan of hosted feature-service table NAMES, checked against the
ACTUAL underlying Data Store (SDE) table name (not the published service's
layer/alias name), for compliance with Esri's feature class/table naming
rules. Reports ONLY rows that violate a naming rule AND have an associated
Portal item id. Portal-only; the live Data Store primary is auto-detected
on every run (HA-aware) unless --db-host forces a specific host. 

Uses UTILITIES.gis_connect.connect_gis() (always TLS-verified) for the arcgis
SDK connection, and this script's own explicit, caller-chosen TLS strategy
(--tls-mode) for its direct requests.Session calls (hosting-server admin API,
item-existence REST calls). Token acquisition is centralized in a
UTILITIES.auth TokenProvider and raw values are exposed only at request
construction. The PostgreSQL connection is encrypted and forced read-only.

Example usage:
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --folder QA
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --workers 32
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --db-host datastore.example.com
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --include-all-rows
    python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --tls-mode os-trust-store
"""

import argparse
import csv
import ipaddress
import os
import re
import sys
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock, local
from urllib.parse import quote, urlsplit

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _utilities  # noqa: E402
from UTILITIES import (   # noqa: E402
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
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
    provider_for_gis,
    redact_token,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES import config as _config   # noqa: E402
from UTILITIES.stack_discovery import short_hostname  # noqa: E402

_EXPECTED_UTILITIES_DIR = os.path.realpath(
    os.path.join(_PROJECT_ROOT, "UTILITIES")
)
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_utilities, "__file__", ""))
)
if os.path.normcase(_ACTUAL_UTILITIES_DIR) != os.path.normcase(
        _EXPECTED_UTILITIES_DIR):
    raise ImportError(
        "Refusing to load UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected {_EXPECTED_UTILITIES_DIR!r}."
    )

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2_IMPORT_ERROR = None
except ImportError as _e:  # pragma: no cover
    psycopg2 = None
    _PSYCOPG2_IMPORT_ERROR = _e

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None


def require_psycopg2(logger):
    if psycopg2 is not None:
        return True

    logger.error(
        "The 'psycopg2' package is required for the Data Store table "
        "existence check but is not installed in this Python environment "
        "(%s).\n"
        "This script does NOT install packages automatically. Install it "
        "manually first, then re-run:\n"
        "    %s -m pip install psycopg2-binary\n"
        "(psycopg2-binary is the prebuilt wheel; use plain 'psycopg2' "
        "instead if you specifically need to build against a local "
        "libpq/PostgreSQL client installation.)",
        _PSYCOPG2_IMPORT_ERROR, sys.executable)
    return False


EMAIL_SUBJECT_PREFIX = "Feature Class Name Compliance Scan"

TARGET = "portal"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

CSV_FIELDS = [
    "platform", "status", "table_name", "raw_table_name", "db_exists",
    "db_relkind", "db_owner", "service_name", "service_type",
    "service_admin_url", "layer_id", "layer_name", "kind",
    "portal_item_ids", "portal_items_exist", "portal_item_titles",
    "portal_item_missing", "name_checked", "name_problem", "notes",
]

RELKIND = {"r": "table", "p": "partitioned table", "v": "view",
           "m": "materialized view", "f": "foreign table"}

DEFAULT_WORKERS = 24
MAX_WORKERS = 64
HTTP_TIMEOUT = 30
ADMIN_LIST_TIMEOUT = 180
MAX_IN_FLIGHT_FACTOR = 2
DB_QUERY_BATCH_SIZE = 5000
DB_STATEMENT_TIMEOUT_MS = 300000
_DB_SSL_MODES = ("require", "verify-ca", "verify-full")

STAGE_CONFIG = "CONFIG"
STAGE_PORTAL = "PORTAL"
STAGE_DATASTORE = "DATASTORE_DISCOVERY"
STAGE_DB = "DATABASE"
STAGE_SERVICE = "SERVICE"

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_ITEM_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:498|499)\b|invalid(?: or expired)? token|token[^\n]*expired)"
)


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


@dataclass(frozen=True)
class IdentifierPart:
    value: str
    quoted: bool = False


class TokenManager:
    """Serialize token refresh while keeping normal reads inexpensive."""

    def __init__(self, provider):
        self.provider = provider
        self._refresh_lock = Lock()

    def get(self):
        return self.provider.get()

    def refresh_if_stale(self, token_used):
        with self._refresh_lock:
            current = self.provider.get()
            if current == token_used:
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
        # Serial construction avoids racing process-wide truststore injection.
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


class ArcGISJsonClient:
    """Strict, token-refreshing JSON client for trusted Portal/admin origins."""

    def __init__(self, session_getter, token_manager, trusted_origins):
        self._session_getter = session_getter
        self._tokens = token_manager
        self._trusted_origins = frozenset(trusted_origins)

    @staticmethod
    def _rest_error(data):
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("error"), dict):
            err = data["error"]
            code = err.get("code")
            message = err.get("message") or "unknown ArcGIS REST error"
            details = "; ".join(str(v) for v in (err.get("details") or []))
            text = f"REST error {code}: {message}"
            if details:
                text += f" ({details})"
            return code, text
        if str(data.get("status", "")).lower() == "error":
            messages = data.get("messages") or data.get("message") or "admin API error"
            return data.get("code"), f"ArcGIS admin error: {messages}"
        return None

    @staticmethod
    def _is_auth_error(status, code, message):
        if status == 401 or str(code).strip() in {"498", "499"}:
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
            raise ArcGISRequestError("REST request URL contains user-info credentials")
        origin = (parsed.hostname.lower().rstrip("."), port)
        if origin not in self._trusted_origins:
            raise ArcGISRequestError(
                f"REST request origin https://{origin[0]}:{origin[1]} is not trusted"
            )
        return parsed.geturl()

    def request(self, method, url, *, params=None, data=None, timeout=HTTP_TIMEOUT):
        checked_url = self._validate_url(url)
        for attempt in range(2):
            token = self._tokens.get()
            query = {"f": "json", "token": token}
            form = None
            if method.upper() == "GET":
                if params:
                    query.update(params)
            else:
                form = {"f": "json", "token": token}
                if data:
                    form.update(data)
                query = params or None
            try:
                response = self._session_getter().request(
                    method.upper(), checked_url,
                    params=query,
                    data=form,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.exceptions.SSLError as exc:
                raise ArcGISRequestError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.Timeout as exc:
                raise ArcGISRequestError(f"Request timed out after {timeout}s") from exc
            except requests.exceptions.RequestException as exc:
                raise ArcGISRequestError(f"REST request failed: {exc}") from exc

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                raise ArcGISRequestError(
                    f"Unexpected HTTP {response.status_code} redirect"
                    + (f" to {location}" if location else "")
                )

            if attempt == 0 and response.status_code in {401, 498, 499}:
                self._tokens.refresh_if_stale(token)
                continue

            parsed_data = None
            try:
                parsed_data = response.json()
            except ValueError as exc:
                if response.status_code >= 400:
                    raise ArcGISRequestError(
                        f"HTTP {response.status_code} {response.reason}",
                        status=response.status_code,
                    ) from exc
                raise ArcGISRequestError(
                    f"REST endpoint returned non-JSON data (HTTP "
                    f"{response.status_code})"
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
            return parsed_data
        raise ArcGISRequestError("Authentication retry failed")

    def get(self, url, *, timeout=HTTP_TIMEOUT, **params):
        return self.request("GET", url, params=params, timeout=timeout)

    def post(self, url, *, timeout=HTTP_TIMEOUT, **data):
        return self.request("POST", url, data=data, timeout=timeout)


_LEGAL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_NAME_LEN = 128

_FORBIDDEN_PREFIXES = ("gdb_", "sde_", "delta_")

PROBLEM_FORBIDDEN_PREFIX = ("Table name uses the unsupported reserved "
                            "prefix '{prefix}'")


def _parse_identifier_path(value):
    """Parse dot-qualified PostgreSQL identifiers, honoring doubled quotes."""
    text = str(value or "").strip()
    if not text:
        return []
    parts = []
    chars = []
    in_quotes = False
    part_quoted = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < len(text) and text[i + 1] == '"':
                    chars.append('"')
                    i += 2
                    continue
                in_quotes = False
            else:
                chars.append(ch)
            i += 1
            continue
        if ch == '"':
            if "".join(chars).strip():
                return []
            in_quotes = True
            part_quoted = True
        elif ch == ".":
            rendered = "".join(chars).strip()
            if not rendered:
                return []
            parts.append(IdentifierPart(rendered, part_quoted))
            chars = []
            part_quoted = False
        else:
            chars.append(ch)
        i += 1
    if in_quotes:
        return []
    rendered = "".join(chars).strip()
    if not rendered:
        return []
    parts.append(IdentifierPart(rendered, part_quoted))
    return parts[-2:]


def _identifier_value(part):
    return part.value if part.quoted else part.value.lower()


def _table_reference(raw_name):
    parts = _parse_identifier_path(raw_name)
    if not parts:
        fallback = str(raw_name or "").replace('"', "").strip()
        fallback_parts = [p.strip() for p in fallback.split(".") if p.strip()]
        fallback_parts = fallback_parts[-2:]
        schema = fallback_parts[-2].lower() if len(fallback_parts) == 2 else None
        relation = fallback_parts[-1].lower() if fallback_parts else ""
        display = ".".join(p.lower() for p in fallback_parts)
        return {
            "table_name": display,
            "schema": schema,
            "relation": relation,
            "name_checked": relation,
            "parse_error": bool(raw_name),
        }
    values = [_identifier_value(part) for part in parts]
    schema = values[-2] if len(values) == 2 else None
    relation = values[-1]
    # Keep the historical lower-case display value for ordinary rows.
    display = ".".join(value.lower() for value in values)
    return {
        "table_name": display,
        "schema": schema,
        "relation": relation,
        "name_checked": relation,
        "parse_error": False,
    }


def unqualified_name(table_name):
    return _table_reference(table_name)["name_checked"]


def _character_rule_problem(name):
    if not name:
        return "Table name is blank"
    if len(name) > MAX_NAME_LEN:
        return (f"Table name exceeds the maximum length of "
                f"{MAX_NAME_LEN} characters")
    if not _LEGAL_NAME_RE.match(name):
        if name[0].isdigit():
            return ("Table name starts with a digit; names must start with "
                    "a letter or underscore")
        return ("Table name contains a character not allowed in a name "
                "(only letters, digits, and underscore are permitted, and "
                "only a letter or underscore may start the name)")
    return ""


def _forbidden_prefix_problem(name):
    if not name:
        return ""
    lower = name.lower()
    for prefix in _FORBIDDEN_PREFIXES:
        if lower.startswith(prefix):
            return PROBLEM_FORBIDDEN_PREFIX.format(prefix=prefix)
    return ""


def evaluate_table_name(table_name):
    name = unqualified_name(table_name)
    problems = []
    for problem in (_character_rule_problem(name),
                    _forbidden_prefix_problem(name)):
        if problem:
            problems.append(problem)
    return ";".join(problems)


def normalize_table(name):
    return _table_reference(name)["table_name"]


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


def parse_service_tables(j):
    if not isinstance(j, dict):
        raise ValueError("service definition was not a JSON object")
    jp = j.get("jsonProperties", {}) or {}
    rows, seen = [], set()

    def entries():
        for kind, container in (("layer", jp.get("layers")),
                                ("table", jp.get("tables")),
                                ("layer", j.get("layers")),
                                ("table", j.get("tables"))):
            if isinstance(container, dict):
                for entry in container.values():
                    yield kind, entry
            elif isinstance(container, list):
                for entry in container:
                    yield kind, entry

    def make_row(raw, entry, kind):
        ref = _table_reference(raw)
        lookup_key = (ref["schema"] or "", ref["relation"])
        return {
            "raw_table_name": raw,
            "table_name": ref["table_name"],
            "layer_id": entry.get("id") if isinstance(entry, dict) else None,
            "layer_name": entry.get("name") if isinstance(entry, dict) else None,
            "kind": kind,
            "_db_schema": ref["schema"],
            "_db_relation": ref["relation"],
            "_db_lookup_key": lookup_key,
            "_name_checked": ref["name_checked"],
            "_identifier_parse_error": ref["parse_error"],
        }

    for kind, entry in entries():
        if not isinstance(entry, dict):
            continue
        admin_info = entry.get("adminLayerInfo", {}) or {}
        raw = admin_info.get("tableName") or next(
            iter(find_table_names(entry)), None
        )
        if not raw:
            continue
        row = make_row(raw, entry, kind)
        key = (row["table_name"], entry.get("id"), kind, str(raw))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)

    if not rows:
        for raw in dict.fromkeys(find_table_names(j)):
            rows.append(make_row(raw, {}, "layer"))
    return rows


def extract_portal_items(j):
    ids = []
    pp = j.get("portalProperties") or {}
    for it in (pp.get("portalItems") or []):
        if isinstance(it, dict) and it.get("itemID") is not None:
            item_id = str(it["itemID"]).strip()
            if item_id:
                ids.append(item_id)
        elif isinstance(it, str) and it.strip():
            ids.append(it.strip())
    return list(dict.fromkeys(ids))


class Admin:
    def __init__(self, hosting_url, client, timeout=HTTP_TIMEOUT,
                 list_timeout=ADMIN_LIST_TIMEOUT):
        self.base = hosting_url.rstrip("/")
        self.client = client
        self.timeout = timeout
        self.list_timeout = list_timeout

    def get(self, path, timeout=None, **params):
        url = f"{self.base}/{path.lstrip('/')}"
        return self.client.get(url, timeout=timeout or self.timeout, **params)

    def list_feature_services(self, folder):
        folder_segment = quote(folder, safe="")
        list_path = "admin/services"
        if folder_segment:
            list_path += f"/{folder_segment}"
        data = self.get(list_path, timeout=self.list_timeout)
        if not isinstance(data, dict) or not isinstance(data.get("services"), list):
            raise ArcGISRequestError(
                "Admin services response did not contain a 'services' list"
            )
        output = []
        for entry in data["services"]:
            if not isinstance(entry, dict) or entry.get("type") != "FeatureServer":
                continue
            name = entry.get("serviceName")
            if not isinstance(name, str) or not name.strip():
                raise ArcGISRequestError(
                    "Admin services response contained a FeatureServer without "
                    "a valid serviceName"
                )
            output.append(f"{name.strip()}.FeatureServer")
        return output


def check_portal_item(portal_url, item_id, client):
    result = {"exists": None, "title": None, "error": None}
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id.strip()):
        result["error"] = f"invalid Portal item id {item_id!r}"
        return result
    item_id = item_id.strip()
    url = (f"{portal_url.rstrip('/')}/sharing/rest/content/items/"
           f"{quote(item_id, safe='')}")
    try:
        data = client.get(url)
    except ArcGISRequestError as exc:
        text = str(exc).lower()
        if exc.status == 404 or (
                str(exc.code) in {"400", "404"}
                and "does not exist" in text
                and "inaccessible" not in text):
            return {"exists": False, "title": None, "error": None}
        result["error"] = str(exc)
        return result
    if not isinstance(data, dict):
        result["error"] = "item endpoint returned a non-object JSON response"
    elif str(data.get("id") or "").lower() == item_id.lower():
        result = {"exists": True, "title": data.get("title"), "error": None}
    else:
        result["error"] = "item endpoint returned an unexpected response shape"
    return result


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


def collect_services(admin, portal_url, folder, client, logger, debug=False,
                     workers=DEFAULT_WORKERS):
    services = admin.list_feature_services(folder)
    logger.info("%d feature service(s) found in /%s.", len(services), folder)

    layer_rows, svc_items = [], {}
    per_service = {}
    counters = {
        "services_total": len(services),
        "services_ok": 0,
        "services_failed": 0,
        "item_checks": 0,
        "item_check_errors": 0,
    }

    folder_segment = quote(folder, safe="")
    service_root_path = "admin/services"
    if folder_segment:
        service_root_path += f"/{folder_segment}"

    def process_service(service):
        service_name = service.rsplit(".", 1)[0]
        service_segment = quote(service, safe=".")
        admin_url = f"{admin.base}/{service_root_path}/{service_segment}"
        try:
            data = admin.get(f"{service_root_path}/{service_segment}")
            rows = parse_service_tables(data)
            ids = extract_portal_items(data)
            return service_name, {
                "rows": rows, "ids": ids, "admin_url": admin_url,
                "note": None,
            }, None
        except Exception as exc:
            error = redact_token(str(exc))
            return service_name, {
                "rows": [], "ids": [], "admin_url": admin_url,
                "note": f"fetch failed: {error}",
            }, error

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
                "rows": [], "ids": [],
                "admin_url": f"{admin.base}/{service_root_path}/"
                             f"{quote(service, safe='.')}",
                "note": f"fetch failed: {error}",
            }
        per_service[service_name] = record
        if error:
            counters["services_failed"] += 1
            logger.warning("Service %s: admin JSON read failed: %s",
                           service, error)
        else:
            counters["services_ok"] += 1
            if debug:
                logger.debug("Service %s: %d table(s), %d portal item(s)",
                             service_name, len(record["rows"]),
                             len(record["ids"]))
        done += 1
        if done % 200 == 0 or done == len(services):
            logger.info("Collected %d / %d service(s) ...",
                        done, len(services))

    all_ids = sorted({iid for rec in per_service.values() for iid in rec["ids"]})
    counters["item_checks"] = len(all_ids)
    logger.info("Checking %d unique portal item id(s) with %d worker(s) ...",
                len(all_ids), workers)

    item_cache = {}
    for item_id, future in _bounded_futures(
            lambda iid: check_portal_item(portal_url, iid, client),
            all_ids, workers):
        try:
            info = future.result()
        except Exception as exc:
            info = {"exists": None, "title": None,
                    "error": redact_token(str(exc))}
        item_cache[item_id] = info
        if info.get("error"):
            counters["item_check_errors"] += 1
            logger.warning("Portal item %s could not be checked: %s",
                           item_id, info["error"])

    for service_name in sorted(per_service):
        record = per_service[service_name]
        rows, ids, admin_url = (
            record["rows"], record["ids"], record["admin_url"]
        )

        if record.get("note") and not rows and not ids:
            svc_items[service_name] = {
                "ids": [], "exist": "n/a", "admin_url": admin_url,
                "titles": [], "missing": [],
            }
            continue

        titles, missing, statuses = [], [], []
        for item_id in ids:
            info = item_cache.get(
                item_id, {"exists": None, "title": None, "error": "not checked"}
            )
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
            "titles": [title for title in titles if title], "missing": missing,
        }

        for row in rows:
            row["service_name"] = service_name
            row["service_type"] = "FeatureServer"
            layer_rows.append(row)

    logger.info(
        "Services: %d OK, %d failed; Portal item checks: %d total, %d error(s)",
        counters["services_ok"], counters["services_failed"],
        counters["item_checks"], counters["item_check_errors"],
    )
    return layer_rows, svc_items, counters


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def fetch_table_existence(conn, layer_rows):
    qualified = sorted({
        (row.get("_db_schema"), row.get("_db_relation"))
        for row in layer_rows
        if row.get("_db_schema") and row.get("_db_relation")
    })
    unqualified = sorted({
        row.get("_db_relation")
        for row in layer_rows
        if not row.get("_db_schema") and row.get("_db_relation")
    })
    output = {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
        for batch in _chunks(qualified, DB_QUERY_BATCH_SIZE):
            schemas = [schema for schema, _relation in batch]
            relations = [relation for _schema, relation in batch]
            cursor.execute("""
                SELECT n.nspname AS schema, c.relname AS relname,
                       c.relkind AS relkind, r.rolname AS owner
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_roles r ON r.oid = c.relowner
                JOIN unnest(%(schemas)s::text[], %(tables)s::text[])
                    AS wanted(schema, relname)
                    ON n.nspname = wanted.schema
                   AND c.relname = wanted.relname
                WHERE c.relkind IN ('r','p','v','m','f');
            """, {"schemas": schemas, "tables": relations})
            for row in cursor.fetchall():
                output[(row["schema"], row["relname"])] = dict(row)

        for batch in _chunks(unqualified, DB_QUERY_BATCH_SIZE):
            cursor.execute("""
                SELECT n.nspname AS schema, c.relname AS relname,
                       c.relkind AS relkind, r.rolname AS owner
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_roles r ON r.oid = c.relowner
                WHERE c.relname = ANY(%(tables)s::text[])
                  AND c.relkind IN ('r','p','v','m','f')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%%';
            """, {"tables": batch})
            matches = {}
            for row in cursor.fetchall():
                matches.setdefault(row["relname"], []).append(dict(row))
            for relation in batch:
                candidates = matches.get(relation, [])
                if len(candidates) == 1:
                    output[("", relation)] = candidates[0]
                elif len(candidates) > 1:
                    output[("", relation)] = {
                        "_ambiguous": True,
                        "matches": [
                            f"{entry['schema']}.{entry['relname']}"
                            for entry in candidates
                        ],
                    }
    return output


def build_rows(layer_rows, svc_items, db_tables, platform):
    rows = []
    for layer_row in layer_rows:
        table_name = layer_row["table_name"]
        db = db_tables.get(layer_row.get("_db_lookup_key"))
        service_info = svc_items.get(layer_row.get("service_name", ""), {})

        row = {
            "platform": platform,
            "table_name": table_name,
            "raw_table_name": layer_row.get("raw_table_name", ""),
            "layer_id": layer_row.get("layer_id", ""),
            "layer_name": layer_row.get("layer_name", ""),
            "kind": layer_row.get("kind", ""),
            "service_name": layer_row.get("service_name", ""),
            "service_type": layer_row.get("service_type", ""),
            "service_admin_url": service_info.get("admin_url", ""),
            "portal_item_ids": ";".join(service_info.get("ids", [])),
            "portal_items_exist": service_info.get("exist", "n/a"),
            "portal_item_titles": ";".join(service_info.get("titles", [])),
            "portal_item_missing": ";".join(service_info.get("missing", [])),
            "_name_checked": layer_row.get("_name_checked", ""),
        }

        if layer_row.get("_identifier_parse_error") or not layer_row.get(
                "_db_relation"):
            row.update(
                status="DB_LOOKUP_UNKNOWN", db_exists="unknown",
                db_relkind="", db_owner="",
                notes="The service tableName could not be parsed into a "
                      "database identifier, so existence could not be checked.",
            )
        elif db and db.get("_ambiguous"):
            row.update(
                status="DB_LOOKUP_AMBIGUOUS", db_exists="unknown",
                db_relkind="", db_owner="",
                notes="The service supplied an unqualified table name that "
                      "matched multiple schemas: " + ", ".join(db["matches"]),
            )
        elif db:
            row.update(
                status="MAPPED_OK", db_exists="yes",
                db_relkind=RELKIND.get(db["relkind"], db["relkind"]),
                db_owner=db["owner"], notes="",
            )
        else:
            row.update(
                status="MISSING_DB_TABLE", db_exists="no",
                db_relkind="", db_owner="",
                notes="Hosted feature service references a table that does "
                      "not exist in the live Data Store (orphan feature "
                      "service reference). Name is still evaluated below in "
                      "case it is also a naming problem, but there is no real "
                      "table to fix -- investigate the broken service/table "
                      "link first.",
            )
        rows.append(row)
    return rows


def apply_naming_compliance(rows):
    for row in rows:
        checked = row.get("_name_checked") or unqualified_name(
            row.get("raw_table_name") or row.get("table_name", "")
        )
        row["name_checked"] = checked
        problems = []
        for problem in (_character_rule_problem(checked),
                        _forbidden_prefix_problem(checked)):
            if problem:
                problems.append(problem)
        row["name_problem"] = ";".join(problems)
    return rows


def filter_naming_violations_with_portal_items(rows):
    return [r for r in rows
           if r.get("name_problem") and r.get("portal_item_ids")]


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


def _normalize_hostname(value):
    candidate = str(value or "").strip().rstrip(".")
    if not candidate or any(ch.isspace() for ch in candidate):
        raise ValueError("hostname is blank or contains whitespace")
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    if len(candidate) > 253:
        raise ValueError("hostname exceeds 253 characters")
    labels = candidate.split(".")
    if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError(f"invalid hostname {candidate!r}")
    return candidate


def _normalize_domain_suffix(value):
    if value is None or not str(value).strip():
        return None
    suffix = _normalize_hostname(value)
    try:
        ipaddress.ip_address(suffix)
    except ValueError:
        return suffix.lower()
    raise ValueError("domain suffix cannot be an IP address")


def domain_suffix_from_configured_host(host):
    if host is None:
        return None
    try:
        candidate = _normalize_hostname(host)
    except ValueError:
        return None
    try:
        ipaddress.ip_address(candidate)
        return None
    except ValueError:
        pass
    if "." not in candidate:
        return None
    return candidate.split(".", 1)[1] or None


def resolve_db_host_fqdn(machine_name, domain_suffix, logger=None):
    if not machine_name:
        return machine_name
    normalized = _normalize_hostname(machine_name)
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    if "." in normalized:
        if domain_suffix and not normalized.lower().endswith(
                "." + domain_suffix.lower()):
            raise ValueError(
                f"auto-discovered Data Store host {normalized!r} is outside "
                f"the configured domain suffix {domain_suffix!r}"
            )
        if logger is not None:
            logger.info("Discovered machine name '%s' is a full FQDN; using "
                        "it as-is.", normalized)
        return normalized
    short = short_hostname(normalized)
    fqdn = f"{short}.{domain_suffix}" if domain_suffix else short
    if logger is not None:
        logger.info("Discovered machine name '%s' has no domain; built FQDN "
                    "'%s' using domain-suffix='%s'.",
                    normalized, fqdn, domain_suffix)
    return fqdn


def _normalize_https_base(value, label, *, strip_admin=False,
                          strip_home=False):
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
        raise StepError(STAGE_CONFIG, f"{label} must not contain a query or fragment")
    path = (parsed.path or "").rstrip("/")
    if strip_admin and path.lower().endswith("/admin"):
        path = path[:-len("/admin")]
    if strip_home and path.lower().endswith("/home"):
        path = path[:-len("/home")]
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


def _registered_server_bases(gis, logger):
    """Return normalized public/admin bases from Portal's federation registry."""
    try:
        raw = gis.admin.federation.servers
        if callable(raw):
            raw = raw()
    except Exception as exc:
        raise StepError(
            STAGE_PORTAL,
            f"Could not read Portal federation metadata: {exc}",
        ) from exc
    entries = raw.get("servers", []) if isinstance(raw, dict) else raw
    if not isinstance(entries, (list, tuple)):
        raise StepError(
            STAGE_PORTAL,
            "Portal federation metadata did not contain a server list",
        )
    bases = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("adminUrl", "url", "serverUrl"):
            candidate = entry.get(key)
            if not candidate:
                continue
            try:
                normalized = _normalize_https_base(
                    candidate, f"federated server {key}", strip_admin=True
                )
            except StepError as exc:
                logger.warning("Ignoring invalid federated-server URL: %s", exc)
                continue
            bases[_server_base_key(normalized)] = normalized
    if not bases:
        raise StepError(
            STAGE_PORTAL,
            "Portal reported no usable HTTPS URLs for its federated servers",
        )
    return bases


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


def _folder_name(value):
    folder = str(value or "").strip()
    if folder in {"/", "\\"}:
        return ""  # ArcGIS Server's root services folder.
    if not folder:
        raise argparse.ArgumentTypeError(
            "--folder cannot be blank; use '/' for the root services folder"
        )
    if folder in {".", ".."} or any(ch in folder for ch in "/\\?#"):
        raise argparse.ArgumentTypeError(
            "--folder must be one ArcGIS services-folder name, not a path"
        )
    if any(ord(ch) < 32 for ch in folder):
        raise argparse.ArgumentTypeError("--folder contains a control character")
    return folder


def _reserve_run_paths(output_dir, org_prefix, platform, timestamp):
    for sequence in range(1000):
        run_stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_fc_name_scan_{run_stamp}.log"
        )
        violations = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_FC_NAME_VIOLATIONS_{run_stamp}.csv",
        )
        all_rows = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_FC_NAME_ALL_ROWS_{run_stamp}.csv",
        )
        if os.path.exists(violations) or os.path.exists(all_rows):
            continue
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_stamp, log_path, violations, all_rows
    raise StepError(STAGE_CONFIG,
                    "Could not reserve a unique output name after 1,000 attempts")


def _discover_data_store(client, hosting_url, logger):
    """Strict equivalent of discover_data_store without raw-response logging."""
    url = hosting_url.rstrip("/") + "/admin/data/findItems"
    data = client.post(url, ancestorPath="/enterpriseDatabases")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise StepError(
            STAGE_DATASTORE,
            "Data Store findItems response did not contain an 'items' list",
        )
    items = []
    distinct_primaries = set()
    ambiguous = False
    all_fqdns = []
    for item_data in data["items"]:
        if not isinstance(item_data, dict):
            ambiguous = True
            continue
        path = item_data.get("path", "")
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        info = item_data.get("info") or {}
        malformed_info = not isinstance(info, dict)
        if malformed_info:
            info = {}
        machines = info.get("machines") or []
        malformed_machines = not isinstance(machines, list)
        if malformed_machines:
            machines = []
        all_names = []
        primary_names = []
        standby = []
        role_present = False
        for machine in machines:
            if not isinstance(machine, dict) or not machine.get("name"):
                continue
            machine_name = str(machine["name"]).strip()
            role = str(machine.get("role") or "").strip().upper()
            all_names.append(machine_name)
            role_present = role_present or bool(role)
            if role == "PRIMARY":
                primary_names.append(machine_name)
            elif role == "STANDBY":
                standby.append(machine_name)
        item_ambiguous = malformed_info or malformed_machines
        primary = None
        if len(primary_names) == 1 and not item_ambiguous:
            primary = primary_names[0]
        elif len(primary_names) > 1:
            item_ambiguous = True
        elif len(all_names) == 1 and not role_present:
            primary = all_names[0]
        else:
            # No machine data, conflicting roles, or multiple machines without
            # a single PRIMARY are all indeterminate for an HA-aware scan.
            item_ambiguous = True
        if primary:
            distinct_primaries.add(primary.lower())
        if item_ambiguous:
            ambiguous = True
        all_fqdns.extend(all_names)
        items.append({
            "name": name, "path": path, "type": item_data.get("type"),
            "primary": primary, "standby": standby,
            "all_machines": all_names, "ambiguous": item_ambiguous,
        })
        logger.info("Data Store item %r: primary=%s standby=%s machines=%s",
                    name, primary, standby or "none", all_names or "none")
    if len(distinct_primaries) > 1:
        ambiguous = True
    resolved = None
    if len(distinct_primaries) == 1:
        wanted = next(iter(distinct_primaries))
        for item in items:
            if item.get("primary") and item["primary"].lower() == wanted:
                resolved = item["primary"]
                break
    return {
        "host": resolved,
        "fqdns": all_fqdns,
        "items": items,
        "ambiguous": ambiguous,
        "error": None,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only scan of hosted feature-service table NAMES "
                   "against the ACTUAL Data Store (SDE) table name for "
                   "compliance with Esri's feature class/table naming "
                   "rules. Portal-only. The Data Store's live primary "
                   "machine is auto-detected on every run (HA-aware) "
                   "unless --db-host is explicitly given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python FEATURE_CLASS_NAME_SCAN.py --config org1.config.toml\n"
            "  python FEATURE_CLASS_NAME_SCAN.py --config org1.config.toml "
            "--folder QA\n"
            "  python FEATURE_CLASS_NAME_SCAN.py --config org1.config.toml "
            "--db-host datastore.example.com\n"
            "  python FEATURE_CLASS_NAME_SCAN.py --config org1.config.toml "
            "--tls-mode os-trust-store\n"
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
        "--folder", type=_folder_name, default="Hosted",
        help="Admin services folder to scan (default: Hosted).")

    grp_db = parser.add_argument_group(
        "Data Store / database (auto-detected primary; config fallback for "
        "non-host settings)")
    grp_db.add_argument(
        "--db-host", default=None,
        help="EXPLICIT OVERRIDE: force this Data Store host, bypassing live "
            "primary auto-detection entirely. Normally omitted -- the "
            "script discovers the LIVE PRIMARY machine automatically "
            "(HA-aware) via the hosting server's admin API on every run.")
    grp_db.add_argument(
        "--domain-suffix", default=None,
        help="Domain suffix appended to a discovered SHORT Data Store "
            "machine name to build a connectable FQDN. If omitted, taken "
            "from the --config org toml ('domain-suffix'), or inferred from "
            "that TOML's configured 'db-host' FQDN when the explicit key is "
            "absent. Ignored if the discovered name is already a full FQDN, "
            "or if --db-host is given explicitly.")
    grp_db.add_argument(
        "--db-port", type=_bounded_int("--db-port", 1, 65535), default=None,
        help="Data Store port (default from config, else 9876).")
    grp_db.add_argument(
        "--dbname", default=None,
        help="Database name. If omitted, taken from the --config org toml.")
    grp_db.add_argument(
        "--db-user", default=None,
        help="DB admin. If omitted, taken from the --config org toml "
            "('db-user').")
    grp_db.add_argument(
        "--keyring-service-db", default=None,
        help="Keyring service name for the DB password. If omitted, taken "
            "from the --config org toml ('keyring-service-db').")
    grp_db.add_argument(
        "--db-sslmode", choices=_DB_SSL_MODES, default=None,
        help="PostgreSQL TLS mode. Defaults to 'require' (encrypted). Use "
             "'verify-full' with --db-sslrootcert for server identity "
             "verification.")
    grp_db.add_argument(
        "--db-sslrootcert", default=None,
        help="PEM root CA for PostgreSQL when --db-sslmode is verify-ca or "
             "verify-full. May also be supplied as config 'db-sslrootcert'.")

    grp_misc = parser.add_argument_group("Performance / debugging")
    grp_misc.add_argument(
        "--workers", type=_bounded_int("--workers", 1, MAX_WORKERS), default=DEFAULT_WORKERS,
        help=f"Concurrent worker threads (default: {DEFAULT_WORKERS}).")
    grp_misc.add_argument(
        "--debug", action="store_true",
        help="Verbose per-service debug logging.")
    grp_misc.add_argument(
        "--include-all-rows", action="store_true",
        help="Also write a SECOND, UNFILTERED CSV containing every "
            "service-referenced table row (MAPPED_OK and MISSING_DB_TABLE "
            "alike), in addition to the default filtered naming-violation "
            "report.")

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the hosting-"
             "server admin API and item-existence REST calls -- NOT the "
             "arcgis SDK's own internal connection, which always verifies "
             "TLS via connect_gis()) verifies TLS certificates. 'standard' "
             "(default): certifi's bundled public CA list -- correct for "
             "any Portal with a public CA cert. 'os-trust-store': verify "
             "against the operating system's native certificate store "
             "(requires the 'truststore' package) -- use for an internal "
             "Enterprise Portal signed by an organizational CA already "
             "trusted at the OS level. 'custom-ca-bundle': verify against "
             "an explicit PEM file passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")

    return parser.parse_args()


def _write_report(path, rows, logger, label):
    try:
        with open(path, "w", newline="", encoding="utf-8") as file_handle:
            writer = csv.DictWriter(file_handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
        logger.info("Wrote %d row(s) to %s%s", len(rows), path, label)
        return True
    except OSError as exc:
        logger.error("Could not write output CSV %s: %s", path, exc)
        return False


def _run_scan(args, logger, timestamp, start_wallclock, start_perf,
              subject, log_file_path, violations_csv,
              all_rows_csv_path, max_email_mb):
    def abort(stage, message):
        raise StepError(stage, redact_token(str(message)))

    hosting_url = resolve_extra(args, "hosting-url", args.target)
    dbname = resolve_extra(args, "dbname", args.target)
    db_user = resolve_extra(args, "db-user", args.target)
    db_keyring = resolve_extra(args, "keyring-service-db", args.target)
    db_sslmode = str(resolve_extra(
        args, "db-sslmode", args.target, default="require"
    )).strip().lower()
    db_sslrootcert = resolve_extra(
        args, "db-sslrootcert", args.target, default=None
    )
    if db_sslmode not in _DB_SSL_MODES:
        abort(STAGE_CONFIG,
              f"Invalid db-sslmode {db_sslmode!r}; choose one of "
              f"{_DB_SSL_MODES}")
    if db_sslmode in {"verify-ca", "verify-full"} and not db_sslrootcert:
        abort(STAGE_CONFIG,
              f"db-sslmode={db_sslmode} requires --db-sslrootcert or config "
              "'db-sslrootcert'")
    if db_sslrootcert and not os.path.isfile(db_sslrootcert):
        abort(STAGE_CONFIG,
              f"PostgreSQL root CA file does not exist: {db_sslrootcert}")

    args.domain_suffix = resolve_extra(args, "domain-suffix", args.target)
    domain_suffix_source = "config/command line 'domain-suffix'"
    if not args.domain_suffix:
        configured_db_host = _config.get_config_value(
            "db-host", args.target, path=args.config
        )
        args.domain_suffix = domain_suffix_from_configured_host(
            configured_db_host
        )
        domain_suffix_source = (
            "inferred from config 'db-host'"
            if args.domain_suffix else "not available"
        )
    try:
        args.domain_suffix = _normalize_domain_suffix(args.domain_suffix)
    except ValueError as exc:
        abort(STAGE_CONFIG, f"Invalid domain suffix: {exc}")

    db_port_raw = resolve_extra(args, "db-port", args.target, default=9876)
    try:
        if isinstance(db_port_raw, bool):
            raise ValueError("boolean is not a valid port")
        db_port = int(db_port_raw)
    except (TypeError, ValueError):
        abort(STAGE_CONFIG, f"Invalid db-port value: {db_port_raw!r}")
    if not 1 <= db_port <= 65535:
        abort(STAGE_CONFIG, "db-port must be between 1 and 65535")

    if requests is None:
        abort(STAGE_CONFIG, "The 'requests' package is required but not installed.")
    if not require_psycopg2(logger):
        abort(
            STAGE_CONFIG,
            "The 'psycopg2' package is required for the Data Store table "
            f"check but is not installed ({_PSYCOPG2_IMPORT_ERROR}). Install "
            f"it manually with:\n    {sys.executable} -m pip install "
            "psycopg2-binary\nthen re-run this script.",
        )

    missing = []
    if not hosting_url:
        missing.append("--hosting-url (config 'hosting-url')")
    if not dbname:
        missing.append("--dbname (config 'dbname')")
    if not db_user:
        missing.append("--db-user (config 'db-user')")
    if not db_keyring:
        missing.append("--keyring-service-db (config 'keyring-service-db')")
    if missing:
        abort(
            STAGE_CONFIG,
            "Missing required Data Store / hosting setting(s): "
            + ", ".join(missing)
            + f". Edit {args.config} (the [{args.target}] or [common] section) "
              "or pass the flag(s) explicitly.",
        )

    try:
        portal_base = _normalize_https_base(
            args.portal_url, "Portal URL", strip_home=True
        )
        hosting_base = _normalize_https_base(
            hosting_url, "hosting URL", strip_admin=True
        )
    except StepError as exc:
        abort(exc.stage, str(exc))

    logger.info("Hosting URL       : %s", hosting_base)
    logger.info("DB name/user/port  : %s / %s / %d", dbname, db_user, db_port)
    logger.info("DB keyring service : %s", db_keyring)
    logger.info("DB TLS mode        : %s", db_sslmode)
    if db_sslrootcert:
        logger.info("DB TLS root CA     : %s", db_sslrootcert)
    logger.info("Domain suffix      : %s (%s)",
                args.domain_suffix or "(none)", domain_suffix_source)

    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        handshake_session = build_secure_session(
            mode,
            ca_bundle_path=args.ca_bundle_path,
            pool_connections=4,
            pool_maxsize=4,
            retry_total=3,
            logger=logger,
        )
        for label, url in (("Portal", portal_base), ("hosting server", hosting_base)):
            ok, detail = verify_tls_handshake(url, handshake_session, logger=logger)
            if not ok:
                abort(
                    STAGE_PORTAL if label == "Portal" else STAGE_SERVICE,
                    f"TLS handshake verification to {label} {url} failed: "
                    f"{detail.get('error')}",
                )
    except StepError:
        raise
    except Exception as exc:
        abort(STAGE_CONFIG,
              f"Could not establish TLS verification strategy "
              f"'{args.tls_mode}': {exc}")

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        gis = connect_gis(
            portal_base, args.username, args.keyring_service, logger,
            verify_mode=mode, ca_bundle_path=ca_path,
        )
    except Exception as exc:
        abort(STAGE_PORTAL, f"Could not connect to portal: {exc}")

    platform_actual = get_platform(portal_base)
    if platform_actual != "PORTAL":
        abort(
            STAGE_PORTAL,
            f"This is a Portal-only scanner, but the configured URL was "
            f"classified as {platform_actual!r}.",
        )

    registered_bases = _registered_server_bases(gis, logger)
    hosting_key = _server_base_key(hosting_base)
    if hosting_key not in registered_bases:
        known = ", ".join(sorted(registered_bases.values()))
        abort(
            STAGE_CONFIG,
            f"Configured hosting URL {hosting_base!r} is not registered with "
            f"this Portal. Registered server URL(s): {known}",
        )
    logger.info("Hosting URL verified against Portal federation metadata: %s",
                registered_bases[hosting_key])

    provider = provider_for_gis(gis, platform_actual)
    token_manager = TokenManager(provider)
    logger.info("Token acquired via %s (token present: %s)",
                type(provider).__name__, provider.is_present())

    trusted_origins = {_origin(portal_base), _origin(hosting_base)}
    sessions = WorkerSessionPool(mode, args.ca_bundle_path)
    client = ArcGISJsonClient(sessions.get, token_manager, trusted_origins)

    try:
        logger.info("STEP 1/4: Detecting live Data Store topology "
                    "(primary/HA-aware)")
        if args.db_host:
            try:
                db_host = _normalize_hostname(args.db_host)
            except ValueError as exc:
                abort(STAGE_CONFIG, f"Invalid --db-host: {exc}")
            logger.warning(
                "--db-host was explicitly given ('%s'); SKIPPING live primary "
                "auto-detection entirely.", db_host,
            )
        else:
            try:
                datastore = _discover_data_store(client, hosting_base, logger)
            except ArcGISRequestError as exc:
                abort(STAGE_DATASTORE,
                      f"Could not discover Data Store topology: {exc}")
            if not datastore.get("items"):
                abort(
                    STAGE_DATASTORE,
                    "The hosting server reported no registered "
                    "enterpriseDatabases items. Verify Data Store registration "
                    "or pass --db-host explicitly.",
                )
            if datastore.get("ambiguous") or not datastore.get("host"):
                detail_lines = [
                    f"item '{item['name']}': machines={item['all_machines']} "
                    f"primary={item['primary']} standby={item['standby']} "
                    f"ambiguous={item['ambiguous']}"
                    for item in datastore["items"]
                ]
                abort(
                    STAGE_DATASTORE,
                    "Could not confidently determine one live PRIMARY Data "
                    "Store machine. Refusing to guess.\n\n" +
                    "\n".join(detail_lines) +
                    "\n\nPass --db-host explicitly to bypass auto-detection.",
                )
            machine = datastore["host"]
            if "." not in str(machine) and not args.domain_suffix:
                abort(
                    STAGE_CONFIG,
                    f"Data Store discovery returned short machine name "
                    f"{machine!r}, but no domain suffix is available.",
                )
            try:
                db_host = resolve_db_host_fqdn(
                    machine, args.domain_suffix, logger=logger
                )
            except ValueError as exc:
                abort(STAGE_DATASTORE, str(exc))

        logger.info("Resolved DB connection target: %s@%s:%s/%s",
                    db_user, db_host, db_port, dbname)
        admin = Admin(hosting_base, client)
        logger.info("STEP 2/4: Collecting FeatureServers from /%s", args.folder)
        try:
            layer_rows, service_items, scan_stats = collect_services(
                admin, portal_base, args.folder, client, logger,
                args.debug, workers=args.workers,
            )
        except ArcGISRequestError as exc:
            abort(STAGE_SERVICE,
                  f"Could not enumerate hosting services: {exc}")
        logger.info("Collected %d layer/table mapping(s) across %d service(s)",
                    len(layer_rows), len(service_items))

        logger.info("STEP 3/4: Confirming referenced table(s) exist in the "
                    "Data Store")
        db_tables = {}
        references = [row for row in layer_rows if row.get("_db_relation")]
        if references:
            db_password = get_keyring_password(
                db_keyring, db_user, "PGPASSWORD", "database", logger
            )
            connect_kwargs = {
                "host": db_host,
                "port": db_port,
                "dbname": dbname,
                "user": db_user,
                "password": db_password,
                "connect_timeout": 10,
                "sslmode": db_sslmode,
                "application_name": "FEATURE_CLASS_NAME_SCAN",
                "options": (
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}"
                ),
            }
            if db_sslrootcert:
                connect_kwargs["sslrootcert"] = db_sslrootcert
            try:
                connection = psycopg2.connect(**connect_kwargs)
            except psycopg2.OperationalError as exc:
                abort(
                    STAGE_DB,
                    f"Could not connect to PostgreSQL "
                    f"{db_user}@{db_host}:{db_port}/{dbname}: {exc}",
                )
            except Exception as exc:
                abort(STAGE_DB, f"Unexpected DB connection error: {exc}")
            finally:
                # Remove caller-held references as soon as connect has either
                # succeeded or failed. The live connection necessarily retains
                # whatever libpq needs internally.
                db_password = None
                connect_kwargs.pop("password", None)
            try:
                connection.set_session(readonly=True, autocommit=True)
                db_tables = fetch_table_existence(connection, layer_rows)
            except psycopg2.Error as exc:
                abort(STAGE_DB, f"Table existence query failed: {exc}")
            finally:
                connection.close()
            logger.info("%d database relation lookup(s) resolved",
                        len(db_tables))
        else:
            db_password = None
            logger.info("No table references found; skipping DB check")

        all_rows = build_rows(layer_rows, service_items, db_tables, platform_actual)
        logger.info("STEP 4/4: Evaluating table names against naming rules")
        apply_naming_compliance(all_rows)
        violation_rows = filter_naming_violations_with_portal_items(all_rows)
        logger.info("%d row(s) violate a naming rule AND have an associated "
                    "Portal item id (out of %d total row(s))",
                    len(violation_rows), len(all_rows))

        if not _write_report(violations_csv, violation_rows, logger, ""):
            abort(STAGE_CONFIG,
                  f"Could not write required output CSV {violations_csv}")
        all_rows_csv = None
        optional_output_error = False
        if args.include_all_rows:
            if _write_report(
                    all_rows_csv_path, all_rows, logger,
                    " (unfiltered, --include-all-rows)"):
                all_rows_csv = all_rows_csv_path
            else:
                optional_output_error = True

        prefix_counts = Counter()
        for row in violation_rows:
            for prefix in _FORBIDDEN_PREFIXES:
                if row.get("name_checked", "").lower().startswith(prefix):
                    prefix_counts[prefix] += 1
        missing_count = sum(
            1 for row in all_rows if row.get("status") == "MISSING_DB_TABLE"
        )
        db_lookup_issues = sum(
            1 for row in all_rows
            if row.get("status") in {"DB_LOOKUP_UNKNOWN", "DB_LOOKUP_AMBIGUOUS"}
        )
        incomplete_reasons = []
        if scan_stats["services_failed"]:
            incomplete_reasons.append(
                f"{scan_stats['services_failed']} service definition(s) failed"
            )
        if scan_stats["item_check_errors"]:
            incomplete_reasons.append(
                f"{scan_stats['item_check_errors']} Portal item check(s) failed"
            )
        if db_lookup_issues:
            incomplete_reasons.append(
                f"{db_lookup_issues} table reference(s) could not be mapped "
                "unambiguously"
            )
        if optional_output_error:
            incomplete_reasons.append("the requested all-rows report was not written")
        incomplete = bool(incomplete_reasons)

        logger.info("=" * 70)
        logger.info("RUN SUMMARY")
        logger.info("Organization                    : %s", args.org_name)
        logger.info("Platform                        : %s", platform_actual)
        logger.info("TLS mode                        : %s", args.tls_mode)
        logger.info("DB TLS mode                     : %s", db_sslmode)
        logger.info("DB host used                    : %s (%s)", db_host,
                    "explicit --db-host override" if args.db_host
                    else "auto-detected live primary")
        logger.info("Services listed / failed        : %d / %d",
                    scan_stats["services_total"], scan_stats["services_failed"])
        logger.info("Portal item check errors        : %d",
                    scan_stats["item_check_errors"])
        logger.info("Total service-table rows        : %d", len(all_rows))
        logger.info("Rows w/ table missing from DB   : %d", missing_count)
        logger.info("DB lookup unknown/ambiguous     : %d", db_lookup_issues)
        logger.info("Naming violations w/ portal item: %d", len(violation_rows))
        for prefix, count in prefix_counts.items():
            logger.info("  ...with forbidden prefix '%s': %d", prefix, count)
        logger.info("Violations CSV                  : %s", violations_csv)
        if all_rows_csv:
            logger.info("All-rows CSV (unfiltered)       : %s", all_rows_csv)
        logger.info("Result                          : %s",
                    "INCOMPLETE" if incomplete else "COMPLETE")
        logger.info("Read-only: no changes were made to services, portal, or DB")
        logger.info("=" * 70)

        status = "INCOMPLETE" if incomplete else "COMPLETE"
        db_host_note = (
            "explicit --db-host override" if args.db_host
            else "auto-detected LIVE PRIMARY (HA-aware)"
        )
        body_lines = [
            EMAIL_SUBJECT_PREFIX,
            f"Organization  : {args.org_name}",
            f"Platform      : {platform_actual}",
            f"Run timestamp : {timestamp}",
            f"Result        : {status}",
            f"Portal URL    : {portal_base}",
            f"Hosting URL   : {hosting_base}",
            f"DB            : {db_user}@{db_host}:{db_port}/{dbname}",
            f"DB host source: {db_host_note}",
            f"DB TLS mode   : {db_sslmode}",
            f"TLS mode      : {args.tls_mode}",
            f"Folder        : /{args.folder}",
            "",
            "This report checks the ACTUAL Data Store table name behind each "
            "hosted layer/table (not the published alias).",
            "",
            f"Services listed                  : {scan_stats['services_total']}",
            f"Service definition failures      : {scan_stats['services_failed']}",
            f"Portal item check errors         : {scan_stats['item_check_errors']}",
            f"Total service-table rows         : {len(all_rows)}",
            f"Rows w/ table missing from DB    : {missing_count}",
            f"DB lookup unknown/ambiguous      : {db_lookup_issues}",
            f"Naming violations w/ portal item : {len(violation_rows)}",
        ]
        body_lines.extend(
            f"  ...with forbidden prefix '{prefix}': {count}"
            for prefix, count in prefix_counts.items()
        )
        body_lines.extend(["", f"Violations CSV : {violations_csv}"])
        if all_rows_csv:
            body_lines.append(f"All-rows CSV   : {all_rows_csv}")
        if incomplete:
            body_lines.extend([
                "",
                "WARNING: This run is incomplete. Confirmed findings are "
                "retained, but absence from the report is not proof of "
                "compliance for failed checks.",
                *[f"  - {reason}" for reason in incomplete_reasons],
            ])
        body_lines.extend([
            "", timing_block(start_wallclock, start_perf),
            "READ-ONLY: no changes were made to services, the portal, or the DB.",
            "See the attached log and report(s) for full detail.",
        ])
        body = "\n".join(body_lines)

        attachments, note = gather_attachments(
            log_file_path,
            [violations_csv, all_rows_csv] if all_rows_csv else [violations_csv],
            args.output_dir,
            logger,
            cap_mb=max_email_mb,
            label="CSV",
        )
        body += note
        completion_subject = subject
        if incomplete:
            completion_subject = subject_line(
                EMAIL_SUBJECT_PREFIX + " (INCOMPLETE)", timestamp,
                platform_actual, org_name=args.org_name,
            )
        send_email(completion_subject, body, attachments, args.smtp_server,
                   args.email_to, args.email_from, logger)
        logger.info("%s", status)
        return 1 if incomplete else 0
    finally:
        sessions.close()
        try:
            handshake_session.close()
        except Exception:
            pass


def main():
    args = parse_args()
    args.target = TARGET

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
              "custom-ca-bundle.", file=sys.stderr)
        raise SystemExit(2)

    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()
    finalize_config(args, required=REQUIRED_SETTINGS)
    platform = get_platform(args.portal_url)
    if platform != "PORTAL":
        print(
            f"This Portal-only scanner requires a Portal URL; the configured "
            f"URL was classified as {platform!r}.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    ensure_output_dir(args.output_dir)
    run_stamp, log_file_path, violations_csv, all_rows_csv = _reserve_run_paths(
        args.output_dir, args.org_name_safe, platform, timestamp
    )
    timestamp = run_stamp
    logger = setup_logging(log_file_path, "fc_name_scan")
    max_email_mb = get_max_email_mb(args.target, args.config)

    logger.info("=" * 70)
    logger.info("Feature Class Name Compliance Scan - run started")
    logger.info("Organization      : %s", args.org_name)
    logger.info("Target            : %s", args.target)
    logger.info("Platform          : %s", platform)
    logger.info("Portal URL        : %s", args.portal_url)
    logger.info("Username          : %s", args.username)
    logger.info("Keyring service   : %s", args.keyring_service)
    logger.info("--db-host override: %s", args.db_host or "(none -- auto-detect)")
    logger.info("Folder            : /%s", args.folder)
    logger.info("TLS mode          : %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path    : %s", args.ca_bundle_path)
    logger.info("Output directory  : %s", args.output_dir)
    logger.info("Worker threads    : %d", args.workers)
    logger.info("Include all rows  : %s", bool(args.include_all_rows))
    logger.info("Max email MB      : %s", max_email_mb)
    logger.info("=" * 70)

    subject = subject_line(
        EMAIL_SUBJECT_PREFIX, timestamp, platform, org_name=args.org_name
    )

    try:
        exit_code = _run_scan(
            args, logger, timestamp, start_wallclock, start_perf,
            subject, log_file_path, violations_csv,
            all_rows_csv, max_email_mb,
        )
    except StepError as exc:
        safe_message = redact_token(str(exc))
        logger.error("[%s] %s", exc.stage, safe_message)
        body = (
            f"{EMAIL_SUBJECT_PREFIX}\n"
            f"Organization  : {args.org_name}\n"
            f"Platform      : {platform}\n"
            f"Run timestamp : {timestamp}\n"
            f"Result        : ABORTED during {exc.stage}\n\n"
            f"Reason:\n{safe_message}\n\n"
            f"{timing_block(start_wallclock, start_perf)}\n"
            f"See the attached log for full detail.\n"
        )
        send_email(subject, body, [log_file_path], args.smtp_server,
                   args.email_to, args.email_from, logger)
        exit_code = 1
    except Exception as exc:
        safe_message = redact_token(str(exc))
        logger.exception("Unexpected fatal error: %s", safe_message)
        body = (
            f"{EMAIL_SUBJECT_PREFIX}\n"
            f"Organization  : {args.org_name}\n"
            f"Platform      : {platform}\n"
            f"Run timestamp : {timestamp}\n"
            f"Result        : ABORTED during unexpected failure\n\n"
            f"Reason:\n{safe_message}\n\n"
            f"{timing_block(start_wallclock, start_perf)}\n"
            f"See the attached log for the sanitized traceback.\n"
        )
        send_email(subject, body, [log_file_path], args.smtp_server,
                   args.email_to, args.email_from, logger)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
