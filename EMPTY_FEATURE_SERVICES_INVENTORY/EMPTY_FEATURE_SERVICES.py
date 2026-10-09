#!/usr/bin/env python3
r"""Find hosted feature services whose layers and tables all contain zero rows.

This hardened edition preserves the original classification rule:

* a service is reported only when it has at least one layer/table; and
* every readable layer/table count is exactly zero.

It keeps the original output columns, test/full modes, exclusion feature,
TLS choices, notifications, and empty-CSV behavior.  It additionally makes
search completeness, URL trust, token renewal, item-check errors, and process
status explicit so request failures cannot be mistaken for non-empty services.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock, local
from typing import Iterable, Iterator
from urllib.parse import quote, urljoin, urlsplit

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _UTILITIES  # noqa: E402

_EXPECTED_UTILITIES_DIR = os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_UTILITIES, "__file__", ""))
)
if _ACTUAL_UTILITIES_DIR != _EXPECTED_UTILITIES_DIR:
    raise ImportError(
        "Refusing to import UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected {_EXPECTED_UTILITIES_DIR!r}."
    )

from UTILITIES import (  # noqa: E402
    DEFAULT_CAP,
    HOSTED_FS_CLAUSE,
    VerifyMode,
    add_common_portal_args,
    add_config_arg,
    add_email_args,
    add_exclude_group_arg,
    add_output_dir_arg,
    add_target_arg,
    add_test_flag,
    build_secure_session,
    build_timestamp,
    connect_gis,
    ensure_output_dir,
    epoch_to_str,
    finalize_config,
    gather_attachments,
    get_max_email_mb,
    get_org_id,
    get_platform,
    provider_for_gis,
    redact_token,
    send_email,
    setup_logging,
    start_timer,
    subject_line,
    timing_block,
    tune_gis_session,
    verify_tls_handshake,
)

EMAIL_SUBJECT_PREFIX = "Empty Feature Services"

MAX_ITEMS = 2_000_000
TEST_MODE_ITEM_LIMIT = 250
DEFAULT_WORKERS = 32
MAX_WORKERS = 64
HTTP_TIMEOUT = 30
SEARCH_PAGE_SIZE = 100
MAX_REDIRECTS = 3
MAX_ERROR_SAMPLES = 50
AGOL_TRUSTED_SUFFIX = ".arcgis.com"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

OUTPUT_FIELDS = [
    "platform",
    "title", "item_id", "date_created", "date_modified", "owner",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:498|499)\b|invalid\s+token|token\s+(?:is\s+)?expired|"
    r"token\s+required|authentication\s+token\s+required|\bHTTP\s+401\b)"
)
_TOKEN_REFRESH_LOCK = Lock()


class ScanAbort(RuntimeError):
    """A controlled failure that makes the inventory invalid."""


@dataclass(frozen=True)
class JsonOutcome:
    data: object | None
    error: str | None = None
    tls_error: bool = False


@dataclass(frozen=True)
class CountOutcome:
    count: int | None
    error: str | None = None
    tls_error: bool = False


@dataclass(frozen=True)
class ItemOutcome:
    row: dict | None = None
    error: str | None = None
    tls_error: bool = False


@dataclass
class ScanStats:
    processed: int = 0
    errors: int = 0
    tls_errors: int = 0
    # Run-level integrity failures are distinct from contained per-item errors.
    # They still produce a nonzero process exit status even if a partial CSV was
    # written, because the scan itself did not complete reliably.
    run_failures: int = 0


class CandidateStore:
    """Disk-backed, insertion-ordered candidate store for large organizations."""

    def __init__(self, path: str, logger=None):
        self.path = path
        self._logger = logger
        self._conn: sqlite3.Connection | None = None
        self._count = 0

    def __enter__(self) -> "CandidateStore":
        try:
            self._conn = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self._conn.execute(
                "CREATE TABLE candidates ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "item_id TEXT NOT NULL UNIQUE, "
                "payload TEXT NOT NULL)"
            )
        except Exception:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            try:
                os.remove(self.path)
            except OSError:
                pass
            raise
        return self

    def add(self, item: dict) -> bool:
        if self._conn is None:
            raise RuntimeError("candidate store is not open")
        item_id = str(item["id"])
        payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO candidates(item_id, payload) VALUES (?, ?)",
            (item_id, payload),
        )
        inserted = cursor.rowcount == 1
        if inserted:
            self._count += 1
        else:
            # Match dict de-duplication semantics: retain first insertion order,
            # but let the most recently returned metadata replace older metadata.
            self._conn.execute(
                "UPDATE candidates SET payload = ? WHERE item_id = ?",
                (payload, item_id),
            )
        return inserted

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def remove_ids(self, item_ids: Iterable[str]) -> int:
        if self._conn is None:
            raise RuntimeError("candidate store is not open")
        ids = [str(value) for value in item_ids if value]
        removed = 0
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            marks = ",".join("?" for _ in batch)
            cursor = self._conn.execute(
                f"DELETE FROM candidates WHERE item_id IN ({marks})", batch
            )
            removed += max(cursor.rowcount, 0)
        self._count -= removed
        self._conn.commit()
        return removed

    def count(self) -> int:
        return self._count

    def iter_items(self) -> Iterator[dict]:
        if self._conn is None:
            raise RuntimeError("candidate store is not open")
        cursor = self._conn.execute(
            "SELECT payload FROM candidates ORDER BY seq"
        )
        for (payload,) in cursor:
            item = json.loads(payload)
            if not isinstance(item, dict):
                raise ScanAbort("Candidate spool contained malformed data.")
            yield item

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                if self._logger is not None:
                    self._logger.warning(
                        "Could not remove temporary candidate spool %s: %s",
                        self.path + suffix, cleanup_error,
                    )


class WorkerSessionPool:
    """Give each worker its own Session while retaining one TLS policy."""

    def __init__(self, mode: VerifyMode, ca_bundle_path: str | None):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._local = local()
        self._create_lock = Lock()
        self._sessions: list[requests.Session] = []

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        # Serialize construction so OS trust-store injection is never raced by
        # multiple first-use worker threads.
        with self._create_lock:
            session = build_secure_session(
                self._mode,
                ca_bundle_path=self._ca_bundle_path,
                pool_connections=2,
                pool_maxsize=4,
                retry_total=2,
                logger=None,
            )
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self) -> None:
        for session in self._sessions:
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


def _bounded_int(label: str, minimum: int, maximum: int):
    def _parse(value: str) -> int:
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
    return _parse


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find hosted feature services in AGOL or Portal where every "
                    "layer and table is empty, write them to a timestamped CSV, "
                    "then email a report. TLS verification strategy for direct "
                    "REST calls and the ArcGIS SDK connection is explicit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (scans only 250 services). Pass "
            "--no-test to scan everything. The search is always scoped to "
            "the authenticated organization (orgid).\n\n"
            "Examples:\n"
            "  python EMPTY_FEATURE_SERVICES.py --config org1.config.toml "
            "--target portal\n"
            "  python EMPTY_FEATURE_SERVICES.py --config org2.config.toml "
            "--target agol --no-test --workers 32\n"
            "  python EMPTY_FEATURE_SERVICES.py --config org1.config.toml "
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
        help_text="FULL mode: scan all hosted feature services (up to the cap). "
                  "If omitted, the script runs in test mode and scans only 250 "
                  "services.",
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("--workers", 1, MAX_WORKERS),
        default=DEFAULT_WORKERS,
        help=f"Concurrent item checks (default: {DEFAULT_WORKERS}; "
             f"maximum: {MAX_WORKERS}).",
    )
    parser.add_argument(
        "--trusted-service-origin",
        action="append",
        default=[],
        metavar="HTTPS_ORIGIN",
        help="Optional additional exact HTTPS origin trusted to receive the "
             "Portal token, for an unusual reverse-proxy topology. May be "
             "specified more than once. Normally federation metadata supplies "
             "all legitimate service origins automatically.",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)"
    )
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How direct REST calls and the ArcGIS SDK connection verify TLS. "
             "'standard' uses the public CA set; 'os-trust-store' uses the "
             "operating-system trust store; 'custom-ca-bundle' uses the PEM "
             "file supplied by --ca-bundle-path.",
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle. Required only for "
             "--tls-mode custom-ca-bundle.",
    )
    return parser.parse_args()


def _normalize_portal_base(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    if base.lower().endswith("/home"):
        base = base[:-len("/home")]
    return base


def portal_base_url(gis) -> str:
    public = _normalize_portal_base(getattr(gis, "url", None))
    if public:
        return public
    portal = getattr(gis, "_portal", None)
    fallback = _normalize_portal_base(getattr(portal, "url", None))
    if not fallback:
        raise ScanAbort("The authenticated GIS object exposed no Portal base URL.")
    return fallback


def _validate_target_platform(target: str, platform: str) -> None:
    expected = "AGOL" if target == "agol" else "PORTAL"
    if platform == "UNKNOWN":
        raise ScanAbort("The configured Portal URL could not be classified.")
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} conflicts with the configured URL, which was "
            f"classified as {platform}."
        )


def _reserve_run_paths(output_dir: str, log_prefix: str, csv_prefix: str,
                       timestamp: str):
    """Reserve a unique log name and use the same run stamp for the CSV."""
    for sequence in range(1000):
        run_stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        log_path = os.path.join(output_dir, f"{log_prefix}_{run_stamp}.log")
        csv_path = os.path.join(output_dir, f"{csv_prefix}_{run_stamp}.csv")
        if os.path.exists(csv_path):
            continue
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        if os.path.exists(csv_path):
            try:
                os.remove(log_path)
            except OSError:
                pass
            continue
        return run_stamp, log_path, csv_path
    raise ScanAbort("Could not reserve a unique run filename after 1,000 attempts.")


def _format_rest_error(data: object) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
        return None
    err = data["error"]
    code = err.get("code", "?")
    message_code = err.get("messageCode")
    message = err.get("message") or "unknown REST error"
    details = "; ".join(str(value) for value in (err.get("details") or []))
    pieces = [f"REST error {code}"]
    if message_code:
        pieces.append(f"messageCode={message_code}")
    pieces.append(str(message))
    if details:
        pieces.append(details)
    return redact_token(": ".join(pieces))


def _is_auth_failure(data: object, error_text: str | None) -> bool:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = str(data["error"].get("code", "")).strip()
        if code in {"498", "499"}:
            return True
    return bool(_AUTH_ERROR_RE.search(str(error_text or "")))


def _refresh_provider_once(token_provider, token_used: str) -> str:
    """Refresh at most once for a stale token shared by concurrent workers."""
    with _TOKEN_REFRESH_LOCK:
        current = token_provider.get()
        if current == token_used:
            token_provider.refresh()
        return token_provider.get()


def _origin_from_url(url: str) -> tuple[str, int] | None:
    candidate = str(url or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        if (parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        return parsed.hostname.lower().rstrip("."), parsed.port or 443
    except (TypeError, ValueError):
        return None


def _add_origin_from_value(origins: set[tuple[str, int]], value: object) -> None:
    if not value:
        return
    origin = _origin_from_url(str(value))
    if origin:
        origins.add(origin)


def _federation_server_dicts(gis) -> list[dict]:
    federation = gis.admin.federation.servers
    if isinstance(federation, dict):
        servers = federation.get("servers", [])
    elif isinstance(federation, list):
        servers = federation
    else:
        servers = []
    return [entry for entry in servers if isinstance(entry, dict)]


def _build_trusted_origins(gis, base_url: str, configured_url: str,
                           platform: str, extra_origins: Iterable[str],
                           logger) -> tuple[frozenset[tuple[str, int]], bool]:
    origins: set[tuple[str, int]] = set()
    _add_origin_from_value(origins, base_url)
    _add_origin_from_value(origins, configured_url)
    _add_origin_from_value(origins, getattr(gis, "url", None))

    for supplied in extra_origins:
        try:
            supplied_parts = urlsplit(str(supplied or "").strip())
        except (TypeError, ValueError):
            supplied_parts = None
        origin = _origin_from_url(supplied)
        if (not origin or supplied_parts is None
                or supplied_parts.path not in ("", "/")
                or supplied_parts.query or supplied_parts.fragment):
            raise ScanAbort(
                f"--trusted-service-origin must be an exact HTTPS origin "
                f"without a path, query, fragment, or user-info: "
                f"{supplied!r}"
            )
        origins.add(origin)

    if platform == "PORTAL":
        try:
            for server in _federation_server_dicts(gis):
                for key in (
                    "url", "serverUrl", "publicUrl",
                    "webAdaptorUrl", "servicesUrl",
                ):
                    _add_origin_from_value(origins, server.get(key))
        except Exception as exc:
            logger.warning(
                "Could not enumerate federated-server origins. Service URLs "
                "outside the Portal origin will be rejected unless supplied "
                "with --trusted-service-origin: %s",
                redact_token(str(exc)),
            )

    if not origins:
        raise ScanAbort("No trusted Portal/service origin could be established.")

    rendered = ", ".join(
        f"https://{host}" + (f":{port}" if port != 443 else "")
        for host, port in sorted(origins)
    )
    logger.info("Trusted token destination origin(s): %s", rendered)
    return frozenset(origins), platform == "AGOL"


def _validated_request_url(url: str,
                           trusted_origins: frozenset[tuple[str, int]],
                           allow_agol_suffix: bool) -> tuple[str | None, str | None]:
    try:
        parsed = urlsplit(str(url or "").strip())
        port = parsed.port or 443
    except (TypeError, ValueError) as exc:
        return None, f"could not parse URL: {redact_token(str(exc))}"

    if parsed.scheme.lower() != "https":
        return None, "URL must use HTTPS"
    if not parsed.hostname:
        return None, "URL has no hostname"
    if parsed.username is not None or parsed.password is not None:
        return None, "URL contains user-info credentials"

    host = parsed.hostname.lower().rstrip(".")
    origin = (host, port)
    trusted = origin in trusted_origins
    if not trusted and allow_agol_suffix:
        trusted = port == 443 and host.endswith(AGOL_TRUSTED_SUFFIX)
    if not trusted:
        return None, (
            f"URL origin https://{host}:{port} is outside the trusted "
            "Portal/federation boundary"
        )
    return parsed.geturl(), None


def _request_json_once(session: requests.Session, url: str, token: str,
                       params: dict | None,
                       trusted_origins: frozenset[tuple[str, int]],
                       allow_agol_suffix: bool,
                       timeout: int = HTTP_TIMEOUT) -> JsonOutcome:
    current_url = url
    merged = {"f": "json", "token": token}
    if params:
        merged.update(params)

    for redirect_count in range(MAX_REDIRECTS + 1):
        checked_url, validation_error = _validated_request_url(
            current_url, trusted_origins, allow_agol_suffix
        )
        if validation_error:
            return JsonOutcome(None, validation_error)

        try:
            response = session.get(
                checked_url,
                params=merged,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            return JsonOutcome(None, f"timeout after {timeout}s")
        except requests.exceptions.SSLError as exc:
            return JsonOutcome(
                None,
                redact_token(f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}"),
                tls_error=True,
            )
        except requests.exceptions.RequestException as exc:
            return JsonOutcome(None, redact_token(f"request failed: {exc}"))

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                return JsonOutcome(
                    None,
                    f"HTTP {response.status_code} redirect had no Location header",
                )
            if redirect_count >= MAX_REDIRECTS:
                return JsonOutcome(
                    None, f"too many redirects (more than {MAX_REDIRECTS})"
                )
            current_url = urljoin(checked_url, location)
            continue

        try:
            data = response.json()
        except ValueError:
            if response.status_code >= 400:
                return JsonOutcome(
                    None, f"HTTP {response.status_code} {response.reason}"
                )
            return JsonOutcome(
                None, f"non-JSON response (HTTP {response.status_code})"
            )

        rest_error = _format_rest_error(data)
        if rest_error:
            return JsonOutcome(data, rest_error)
        if response.status_code >= 400:
            return JsonOutcome(
                data, f"HTTP {response.status_code} {response.reason}"
            )
        return JsonOutcome(data)

    return JsonOutcome(None, "redirect processing failed")


def _request_json(session: requests.Session, url: str, token_provider,
                  params: dict | None,
                  trusted_origins: frozenset[tuple[str, int]],
                  allow_agol_suffix: bool,
                  timeout: int = HTTP_TIMEOUT) -> JsonOutcome:
    token = token_provider.get()
    outcome = _request_json_once(
        session, url, token, params, trusted_origins, allow_agol_suffix,
        timeout,
    )
    if not _is_auth_failure(outcome.data, outcome.error):
        return outcome

    refreshed = _refresh_provider_once(token_provider, token)
    return _request_json_once(
        session, url, refreshed, params, trusted_origins, allow_agol_suffix,
        timeout,
    )


def _strict_search_page(base_url: str, query: str, cap: int,
                        session: requests.Session, token_provider,
                        trusted_origins: frozenset[tuple[str, int]],
                        allow_agol_suffix: bool) -> list[dict]:
    search_url = f"{base_url.rstrip('/')}/sharing/rest/search"
    results: list[dict] = []
    start = 1

    while len(results) < cap:
        num_to_get = min(SEARCH_PAGE_SIZE, cap - len(results))
        if start + num_to_get - 1 > cap:
            break
        outcome = _request_json(
            session,
            search_url,
            token_provider,
            {
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": num_to_get,
            },
            trusted_origins,
            allow_agol_suffix,
        )
        if outcome.error:
            raise ScanAbort(
                f"Search page starting at {start} failed: {outcome.error}"
            )
        data = outcome.data
        if not isinstance(data, dict):
            raise ScanAbort("Search returned a non-object JSON response.")

        page = data.get("results")
        if page is None:
            page = data.get("items")
        if page is None:
            page = []
        if not isinstance(page, list):
            raise ScanAbort("Search response did not contain a list of results.")
        if any(not isinstance(item, dict) for item in page):
            raise ScanAbort("Search returned a malformed non-object item.")

        if not page:
            try:
                declared_total = int(data.get("total", len(results)))
            except (TypeError, ValueError):
                declared_total = len(results)
            if declared_total > len(results):
                raise ScanAbort(
                    "Search returned an empty page before its declared total "
                    "was reached."
                )
            break

        results.extend(page)
        previous_start = start
        next_start = data.get("nextStart")
        if isinstance(next_start, int) and next_start > 0:
            start = next_start
        else:
            start += len(page)
        if start <= previous_start:
            raise ScanAbort("Search pagination made no forward progress.")

        try:
            total = int(data.get("total", len(results)))
        except (TypeError, ValueError):
            total = len(results)
        if len(results) >= total:
            break

    return results[:cap]


def _minimal_candidate(result: dict, org_id: str) -> dict:
    item_id = str(result.get("id") or "").strip()
    if not item_id:
        raise ScanAbort("Search returned an item without an id.")
    reported_org = result.get("orgId")
    if reported_org not in (None, "", org_id):
        raise ScanAbort(
            f"Search returned out-of-organization item {item_id!r} "
            f"(orgId={reported_org!r}, expected {org_id!r})."
        )
    raw_url = result.get("url")
    return {
        "id": item_id,
        "title": result.get("title") or "",
        "url": raw_url or "",
        "created": result.get("created"),
        "modified": result.get("modified"),
        "owner": result.get("owner") or "",
        "orgId": reported_org,
    }


def search_hosted_feature_services(
        session: requests.Session,
        base_url: str,
        token_provider,
        org_id: str,
        max_items: int,
        store: CandidateStore,
        trusted_origins: frozenset[tuple[str, int]],
        allow_agol_suffix: bool,
        logger,
        fail_if_limit_reached: bool = False) -> int:
    """Strict, org-scoped date bucketing with early stop at max_items."""
    if not org_id:
        raise ScanAbort(
            "Could not determine the organization id; refusing an unscoped "
            "hosted-service search."
        )

    extra_clause = f"({HOSTED_FS_CLAUSE}) AND orgid:{org_id}"
    span_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    query_count = 0

    def search_range(lo_ms: int, hi_ms: int) -> bool:
        nonlocal query_count
        if store.count() >= max_items:
            if fail_if_limit_reached:
                raise ScanAbort(
                    f"The full scan reached its {max_items:,}-item safety cap; "
                    "catalog completeness cannot be guaranteed."
                )
            return True
        query_count += 1
        query = f"{extra_clause} AND created:[{lo_ms} TO {hi_ms}]"
        results = _strict_search_page(
            base_url, query, DEFAULT_CAP, session, token_provider,
            trusted_origins, allow_agol_suffix,
        )

        if len(results) >= DEFAULT_CAP and hi_ms > lo_ms:
            midpoint = lo_ms + (hi_ms - lo_ms) // 2
            if midpoint <= lo_ms:
                midpoint = lo_ms + 1
            if search_range(lo_ms, midpoint):
                return True
            return search_range(midpoint + 1, hi_ms)
        if len(results) >= DEFAULT_CAP:
            raise ScanAbort(
                f"A one-millisecond search bucket [{lo_ms} TO {hi_ms}] "
                f"reached the {DEFAULT_CAP}-item cap; completeness cannot be "
                "guaranteed."
            )

        for result in results:
            store.add(_minimal_candidate(result, org_id))
            if store.count() >= max_items:
                store.commit()
                if fail_if_limit_reached:
                    raise ScanAbort(
                        f"The full scan reached its {max_items:,}-item safety "
                        "cap; catalog completeness cannot be guaranteed."
                    )
                return True
        store.commit()
        if query_count % 20 == 0:
            logger.info(
                "  %d bucket queries run, %d unique item(s) retained.",
                query_count, store.count(),
            )
        return False

    logger.info(
        "Strict org-scoped bucketing of created dates from %s to %s "
        "(limit=%d) ...",
        span_start.date(), span_end.date(), max_items,
    )
    cursor = span_start
    while cursor < span_end and store.count() < max_items:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        if search_range(
            int(cursor.timestamp() * 1000),
            int(next_month.timestamp() * 1000) - 1,
        ):
            break
        cursor = next_month

    logger.info(
        "Hosted-service search complete: %d bucket query/queries, %d unique "
        "item(s).", query_count, store.count(),
    )
    return store.count()


def _resolve_excluded_item_ids_strict(gis, raw_group_ids: str | None,
                                      logger) -> set[str]:
    """Honor requested exclusions completely or abort rather than broaden."""
    group_ids = [
        value.strip() for value in str(raw_group_ids or "").split(",")
        if value.strip()
    ]
    excluded: set[str] = set()
    for group_id in group_ids:
        try:
            group = gis.groups.get(group_id)
        except Exception as exc:
            raise ScanAbort(
                f"Could not resolve exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        if group is None:
            raise ScanAbort(f"Exclusion group {group_id!r} was not found.")
        try:
            members = group.content()
        except Exception as exc:
            raise ScanAbort(
                f"Could not read exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        member_ids = {
            str(getattr(member, "id", "")).strip()
            for member in (members or [])
            if getattr(member, "id", None)
        }
        excluded.update(member_ids)
        logger.info(
            "Exclusion group %s (%s) contributes %d item id(s).",
            group_id, getattr(group, "title", "?"), len(member_ids),
        )
    return excluded


def _normalize_sublayer_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    return str(int(text))


def sublayer_count(session: requests.Session, layer_url: str, token_provider,
                   trusted_origins: frozenset[tuple[str, int]],
                   allow_agol_suffix: bool) -> CountOutcome:
    outcome = _request_json(
        session,
        f"{layer_url.rstrip('/')}/query",
        token_provider,
        {"where": "1=1", "returnCountOnly": "true"},
        trusted_origins,
        allow_agol_suffix,
    )
    if outcome.error:
        return CountOutcome(None, outcome.error, outcome.tls_error)
    data = outcome.data
    if not isinstance(data, dict):
        return CountOutcome(None, "count query returned a non-object response")
    if "count" not in data:
        return CountOutcome(None, "count query returned no 'count' field")

    raw_count = data.get("count")
    if isinstance(raw_count, bool):
        return CountOutcome(None, "count query returned a Boolean count")
    try:
        if isinstance(raw_count, float) and not raw_count.is_integer():
            raise ValueError("fractional count")
        count = int(raw_count)
    except (TypeError, ValueError):
        return CountOutcome(None, f"count query returned invalid count {raw_count!r}")
    if count < 0:
        return CountOutcome(None, f"count query returned negative count {count}")
    return CountOutcome(count)


def _service_url_from_item(session: requests.Session, base_url: str,
                           token_provider, item: dict,
                           trusted_origins: frozenset[tuple[str, int]],
                           allow_agol_suffix: bool) -> JsonOutcome:
    service_url = str(item.get("url") or "").strip()
    if service_url:
        return JsonOutcome(service_url)

    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return JsonOutcome(None, "item has no id and no service URL")
    endpoint = (
        f"{base_url.rstrip('/')}/sharing/rest/content/items/"
        f"{quote(item_id, safe='')}"
    )
    outcome = _request_json(
        session, endpoint, token_provider, None,
        trusted_origins, allow_agol_suffix,
    )
    if outcome.error:
        return outcome
    if not isinstance(outcome.data, dict):
        return JsonOutcome(None, "item metadata response was not a JSON object")
    resolved = outcome.data.get("url")
    if not isinstance(resolved, str) or not resolved.strip():
        return JsonOutcome(None, "item metadata contained no service URL")
    return JsonOutcome(resolved.strip())


def _normalize_feature_service_root(url: str) -> tuple[str | None, str | None]:
    """Validate an item URL as a FeatureServer root and normalize a layer URL."""
    candidate = str(url or "").strip()
    try:
        parsed = urlsplit(candidate)
    except (TypeError, ValueError) as exc:
        return None, f"could not parse service URL: {redact_token(str(exc))}"
    if parsed.query or parsed.fragment:
        return None, "service URL must not contain a query string or fragment"
    path = (parsed.path or "").rstrip("/")
    match = re.search(r"(?i)/featureserver(?:/\d+)?$", path)
    if match is None:
        return None, "item URL is not a FeatureServer root or layer URL"
    root_path = re.sub(r"/\d+$", "", path)
    normalized = parsed._replace(path=root_path, query="", fragment="").geturl()
    return normalized, None


def service_is_completely_empty(
        session: requests.Session,
        base_url: str,
        token_provider,
        item: dict,
        trusted_origins: frozenset[tuple[str, int]],
        allow_agol_suffix: bool) -> ItemOutcome:
    """Return a confirmed empty row, a confirmed non-empty result, or an error."""
    item_id = str(item.get("id") or "").strip()
    url_outcome = _service_url_from_item(
        session, base_url, token_provider, item,
        trusted_origins, allow_agol_suffix,
    )
    if url_outcome.error:
        return ItemOutcome(
            error=f"{item_id}: could not resolve service URL: {url_outcome.error}",
            tls_error=url_outcome.tls_error,
        )
    service_url, service_url_error = _normalize_feature_service_root(
        str(url_outcome.data)
    )
    if service_url_error:
        return ItemOutcome(
            error=f"{item_id}: invalid service URL: {service_url_error}"
        )
    assert service_url is not None

    definition_outcome = _request_json(
        session, service_url, token_provider, None,
        trusted_origins, allow_agol_suffix,
    )
    if definition_outcome.error:
        return ItemOutcome(
            error=(f"{item_id}: could not read service definition: "
                   f"{definition_outcome.error}"),
            tls_error=definition_outcome.tls_error,
        )
    definition = definition_outcome.data
    if not isinstance(definition, dict):
        return ItemOutcome(
            error=f"{item_id}: service definition was not a JSON object"
        )

    sublayer_ids: list[str] = []
    for section_name in ("layers", "tables"):
        section = definition.get(section_name, [])
        if section is None:
            section = []
        if not isinstance(section, list):
            return ItemOutcome(
                error=f"{item_id}: service definition field {section_name!r} "
                      "was not a list"
            )
        for entry in section:
            if not isinstance(entry, dict):
                return ItemOutcome(
                    error=f"{item_id}: service definition contained a malformed "
                          f"{section_name} entry"
                )
            raw_sub_id = entry.get("id")
            if raw_sub_id is None:
                # Preserve the original behavior, which ignored entries that
                # did not expose a layer/table id.
                continue
            sub_id = _normalize_sublayer_id(raw_sub_id)
            if sub_id is None:
                return ItemOutcome(
                    error=f"{item_id}: service definition contained an invalid "
                          f"{section_name} id {raw_sub_id!r}"
                )
            sublayer_ids.append(sub_id)

    # Preserve the original behavior: a service with no layers or tables is not
    # reported as an empty feature service.
    if not sublayer_ids:
        return ItemOutcome()

    for sub_id in sublayer_ids:
        count_outcome = sublayer_count(
            session,
            f"{service_url}/{quote(sub_id, safe='')}",
            token_provider,
            trusted_origins,
            allow_agol_suffix,
        )
        if count_outcome.error:
            return ItemOutcome(
                error=(f"{item_id}: layer/table {sub_id} count failed: "
                       f"{count_outcome.error}"),
                tls_error=count_outcome.tls_error,
            )
        if count_outcome.count != 0:
            return ItemOutcome()

    row = {
        "platform": "",  # populated by process_one_item
        "title": item.get("title", ""),
        "item_id": item_id,
        "date_created": epoch_to_str(item.get("created")),
        "date_modified": epoch_to_str(item.get("modified")),
        "owner": item.get("owner", ""),
    }
    return ItemOutcome(row=row)


def process_one_item(session: requests.Session, base_url: str, token_provider,
                     platform: str, item: dict,
                     trusted_origins: frozenset[tuple[str, int]],
                     allow_agol_suffix: bool) -> ItemOutcome:
    try:
        outcome = service_is_completely_empty(
            session, base_url, token_provider, item,
            trusted_origins, allow_agol_suffix,
        )
        if outcome.row is None:
            return outcome
        row = dict(outcome.row)
        row["platform"] = platform
        return ItemOutcome(row=row)
    except Exception as exc:  # final per-item containment
        item_id = item.get("id") if isinstance(item, dict) else "?"
        return ItemOutcome(
            error=redact_token(f"{item_id}: unexpected item-check failure: {exc}")
        )


def _bounded_future_results(executor, items: Iterable[dict], worker,
                            max_in_flight: int):
    iterator = iter(items)
    pending = {}

    def submit_next() -> bool:
        try:
            item = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(worker, item)] = item
        return True

    for _ in range(max_in_flight):
        if not submit_next():
            break

    while pending:
        completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in completed:
            item = pending.pop(future)
            yield item, future
            submit_next()


def scan_candidates(items: Iterable[dict], total_items: int,
                    base_url: str, token_provider, platform: str,
                    mode: VerifyMode, ca_bundle_path: str | None,
                    workers: int,
                    trusted_origins: frozenset[tuple[str, int]],
                    allow_agol_suffix: bool, logger):
    rows: list[dict] = []
    stats = ScanStats()
    error_samples: list[str] = []
    worker_sessions = WorkerSessionPool(mode, ca_bundle_path)

    if total_items == 0:
        logger.warning("No services found. An empty CSV will be written.")
        return rows, stats, error_samples

    logger.info("Checking emptiness with %d worker(s) ...", workers)

    def worker(item: dict) -> ItemOutcome:
        return process_one_item(
            worker_sessions.get(), base_url, token_provider, platform, item,
            trusted_origins, allow_agol_suffix,
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for item, future in _bounded_future_results(
                    executor, items, worker, max(workers, workers * 2)):
                stats.processed += 1
                try:
                    outcome = future.result()
                except Exception as exc:
                    item_id = item.get("id") if isinstance(item, dict) else "?"
                    outcome = ItemOutcome(
                        error=redact_token(
                            f"{item_id}: unexpected worker failure: {exc}"
                        )
                    )

                if outcome.error:
                    stats.errors += 1
                    if outcome.tls_error:
                        stats.tls_errors += 1
                    if len(error_samples) < MAX_ERROR_SAMPLES:
                        error_samples.append(outcome.error)
                    logger.warning("Item error: %s", outcome.error)
                elif outcome.row:
                    rows.append(outcome.row)

                if stats.processed % 50 == 0 or stats.processed == total_items:
                    logger.info(
                        "Progress: %d / %d inspected (%d empty, %d error(s))",
                        stats.processed, total_items, len(rows), stats.errors,
                    )
    finally:
        worker_sessions.close()

    return rows, stats, error_samples


def _write_csv(rows: list[dict], csv_path: str, logger) -> bool:
    """Retain the original behavior, including a header-only file for no rows."""
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        logger.info("Wrote %d row(s) to %s", len(rows), csv_path)
        return True
    except Exception as exc:
        logger.error("Failed to write CSV: %s", exc)
        return False


def _send_abort(subject: str, reason: object, log_path: str, args, logger,
                start_wallclock: str, start_perf: float) -> None:
    safe_reason = redact_token(str(reason))
    logger.error("Run ABORTED: %s", safe_reason)
    body = (
        f"Run ABORTED. {safe_reason}\n\n"
        + timing_block(start_wallclock, start_perf)
    )
    send_email(
        subject, body, [log_path], args.smtp_server, args.email_to,
        args.email_from, logger,
    )


def _log_run_header(args, platform: str, run_stamp: str, log_path: str,
                    csv_path: str, per_run_limit: int, max_email_mb, logger):
    logger.info("=" * 70)
    logger.info("Empty Feature Services - run started")
    logger.info("Organization      : %s", args.org_name)
    logger.info("Target            : %s", args.target)
    logger.info("Platform          : %s", platform)
    logger.info("Mode              : %s",
                "TEST (250 services)" if args.test else "FULL")
    logger.info("Run timestamp     : %s", run_stamp)
    logger.info("Portal URL        : %s", args.portal_url)
    logger.info("Username          : %s", args.username)
    logger.info("Keyring service   : %s", args.keyring_service)
    logger.info("TLS mode          : %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path    : %s", args.ca_bundle_path)
    logger.info("Output directory  : %s", args.output_dir)
    logger.info("Log file          : %s", log_path)
    logger.info("Output CSV        : %s", csv_path)
    logger.info("SMTP server       : %s", args.smtp_server)
    logger.info("Email to          : %s", args.email_to)
    logger.info("Email from        : %s", args.email_from)
    logger.info("Max email MB      : %s", max_email_mb)
    logger.info("Services cap      : %d", per_run_limit)
    logger.info("Worker threads    : %d", args.workers)
    logger.info("Exclude group ID  : %s", args.exclude_group_id or "(none)")
    logger.info("=" * 70)


def _send_completion(args, platform: str, run_stamp: str, log_path: str,
                     csv_path: str, csv_written: bool, max_email_mb,
                     rows: list[dict], stats: ScanStats,
                     error_samples: list[str], start_wallclock: str,
                     start_perf: float, logger) -> int:
    complete = stats.errors == 0 and csv_written
    status = "COMPLETE" if complete else "INCOMPLETE"

    logger.info("=" * 70)
    logger.info("RUN SUMMARY - %s", status)
    logger.info("Organization      : %s", args.org_name)
    logger.info("Platform          : %s", platform)
    logger.info("Mode              : %s", "TEST" if args.test else "FULL")
    logger.info("TLS mode          : %s", args.tls_mode)
    logger.info("Services inspected: %d", stats.processed)
    logger.info("Empty services    : %d", len(rows))
    logger.info("Item errors       : %d", stats.errors)
    if stats.tls_errors:
        logger.info("  of which TLS/CERT verification failures: %d",
                    stats.tls_errors)
    logger.info("Output CSV        : %s",
                csv_path if csv_written else "(NOT WRITTEN)")
    logger.info("=" * 70)

    prefix = EMAIL_SUBJECT_PREFIX if complete else \
        f"{EMAIL_SUBJECT_PREFIX} (INCOMPLETE)"
    subject = subject_line(prefix, run_stamp, platform, org_name=args.org_name)
    mode_label = "TEST (250 services)" if args.test else "FULL"
    body_lines = [
        f"Empty Feature Services - {status}",
        f"Organization  : {args.org_name}",
        f"Platform      : {platform}",
        f"Run timestamp : {run_stamp}",
        f"Mode          : {mode_label}",
        f"TLS mode      : {args.tls_mode}",
        f"Username      : {args.username}",
        f"Excluded group(s): {args.exclude_group_id or '(none)'}",
        "",
        f"Services inspected : {stats.processed}",
        f"Empty services     : {len(rows)}",
        f"Item errors        : {stats.errors}",
    ]
    if stats.tls_errors:
        body_lines.append(
            f"  of which TLS/CERT verification failures: {stats.tls_errors}"
        )
    if not complete:
        body_lines.extend([
            "",
            "This run is INCOMPLETE. Confirmed empty-service findings are "
            "included, but one or more candidates could not be classified. "
            "Do not interpret missing rows as proof that those services are "
            "non-empty. Review the attached log and re-run after correcting "
            "the reported errors.",
        ])
        if error_samples:
            body_lines.append(
                f"The log contains {stats.errors} item error(s); the first "
                f"{min(len(error_samples), MAX_ERROR_SAMPLES)} were retained "
                "as diagnostic samples."
            )
    body_lines.extend([
        "",
        f"Output CSV         : {csv_path if csv_written else '(NOT WRITTEN)'}",
        "",
        timing_block(start_wallclock, start_perf),
        "See the attached log file and CSV for full detail.",
    ])

    attachments, note = gather_attachments(
        log_path, [csv_path] if csv_written else [], args.output_dir, logger,
        cap_mb=max_email_mb,
    )
    if note:
        body_lines.append(note)
    send_email(
        subject, "\n".join(body_lines), attachments, args.smtp_server,
        args.email_to, args.email_from, logger,
    )

    # Report completeness and process success are intentionally separate.
    # Unreachable or unqueryable individual services keep the report marked
    # INCOMPLETE, but they are contained item-level outcomes: when every
    # candidate was attempted and the CSV was written, Windows Task Scheduler
    # should record a successful task run (exit status 0).
    task_completed = csv_written and stats.run_failures == 0
    if task_completed:
        if stats.errors:
            logger.warning(
                "Run completed with %d item-level error(s); returning exit "
                "status 0. The report remains marked INCOMPLETE because "
                "those services could not be classified.",
                stats.errors,
            )
        else:
            logger.info("Success")
        return 0

    if not csv_written:
        logger.error(
            "Run failed because the output CSV was not written; returning "
            "exit status 1."
        )
    else:
        logger.error(
            "Run failed because %d run-level integrity failure(s) occurred; "
            "returning exit status 1.",
            stats.run_failures,
        )
    return 1


def _run(args, platform: str, run_stamp: str, log_path: str, csv_path: str,
         max_email_mb, logger, start_wallclock: str, start_perf: float) -> int:
    _validate_target_platform(args.target, platform)
    per_run_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS
    mode = _TLS_MODE_CHOICES[args.tls_mode]

    direct_session = None
    try:
        direct_session = build_secure_session(
            mode,
            ca_bundle_path=args.ca_bundle_path,
            pool_connections=max(32, args.workers),
            pool_maxsize=max(32, args.workers),
            retry_total=2,
            logger=logger,
        )
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, direct_session, logger=logger
        )
    except Exception as exc:
        if direct_session is not None:
            direct_session.close()
        raise ScanAbort(
            f"Could not establish or verify TLS strategy {args.tls_mode!r}: "
            f"{redact_token(str(exc))}"
        ) from exc
    if not handshake_ok:
        direct_session.close()
        raise ScanAbort(
            f"TLS handshake verification to {args.portal_url} failed: "
            f"{redact_token(str(handshake_detail.get('error')))}"
        )

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
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
            raise ScanAbort(
                f"Could not connect to the target GIS: {redact_token(str(exc))}"
            ) from exc

        tune_gis_session(
            gis, args.workers, timeout=HTTP_TIMEOUT, retry_total=2
        )
        token_provider = provider_for_gis(gis, platform)
        logger.info(
            "Token acquired via %s (token present: %s)",
            type(token_provider).__name__, token_provider.is_present(),
        )

        base_url = portal_base_url(gis)
        trusted_origins, allow_agol_suffix = _build_trusted_origins(
            gis, base_url, args.portal_url, platform,
            args.trusted_service_origin, logger,
        )

        org_id = get_org_id(gis, logger)
        if not org_id:
            raise ScanAbort(
                "Could not determine the authenticated organization id; "
                "refusing an unscoped catalog search."
            )
        logger.info("Restricting search to org-owned content (orgid:%s).", org_id)

        excluded_ids = _resolve_excluded_item_ids_strict(
            gis, args.exclude_group_id, logger
        )

        spool_fd, spool_path = tempfile.mkstemp(
            prefix=(f".{args.org_name_safe}_{platform}_"
                    "EMPTY_FEATURE_SERVICES_"),
            suffix="_candidates.sqlite3",
        )
        os.close(spool_fd)
        with CandidateStore(spool_path, logger=logger) as store:
            search_hosted_feature_services(
                direct_session, base_url, token_provider, org_id,
                per_run_limit, store, trusted_origins, allow_agol_suffix,
                logger, fail_if_limit_reached=not args.test,
            )
            removed = store.remove_ids(excluded_ids) if excluded_ids else 0
            if removed:
                logger.info(
                    "Excluded %d hosted feature service item(s); %d remain.",
                    removed, store.count(),
                )
            total_items = store.count()
            logger.info("Total services to inspect: %d", total_items)

            rows, stats, error_samples = scan_candidates(
                store.iter_items(), total_items, base_url, token_provider,
                platform, mode, ca_path, args.workers, trusted_origins,
                allow_agol_suffix, logger,
            )
            if stats.processed != total_items:
                mismatch = (
                    f"candidate spool contained {total_items} item(s), but only "
                    f"{stats.processed} item(s) were processed"
                )
                stats.errors += 1
                stats.run_failures += 1
                if len(error_samples) < MAX_ERROR_SAMPLES:
                    error_samples.append(mismatch)
                logger.error("%s", mismatch)

        if stats.tls_errors:
            logger.error(
                "*** %d of %d item error(s) were TLS/CERTIFICATE "
                "VERIFICATION FAILURES. The run is incomplete; investigate "
                "the affected service certificates before re-running.",
                stats.tls_errors, stats.errors,
            )

        csv_written = _write_csv(rows, csv_path, logger)
        return _send_completion(
            args, platform, run_stamp, log_path, csv_path, csv_written,
            max_email_mb, rows, stats, error_samples,
            start_wallclock, start_perf, logger,
        )
    finally:
        direct_session.close()


def main():
    args = parse_args()

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    finalize_config(args, required=REQUIRED_SETTINGS)
    ensure_output_dir(args.output_dir)

    platform = get_platform(args.portal_url)
    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()
    log_prefix = f"{args.org_name_safe}_{platform}_empty_feature_services"
    csv_prefix = f"{args.org_name_safe}_{platform}_EMPTY_FEATURE_SERVICES"
    run_stamp, log_path, csv_path = _reserve_run_paths(
        args.output_dir, log_prefix, csv_prefix, timestamp
    )
    logger = setup_logging(log_path, "empty_feature_services")
    max_email_mb = get_max_email_mb(args.target, args.config)
    per_run_limit = TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS
    _log_run_header(
        args, platform, run_stamp, log_path, csv_path, per_run_limit,
        max_email_mb, logger,
    )

    abort_subject = subject_line(
        f"{EMAIL_SUBJECT_PREFIX} (ABORTED)", run_stamp, platform,
        org_name=args.org_name,
    )
    try:
        exit_code = _run(
            args, platform, run_stamp, log_path, csv_path, max_email_mb,
            logger, start_wallclock, start_perf,
        )
    except ScanAbort as exc:
        _send_abort(
            abort_subject, exc, log_path, args, logger,
            start_wallclock, start_perf,
        )
        exit_code = 1
    except Exception as exc:  # fail-closed final boundary
        logger.exception("Unexpected fatal error")
        _send_abort(
            abort_subject,
            f"Unexpected fatal error: {redact_token(str(exc))}",
            log_path, args, logger, start_wallclock, start_perf,
        )
        exit_code = 1

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
