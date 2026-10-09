#!/usr/bin/env python3
r"""Inventory ALL ArcGIS groups older than 90 days, for ArcGIS Online (AGOL)
or ArcGIS Enterprise Portal.

Every run requires --config (an org-specific TOML file) and --target
(portal or agol). Connection, output, and email settings are resolved from
that TOML unless overridden on the command line. Passwords come from the
OS keyring (falling back to the PORTAL_PASSWORD environment variable).

OUTPUT SCOPE
------------
This report lists EVERY group discovered in the organization within the age
range -- not only stale ones. Each discovered group produces exactly one row,
including administrative groups (leavingDisallowed: true) and groups whose
item/member counts could not be measured. The 'status' column describes each
group (Active / Stale variants / Indeterminate), the 'administrative' column
flags leavingDisallowed groups, and count cells are left blank when a count
could not be determined. Only entries that discovery itself rejected as
malformed (bad id or unusable created date) are omitted; those are reported
as discovery-entry errors.

SECURITY / RELIABILITY
----------------------
The direct REST client and the ArcGIS SDK connection use the same explicit
TLS verification mode. Direct REST calls are restricted to the authenticated
Portal origin, never follow redirects, validate ArcGIS error responses, and
retry once after a synchronized token refresh on authentication expiry.

Group discovery is organization-scoped for BOTH AGOL and Enterprise Portal
and fails closed if the organization id cannot be established. This prevents
Portal searches with a created-date clause from including externally visible
Esri/public groups. Catalog paging is strict: a failed, malformed, capped, or
non-progressing search never becomes a successful partial inventory.

The token is centrally held by a TokenProvider and exposed only at the
request-building boundary. Any exception text used outside the redacting
logger is explicitly sanitized.

Usage examples
---------------
    python GROUPS_SCAN.py --config ORG.config.toml --target portal
    python GROUPS_SCAN.py --config ORG.config.toml --target agol
    python GROUPS_SCAN.py --config OTHER_ORG.config.toml --target agol --workers 16
    python GROUPS_SCAN.py --config ORG.config.toml --target portal --output-dir "D:\\reports"
    python GROUPS_SCAN.py --config ORG.config.toml --target portal --tls-mode os-trust-store
    python GROUPS_SCAN.py --config ORG.config.toml --target portal --email-to "analyst@example.com;admin@example.com"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timedelta
from threading import Lock, local
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, TypeVar
from urllib.parse import urlsplit, urlunsplit

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _UTILITIES_PACKAGE  # noqa: E402
from UTILITIES import (  # noqa: E402
    VerifyMode,
    build_secure_session,
    build_timestamp,
    connect_gis,
    ensure_output_dir,
    epoch_to_date,
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
    add_common_portal_args,
    add_config_arg,
    add_email_args,
    add_output_dir_arg,
    add_target_arg,
)
from arcgis.gis import GIS  # noqa: E402

_EXPECTED_UTILITIES_DIR = os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_UTILITIES_PACKAGE, "__file__", ""))
)
if _ACTUAL_UTILITIES_DIR != _EXPECTED_UTILITIES_DIR:
    raise ImportError(
        "Refusing to use an unexpected UTILITIES package. "
        f"Expected {_EXPECTED_UTILITIES_DIR!r}, loaded {_ACTUAL_UTILITIES_DIR!r}."
    )


EMAIL_SUBJECT_PREFIX = "Groups Inventory"
LOGGER_NAME = "groups_inventory"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

AGE_DAYS = 90
DEFAULT_WORKERS = 32
MIN_WORKERS = 1
MAX_WORKERS = 64
PAGE_SIZE = 100
GROUP_SEARCH_CAP = 10_000
HTTP_TIMEOUT_SEC = 30
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ERROR_SAMPLES = 20

OUTPUT_FIELDS = [
    "platform",
    "item_id", "title", "owner", "date_created", "date_modified",
    "memberCount", "itemCount", "status", "administrative",
    "leavingDisallowed", "isInvitationOnly", "access",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_GROUP_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_SAFE_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:invalid|expired)\s+(?:token|credential)|token\s+(?:is\s+)?invalid"
)

_T = TypeVar("_T")
_R = TypeVar("_R")


class ScanAbort(RuntimeError):
    """Fatal condition that makes the organization-wide result invalid."""


class RestCallError(RuntimeError):
    """Strict Portal REST failure with useful classification."""

    def __init__(self, message: str, *, tls_error: bool = False,
                 auth_error: bool = False):
        super().__init__(message)
        self.tls_error = tls_error
        self.auth_error = auth_error


@dataclass
class DiscoveryResult:
    groups: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class GroupScanResult:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    groups_discovered: int = 0
    old_candidates: int = 0
    administrative_included: int = 0
    groups_measured: int = 0
    groups_fully_measured: int = 0
    groups_indeterminate: int = 0
    stale_count: int = 0
    active_count: int = 0
    leaving_lookup_errors: int = 0
    item_count_errors: int = 0
    member_count_errors: int = 0
    discovery_entry_errors: int = 0
    tls_errors: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return (
            self.leaving_lookup_errors
            + self.item_count_errors
            + self.member_count_errors
            + self.discovery_entry_errors
        )

    @property
    def incomplete(self) -> bool:
        return self.error_count > 0

    def add_error(self, message: str, *, category: str,
                  tls_error: bool = False) -> None:
        safe = str(redact_token(str(message)))
        self.errors.append(safe)
        if category == "leaving":
            self.leaving_lookup_errors += 1
        elif category == "item":
            self.item_count_errors += 1
        elif category == "member":
            self.member_count_errors += 1
        elif category == "discovery":
            self.discovery_entry_errors += 1
        else:
            raise ValueError(f"Unknown error category: {category!r}")
        if tls_error:
            self.tls_errors += 1


class FixedSessionPool:
    """Adapter retaining the old single-session callable interfaces."""

    def __init__(self, session: requests.Session):
        self._session = session

    def get(self) -> requests.Session:
        return self._session


class WorkerSessionPool:
    """Give each worker its own Session under one explicit TLS policy."""

    def __init__(self, mode: VerifyMode, ca_bundle_path: Optional[str],
                 initial_session: Optional[requests.Session] = None):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._local = local()
        self._create_lock = Lock()
        self._sessions: List[requests.Session] = []
        if initial_session is not None:
            self._local.session = initial_session
            self._sessions.append(initial_session)

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        # OS trust-store injection is process-global. Serialize first-use
        # construction so worker startup cannot race that global operation.
        with self._create_lock:
            session = build_secure_session(
                self._mode,
                ca_bundle_path=self._ca_bundle_path,
                pool_connections=2,
                pool_maxsize=4,
                retry_total=3,
                logger=None,
            )
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self) -> None:
        seen: set[int] = set()
        for session in self._sessions:
            if id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


class PortalRestClient:
    """Strict JSON client restricted to the authenticated Portal origin."""

    def __init__(self, portal_base: str, session_pool, token_provider, gis,
                 logger):
        self.portal_base = _normalize_portal_base(portal_base)
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.gis = gis
        self.logger = logger
        self._refresh_lock = Lock()

    def _url_for_path(self, path: str) -> str:
        if not isinstance(path, str) or not path.startswith("/sharing/rest/"):
            raise RestCallError(
                "Portal REST path must begin with '/sharing/rest/'"
            )
        if "?" in path or "#" in path or "\\" in path:
            raise RestCallError("Portal REST path contains unsafe characters")
        segments = path.split("/")
        if any(segment in (".", "..") for segment in segments):
            raise RestCallError("Portal REST path contains traversal")
        return self.portal_base + path

    def _refresh_if_stale(self, failed_token: str) -> str:
        with self._refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return current
            # A lightweight SDK request gives its connection a chance to renew
            # the underlying token before the provider re-reads it.
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

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None,
                 timeout: int = HTTP_TIMEOUT_SEC) -> Dict[str, Any]:
        url = self._url_for_path(path)
        token = self.token_provider.get()
        try:
            return self._get_json_once(url, params, timeout, token)
        except RestCallError as exc:
            if not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._get_json_once(url, params, timeout, refreshed)

    def _get_json_once(self, url: str, params: Optional[Dict[str, Any]],
                       timeout: int, token: str) -> Dict[str, Any]:
        merged: Dict[str, Any] = {"f": "json"}
        if params:
            merged.update(params)
        # Callers may not smuggle or replace authentication parameters.
        merged.pop("token", None)
        merged["token"] = token

        try:
            response = self.session_pool.get().get(
                url,
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

        if 300 <= response.status_code < 400:
            status = response.status_code
            response.close()
            raise RestCallError(
                f"HTTP {status} redirect refused for a token-bearing request"
            )

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

        chunks: List[bytes] = []
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
        except requests.exceptions.Timeout as exc:
            raise RestCallError(
                f"timeout while reading response after {timeout}s"
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise RestCallError(
                f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
                tls_error=True,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise RestCallError(
                f"request failed while reading response: {exc}"
            ) from exc
        finally:
            response.close()

        body = b"".join(chunks)
        status = response.status_code
        reason = response.reason
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
            raise RestCallError(
                f"endpoint returned status=error: {messages or 'no message supplied'}"
            )
        return data


def now_ms() -> int:
    return int(time.time() * 1000)


def cutoff_ms(days: int) -> int:
    return now_ms() - int(timedelta(days=days).total_seconds() * 1000)


def portal_base_url(gis: GIS) -> str:
    public_url = getattr(gis, "url", None)
    if public_url:
        return _normalize_portal_base(str(public_url))
    portal = getattr(gis, "_portal", None)
    fallback = getattr(portal, "url", None)
    if not fallback:
        raise ScanAbort("The authenticated GIS object exposed no Portal URL.")
    return _normalize_portal_base(str(fallback))


def _normalize_portal_base(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ScanAbort("Portal URL is blank.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ScanAbort(f"Portal URL could not be parsed: {text!r}") from exc
    if parts.scheme.lower() != "https":
        raise ScanAbort("Portal URL must use HTTPS.")
    if not parts.hostname:
        raise ScanAbort("Portal URL contains no hostname.")
    if parts.username is not None or parts.password is not None:
        raise ScanAbort("Portal URL must not contain user-info credentials.")
    if parts.query or parts.fragment:
        raise ScanAbort("Portal URL must not contain a query or fragment.")
    path = (parts.path or "").rstrip("/")
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


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


def _reserve_run_paths(output_dir: str, org_prefix: str, platform: str,
                       timestamp: str) -> tuple[str, str, str]:
    csv_base = f"{org_prefix}_{platform}_GROUPS_INVENTORY"
    for suffix in range(0, 1000):
        run_stamp = timestamp if suffix == 0 else f"{timestamp}_{suffix:02d}"
        log_path = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_groups_inventory_{run_stamp}.log",
        )
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        csv_path = os.path.join(output_dir, f"{csv_base}_{run_stamp}.csv")
        return run_stamp, log_path, csv_path
    raise RuntimeError("Could not reserve a unique output name after 999 collisions.")


def _platform_expected_for_target(target: str) -> str:
    return "AGOL" if str(target).lower() == "agol" else "PORTAL"


def _validate_target_platform(target: str, platform: str, portal_url: str) -> None:
    expected = _platform_expected_for_target(target)
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} requires platform {expected}, but "
            f"portal-url {portal_url!r} was classified as {platform}."
        )


def _safe_group_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _GROUP_ID_RE.fullmatch(text):
        raise ValueError(f"invalid ArcGIS group id: {text!r}")
    return text


def _safe_org_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _SAFE_ORG_ID_RE.fullmatch(text):
        raise ScanAbort(f"Invalid organization id returned by Portal: {text!r}")
    return text


def _coerce_epoch_ms(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} is Boolean, not an epoch timestamp")
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not an integer epoch timestamp: {value!r}") from exc
    if numeric <= 0:
        raise ValueError(f"{label} must be positive, got {numeric}")
    # Accept epoch seconds defensively; ArcGIS REST normally supplies millis.
    if numeric < 100_000_000_000:
        numeric *= 1000
    return numeric


def _strict_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError(f"{label} was not a Boolean value: {value!r}")


def _search_batch(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if "results" in data:
        batch = data.get("results")
    elif "groups" in data:
        batch = data.get("groups")
    else:
        batch = []
    if not isinstance(batch, list):
        raise ScanAbort("Group search returned a malformed results/groups value.")
    if any(not isinstance(group, dict) for group in batch):
        raise ScanAbort("Group search returned a non-object group entry.")
    return batch


def _search_total(data: Dict[str, Any]) -> int:
    value = data.get("total")
    if isinstance(value, bool):
        raise ScanAbort("Group search returned a Boolean total.")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(f"Group search returned an invalid total: {value!r}") from exc
    if total < 0:
        raise ScanAbort(f"Group search returned a negative total: {total}")
    return total


def _search_next_start(data: Dict[str, Any]) -> int:
    value = data.get("nextStart", -1)
    if isinstance(value, bool):
        raise ScanAbort("Group search returned a Boolean nextStart.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"Group search returned an invalid nextStart: {value!r}"
        ) from exc


def _query_for_range(target: str, org_id: Optional[str], lo_ms: int,
                     hi_ms: int) -> str:
    del target  # Organization scoping is mandatory for both Portal and AGOL.
    if not org_id:
        raise ScanAbort(
            "Organization id is unavailable; refusing an unscoped group "
            "search."
        )
    range_clause = f"created:[{lo_ms} TO {hi_ms}]"
    return f"orgid:{org_id} AND {range_clause}"


def _enumerate_group_range(client: PortalRestClient, target: str,
                           org_id: Optional[str], lo_ms: int, hi_ms: int,
                           seen: Dict[str, Dict[str, Any]], logger,
                           stats: Dict[str, int]) -> None:
    query = _query_for_range(target, org_id, lo_ms, hi_ms)
    params = {
        "q": query,
        "sortField": "created",
        "sortOrder": "asc",
        "num": PAGE_SIZE,
        "start": 1,
    }
    first = client.get_json("/sharing/rest/community/groups", params=params)
    stats["queries"] += 1
    total = _search_total(first)

    if total >= GROUP_SEARCH_CAP:
        if hi_ms <= lo_ms:
            raise ScanAbort(
                f"Group search range [{lo_ms} TO {hi_ms}] reached the "
                f"{GROUP_SEARCH_CAP}-result cap and cannot be split further."
            )
        mid = lo_ms + (hi_ms - lo_ms) // 2
        if mid < lo_ms:
            raise ScanAbort("Group-search subdivision made no progress.")
        _enumerate_group_range(
            client, target, org_id, lo_ms, mid, seen, logger, stats
        )
        _enumerate_group_range(
            client, target, org_id, mid + 1, hi_ms, seen, logger, stats
        )
        return

    start = 1
    data = first
    collected = 0
    range_ids: set[str] = set()
    while True:
        page_total = _search_total(data)
        if page_total != total:
            raise ScanAbort(
                f"Group-search total changed during paging ({total} -> "
                f"{page_total}) for range [{lo_ms} TO {hi_ms}]."
            )
        batch = _search_batch(data)
        if not batch and collected < total:
            raise ScanAbort(
                f"Group search returned an empty page after {collected} of "
                f"{total} results for range [{lo_ms} TO {hi_ms}]."
            )
        collected += len(batch)
        for group in batch:
            raw_gid = group.get("id")
            try:
                gid = _safe_group_id(raw_gid)
            except ValueError as exc:
                raise ScanAbort(str(exc)) from exc
            if gid in range_ids:
                raise ScanAbort(
                    f"Group search repeated id {gid} within one paged range; "
                    "the catalog changed or paging was unstable."
                )
            range_ids.add(gid)
            if org_id:
                explicit_org = group.get("orgId") or group.get("orgid")
                if explicit_org and str(explicit_org) != org_id:
                    raise ScanAbort(
                        f"Group search returned cross-organization group {gid} "
                        f"(orgId={explicit_org!r}, expected {org_id!r})."
                    )
            if gid not in seen:
                seen[gid] = group

        next_start = _search_next_start(data)
        if next_start <= 0:
            if collected != total:
                raise ScanAbort(
                    f"Group search ended after {collected} of {total} results "
                    f"for range [{lo_ms} TO {hi_ms}]."
                )
            break
        if next_start <= start:
            raise ScanAbort(
                f"Group-search pagination did not advance (start={start}, "
                f"nextStart={next_start})."
            )
        if next_start > GROUP_SEARCH_CAP:
            raise ScanAbort(
                f"Group-search nextStart {next_start} exceeded the API cap."
            )
        start = next_start
        params["start"] = start
        data = client.get_json("/sharing/rest/community/groups", params=params)
        stats["queries"] += 1

    if collected != total or len(range_ids) != total:
        raise ScanAbort(
            f"Group search paging mismatch for range [{lo_ms} TO {hi_ms}]: "
            f"expected {total}, read {collected}, unique {len(range_ids)}."
        )


def list_all_groups_fast(gis: GIS, session: requests.Session, token_provider,
                         logger, max_created_ms: Optional[int] = None) -> List[Dict]:
    """Backward-compatible wrapper around strict organization discovery."""
    pool = FixedSessionPool(session)
    client = PortalRestClient(portal_base_url(gis), pool, token_provider, gis,
                              logger)
    target = "agol" if get_platform(portal_base_url(gis)) == "AGOL" else "portal"
    org_id_value = get_org_id(gis, logger=logger)
    org_id = _safe_org_id(org_id_value) if org_id_value else None
    if not org_id:
        raise ScanAbort(
            "Could not determine the organization id; refusing an unscoped "
            "group search."
        )
    hi_ms = max_created_ms if max_created_ms is not None else now_ms()
    seen: Dict[str, Dict[str, Any]] = {}
    stats = {"queries": 0}
    _enumerate_group_range(client, target, org_id, 0, hi_ms, seen, logger, stats)
    logger.info(
        "Discovered %d unique group(s) in %d strict search request(s).",
        len(seen), stats["queries"],
    )
    return list(seen.values())


def _discover_old_groups(gis: GIS, client: PortalRestClient, target: str,
                         age_days: int, logger) -> DiscoveryResult:
    org_id_value = get_org_id(gis, logger=logger)
    org_id = _safe_org_id(org_id_value) if org_id_value else None
    if not org_id:
        raise ScanAbort(
            "Could not determine the organization id; refusing an unscoped "
            "group search."
        )
    logger.info(
        "Restricting %s group discovery to organization id %s.",
        target.upper(), org_id,
    )

    cutoff = cutoff_ms(age_days)
    seen: Dict[str, Dict[str, Any]] = {}
    stats = {"queries": 0}
    _enumerate_group_range(
        client, target, org_id, 0, cutoff, seen, logger, stats
    )

    outcome = DiscoveryResult()
    for gid, group in seen.items():
        try:
            created_ms = _coerce_epoch_ms(group.get("created"),
                                          f"group {gid} created")
            if created_ms > cutoff:
                raise ValueError(
                    f"group {gid} was outside the requested age range"
                )
        except ValueError as exc:
            outcome.errors.append(str(exc))
            continue
        outcome.groups.append(group)

    logger.info(
        "Discovered %d group(s) older than %d days in %d strict search "
        "request(s); %d malformed result(s) skipped.",
        len(outcome.groups), age_days, stats["queries"], len(outcome.errors),
    )
    return outcome


def group_member_count_excluding_owner(gis, session, token_provider,
                                       group) -> int:
    """Backward-compatible strict membership count using the supplied session."""
    client = PortalRestClient(
        portal_base_url(gis), FixedSessionPool(session), token_provider, gis,
        logger=_NullLogger(),
    )
    return _group_member_count(client, group)


def _group_member_count(client: PortalRestClient, group: Dict[str, Any]) -> int:
    gid = _safe_group_id(group.get("id"))
    owner = str(group.get("owner") or "").strip()
    data = client.get_json(
        f"/sharing/rest/community/groups/{gid}/users"
    )
    response_owner = data.get("owner")
    if isinstance(response_owner, str) and response_owner.strip():
        owner = response_owner.strip()
    if not owner:
        raise ValueError(f"group {gid} had no readable owner")

    if "admins" not in data and "users" not in data:
        raise ValueError(
            f"group {gid} membership response omitted admins/users"
        )

    usernames: set[str] = set()
    for key in ("admins", "users"):
        values = data.get(key, [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError(
                f"group {gid} membership field {key!r} was not a list"
            )
        for value in values:
            if isinstance(value, str):
                username = value.strip()
            elif isinstance(value, dict):
                username = str(
                    value.get("username") or value.get("userName") or ""
                ).strip()
            else:
                raise ValueError(
                    f"group {gid} membership field {key!r} contained a "
                    f"malformed entry"
                )
            if username:
                usernames.add(username.casefold())

    usernames.discard(owner.casefold())
    return len(usernames)


def group_leaving_disallowed(gis, session, token_provider, group,
                             group_id) -> bool:
    """Backward-compatible strict leavingDisallowed resolution."""
    checked = dict(group or {})
    checked.setdefault("id", group_id)
    client = PortalRestClient(
        portal_base_url(gis), FixedSessionPool(session), token_provider, gis,
        logger=_NullLogger(),
    )
    return _group_leaving_disallowed(client, checked)


def _group_leaving_disallowed(client: PortalRestClient,
                               group: Dict[str, Any]) -> bool:
    gid = _safe_group_id(group.get("id"))
    if "leavingDisallowed" in group:
        return _strict_bool(
            group.get("leavingDisallowed"),
            f"group {gid} leavingDisallowed",
        )
    data = client.get_json(f"/sharing/rest/community/groups/{gid}")
    response_id = data.get("id")
    if response_id and str(response_id) != gid:
        raise ValueError(
            f"group definition returned id {response_id!r}, expected {gid!r}"
        )
    if "leavingDisallowed" not in data:
        # ArcGIS versions commonly omit this false-valued optional property.
        return False
    return _strict_bool(
        data.get("leavingDisallowed"),
        f"group {gid} leavingDisallowed",
    )


def group_item_count(gis: GIS, group_id: str) -> int:
    gid = _safe_group_id(group_id)
    group = gis.groups.get(gid)
    if group is None:
        raise LookupError(f"group {gid} could not be resolved")
    # Preserve the deployed SDK's established behavior. In this environment,
    # passing max_items=-1 changes the result (commonly truncating it to 10 and
    # occasionally returning 0), whereas the no-argument call returns the full
    # group content collection used by the original scanner.
    content = group.content()
    if content is None:
        raise ValueError(f"group {gid} content lookup returned None")
    if isinstance(content, (str, bytes, dict)):
        raise ValueError(
            f"group {gid} content lookup returned an unexpected value"
        )
    try:
        return len(content)
    except TypeError:
        materialized = list(content)
        return len(materialized)


def _bounded_results(items: Iterable[_T], worker: Callable[[_T], _R],
                     workers: int, *, pending_factor: int = 2
                     ) -> Iterator[tuple[_T, Optional[_R], Optional[BaseException]]]:
    iterator = iter(items)
    max_pending = max(workers, workers * pending_factor)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: Dict[Any, _T] = {}

        def fill() -> None:
            while len(pending) < max_pending:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                pending[executor.submit(worker, item)] = item

        fill()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                item = pending.pop(future)
                try:
                    yield item, future.result(), None
                except BaseException as exc:  # contained and reported per item
                    yield item, None, exc
            fill()


def _error_is_tls(exc: BaseException) -> bool:
    if isinstance(exc, RestCallError):
        return exc.tls_error
    return isinstance(exc, requests.exceptions.SSLError)


def _status_from_counts(item_count: Optional[int],
                        member_count: Optional[int]) -> str:
    """Describe a group from its measured counts.

    Every group gets a descriptive status. Groups with an unmeasured count
    (None) are 'Indeterminate'; otherwise the stale variants or 'Active'.
    """
    if item_count is None or member_count is None:
        return "Indeterminate"
    if item_count == 0 and member_count == 0:
        return "Stale: No content or members"
    if member_count == 0:
        return "Stale: no members"
    if item_count == 0:
        return "Stale: no content"
    return "Active"


def _is_stale_status(status: str) -> bool:
    return status.startswith("Stale")


def _scan_all_old_groups_detailed(gis, client: PortalRestClient,
                                  platform: str, target: str, age_days: int,
                                  workers: int, logger) -> GroupScanResult:
    result = GroupScanResult()
    discovery = _discover_old_groups(gis, client, target, age_days, logger)
    result.groups_discovered = len(discovery.groups) + len(discovery.errors)
    result.old_candidates = len(discovery.groups)
    for message in discovery.errors:
        result.add_error(message, category="discovery")
        logger.warning("Discovery entry error: %s", redact_token(message))

    candidates = discovery.groups
    if not candidates:
        logger.info("No groups older than %d days were found.", age_days)
        return result

    # Resolve leavingDisallowed for every discovered group. This is now
    # recorded as data (the 'administrative' column) rather than used to
    # exclude any group from the output.
    leaving_status: Dict[str, Optional[bool]] = {}
    inline_groups: List[Dict[str, Any]] = []
    lookup_groups: List[Dict[str, Any]] = []
    for group in candidates:
        gid = str(group.get("id") or "")
        if "leavingDisallowed" in group:
            inline_groups.append(group)
        else:
            lookup_groups.append(group)

    for group in inline_groups:
        gid = str(group.get("id") or "")
        try:
            leaving_status[gid] = _group_leaving_disallowed(client, group)
        except Exception as exc:
            leaving_status[gid] = None
            result.add_error(
                f"{gid}: could not interpret leavingDisallowed: {exc}",
                category="leaving", tls_error=_error_is_tls(exc),
            )
            logger.warning(
                "Group %s leavingDisallowed error: %s",
                gid, redact_token(str(exc)),
            )

    if lookup_groups:
        logger.info(
            "Fetching group definitions for %d group(s) missing "
            "'leavingDisallowed' (%d worker(s)) ...",
            len(lookup_groups), workers,
        )

    def leaving_worker(group: Dict[str, Any]) -> bool:
        return _group_leaving_disallowed(client, group)

    completed = 0
    for group, value, exc in _bounded_results(
            lookup_groups, leaving_worker, workers):
        gid = str(group.get("id") or "")
        completed += 1
        if exc is not None:
            leaving_status[gid] = None
            result.add_error(
                f"{gid}: group-definition lookup failed: {exc}",
                category="leaving", tls_error=_error_is_tls(exc),
            )
            logger.warning(
                "Group %s definition error: %s", gid,
                redact_token(str(exc)),
            )
        else:
            leaving_status[gid] = bool(value)
        if completed % 250 == 0 or completed == len(lookup_groups):
            logger.info(
                "Group-definition progress: %d / %d.", completed,
                len(lookup_groups),
            )

    # Every discovered group is measured and reported -- administrative groups
    # included.
    eligible = candidates
    result.administrative_included = sum(
        1 for gid in (str(g.get("id") or "") for g in eligible)
        if leaving_status.get(gid) is True
    )
    if result.administrative_included:
        logger.info(
            "Including %d administrative group(s) with "
            "'leavingDisallowed': true in the report.",
            result.administrative_included,
        )

    result.groups_measured = len(eligible)
    logger.info(
        "%d group(s) to measure; measuring item counts and non-owner member "
        "counts across %d worker(s) ...", len(eligible), workers,
    )

    item_counts: Dict[str, Optional[int]] = {
        str(group.get("id")): None for group in eligible
    }

    def item_worker(group: Dict[str, Any]) -> int:
        return group_item_count(gis, str(group.get("id") or ""))

    completed = 0
    for group, value, exc in _bounded_results(eligible, item_worker, workers):
        gid = str(group.get("id") or "")
        completed += 1
        if exc is not None:
            result.add_error(
                f"{gid}: item-count lookup failed: {exc}", category="item",
                tls_error=_error_is_tls(exc),
            )
            logger.warning(
                "PLATFORM=%s | Item-count error for %s: %s",
                platform, gid, redact_token(str(exc)),
            )
        else:
            try:
                count = int(value) if value is not None else None
            except (TypeError, ValueError) as exc2:
                result.add_error(
                    f"{gid}: item count was invalid: {value!r}",
                    category="item",
                )
                logger.warning(
                    "PLATFORM=%s | Item-count error for %s: %s",
                    platform, gid, redact_token(str(exc2)),
                )
            else:
                if count is None or count < 0:
                    result.add_error(
                        f"{gid}: item count was invalid: {value!r}",
                        category="item",
                    )
                else:
                    item_counts[gid] = count
        if completed % 250 == 0 or completed == len(eligible):
            logger.info("Item-count progress: %d / %d.", completed, len(eligible))

    member_counts: Dict[str, Optional[int]] = {
        str(group.get("id")): None for group in eligible
    }

    def member_worker(group: Dict[str, Any]) -> int:
        return _group_member_count(client, group)

    completed = 0
    for group, value, exc in _bounded_results(eligible, member_worker, workers):
        gid = str(group.get("id") or "")
        completed += 1
        if exc is not None:
            result.add_error(
                f"{gid}: member-count lookup failed: {exc}",
                category="member", tls_error=_error_is_tls(exc),
            )
            logger.warning(
                "PLATFORM=%s | Member-count error for %s: %s",
                platform, gid, redact_token(str(exc)),
            )
        else:
            try:
                count = int(value) if value is not None else None
            except (TypeError, ValueError) as exc2:
                result.add_error(
                    f"{gid}: member count was invalid: {value!r}",
                    category="member",
                )
                logger.warning(
                    "PLATFORM=%s | Member-count error for %s: %s",
                    platform, gid, redact_token(str(exc2)),
                )
            else:
                if count is None or count < 0:
                    result.add_error(
                        f"{gid}: member count was invalid: {value!r}",
                        category="member",
                    )
                else:
                    member_counts[gid] = count
        if completed % 250 == 0 or completed == len(eligible):
            logger.info(
                "Member-count progress: %d / %d.", completed, len(eligible)
            )

    # Emit exactly one row per discovered group.
    for group in eligible:
        gid = str(group.get("id") or "")
        item_count = item_counts.get(gid)
        member_count = member_counts.get(gid)
        if item_count is not None and member_count is not None:
            result.groups_fully_measured += 1
        else:
            result.groups_indeterminate += 1

        status = _status_from_counts(item_count, member_count)
        if status == "Active":
            result.active_count += 1
        elif _is_stale_status(status):
            result.stale_count += 1

        administrative = leaving_status.get(gid)
        result.rows.append({
            "platform": platform,
            "item_id": gid,
            "title": group.get("title", ""),
            "owner": group.get("owner", ""),
            "date_created": epoch_to_date(group.get("created", 0)),
            "date_modified": epoch_to_date(group.get("modified")),
            "memberCount": "" if member_count is None else member_count,
            "itemCount": "" if item_count is None else item_count,
            "status": status,
            "administrative": (
                "" if administrative is None else bool(administrative)
            ),
            "leavingDisallowed": (
                "" if administrative is None else bool(administrative)
            ),
            "isInvitationOnly": group.get("isInvitationOnly", ""),
            "access": group.get("access", ""),
        })

    logger.info(
        "Scan complete: %d group(s) reported (%d stale, %d active, %d "
        "indeterminate; %d administrative included); %d fully measured "
        "(%d discovery-entry, %d administrative-flag, %d item-count, %d "
        "member-count error(s); %d TLS error(s)).",
        len(result.rows), result.stale_count, result.active_count,
        result.groups_indeterminate, result.administrative_included,
        result.groups_fully_measured, result.discovery_entry_errors,
        result.leaving_lookup_errors, result.item_count_errors,
        result.member_count_errors, result.tls_errors,
    )
    return result


def scan_empty_old_groups(gis, session, token_provider, platform, age_days,
                          workers, logger):
    """Backward-compatible list-returning wrapper used by older callers.

    Returns all discovered group rows (the report is no longer limited to
    stale groups).
    """
    target = "agol" if str(platform).upper() == "AGOL" else "portal"
    client = PortalRestClient(
        portal_base_url(gis), FixedSessionPool(session), token_provider, gis,
        logger,
    )
    return _scan_all_old_groups_detailed(
        gis, client, platform, target, age_days, workers, logger
    ).rows


class _NullLogger:
    def debug(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None

    def error(self, *_args, **_kwargs):
        return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inventory ALL groups older than AGE_DAYS in AGOL or "
                    "Portal, write them to a timestamped CSV, then email a "
                    "report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python GROUPS_SCAN.py --config org1.config.toml --target portal\n"
            "  python GROUPS_SCAN.py --config org2.config.toml --target agol "
            "--output-dir \"D:\\reports\"\n"
            "  python GROUPS_SCAN.py --config org1.config.toml --target portal "
            "--tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    parser.add_argument(
        "--workers",
        type=_bounded_int("--workers", MIN_WORKERS, MAX_WORKERS),
        default=DEFAULT_WORKERS,
        help=(f"Number of concurrent worker threads (default: "
              f"{DEFAULT_WORKERS}; allowed {MIN_WORKERS}-{MAX_WORKERS})."),
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="TLS verification policy used consistently by the startup "
             "handshake, every direct REST request, and the ArcGIS SDK "
             "connection. 'standard' (default): certifi's public CA list. "
             "'os-trust-store': the operating system certificate store "
             "(requires truststore). 'custom-ca-bundle': the PEM file named "
             "by --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. Required only when --tls-mode is "
             "custom-ca-bundle; used by both direct REST and the SDK.")
    return parser.parse_args()


def _safe_error_text(value: Any) -> str:
    return str(redact_token(str(value)))


def _send_abort(args, subject: str, log_file_path: str, reason: Any,
                start_wallclock: str, start_perf: float, logger) -> None:
    safe = _safe_error_text(reason)
    logger.error("Run ABORTED: %s", safe)
    body = (
        f"Groups Inventory Scan\n"
        f"Organization: {args.org_name}\n"
        f"Target: {args.target}\n"
        f"Result: ABORTED\n\n"
        f"Reason:\n{safe}\n\n"
        f"{timing_block(start_wallclock, start_perf)}\n"
        f"See the attached log for full detail.\n"
    )
    send_email(
        subject, body, [log_file_path], args.smtp_server, args.email_to,
        args.email_from, logger,
    )


def _run(args, run_stamp: str, log_file_path: str, csv_path: str,
         logger, start_wallclock: str, start_perf: float) -> int:
    platform = get_platform(args.portal_url)
    _validate_target_platform(args.target, platform, args.portal_url)
    max_email_mb = get_max_email_mb(args.target, args.config)

    logger.info("=" * 70)
    logger.info("Groups Inventory Scan - run started")
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("Output CSV: %s", csv_path)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Age threshold (days): %d", AGE_DAYS)
    logger.info("Worker threads: %d", args.workers)
    logger.info("=" * 70)

    mode = _TLS_MODE_CHOICES[args.tls_mode]
    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        startup_session = build_secure_session(
            mode,
            ca_bundle_path=ca_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2),
            retry_total=3,
            logger=logger,
        )
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, startup_session, logger=logger
        )
    except Exception as exc:
        raise ScanAbort(
            f"Could not establish TLS verification strategy "
            f"'{args.tls_mode}': {_safe_error_text(exc)}"
        ) from exc
    if not handshake_ok:
        raise ScanAbort(
            f"TLS handshake verification to {args.portal_url} failed: "
            f"{_safe_error_text(handshake_detail.get('error'))}"
        )

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
        try:
            startup_session.close()
        except Exception:
            pass
        raise ScanAbort(
            f"Could not connect to Portal/AGOL: {_safe_error_text(exc)}"
        ) from exc

    connected_platform = get_platform(portal_base_url(gis))
    _validate_target_platform(args.target, connected_platform,
                              portal_base_url(gis))
    platform = connected_platform

    token_provider = provider_for_gis(gis, platform)
    logger.info(
        "Token acquired via %s (token present: %s).",
        type(token_provider).__name__, token_provider.is_present(),
    )
    tune_gis_session(gis, args.workers, timeout=HTTP_TIMEOUT_SEC,
                     retry_total=3)

    session_pool = WorkerSessionPool(mode, ca_path, startup_session)
    try:
        client = PortalRestClient(
            portal_base_url(gis), session_pool, token_provider, gis, logger
        )
        scan = _scan_all_old_groups_detailed(
            gis, client, platform, args.target, AGE_DAYS, args.workers, logger
        )
    finally:
        session_pool.close()

    csv_written = False
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            for row in scan.rows:
                row["platform"] = platform
                writer.writerow(row)
        logger.info("Wrote %d row(s) to %s", len(scan.rows), csv_path)
        csv_written = True
    except Exception as exc:
        logger.error("Failed to write CSV: %s", exc)

    incomplete = scan.incomplete or not csv_written
    result_label = "INCOMPLETE" if incomplete else "COMPLETE"

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result: %s", result_label)
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Age threshold: %d day(s)", AGE_DAYS)
    logger.info("Groups discovered in age range: %d", scan.groups_discovered)
    logger.info("Valid old candidates: %d", scan.old_candidates)
    logger.info("Administrative groups included: %d",
                scan.administrative_included)
    logger.info("Groups measured: %d", scan.groups_measured)
    logger.info("Groups fully measured: %d", scan.groups_fully_measured)
    logger.info("Groups indeterminate: %d", scan.groups_indeterminate)
    logger.info("Total groups reported: %d", len(scan.rows))
    logger.info("Stale groups: %d", scan.stale_count)
    logger.info("Active groups: %d", scan.active_count)
    logger.info("Discovery-entry errors: %d", scan.discovery_entry_errors)
    logger.info("Administrative-flag errors: %d", scan.leaving_lookup_errors)
    logger.info("Item-count errors: %d", scan.item_count_errors)
    logger.info("Member-count errors: %d", scan.member_count_errors)
    logger.info("TLS/CERT errors: %d", scan.tls_errors)
    logger.info("Output CSV: %s", csv_path if csv_written else "(NOT WRITTEN)")
    logger.info("=" * 70)

    subject_prefix = EMAIL_SUBJECT_PREFIX
    if incomplete:
        subject_prefix += " (INCOMPLETE)"
    subject = subject_line(
        subject_prefix, run_stamp, platform, org_name=args.org_name
    )

    body_lines = [
        "Groups Inventory Scan",
        f"Organization: {args.org_name}",
        f"Platform: {platform}",
        f"Run timestamp: {run_stamp}",
        f"Result: {result_label}",
        f"TLS mode: {args.tls_mode}",
        f"Username: {args.username}",
        "",
        f"Age threshold: {AGE_DAYS} day(s)",
        f"Groups discovered in age range: {scan.groups_discovered}",
        f"Valid old candidates: {scan.old_candidates}",
        f"Administrative groups included: {scan.administrative_included}",
        f"Groups measured: {scan.groups_measured}",
        f"Groups fully measured: {scan.groups_fully_measured}",
        f"Groups indeterminate: {scan.groups_indeterminate}",
        f"Total groups reported: {len(scan.rows)}",
        f"Stale groups: {scan.stale_count}",
        f"Active groups: {scan.active_count}",
        f"Discovery-entry errors: {scan.discovery_entry_errors}",
        f"Administrative-flag errors: {scan.leaving_lookup_errors}",
        f"Item-count errors: {scan.item_count_errors}",
        f"Member-count errors: {scan.member_count_errors}",
        f"TLS/CERT errors: {scan.tls_errors}",
        f"Output CSV: {csv_path if csv_written else '(NOT WRITTEN)'}",
    ]
    if incomplete:
        body_lines.extend([
            "",
            "WARNING: This run is incomplete. Reported rows are retained, but "
            "some groups may have blank/indeterminate counts, and absence of "
            "a definitive count is not proof of a group's content or "
            "membership state.",
        ])
        if scan.errors:
            body_lines.extend(["", "First error(s):"])
            body_lines.extend(
                f"  - {message}" for message in scan.errors[:MAX_ERROR_SAMPLES]
            )
            if len(scan.errors) > MAX_ERROR_SAMPLES:
                body_lines.append(
                    f"  - ...and {len(scan.errors) - MAX_ERROR_SAMPLES} more; "
                    "see the log."
                )
    body_lines.extend([
        "",
        timing_block(start_wallclock, start_perf),
        "See the attached log file and CSV for full detail.",
    ])
    body = "\n".join(body_lines)

    attachments, note = gather_attachments(
        log_file_path,
        [csv_path] if csv_written else [],
        args.output_dir,
        logger,
        cap_mb=max_email_mb,
    )
    body += note
    send_email(
        subject, body, attachments, args.smtp_server, args.email_to,
        args.email_from, logger,
    )

    if incomplete:
        logger.error("Run finished INCOMPLETE.")
        return 1
    logger.info("Success")
    return 0


def main():
    args = parse_args()

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.tls_mode != "custom-ca-bundle" and args.ca_bundle_path:
        print(
            "--ca-bundle-path may be supplied only with --tls-mode "
            "custom-ca-bundle.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    start_wallclock, start_perf = start_timer()
    finalize_config(args, required=REQUIRED_SETTINGS)
    ensure_output_dir(args.output_dir)

    preliminary_platform = get_platform(args.portal_url)
    timestamp = build_timestamp()
    run_stamp, log_file_path, csv_path = _reserve_run_paths(
        args.output_dir, args.org_name_safe, preliminary_platform, timestamp
    )
    logger = setup_logging(log_file_path, LOGGER_NAME)

    abort_subject = subject_line(
        EMAIL_SUBJECT_PREFIX + " (ABORTED)", run_stamp,
        preliminary_platform, org_name=args.org_name,
    )
    try:
        exit_code = _run(
            args, run_stamp, log_file_path, csv_path, logger,
            start_wallclock, start_perf,
        )
    except ScanAbort as exc:
        _send_abort(
            args, abort_subject, log_file_path, exc,
            start_wallclock, start_perf, logger,
        )
        exit_code = 1
    except Exception as exc:
        logger.exception("Unexpected fatal error during group scan.")
        _send_abort(
            args, abort_subject, log_file_path,
            f"Unexpected fatal error: {_safe_error_text(exc)}",
            start_wallclock, start_perf, logger,
        )
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()