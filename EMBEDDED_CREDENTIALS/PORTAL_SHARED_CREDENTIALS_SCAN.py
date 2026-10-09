#!/usr/bin/env python3
"""Scan an ArcGIS organization for items that expose shared credentials.

The scan keeps the original detection signals and report columns, while making
organization scoping, TLS configuration, token refresh, URL handling, search
completeness, and process status explicit and fail-closed.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock
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
    filter_excluded,
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
    write_dict_csv,
    ensure_output_dir,
)

SCRIPT_TAG = "SHARED_CREDENTIALS_SCAN"
REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

CREDENTIALED_ITEM_TYPES = (
    "Feature Service", "Map Service", "Image Service", "WMS", "WMTS",
    "Vector Tile Service", "Scene Service", "Geocoding Service",
    "Geometry Service", "Geodata Service", "Network Analysis Service",
    "Geoprocessing Service", "Relational Database Connection",
)
APIKEY_ITEM_TYPES = ("API Key",)
ALL_ITEM_TYPES = CREDENTIALED_ITEM_TYPES + APIKEY_ITEM_TYPES

# These are host names, not arbitrary URL substrings.
PROXY_URL_HOSTS = frozenset({"utility.arcgis.com"})
AGOL_TRUSTED_SUFFIX = ".arcgis.com"

STORED_CRED_VALIDATION_ERROR_CODE = "CONT_0044"
STORED_CRED_VALIDATION_MESSAGE = (
    "unable to validate stored credentials used for this service"
)
REQUEST_TIMEOUT = 60
MAX_WORKERS = 64
MAX_REDIRECTS = 3
SEARCH_PAGE_SIZE = 100

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}

_TOKEN_REFRESH_LOCK = Lock()
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:498|499)\b|invalid\s+token|token\s+(?:is\s+)?expired|"
    r"token\s+required|authentication\s+token\s+required|\bHTTP\s+401\b)"
)
_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class ScanAbort(RuntimeError):
    """A controlled, user-reportable failure that invalidates the run."""


@dataclass(frozen=True)
class CheckOutcome:
    has_credentials: bool
    detail: dict | None = None
    error: str | None = None


def _bounded_int(label, minimum, maximum):
    def _parse(value):
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
        description="Scan a Portal/AGOL org for service items with embedded "
                    "(shared) credentials to a remote hosted service."
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    add_exclude_group_arg(parser)
    add_test_flag(
        parser,
        help_text="Disable TEST mode (scan only the first --test-count "
                  "candidate items by default) and scan the full org.",
    )
    parser.add_argument(
        "--test-count",
        type=_bounded_int("--test-count", 1, DEFAULT_CAP),
        default=25,
        help=f"Number of candidate items to scan in TEST mode "
             f"(default 25; maximum {DEFAULT_CAP}).",
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("--workers", 1, MAX_WORKERS),
        default=12,
        help=f"Connection-pool size / concurrency for credential checks "
             f"(default: 12; maximum: {MAX_WORKERS}).",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)"
    )
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's direct REST session and the ArcGIS SDK "
             "connection verify TLS certificates. 'standard' uses the public "
             "CA set; 'os-trust-store' uses the operating-system trust store; "
             "'custom-ca-bundle' uses --ca-bundle-path.",
    )
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle. Required only for "
             "--tls-mode custom-ca-bundle.",
    )
    return parser.parse_args()


def _type_clause(types):
    return "(" + " OR ".join(f'type:"{item_type}"' for item_type in types) + ")"


def _portal_base_url(gis):
    public_url = (getattr(gis, "url", None) or "").rstrip("/")
    if public_url:
        return public_url
    portal = getattr(gis, "_portal", None)
    fallback = (getattr(portal, "url", None) or "").rstrip("/")
    if not fallback:
        raise ScanAbort("The authenticated GIS object did not expose a Portal base URL.")
    return fallback


def _reserve_log_path(output_dir, prefix, timestamp):
    """Atomically reserve a unique run name, retaining the normal name unless needed."""
    for sequence in range(1000):
        run_stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        path = os.path.join(output_dir, f"{prefix}_{run_stamp}.log")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_stamp, path
    raise ScanAbort("Could not reserve a unique log filename after 1,000 attempts.")


def _format_rest_error(data):
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


def _is_auth_failure(data, error_text):
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = str(data["error"].get("code", "")).strip()
        if code in {"498", "499"}:
            return True
    return bool(_AUTH_ERROR_RE.search(str(error_text or "")))


def _refresh_provider_once(token_provider, token_used):
    """Refresh at most once for a stale token shared by concurrent workers."""
    with _TOKEN_REFRESH_LOCK:
        current = token_provider.get()
        if current == token_used:
            token_provider.refresh()
        return token_provider.get()


def _host_is_trusted(host, trusted_hosts, allow_agol_suffix):
    normalized = (host or "").strip().lower().rstrip(".")
    if not normalized:
        return False
    if normalized in trusted_hosts:
        return True
    return allow_agol_suffix and normalized.endswith(AGOL_TRUSTED_SUFFIX)


def _validated_request_url(url, trusted_hosts, allow_agol_suffix):
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception as exc:
        return None, f"could not parse URL: {redact_token(str(exc))}"

    if parsed.scheme.lower() != "https":
        return None, "URL must use HTTPS"
    if not parsed.hostname:
        return None, "URL has no hostname"
    if parsed.username is not None or parsed.password is not None:
        return None, "URL contains user-info credentials"
    if not _host_is_trusted(parsed.hostname, trusted_hosts, allow_agol_suffix):
        return None, (
            f"URL host {parsed.hostname!r} is outside the trusted "
            "Portal/federation boundary"
        )
    return parsed.geturl(), None


def _request_json_once(session, url, token, params, trusted_hosts,
                       allow_agol_suffix, timeout=REQUEST_TIMEOUT):
    current_url = url
    merged = {"f": "json", "token": token}
    if params:
        merged.update(params)

    for redirect_count in range(MAX_REDIRECTS + 1):
        checked_url, validation_error = _validated_request_url(
            current_url, trusted_hosts, allow_agol_suffix
        )
        if validation_error:
            return None, validation_error

        try:
            response = session.get(
                checked_url,
                params=merged,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.exceptions.Timeout:
            return None, f"timeout after {timeout}s"
        except requests.exceptions.SSLError as exc:
            return None, redact_token(
                f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}"
            )
        except requests.exceptions.RequestException as exc:
            return None, redact_token(f"request failed: {exc}")

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            if not location:
                return None, f"HTTP {response.status_code} redirect had no Location header"
            if redirect_count >= MAX_REDIRECTS:
                return None, f"too many redirects (more than {MAX_REDIRECTS})"
            current_url = urljoin(checked_url, location)
            # Rebuild params on each trusted hop. Never permit requests to carry
            # the Portal token to a host outside the approved boundary.
            continue

        try:
            data = response.json()
        except ValueError:
            if response.status_code >= 400:
                return None, f"HTTP {response.status_code} {response.reason}"
            return None, f"non-JSON response (HTTP {response.status_code})"

        rest_error = _format_rest_error(data)
        if rest_error:
            return data, rest_error
        if response.status_code >= 400:
            return data, f"HTTP {response.status_code} {response.reason}"
        return data, None

    return None, "redirect processing failed"


def _request_json(session, url, token_provider, params, trusted_hosts,
                  allow_agol_suffix, timeout=REQUEST_TIMEOUT):
    token = token_provider.get()
    data, error = _request_json_once(
        session, url, token, params, trusted_hosts, allow_agol_suffix, timeout
    )
    if not _is_auth_failure(data, error):
        return data, error

    refreshed = _refresh_provider_once(token_provider, token)
    return _request_json_once(
        session, url, refreshed, params, trusted_hosts, allow_agol_suffix,
        timeout,
    )


def _strict_search_page(base_url, query, cap, session, token_provider,
                        trusted_hosts, allow_agol_suffix):
    search_url = f"{base_url.rstrip('/')}/sharing/rest/search"
    results = []
    start = 1

    while len(results) < cap:
        num_to_get = min(SEARCH_PAGE_SIZE, cap - len(results))
        if start + num_to_get - 1 > cap:
            break
        data, error = _request_json(
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
            trusted_hosts,
            allow_agol_suffix,
        )
        if error:
            raise ScanAbort(f"Search page starting at {start} failed: {error}")
        if not isinstance(data, dict):
            raise ScanAbort("Search returned a non-object JSON response.")

        items = data.get("results")
        if items is None:
            items = data.get("items")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ScanAbort("Search response did not contain a list of results.")
        if any(not isinstance(item, dict) for item in items):
            raise ScanAbort("Search returned a malformed non-object item result.")

        if not items:
            try:
                declared_total = int(data.get("total", len(results)))
            except (TypeError, ValueError):
                declared_total = len(results)
            if declared_total > len(results):
                raise ScanAbort(
                    "Search returned an empty page before its declared total was reached."
                )
            break

        results.extend(items)
        previous_start = start
        next_start = data.get("nextStart")
        if isinstance(next_start, int) and next_start > 0:
            start = next_start
        else:
            start += len(items)
        if start <= previous_start:
            raise ScanAbort("Search pagination made no forward progress.")

        try:
            total = int(data.get("total", len(results)))
        except (TypeError, ValueError):
            total = len(results)
        if len(results) >= total:
            break

    return results[:cap]


def _strict_bucketed_search(search_fn, extra_clause, logger):
    """The shared bucketing algorithm, but any failed/unsplittable bucket aborts."""
    span_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    span_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
    items_by_id = {}
    query_count = 0

    def search_range(lo_ms, hi_ms):
        nonlocal query_count
        query_count += 1
        query = f"{extra_clause} AND created:[{lo_ms} TO {hi_ms}]"
        results = search_fn(query, DEFAULT_CAP)
        if len(results) >= DEFAULT_CAP and hi_ms > lo_ms:
            midpoint = lo_ms + (hi_ms - lo_ms) // 2
            if midpoint <= lo_ms:
                midpoint = lo_ms + 1
            search_range(lo_ms, midpoint)
            search_range(midpoint + 1, hi_ms)
            return
        if len(results) >= DEFAULT_CAP:
            raise ScanAbort(
                f"A one-millisecond search bucket [{lo_ms} TO {hi_ms}] reached "
                f"the {DEFAULT_CAP}-item cap; completeness cannot be guaranteed."
            )
        for result in results:
            item_id = result.get("id")
            if not item_id:
                raise ScanAbort("Search returned an item without an id.")
            items_by_id[item_id] = result
        if query_count % 20 == 0:
            logger.info("  %d bucket queries run, %d unique item(s) so far.",
                        query_count, len(items_by_id))

    logger.info("Bucketing created dates from %s to %s (strict mode) ...",
                span_start.date(), span_end.date())
    cursor = span_start
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

    logger.info("Bucketed search complete: %d queries, %d unique item(s).",
                query_count, len(items_by_id))
    return list(items_by_id.values())


def _scoped_item_clause(org_id):
    if not org_id:
        raise ScanAbort(
            "Could not resolve the authenticated organization id; refusing to "
            "run an unscoped credential inventory."
        )
    return f"{_type_clause(ALL_ITEM_TYPES)} AND orgid:{org_id}"


def _item_search_full(gis, session, token_provider, org_id, logger,
                      trusted_hosts, allow_agol_suffix):
    base_url = _portal_base_url(gis)
    extra_clause = _scoped_item_clause(org_id)
    logger.info("Scoping full item search to org id: %s", org_id)

    def search_fn(query, cap):
        return _strict_search_page(
            base_url, query, cap, session, token_provider,
            trusted_hosts, allow_agol_suffix,
        )

    return _strict_bucketed_search(search_fn, extra_clause, logger)


def _item_search_test(gis, session, token_provider, org_id, cap,
                      trusted_hosts, allow_agol_suffix):
    base_url = _portal_base_url(gis)
    query = _scoped_item_clause(org_id)
    return _strict_search_page(
        base_url, query, cap, session, token_provider,
        trusted_hosts, allow_agol_suffix,
    )


def _validate_candidate_org(candidates, org_id):
    for item in candidates:
        if not isinstance(item, dict):
            raise ScanAbort("Candidate search returned a non-object item.")
        reported_org = item.get("orgId")
        if reported_org not in (None, "", org_id):
            raise ScanAbort(
                f"Search returned out-of-organization item {item.get('id')!r} "
                f"(orgId={reported_org!r}, expected {org_id!r})."
            )


def _build_trusted_service_hosts(gis, base_url, platform, logger):
    base_host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    if not base_host:
        raise ScanAbort("Could not determine the Portal hostname.")
    trusted = {base_host, *PROXY_URL_HOSTS}

    if platform == "PORTAL":
        try:
            federation = gis.admin.federation.servers
            servers = federation.get("servers", []) if isinstance(federation, dict) else []
            for server in servers:
                if not isinstance(server, dict):
                    continue
                for key in ("url", "adminUrl"):
                    host = urlsplit(str(server.get(key) or "")).hostname
                    if host:
                        trusted.add(host.lower().rstrip("."))
        except Exception as exc:
            logger.warning(
                "Could not enumerate federated server hosts; direct service-URL "
                "checks will be limited to already trusted hosts: %s",
                redact_token(str(exc)),
            )

    logger.info("Trusted token destination host(s): %s",
                ", ".join(sorted(trusted)))
    return frozenset(trusted), platform == "AGOL"


def _resolve_excluded_item_ids_strict(gis, raw_group_ids, logger):
    """Honor requested exclusions completely, or abort instead of broadening."""
    group_ids = [
        value.strip() for value in str(raw_group_ids or "").split(",")
        if value.strip()
    ]
    excluded = set()
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
            getattr(member, "id", None) for member in (members or [])
        }
        member_ids.discard(None)
        excluded.update(member_ids)
        logger.info(
            "Exclusion group %s (%s) contributes %d item id(s).",
            group_id, getattr(group, "title", "?"), len(member_ids),
        )
    return excluded


def _no_stored_credentials_response(error_text):
    lowered = str(error_text or "").lower()
    markers = (
        "http 404",
        "rest error 404",
        "does not exist",
        "info file for item not found",
        "item does not have credentials",
    )
    return any(marker in lowered for marker in markers)


def _is_stored_credential_validation_error(data):
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    if not isinstance(err, dict):
        return False
    if str(err.get("messageCode", "")).strip().upper() == \
            STORED_CRED_VALIDATION_ERROR_CODE:
        return True
    details = err.get("details") or []
    if any(STORED_CRED_VALIDATION_MESSAGE in str(value).lower()
           for value in details):
        return True
    return STORED_CRED_VALIDATION_MESSAGE in str(err.get("message", "")).lower()


def _check_credentials_info_file(session, base_url, token_provider, item_id,
                                 trusted_hosts, allow_agol_suffix):
    encoded_id = quote(item_id, safe="")
    url = f"{base_url}/sharing/rest/content/items/{encoded_id}/info/credentials"
    data, error = _request_json(
        session, url, token_provider, None, trusted_hosts, allow_agol_suffix
    )
    if error:
        if _no_stored_credentials_response(error):
            return False, None
        return False, error
    if not isinstance(data, dict):
        return False, "credentials info-file response was not a JSON object"
    username = data.get("username")
    return bool(username and str(username).strip()), None


def _check_service_url_for_credential_error(session, token_provider,
                                            service_url, trusted_hosts,
                                            allow_agol_suffix):
    if not service_url:
        return False, None
    checked_url, validation_error = _validated_request_url(
        service_url, trusted_hosts, allow_agol_suffix
    )
    if validation_error:
        return False, (
            "service URL was not requested because sending the organization "
            f"token there would be unsafe: {validation_error}"
        )

    data, error = _request_json(
        session, checked_url, token_provider, None,
        trusted_hosts, allow_agol_suffix,
    )
    if _is_stored_credential_validation_error(data):
        return True, None
    if error:
        return False, error
    if not isinstance(data, dict):
        return False, "service URL returned a non-object JSON response"
    return False, None


def _check_proxy_url_pattern(item):
    url = str(item.get("url") or "").strip()
    source_url = str(item.get("sourceUrl") or "").strip()
    if not url or not source_url or source_url == url:
        return False, None
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False, None
    if host not in PROXY_URL_HOSTS:
        return False, None
    return True, {"backend_source_url": source_url}


def _check_item_credentials(base_url, session, token_provider, item,
                            trusted_hosts, allow_agol_suffix):
    item_id = str(item.get("id") or "").strip()
    service_url = item.get("url")

    via_proxy, detail = _check_proxy_url_pattern(item)
    if via_proxy:
        return CheckOutcome(True, detail=detail)

    has_credentials, error = _check_credentials_info_file(
        session, base_url, token_provider, item_id,
        trusted_hosts, allow_agol_suffix,
    )
    if error:
        return CheckOutcome(
            False,
            error=redact_token(
                f"credentials info-file check failed for {item_id}: {error}"
            ),
        )
    if has_credentials:
        return CheckOutcome(True)

    via_url, error = _check_service_url_for_credential_error(
        session, token_provider, service_url,
        trusted_hosts, allow_agol_suffix,
    )
    if error:
        return CheckOutcome(
            False,
            error=redact_token(
                f"service-URL credential check failed for {item_id}: {error}"
            ),
        )
    return CheckOutcome(via_url)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _join_text_values(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(entry) for entry in value)
    return str(value)


def _check_apikey_item(base_url, session, token_provider, item,
                       trusted_hosts, allow_agol_suffix):
    item_id = str(item.get("id") or "").strip()
    owner = str(item.get("owner") or "").strip()
    if not owner:
        return CheckOutcome(False, error=f"API Key item {item_id} has no owner")

    url = (f"{base_url}/sharing/rest/content/users/{quote(owner, safe='')}/items/"
           f"{quote(item_id, safe='')}/registeredAppInfo")
    data, error = _request_json(
        session, url, token_provider, None, trusted_hosts, allow_agol_suffix
    )
    if error:
        if _no_stored_credentials_response(error):
            return CheckOutcome(False)
        return CheckOutcome(
            False,
            error=redact_token(
                f"registered-app info check failed for {item_id}: {error}"
            ),
        )
    if not isinstance(data, dict):
        return CheckOutcome(
            False,
            error=f"registered-app info for {item_id} was not a JSON object",
        )

    app_type = str(data.get("appType") or "").strip().lower()
    has_api_key = bool(str(data.get("apiKey") or "").strip())
    has_client_secret = bool(str(
        data.get("client_secret") or data.get("clientSecret") or ""
    ).strip())
    is_personal = _as_bool(data.get("isPersonalAPIToken"))

    is_live_key = (
        app_type == "apikey" or has_api_key or has_client_secret
    ) and not is_personal
    if not is_live_key:
        return CheckOutcome(False)

    detail = {
        "privileges": _join_text_values(data.get("privileges")),
        "http_referrers": _join_text_values(data.get("httpReferrers")),
        "redirect_uris": _join_text_values(data.get("redirect_uris")),
    }
    return CheckOutcome(True, detail=detail)


def _validate_candidate(item):
    if not isinstance(item, dict):
        return "candidate was not an object"
    item_id = str(item.get("id") or "").strip()
    if not _ITEM_ID_RE.fullmatch(item_id):
        return f"candidate has an invalid item id: {item_id!r}"
    item_type = str(item.get("type") or "").strip()
    if item_type not in ALL_ITEM_TYPES:
        return f"candidate {item_id} has unexpected type {item_type!r}"
    return None


def _bounded_future_results(executor, items, worker, max_in_flight):
    iterator = iter(items)
    pending = {}

    def submit_next():
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


def _scan_candidates(candidates, base_url, session, token_provider,
                     trusted_hosts, allow_agol_suffix, workers, logger,
                     platform):
    findings = []
    error_messages = []
    done_count = 0

    def worker(item):
        validation_error = _validate_candidate(item)
        if validation_error:
            return CheckOutcome(False, error=validation_error)
        item_type = item.get("type") or ""
        if item_type in APIKEY_ITEM_TYPES:
            return _check_apikey_item(
                base_url, session, token_provider, item,
                trusted_hosts, allow_agol_suffix,
            )
        return _check_item_credentials(
            base_url, session, token_provider, item,
            trusted_hosts, allow_agol_suffix,
        )

    max_in_flight = max(workers, workers * 2)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for item, future in _bounded_future_results(
                executor, candidates, worker, max_in_flight):
            done_count += 1
            try:
                outcome = future.result()
            except Exception as exc:  # last-resort worker containment
                outcome = CheckOutcome(
                    False,
                    error=redact_token(
                        f"unexpected worker failure for item "
                        f"{item.get('id') if isinstance(item, dict) else '?'}: {exc}"
                    ),
                )

            if outcome.error:
                error_messages.append(outcome.error)
                logger.warning("%s", outcome.error)
            elif outcome.has_credentials:
                item_id = item.get("id")
                owner = item.get("owner") or ""
                title = item.get("title") or ""
                item_type = item.get("type") or ""
                service_url = item.get("url") or ""
                item_home = f"{base_url}/home/item.html?id={quote(str(item_id), safe='')}"
                logger.warning(
                    "EMBEDDED CREDENTIALS FOUND: id=%s title=%r owner=%s type=%s",
                    item_id, title, owner, item_type,
                )
                finding = {
                    "item_id": item_id,
                    "title": title,
                    "owner": owner,
                    "type": item_type,
                    "service_url": service_url,
                    "item_home_url": item_home,
                    "platform": platform,
                    "privileges": "",
                    "http_referrers": "",
                    "redirect_uris": "",
                    "backend_source_url": "",
                }
                if outcome.detail:
                    finding.update(outcome.detail)
                findings.append(finding)

            if done_count % 500 == 0 or done_count == len(candidates):
                logger.info(
                    "Progress: %d/%d checked, %d finding(s), %d error(s).",
                    done_count, len(candidates), len(findings),
                    len(error_messages),
                )

    return findings, error_messages


def _validate_target_platform(target, platform):
    expected = "AGOL" if target == "agol" else "PORTAL"
    if platform == "UNKNOWN":
        raise ScanAbort("The configured Portal URL could not be classified.")
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} conflicts with the configured URL, which was "
            f"classified as {platform}."
        )


def _send_abort(subject, reason, log_path, args, logger,
                start_wallclock, start_perf):
    safe_reason = redact_token(str(reason))
    logger.error("Run ABORTED: %s", safe_reason)
    body = f"Run ABORTED. {safe_reason}\n\n" + timing_block(
        start_wallclock, start_perf
    )
    send_email(
        subject, body, [log_path], args.smtp_server, args.email_to,
        args.email_from, logger,
    )


def _run(args, run_stamp, log_path, logger, start_wallclock, start_perf):
    platform = get_platform(args.portal_url)
    _validate_target_platform(args.target, platform)
    logger.info("Platform: %s", platform)

    mode = _TLS_MODE_CHOICES[args.tls_mode]
    try:
        direct_session = build_secure_session(
            mode,
            ca_bundle_path=args.ca_bundle_path,
            pool_connections=max(32, args.workers),
            pool_maxsize=max(32, args.workers),
            retry_total=3,
            logger=logger,
        )
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, direct_session, logger=logger
        )
    except Exception as exc:
        raise ScanAbort(
            f"Could not establish or verify TLS strategy {args.tls_mode!r}: "
            f"{redact_token(str(exc))}"
        ) from exc
    if not handshake_ok:
        raise ScanAbort(
            f"TLS handshake verification to {args.portal_url} failed: "
            f"{redact_token(str(handshake_detail.get('error')))}"
        )

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
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

    # Tune the SDK's own session for SDK calls, but retain the separately built
    # direct REST session above so --tls-mode is not accidentally discarded.
    tune_gis_session(gis, workers=args.workers, timeout=REQUEST_TIMEOUT)
    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
                type(token_provider).__name__, token_provider.is_present())

    base_url = _portal_base_url(gis)
    trusted_hosts, allow_agol_suffix = _build_trusted_service_hosts(
        gis, base_url, platform, logger
    )

    org_id = get_org_id(gis, logger=logger)
    if not org_id:
        raise ScanAbort(
            "Could not resolve the authenticated organization id; the scan "
            "will not run without an organization boundary."
        )
    logger.info("Authenticated organization id: %s", org_id)

    excluded_ids = _resolve_excluded_item_ids_strict(
        gis, args.exclude_group_id, logger
    )

    if args.test:
        logger.info(
            "Searching for candidate service item(s) (mode=TEST, cap=%d, "
            "org-scoped) ...", args.test_count,
        )
        candidates = _item_search_test(
            gis, direct_session, token_provider, org_id, args.test_count,
            trusted_hosts, allow_agol_suffix,
        )
    else:
        logger.info(
            "Searching for candidate service item(s) (mode=FULL, strict "
            "bucketed, org-scoped) ..."
        )
        candidates = _item_search_full(
            gis, direct_session, token_provider, org_id, logger,
            trusted_hosts, allow_agol_suffix,
        )

    _validate_candidate_org(candidates, org_id)
    logger.info("Found %d candidate service item(s) to check.", len(candidates))

    candidates = filter_excluded(
        candidates,
        excluded_ids,
        id_getter=lambda item: item.get("id"),
        logger=logger,
        label="candidate service item",
    )

    findings, error_messages = _scan_candidates(
        candidates, base_url, direct_session, token_provider,
        trusted_hosts, allow_agol_suffix, args.workers, logger, platform,
    )
    error_count = len(error_messages)
    successful_checks = len(candidates) - error_count
    incomplete = error_count > 0

    logger.info(
        "Scan complete: %d candidate(s), %d successful check(s), %d with "
        "embedded credentials, %d check error(s).",
        len(candidates), successful_checks, len(findings), error_count,
    )

    csv_path = None
    if findings:
        csv_name = (
            f"{args.org_name_safe}_{SCRIPT_TAG}_{args.target}_{run_stamp}.csv"
        )
        csv_path = os.path.join(args.output_dir, csv_name)
        csv_path = write_dict_csv(
            findings,
            csv_path,
            fieldnames=[
                "item_id", "title", "owner", "type", "service_url",
                "item_home_url", "platform", "privileges",
                "http_referrers", "redirect_uris", "backend_source_url",
            ],
            logger=logger,
            label="finding",
            force_values={"platform": platform},
        )

    timing = timing_block(start_wallclock, start_perf)
    subject_prefix = (
        "Shared Credentials Scan (INCOMPLETE)" if incomplete
        else "Shared Credentials Scan"
    )
    subject = subject_line(
        subject_prefix, run_stamp, platform.upper(), args.org_name
    )

    status_line = (
        "completed with errors; results are incomplete"
        if incomplete else "completed successfully"
    )
    body_lines = [
        f"Shared Credentials Scan {status_line} for "
        f"{args.org_name} ({platform}).",
        "",
        f"Mode: {'TEST (test-count=%d)' % args.test_count if args.test else 'FULL'}",
        f"Organization id: {org_id}",
        f"Candidates after exclusions: {len(candidates)}",
        f"Successful checks: {successful_checks}",
        f"Items with creds: {len(findings)}",
        f"Check errors: {error_count}",
        "",
        timing,
    ]
    if incomplete:
        body_lines.extend([
            "WARNING: One or more candidate checks failed. Do not interpret a "
            "zero-finding result as proof that the organization has no shared "
            "credentials. See the attached log for the failed item checks.",
            "",
        ])
    if findings:
        body_lines.extend([
            "Items with embedded/shared credentials were found. See the "
            "attached report for the full list, including each item's title, "
            "owner, type, service URL, and a direct item link.",
            "",
            "Embedded credentials on a service item bypass the access controls "
            "of the remote service's actual owner, obscure who is really "
            "accessing that service, and make per-user access logging "
            "impossible. Recommended alternatives: scoped ArcGIS API keys, "
            "inter-organization collaboration, a more permissive view of the "
            "underlying data, or a dedicated non-personal account intended for "
            "broad/public access points.",
        ])
    elif not incomplete:
        body_lines.append(
            "No service items with embedded/shared credentials were found "
            "among the items checked."
        )
    else:
        body_lines.append(
            "No positive findings were produced among the successfully checked "
            "items; the failed checks prevent a complete negative conclusion."
        )

    body = "\n".join(body_lines)
    max_email_mb = get_max_email_mb(args.target, args.config)
    attachments, note = gather_attachments(
        log_path,
        [csv_path] if csv_path else [],
        args.output_dir,
        logger,
        cap_mb=max_email_mb,
        label="CSV",
    )
    if note:
        body += "\n" + note

    send_email(
        subject, body, attachments, args.smtp_server, args.email_to,
        args.email_from, logger,
    )
    logger.info("%s finished (%s).", SCRIPT_TAG,
                "INCOMPLETE" if incomplete else "SUCCESS")
    direct_session.close()
    if incomplete:
        logger.warning(
            "%s completed with %d candidate-check error(s); returning "
            "exit status 0 because the scan ran to completion. The report "
            "remains marked INCOMPLETE.",
            SCRIPT_TAG, error_count,
        )
    return 0


def main():
    args = parse_args()
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print(
            "--ca-bundle-path is required when --tls-mode is custom-ca-bundle.",
            file=sys.stderr,
        )
        return 2

    finalize_config(args, required=REQUIRED_SETTINGS)
    ensure_output_dir(args.output_dir)

    base_stamp = build_timestamp()
    log_prefix = f"{args.org_name_safe}_{SCRIPT_TAG}_{args.target}"
    try:
        run_stamp, log_path = _reserve_log_path(
            args.output_dir, log_prefix, base_stamp
        )
    except ScanAbort as exc:
        print(str(exc), file=sys.stderr)
        return 1

    logger = setup_logging(log_path, f"{SCRIPT_TAG}_{args.target}")
    start_wallclock, start_perf = start_timer()

    logger.info("=" * 70)
    logger.info("%s - run started", SCRIPT_TAG)
    logger.info("Org name: %s", args.org_name)
    logger.info("Target: %s", args.target)
    logger.info(
        "Mode: %s",
        "TEST (test-count=%d)" % args.test_count if args.test else "FULL",
    )
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Workers: %d", args.workers)
    logger.info("=" * 70)

    platform = get_platform(args.portal_url)
    abort_subject = subject_line(
        "Shared Credentials Scan (ABORTED)", run_stamp,
        platform.upper(), args.org_name,
    )

    try:
        return _run(
            args, run_stamp, log_path, logger, start_wallclock, start_perf
        )
    except ScanAbort as exc:
        _send_abort(
            abort_subject, exc, log_path, args, logger,
            start_wallclock, start_perf,
        )
        return 1
    except Exception as exc:  # fail closed on an unforeseen programming/runtime error
        safe = redact_token(str(exc))
        logger.exception("Unexpected fatal error: %s", safe)
        _send_abort(
            abort_subject,
            f"Unexpected fatal error: {safe}",
            log_path,
            args,
            logger,
            start_wallclock,
            start_perf,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
