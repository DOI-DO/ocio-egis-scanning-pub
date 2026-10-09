#!/usr/bin/env python3
r"""
NAMED_USER_LICENSE_INVENTORY.py

Inventories ArcGIS Pro named-user licenses for every user in a Portal or AGOL
organization: license tier, assigned extensions, offline/disconnected
checkout status, real Pro "last used" dates, and role/site-login history.

pro_last_used_session is resolved in priority order per user:
  1. PRIMARY   - the user's own listing /sessions endpoint (max 'modified'
                 timestamp across all sessions -- matches the My Organization
                 -> Licenses UI's "Last used" display).
  2. SECONDARY - the org-wide usage_reports.applications() report, filtered
                 to appId "arcgisprodesktop" (AGOL only), used only when the
                 sessions endpoint had nothing for that user.
  3. TERTIARY  - userEntitlements.lastLogin, used only when neither of the
                 above produced a value. Always present regardless of
                 recency, which covers users who haven't used Pro recently
                 enough to still have a session or usage-report record.

This script's own direct requests.Session (user-enumeration, userEntitlements,
and per-user sessions REST calls -- separate from the arcgis SDK's own
connection) uses an explicit, caller-chosen TLS verification strategy
(--tls-mode), with a one-time startup handshake check before any real API
traffic. The auth token is held centrally in a TokenProvider (UTILITIES.auth) and is
exposed as a raw string only at the request-construction boundary. Text that
could embed a token is redacted before it reaches a log line, the console,
an email, or the output CSV.

Example usage:
    python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol
    python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal --no-test
    python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test --assigned-only
    python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --max-users 500
    python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --tls-mode os-trust-store
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from queue import LifoQueue
from threading import Lock
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests


def _locate_project_root():
    """Locate this project's real UTILITIES directory without importing it."""
    here = os.path.dirname(os.path.abspath(__file__))
    probe = here
    for _ in range(6):
        utilities_dir = os.path.join(probe, "UTILITIES")
        init_path = os.path.join(utilities_dir, "__init__.py")
        if os.path.isdir(utilities_dir) and os.path.isfile(init_path):
            return os.path.realpath(probe), os.path.realpath(utilities_dir)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    raise ImportError(
        "Could not locate the project's UTILITIES directory in this script's "
        "directory or any of its first five parent directories."
    )


_PROJECT_ROOT, _EXPECTED_UTILITIES_DIR = _locate_project_root()
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Check import origin before package code executes. This detects a shadow package
# elsewhere on sys.path. Filesystem ACLs must still protect the expected project
# directory itself from modification by untrusted accounts.
_utilities_spec = importlib.util.find_spec("UTILITIES")
_utilities_origin = (
    os.path.normcase(os.path.realpath(_utilities_spec.origin))
    if _utilities_spec is not None and _utilities_spec.origin
    else ""
)
_expected_utilities_init = os.path.normcase(os.path.realpath(
    os.path.join(_EXPECTED_UTILITIES_DIR, "__init__.py")
))
if _utilities_origin != _expected_utilities_init:
    raise ImportError(
        "Refusing to import UTILITIES from an unexpected location: "
        f"{_utilities_origin or '(not found)'}; expected "
        f"{_expected_utilities_init}."
    )

from UTILITIES import (
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    build_timestamp,
    start_timer,
    timing_block,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    subject_line,
    add_target_arg,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_config_arg,
    finalize_config,
    get_max_email_mb,
    epoch_to_date,
    get_org_id,
    get_json,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)


SCRIPT_NAME = "NAMED_USER_LICENSE_INVENTORY"
SUBJECT_PREFIX = "Named User License Inventory"

CSV_FIELDS = [
    "PLATFORM",
    "username",
    "full_name",
    "user_level",
    "role",
    "has_pro_license",
    "pro_license",
    "pro_extensions",
    "all_entitlements",
    "pro_last_used_session",
    "pro_last_used_source",
    "last_login",
    "taken_offline",
    "disconnected_session",
    "disconnected_since",
    "disconnected_until",
]

APP_USAGE_WINDOWS = {
    "week": "week", "14days": "14days", "30days": "30days",
    "60days": "60days", "90days": "90days", "6months": "6months",
    "year": "year",
}

PRO_DESKTOP_APP_ID = "arcgisprodesktop"

PRO_LEVEL_ENTITLEMENTS = {
    "desktopBasicN": "ArcGIS Pro Basic",
    "desktopStdN": "ArcGIS Pro Standard",
    "desktopAdvN": "ArcGIS Pro Advanced",
}

PRO_EXTENSION_ENTITLEMENTS = {
    "3DAnalystN": "3D Analyst", "airportsN": "Aviation Airports",
    "aviationChartsN": "Aviation Charting", "bathymetryN": "Bathymetry",
    "businessAnalystN": "Business Analyst",
    "dataInteropN": "Data Interoperability", "dataReviewerN": "Data Reviewer",
    "defenseN": "Defense Mapping",
    "geostatAnalystN": "Geostatistical Analyst",
    "imageAnalystN": "Image Analyst", "indoorsN": "ArcGIS Indoors",
    "locateXtN": "LocateXT", "locateXTN": "LocateXT",
    "maritimeN": "Maritime", "networkAnalystN": "Network Analyst",
    "productionMapN": "Production Mapping", "publisherN": "Publisher",
    "spatialAnalystN": "Spatial Analyst",
    "topoMapN": "Topographic Production",
    "workflowMgrN": "Workflow Manager",
}

DEFAULT_WORKERS = 12

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}



# ---------------------------------------------------------------------------
# Hardened direct-REST, concurrency, and run-integrity controls. These remain
# local so no shared helper behavior changes underneath other production jobs.
# ---------------------------------------------------------------------------
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ERROR_SAMPLES = 50
MAX_REDIRECTS = 3
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)
_NO_ENTITLEMENT_RE = re.compile(
    r"(?i)(?:user\s+does\s+not\s+have\s+(?:an?\s+)?entitlement|"
    r"no\s+(?:user\s+)?entitlements?(?:\s+(?:exist|found))?|"
    r"userentitlements?\s+(?:was\s+)?not\s+found|not\s+entitled)"
)
_NO_SESSION_RE = re.compile(
    r"(?i)(?:no\s+(?:license\s+)?sessions?(?:\s+(?:exist|found))?|"
    r"sessions?\s+(?:was\s+)?not\s+found)"
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InventoryAbort(RuntimeError):
    """Controlled failure that invalidates the required user inventory."""


class RestCallError(RuntimeError):
    """Sanitized direct-REST failure with classification metadata."""

    def __init__(self, message, *, tls_error=False, auth_error=False,
                 status_code=None, rest_code=None):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.status_code = status_code
        self.rest_code = rest_code


def _bounded_int(label, minimum, maximum):
    def _parse(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer from {minimum} through {maximum}."
            ) from exc
        if parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be from {minimum} through {maximum}; got {parsed}."
            )
        return parsed
    return _parse


def _split_https_url(raw, label="URL"):
    text = str(raw or "").strip()
    if not text:
        raise InventoryAbort(f"{label} is blank.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise InventoryAbort(f"{label} could not be parsed: {text!r}") from exc
    if parts.scheme.lower() != "https":
        raise InventoryAbort(f"{label} must use HTTPS: {text!r}")
    if not parts.hostname:
        raise InventoryAbort(f"{label} contains no hostname: {text!r}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise InventoryAbort(f"{label} contains an invalid port: {text!r}") from exc
    if parts.username is not None or parts.password is not None:
        raise InventoryAbort(f"{label} must not contain user-info credentials.")
    return parts


def _origin(parts):
    return (parts.scheme.lower(), (parts.hostname or "").lower().rstrip("."),
            parts.port or 443)


def _validate_path(path, label="request URL"):
    raw = str(path or "")
    lower = raw.lower()
    if "%2f" in lower or "%5c" in lower:
        raise RestCallError(f"{label} contains an encoded slash or backslash.")
    decoded = unquote(raw)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError(f"{label} contains unsafe path characters.")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise RestCallError(f"{label} contains path traversal.")
    return raw.rstrip("/")


def _normalize_portal_base(raw):
    parts = _split_https_url(raw, "Portal URL")
    if parts.query or parts.fragment:
        raise InventoryAbort("Portal URL must not contain a query or fragment.")
    path = _validate_path(parts.path, "Portal URL")
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def _portal_base_url(gis, configured_url):
    raw = getattr(gis, "url", None)
    if not raw:
        portal = getattr(gis, "_portal", None)
        raw = getattr(portal, "url", None)
    if not raw:
        raw = configured_url
    return _normalize_portal_base(raw)


def _validate_target_platform(target, platform, portal_url):
    expected = "AGOL" if str(target).lower() == "agol" else "PORTAL"
    if platform != expected:
        raise InventoryAbort(
            f"--target {target!r} requires platform {expected}, but "
            f"portal-url {portal_url!r} was classified as {platform}."
        )


def _safe_path_segment(value, label):
    rendered = str(value or "").strip()
    if not rendered:
        raise RestCallError(f"{label} is blank.")
    if any(ord(ch) < 32 for ch in rendered):
        raise RestCallError(f"{label} contains a control character.")
    return quote(rendered, safe="")


def _validate_org_id(value):
    rendered = str(value or "").strip()
    if not rendered or not _SAFE_ID_RE.fullmatch(rendered):
        raise InventoryAbort(
            "Could not establish a safe authenticated organization id; "
            "refusing to enumerate users without an orgid boundary."
        )
    return rendered


def _response_bytes(response, maximum=MAX_JSON_BYTES):
    length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if length:
        try:
            if int(length) > maximum:
                raise RestCallError(
                    f"JSON response declared {int(length)} bytes, exceeding "
                    f"the {maximum}-byte limit."
                )
        except ValueError:
            pass
    out = bytearray()
    if hasattr(response, "iter_content"):
        iterator = response.iter_content(chunk_size=64 * 1024)
    else:
        iterator = [getattr(response, "content", b"")]
    for chunk in iterator:
        if not chunk:
            continue
        out.extend(chunk)
        if len(out) > maximum:
            raise RestCallError(
                f"JSON response exceeded the {maximum}-byte limit."
            )
    return bytes(out)


def _decode_json_response(response):
    raw = _response_bytes(response)
    if not raw:
        raise RestCallError(
            f"HTTP {getattr(response, 'status_code', '?')} returned an empty body."
        )
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        snippet = raw[:160].decode("utf-8", "replace").replace("\n", " ")
        raise RestCallError(
            f"non-JSON response (HTTP {getattr(response, 'status_code', '?')}): "
            f"{snippet!r}"
        ) from exc


def _format_arcgis_error(data):
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, None
    code = err.get("code")
    message = str(err.get("message") or "unknown REST error")
    details = err.get("details") or []
    if isinstance(details, (list, tuple)):
        detail_text = "; ".join(str(v) for v in details if v not in (None, ""))
    else:
        detail_text = str(details)
    text = f"REST error {code if code is not None else '?'}: {message}"
    if detail_text:
        text += f" ({detail_text})"
    return code, text


class WorkerSessionPool:
    """Pre-created sessions borrowed one task at a time; never shared concurrently."""

    def __init__(self, mode, ca_bundle_path, capacity):
        self._queue = LifoQueue(maxsize=capacity)
        self._sessions = []
        try:
            for _ in range(capacity):
                session = build_secure_session(
                    mode, ca_bundle_path=ca_bundle_path,
                    pool_connections=2, pool_maxsize=4, retry_total=3,
                    logger=None)
                self._sessions.append(session)
                self._queue.put(session)
        except Exception:
            self.close()
            raise

    @contextmanager
    def borrow(self):
        session = self._queue.get()
        try:
            yield session
        finally:
            self._queue.put(session)

    def close(self):
        for session in self._sessions:
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


class PortalRestClient:
    """Strict same-Portal GET-as-JSON client with token refresh and size limits."""

    def __init__(self, portal_base, token_provider, session, gis, logger):
        self.base = _normalize_portal_base(portal_base)
        parts = _split_https_url(self.base, "authenticated Portal URL")
        self.origin = _origin(parts)
        self.base_path = (parts.path or "").rstrip("/")
        self.token_provider = token_provider
        self.session = session
        self.gis = gis
        self.logger = logger
        self._refresh_lock = Lock()

    def _url_for_path(self, path):
        if not isinstance(path, str) or not path.startswith("/sharing/rest/"):
            raise RestCallError(
                "Portal REST path must begin with '/sharing/rest/'."
            )
        if "?" in path or "#" in path:
            raise RestCallError("Portal REST path contains a query or fragment.")
        _validate_path(path, "Portal REST path")
        return self.base + path

    def _validate_redirect(self, raw):
        parts = _split_https_url(raw, "Portal REST redirect")
        if parts.query or parts.fragment:
            raise RestCallError(
                "Portal REST redirect contained a query or fragment."
            )
        if _origin(parts) != self.origin:
            raise RestCallError("Portal REST redirect changed origin.")
        path = _validate_path(parts.path, "Portal REST redirect")
        prefix = self.base_path + "/sharing/rest/"
        if not path.startswith(prefix):
            raise RestCallError(
                "Portal REST redirect left the authenticated Portal REST path."
            )
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def _refresh(self, failed_token):
        with self._refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return
            try:
                _ = self.gis.properties.get("name")
            except Exception as exc:
                self.logger.debug(
                    "SDK token-renewal probe failed before refresh: %s",
                    redact_token(str(exc)))
            self.token_provider.refresh()
            self.logger.info("Refreshed the Portal/AGOL token after an "
                             "authentication failure; retrying once.")

    def _request_once(self, session, url, params, timeout):
        current = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            response = None
            try:
                response = session.get(
                    current, params=params, timeout=timeout,
                    allow_redirects=False, stream=True)
                status = int(response.status_code)
                if status in (301, 302, 303, 307, 308):
                    if redirect_count >= MAX_REDIRECTS:
                        raise RestCallError(
                            f"Portal REST request exceeded {MAX_REDIRECTS} redirects."
                        )
                    location = response.headers.get("Location")
                    if not location:
                        raise RestCallError(
                            f"HTTP {status} redirect omitted Location."
                        )
                    current = self._validate_redirect(urljoin(current, location))
                    continue
                if status >= 400:
                    try:
                        data = _decode_json_response(response)
                        code, arc_text = _format_arcgis_error(data)
                    except RestCallError:
                        code, arc_text = None, None
                    message = (arc_text or
                               f"HTTP {status} "
                               f"{getattr(response, 'reason', '')}".strip())
                    auth = status in (401, 403, 498, 499) or \
                           str(code) in ("401", "403", "498", "499") or \
                           bool(_AUTH_ERROR_RE.search(message))
                    raise RestCallError(
                        message, auth_error=auth, status_code=status,
                        rest_code=code)
                data = _decode_json_response(response)
                code, arc_text = _format_arcgis_error(data)
                if arc_text:
                    auth = str(code) in ("401", "403", "498", "499") or \
                           bool(_AUTH_ERROR_RE.search(arc_text))
                    raise RestCallError(
                        arc_text, auth_error=auth, status_code=status,
                        rest_code=code)
                return data
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True) from exc
            except requests.exceptions.Timeout as exc:
                raise RestCallError(f"timeout after {timeout}s: {exc}") from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(f"request failed: {exc}") from exc
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
        raise RestCallError("Portal REST redirect loop did not terminate.")

    def get_json(self, path, *, params=None, timeout=30, session=None):
        url = self._url_for_path(path)
        request_session = session or self.session
        supplied = dict(params or {})
        supplied.pop("token", None)
        supplied.pop("f", None)
        failed_token = None
        for attempt in range(2):
            token = self.token_provider.get()
            merged = dict(supplied)
            merged.update({"f": "json", "token": token})
            try:
                return self._request_once(request_session, url, merged, timeout)
            except RestCallError as exc:
                if attempt == 0 and exc.auth_error:
                    failed_token = token
                    self._refresh(failed_token)
                    continue
                raise
        raise RestCallError("Portal REST request failed after token refresh.")


def _bounded_future_results(executor, items, worker, max_in_flight):
    iterator = iter(items)
    pending = {}
    exhausted = False
    while pending or not exhausted:
        while not exhausted and len(pending) < max_in_flight:
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                break
            pending[executor.submit(worker, item)] = item
        if not pending:
            continue
        done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in done:
            item = pending.pop(future)
            try:
                yield item, future.result(), None
            except Exception as exc:
                yield item, None, exc


def _reserve_run_paths(output_dir, org_prefix, timestamp):
    """Atomically reserve a unique log name and reuse its suffix for the CSV."""
    for index in range(1000):
        suffix = "" if index == 0 else f"_{index:02d}"
        stamp = timestamp + suffix
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{SCRIPT_NAME}_{stamp}.log")
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(fd)
            csv_path = os.path.join(
                output_dir,
                f"{org_prefix}_{{platform}}_NAMED_USER_LICENSE_INVENTORY_"
                f"{stamp}.csv")
            return log_path, csv_path, stamp
    raise InventoryAbort(
        "Could not reserve a unique output name after 1,000 attempts."
    )


def _parse_arcgis_bool(value):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    rendered = str(value).strip().lower()
    if rendered in ("true", "yes", "y", "1"):
        return True
    if rendered in ("false", "no", "n", "0"):
        return False
    return None


def _normalize_entitlement_codes(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if isinstance(value, (tuple, set)):
        return [str(v) for v in value if v not in (None, "")]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


def _safe_error_entry(stage, subject, reason, *, tls_error=False,
                      license_unknown=False):
    return {
        "stage": str(stage),
        "subject": str(subject),
        "reason": redact_token(str(reason)),
        "tls_error": bool(tls_error),
        "license_unknown": bool(license_unknown),
    }


def _never_or_date(value, logger):
    if value is None or value == "":
        return ""
    try:
        if int(float(str(value).strip())) <= 0:
            return "never"
    except (TypeError, ValueError):
        pass
    return epoch_to_date(value, fmt="%Y/%m/%d %H:%M", logger=logger)


def _classify_entitlements(entitlements):
    levels, extensions = [], []
    for code in _normalize_entitlement_codes(entitlements):
        if code in PRO_LEVEL_ENTITLEMENTS:
            levels.append(PRO_LEVEL_ENTITLEMENTS[code])
        elif code in PRO_EXTENSION_ENTITLEMENTS:
            extensions.append(PRO_EXTENSION_ENTITLEMENTS[code])
        else:
            extensions.append(code)
    return "; ".join(sorted(set(levels))), sorted(set(extensions))


def _standard_role_label(role_value):
    labels = {
        "org_admin": "Administrator", "account_admin": "Administrator",
        "org_publisher": "Publisher", "account_publisher": "Publisher",
        "org_user": "User", "account_user": "User",
        "viewer": "Viewer", "view_only": "Viewer",
    }
    if not role_value:
        return ""
    return labels.get(str(role_value), str(role_value))


def _lookup_result(value, error, return_error):
    return (value, error) if return_error else value


def find_pro_listing_item_id(gis, session, portal_url, token_provider, logger,
                             is_agol=True, rest_client=None,
                             return_error=False):
    """Resolve the Pro listing while distinguishing confirmed absence/errors."""
    try:
        lm = gis.admin.license
        for name in ("ArcGIS Pro", "ArcGIS Pro "):
            try:
                lic = lm.get(name)
            except Exception:
                lic = None
            if lic is None:
                continue
            try:
                raw_props = lic.properties or {}
                props = (dict(raw_props) if not isinstance(raw_props, dict)
                         else raw_props)
            except Exception:
                props = {}
            listing = props.get("listing") if isinstance(props, dict) else None
            if listing is not None and not isinstance(listing, dict):
                try:
                    listing = dict(listing)
                except Exception:
                    listing = None
            if isinstance(listing, dict):
                iid = listing.get("itemId")
                if iid:
                    logger.info("Pro listing item id (from LicenseManager): %s",
                                iid)
                    return _lookup_result(str(iid), None, return_error)
            prov = props.get("provision") if isinstance(props, dict) else None
            if prov is not None and not isinstance(prov, dict):
                try:
                    prov = dict(prov)
                except Exception:
                    prov = None
            if isinstance(prov, dict):
                iid = prov.get("itemId") or prov.get("provisionedItemId")
                if iid:
                    logger.info("Pro listing item id (from provision): %s", iid)
                    return _lookup_result(str(iid), None, return_error)
    except Exception as exc:
        logger.debug("LicenseManager path for item id failed: %s",
                     redact_token(str(exc)))

    if is_agol:
        path = "/sharing/rest/portals/self/purchases"
        try:
            if rest_client is not None:
                data = rest_client.get_json(path, session=session)
                err = None
            else:
                url = portal_url.rstrip("/") + path
                data, err = get_json(session, url, token_provider.get())
        except RestCallError as exc:
            data, err = None, str(exc)
        if err:
            error = redact_token(
                f"Could not read portals/self/purchases: {err}")
            logger.warning("%s", error)
            return _lookup_result(None, error, return_error)
        if not isinstance(data, dict):
            error = "portals/self/purchases returned a non-object response."
            logger.warning("%s", error)
            return _lookup_result(None, error, return_error)
        if "purchases" not in data:
            error = ("portals/self/purchases succeeded but omitted the "
                     "purchases collection.")
            logger.warning("%s", error)
            return _lookup_result(None, error, return_error)
        purchases = data.get("purchases")
        if purchases is None:
            purchases = []
        if not isinstance(purchases, list):
            error = ("portals/self/purchases returned a malformed purchases "
                     "collection.")
            logger.warning("%s", error)
            return _lookup_result(None, error, return_error)
        for purchase in purchases:
            listing = (purchase.get("listing")
                       if isinstance(purchase, dict) else None)
            if not isinstance(listing, dict):
                continue
            title = str(listing.get("title") or "")
            if "arcgis pro" in title.lower():
                iid = listing.get("itemId") or listing.get("id")
                if iid:
                    logger.info("Pro listing item id (from purchases, %r): %s",
                                title, iid)
                    return _lookup_result(str(iid), None, return_error)
        logger.info("No ArcGIS Pro listing was present in the successful "
                    "portals/self/purchases response.")
        return _lookup_result(None, None, return_error)

    iid, search_error = _find_portal_pro_listing(
        gis, logger, return_error=True)
    if iid:
        return _lookup_result(str(iid), None, return_error)
    if search_error:
        logger.warning("%s", search_error)
    else:
        logger.info("No ArcGIS Pro provisioned listing was found by the "
                    "successful Portal content searches.")
    return _lookup_result(None, search_error, return_error)


def _find_portal_pro_listing(gis, logger, return_error=False):
    queries = [
        'type:"Provisioning" AND title:"ArcGIS Pro"',
        'title:"ArcGIS Pro" AND typekeywords:"Provisioning"',
        'type:"Provisioning"',
    ]
    successful_queries = 0
    failures = []
    for query in queries:
        try:
            items = gis.content.search(query=query, max_items=50) or []
            if not isinstance(items, (list, tuple)):
                raise TypeError(
                    "Portal provisioning-item search returned "
                    f"{type(items).__name__}, not a sequence.")
            successful_queries += 1
        except Exception as exc:
            failures.append(redact_token(str(exc)))
            logger.debug("Portal listing search %r failed: %s", query,
                         redact_token(str(exc)))
            continue
        for item in items:
            title = str(getattr(item, "title", "") or "")
            if "arcgis pro" in title.lower():
                iid = getattr(item, "id", None)
                if iid:
                    logger.info("Pro listing item id (Portal content search, "
                                "%r): %s", title, iid)
                    return _lookup_result(iid, None, return_error)
    error = None
    if successful_queries == 0:
        error = ("Every Portal ArcGIS Pro provisioning-item search failed: "
                 + "; ".join(failures[:3]))
    return _lookup_result(None, error, return_error)

def enumerate_all_users(session, portal_url, token_provider, org_id, logger,
                        page_size=100, max_users=None, rest_client=None):
    """Enumerate users in username order; strict when a RestClient is supplied."""
    safe_org_id = _validate_org_id(org_id) if rest_client is not None else org_id
    path = "/sharing/rest/portals/self/users/search"
    search_url = portal_url.rstrip("/") + path
    users = []
    seen = set()
    start = 1
    raw_received = 0
    declared_total = None
    while True:
        params = {"num": page_size, "start": start,
                  "sortField": "username", "sortOrder": "asc",
                  "q": f"orgid:{safe_org_id}" if safe_org_id else ""}
        if rest_client is not None:
            data = rest_client.get_json(path, params=params, session=session)
            err = None
        else:
            data, err = get_json(session, search_url, token_provider.get(),
                                 params=params)
        if err:
            logger.warning("users/search page at start=%d failed: %s",
                           start, redact_token(str(err)))
            break
        if not isinstance(data, dict):
            if rest_client is not None:
                raise InventoryAbort(
                    f"users/search page at start={start} was not a JSON object."
                )
            break

        batch = data.get("users")
        if not batch:
            batch = data.get("results")
        if batch is None:
            batch = []
        if not isinstance(batch, list):
            raise InventoryAbort(
                f"users/search page at start={start} returned a malformed user list."
            )

        total_value = data.get("total")
        if total_value is not None:
            try:
                current_total = int(total_value)
            except (TypeError, ValueError) as exc:
                raise InventoryAbort(
                    f"users/search page at start={start} returned invalid total "
                    f"{total_value!r}."
                ) from exc
            if current_total < 0:
                raise InventoryAbort("users/search returned a negative total.")
            if declared_total is None:
                declared_total = current_total
            elif current_total != declared_total:
                raise InventoryAbort(
                    "users/search total changed during pagination "
                    f"({declared_total} -> {current_total})."
                )
            if current_total == 0 and batch:
                raise InventoryAbort(
                    "users/search returned users while declaring total=0."
                )

        if not batch:
            if (rest_client is not None and max_users is None
                    and declared_total is not None
                    and raw_received < declared_total):
                raise InventoryAbort(
                    "users/search returned an empty page before its declared "
                    f"total was reached ({raw_received}/{declared_total})."
                )
            break

        raw_received += len(batch)
        for entry in batch:
            if not isinstance(entry, dict):
                if rest_client is not None:
                    raise InventoryAbort(
                        f"users/search page at start={start} contained a "
                        "non-object user entry."
                    )
                continue
            uname = entry.get("username")
            if not isinstance(uname, str) or not uname.strip():
                if rest_client is not None:
                    raise InventoryAbort(
                        f"users/search page at start={start} contained a user "
                        "without a usable username."
                    )
                continue
            returned_org = (entry.get("orgId") or entry.get("orgID") or
                            entry.get("orgid"))
            if (rest_client is not None and returned_org and
                    str(returned_org) != str(safe_org_id)):
                raise InventoryAbort(
                    f"users/search returned user {uname!r} from organization "
                    f"{returned_org!r}, outside authenticated org "
                    f"{safe_org_id!r}."
                )
            if uname in seen:
                if rest_client is not None:
                    raise InventoryAbort(
                        f"users/search returned duplicate username {uname!r}; "
                        "paging was not stable."
                    )
                continue
            seen.add(uname)
            users.append(entry)

        logger.info("Enumerated %d org user(s) so far ...", len(users))
        if max_users is not None and len(users) >= max_users:
            users = users[:max_users]
            logger.info("Reached user cap of %d; stopping enumeration.",
                        max_users)
            break

        got = len(batch)
        nxt = data.get("nextStart")
        if nxt is not None:
            try:
                nxt = int(nxt)
            except (TypeError, ValueError) as exc:
                raise InventoryAbort(
                    f"users/search returned invalid nextStart {nxt!r}."
                ) from exc
        if nxt is not None and nxt != -1:
            if nxt <= start:
                raise InventoryAbort(
                    f"users/search pagination did not advance ({start} -> {nxt})."
                )
            start = nxt
        else:
            if got < page_size:
                if (rest_client is not None and max_users is None
                        and declared_total is not None
                        and raw_received < declared_total):
                    raise InventoryAbort(
                        "users/search ended before its declared total was "
                        f"reached ({raw_received}/{declared_total})."
                    )
                break
            start += got

        if (declared_total is not None and raw_received >= declared_total):
            break

    logger.info("Enumerated %d total org user(s).", len(users))
    return users


def fetch_user_entitlements(session, portal_url, token_provider, pro_item_id,
                            username, timeout=30):
    url = (portal_url.rstrip("/")
           + f"/sharing/rest/content/listings/{pro_item_id}"
           + f"/userEntitlements/{username}")
    data, err = get_json(session, url, token_provider.get(), timeout=timeout)
    if err or not isinstance(data, dict):
        return {}
    ue = data.get("userEntitlements")
    return ue if isinstance(ue, dict) else {}


def _fetch_user_entitlements_detailed(rest_client, session, pro_item_id,
                                      username, timeout=30):
    item_segment = _safe_path_segment(pro_item_id, "Pro listing item id")
    user_segment = _safe_path_segment(username, "username")
    path = (f"/sharing/rest/content/listings/{item_segment}"
            f"/userEntitlements/{user_segment}")
    try:
        data = rest_client.get_json(path, timeout=timeout, session=session)
    except RestCallError as exc:
        if (not exc.auth_error and
                _NO_ENTITLEMENT_RE.search(str(exc))):
            return {}, None
        return {}, _safe_error_entry(
            "userEntitlements", username, exc, tls_error=exc.tls_error,
            license_unknown=True)
    if not isinstance(data, dict):
        return {}, _safe_error_entry(
            "userEntitlements", username, "response was not a JSON object",
            license_unknown=True)
    ue = data.get("userEntitlements")
    # A successful object with no userEntitlements means no assignment. This is
    # a confirmed negative, not a transport failure.
    if ue is None:
        return {}, None
    if not isinstance(ue, dict):
        return {}, _safe_error_entry(
            "userEntitlements", username,
            "userEntitlements was present but was not an object",
            license_unknown=True)
    cleaned = dict(ue)
    raw_codes = cleaned.get("entitlements")
    if raw_codes is not None and not isinstance(raw_codes, list):
        cleaned["entitlements"] = _normalize_entitlement_codes(raw_codes)
        return cleaned, _safe_error_entry(
            "userEntitlements", username,
            "entitlements was not an array; normalized defensively")
    disconnected_raw = cleaned.get("disconnected")
    if (disconnected_raw not in (None, "") and
            _parse_arcgis_bool(disconnected_raw) is None):
        return cleaned, _safe_error_entry(
            "userEntitlements", username,
            "disconnected was present but was not a recognized Boolean")
    dinfo = cleaned.get("disconnectedInfo")
    if dinfo not in (None, "") and not isinstance(dinfo, dict):
        cleaned["disconnectedInfo"] = {}
        return cleaned, _safe_error_entry(
            "userEntitlements", username,
            "disconnectedInfo was not an object")
    return cleaned, None


def fetch_all_entitlements(session, portal_url, token_provider, pro_item_id,
                           usernames, logger, workers=DEFAULT_WORKERS,
                           rest_client=None, session_pool=None,
                           return_errors=False):
    result = {}
    errors = []
    if not pro_item_id:
        logger.warning("No Pro listing item id; skipping userEntitlements "
                       "(entitlement columns will be blank).")
        result = {u: {} for u in usernames}
        return (result, errors) if return_errors else result

    logger.info("Fetching /userEntitlements for %d user(s) in %d thread(s) ...",
                len(usernames), workers)

    def _one(uname):
        if rest_client is None:
            return uname, fetch_user_entitlements(
                session, portal_url, token_provider, pro_item_id, uname), None
        if session_pool is None:
            ue, err = _fetch_user_entitlements_detailed(
                rest_client, session, pro_item_id, uname)
        else:
            with session_pool.borrow() as worker_session:
                ue, err = _fetch_user_entitlements_detailed(
                    rest_client, worker_session, pro_item_id, uname)
        return uname, ue, err

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for uname, payload, exc in _bounded_future_results(
                ex, usernames, _one, max_in_flight=max(1, workers * 2)):
            if exc is not None:
                err = _safe_error_entry(
                    "userEntitlements", uname, exc,
                    tls_error=isinstance(exc, requests.exceptions.SSLError),
                    license_unknown=True)
                result[uname] = {}
                errors.append(err)
            else:
                _returned_uname, ue, err = payload
                result[uname] = ue
                if err:
                    errors.append(err)
            done += 1
            if done % 100 == 0 or done == len(usernames):
                logger.info("  ...%d/%d userEntitlements fetched.",
                            done, len(usernames))

    for uname in usernames:
        result.setdefault(uname, {})
    n_with = sum(1 for v in result.values()
                 if _normalize_entitlement_codes(v.get("entitlements")))
    logger.info("userEntitlements fetched: %d user(s), %d with a Pro "
                "entitlement, %d error(s).", len(result), n_with, len(errors))
    for err in errors[:MAX_ERROR_SAMPLES]:
        logger.warning("  userEntitlements %s: %s", err["subject"],
                       err["reason"])
    return (result, errors) if return_errors else result


def fetch_user_sessions(session, portal_url, token_provider, pro_item_id,
                        username, timeout=30):
    url = (portal_url.rstrip("/")
           + f"/sharing/rest/content/listings/{pro_item_id}"
           + f"/users/{username}/sessions")
    data, err = get_json(session, url, token_provider.get(), timeout=timeout)
    if err or not isinstance(data, dict):
        return None
    sessions = data.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        return None

    best = None
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        modified = entry.get("modified")
        try:
            modified = int(modified)
        except (TypeError, ValueError):
            continue
        if modified <= 0:
            continue
        if best is None or modified > best:
            best = modified
    return best


def _fetch_user_sessions_detailed(rest_client, session, pro_item_id,
                                  username, timeout=30):
    item_segment = _safe_path_segment(pro_item_id, "Pro listing item id")
    user_segment = _safe_path_segment(username, "username")
    path = (f"/sharing/rest/content/listings/{item_segment}"
            f"/users/{user_segment}/sessions")
    try:
        data = rest_client.get_json(path, timeout=timeout, session=session)
    except RestCallError as exc:
        if not exc.auth_error and _NO_SESSION_RE.search(str(exc)):
            return None, None
        return None, _safe_error_entry(
            "sessions", username, exc, tls_error=exc.tls_error)
    if not isinstance(data, dict):
        return None, _safe_error_entry(
            "sessions", username, "response was not a JSON object")
    sessions = data.get("sessions")
    if sessions is None:
        return None, None
    if not isinstance(sessions, list):
        return None, _safe_error_entry(
            "sessions", username, "sessions was present but was not an array")
    if not sessions:
        return None, None

    best = None
    malformed = 0
    for entry in sessions:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        modified = entry.get("modified")
        try:
            modified = int(modified)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if modified <= 0:
            continue
        if best is None or modified > best:
            best = modified
    if malformed:
        return best, _safe_error_entry(
            "sessions", username,
            f"{malformed} session record(s) had malformed modified values")
    return best, None


def fetch_all_sessions(session, portal_url, token_provider, pro_item_id,
                       usernames, logger, workers=DEFAULT_WORKERS,
                       rest_client=None, session_pool=None,
                       return_errors=False):
    result = {}
    errors = []
    if not pro_item_id:
        logger.warning("No Pro listing item id; skipping per-user sessions "
                       "lookup (pro_last_used_session will rely on the "
                       "usage-report / userEntitlements.lastLogin fallbacks "
                       "only).")
        result = {u: None for u in usernames}
        return (result, errors) if return_errors else result

    logger.info("Fetching /users/{username}/sessions for %d user(s) in %d "
                "thread(s) ...", len(usernames), workers)

    def _one(uname):
        if rest_client is None:
            return uname, fetch_user_sessions(
                session, portal_url, token_provider, pro_item_id, uname), None
        if session_pool is None:
            last, err = _fetch_user_sessions_detailed(
                rest_client, session, pro_item_id, uname)
        else:
            with session_pool.borrow() as worker_session:
                last, err = _fetch_user_sessions_detailed(
                    rest_client, worker_session, pro_item_id, uname)
        return uname, last, err

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for uname, payload, exc in _bounded_future_results(
                ex, usernames, _one, max_in_flight=max(1, workers * 2)):
            if exc is not None:
                result[uname] = None
                errors.append(_safe_error_entry(
                    "sessions", uname, exc,
                    tls_error=isinstance(exc, requests.exceptions.SSLError)))
            else:
                _returned_uname, last_modified, err = payload
                result[uname] = last_modified
                if err:
                    errors.append(err)
            done += 1
            if done % 100 == 0 or done == len(usernames):
                logger.info("  ...%d/%d user session lookups done.",
                            done, len(usernames))

    for uname in usernames:
        result.setdefault(uname, None)
    n_with = sum(1 for v in result.values() if v)
    logger.info("Per-user sessions fetched: %d user(s), %d with at least "
                "one session bearing a usable 'modified' value, %d error(s).",
                len(result), n_with, len(errors))
    for err in errors[:MAX_ERROR_SAMPLES]:
        logger.warning("  sessions %s: %s", err["subject"], err["reason"])
    return (result, errors) if return_errors else result


def fetch_pro_session_usage(gis, time_frame, logger, org_id=None,
                            return_error=False):
    error = None
    try:
        logger.info("Fetching Pro-desktop usage report (time_frame=%s) -- can "
                    "be slow on a large org ...", time_frame)
        report = gis.admin.usage_reports.applications(time_frame=time_frame)
    except Exception as exc:
        error = redact_token(f"applications() usage report failed: {exc}")
        logger.warning("%s", error)
        return ({}, error) if return_error else {}
    if not isinstance(report, dict):
        error = f"applications() returned unexpected type {type(report).__name__}."
        logger.warning("%s", error)
        return ({}, error) if return_error else {}

    data = report.get("data") or []
    if not isinstance(data, list):
        error = "applications() returned a malformed data collection."
        logger.warning("%s", error)
        return ({}, error) if return_error else {}
    logger.info("Usage report returned %d (user, app) series.", len(data))
    last_by_user = {}
    malformed = 0
    for series in data:
        if not isinstance(series, dict):
            malformed += 1
            continue
        if series.get("appId") != PRO_DESKTOP_APP_ID:
            continue
        if org_id and series.get("userOrgId") and \
                series.get("userOrgId") != org_id:
            continue
        uname = series.get("username")
        if not uname:
            malformed += 1
            continue
        pairs = series.get("num") or []
        if not isinstance(pairs, list):
            malformed += 1
            continue
        best = last_by_user.get(uname)
        for pair in pairs:
            try:
                day_epoch = int(pair[0])
                count = int(pair[1])
            except (TypeError, ValueError, IndexError):
                malformed += 1
                continue
            if count > 0 and (best is None or day_epoch > best):
                best = day_epoch
        if best is not None:
            last_by_user[uname] = best

    if malformed:
        error = f"usage report contained {malformed} malformed series/value(s)"
        logger.warning("%s", error)
    logger.info("Pro-desktop sessions found for %d user(s) in the window.",
                len(last_by_user))
    return (last_by_user, error) if return_error else last_by_user


def build_role_name_map(gis, logger, return_error=False):
    role_names = {}
    error = None
    try:
        roles = gis.users.roles.all() or []
        for role in roles:
            rid = getattr(role, "role_id", None) or getattr(role, "id", None)
            rname = getattr(role, "name", None)
            if rid and rname:
                role_names[rid] = rname
    except Exception as exc:
        error = redact_token(f"Could not enumerate custom roles: {exc}")
        logger.debug("%s", error)
    if role_names:
        logger.info("Resolved %d custom role name(s).", len(role_names))
    return (role_names, error) if return_error else role_names


def _role_label(user_dict, role_names):
    rid = user_dict.get("role") or user_dict.get("roleId")
    if rid in role_names:
        return role_names[rid]
    return _standard_role_label(rid)


def _resolve_pro_last_used(uname, session_last_used, usage_report_fallback,
                           entitlements_last_login):
    if session_last_used:
        return session_last_used, "sessions-endpoint"
    fallback = usage_report_fallback.get(uname)
    if fallback:
        return fallback, "usage-report (fallback)"
    if entitlements_last_login:
        try:
            if int(float(str(entitlements_last_login).strip())) > 0:
                return entitlements_last_login, "userEntitlements.lastLogin"
        except (TypeError, ValueError):
            pass
    return None, ""


def build_rows(platform, users, entitlements_by_user, sessions_last_used,
              pro_sessions_fallback, role_names, pro_item_id, logger,
              assigned_only=False, entitlement_unknown_users=None):
    # Retained in the public signature for compatibility with existing callers.
    _ = pro_item_id
    rows = []
    entitlement_unknown_users = set(entitlement_unknown_users or ())
    for user in users:
        if not isinstance(user, dict):
            continue
        uname = user.get("username")
        if not uname:
            continue
        ue = entitlements_by_user.get(uname) or {}
        if not isinstance(ue, dict):
            ue = {}
        codes = _normalize_entitlement_codes(ue.get("entitlements"))
        license_unknown = uname in entitlement_unknown_users
        if assigned_only and not codes:
            continue
        pro_level, pro_exts = _classify_entitlements(codes)
        has_pro = None if license_unknown else bool(pro_level or pro_exts)
        disconnected = (None if license_unknown else
                        _parse_arcgis_bool(ue.get("disconnected")))
        dinfo = ue.get("disconnectedInfo") or {}
        disc_session = dinfo.get("sessionName", "") if isinstance(dinfo, dict) else ""
        disc_since = _never_or_date(
            dinfo.get("disconnectedSince") if isinstance(dinfo, dict) else None,
            logger)
        disc_until = _never_or_date(
            dinfo.get("disconnectedUntil") if isinstance(dinfo, dict) else None,
            logger)
        entitlements_last_login = ue.get("lastLogin")
        last_login_epoch = entitlements_last_login
        if last_login_epoch is None:
            last_login_epoch = user.get("lastLogin")

        pro_last_used_epoch, pro_last_used_source = _resolve_pro_last_used(
            uname, sessions_last_used.get(uname), pro_sessions_fallback,
            entitlements_last_login)

        user_level = (user.get("userLicenseTypeId") or user.get("level") or "")

        rows.append({
            "PLATFORM": platform,
            "username": uname,
            "full_name": user.get("fullName", "") or "",
            "user_level": user_level,
            "role": _role_label(user, role_names),
            "has_pro_license": ("" if has_pro is None else
                                ("yes" if has_pro else "no")),
            "pro_license": pro_level,
            "pro_extensions": "; ".join(pro_exts),
            "all_entitlements": "; ".join(codes),
            "pro_last_used_session": _never_or_date(pro_last_used_epoch, logger),
            "pro_last_used_source": pro_last_used_source,
            "last_login": _never_or_date(last_login_epoch, logger),
            "taken_offline": ("yes" if disconnected is True else
                              ("no" if disconnected is False else "")),
            "disconnected_session": disc_session,
            "disconnected_since": disc_since,
            "disconnected_until": disc_until,
        })

    logger.info("Built %d license inventory row(s).", len(rows))
    return rows


def build_email_body(platform, org_name, args, row_count, user_count,
                     pro_count, timing, attach_note, max_email_mb=None,
                     result="COMPLETE", error_counts=None,
                     incomplete_reasons=None):
    hint = (
        "One row per ORG USER. Entitlement data comes from each user's "
        "/userEntitlements record. pro_last_used_session is resolved in "
        "priority order: (1) each user's own listing /sessions endpoint "
        "(the max 'modified' timestamp across their session(s) -- the same "
        "signal the My Organization -> Licenses UI uses for 'Last used'); "
        "(2) the org-wide usage report, used only when a user has no "
        "sessions-endpoint data; (3) userEntitlements.lastLogin, used only "
        "when NEITHER of the above produced a value -- this covers users "
        "who have not used Pro recently enough for a session or usage-"
        "report entry to still exist, since lastLogin is always present on "
        "a user's entitlement record regardless of recency or "
        "connected/disconnected status. See pro_last_used_source for which "
        "of the three signals was actually used for each row."
    )
    cap_str = (str(args.max_users) if (args.max_users is not None
                                        and args.max_users > 0)
               else ("50 (test default)" if args.test and args.max_users is None
                     else "none"))
    lines = [
        f"{SUBJECT_PREFIX}",
        "",
        f"Result: {result}",
        f"Organization: {org_name}",
        f"Config file: {args.config}",
        f"Target: {args.target}",
        f"Platform: {platform}",
        f"Mode: {'TEST' if args.test else 'PRODUCTION'}",
        f"TLS mode: {args.tls_mode}",
        f"Portal URL: {args.portal_url}",
        f"Username: {args.username}",
        f"Org users inventoried: {user_count}",
        f"Rows emitted: {row_count}",
        f"Users with a Pro license: {pro_count}",
        f"Assigned-only mode: {'yes' if args.assigned_only else 'no'}",
        f"User cap: {cap_str}",
        f"Usage window (fallback): {args.app_usage_window}",
        f"Worker threads: {args.workers}",
        f"Output directory: {args.output_dir}",
        f"Max email attachment size (MB): "
        f"{max_email_mb if max_email_mb is not None else 'default'}",
    ]
    if error_counts:
        lines.extend([
            f"Entitlement lookup errors: {error_counts.get('entitlements', 0)}",
            f"Session lookup errors: {error_counts.get('sessions', 0)}",
            f"TLS/CERT lookup errors: {error_counts.get('tls', 0)}",
            f"Other incomplete stages: {error_counts.get('other', 0)}",
        ])
    if incomplete_reasons:
        lines.extend(["", "INCOMPLETE RUN DETAILS:"])
        lines.extend(f"  - {redact_token(str(reason))}"
                     for reason in incomplete_reasons[:MAX_ERROR_SAMPLES])
        lines.append(
            "Absence of a license/session value must not be treated as a "
            "confirmed negative for affected users."
        )
    lines.extend(["", hint, "", timing])
    if attach_note:
        lines.append(attach_note)
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Inventory ArcGIS Pro named-user licenses (tier, "
                    "extensions, offline status, real last-used) for every "
                    "user on Portal or AGOL. TLS verification strategy for "
                    "this script's own direct REST calls is explicit "
                    "(--tls-mode); the arcgis SDK connection (via "
                    "connect_gis()) always verifies TLS regardless.")
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    add_test_flag(
        parser,
        help_text="Run in PRODUCTION mode (default is TEST mode). TEST tags "
                  "the subject [TEST] and caps the run at 50 users unless "
                  "--max-users is given; --no-test removes both.")
    parser.add_argument(
        "--assigned-only", action="store_true",
        help="Only emit rows for users who actually hold a Pro entitlement.")
    parser.add_argument(
        "--app-usage-window", default="year",
        choices=sorted(APP_USAGE_WINDOWS.keys()),
        help="Window for the SECONDARY (fallback) Pro-session usage report "
             "(default 'year'). Only consulted for a user whose "
             "sessions-endpoint lookup produced nothing.")
    parser.add_argument(
        "--max-users", type=int, default=None,
        help="Cap the inventory to at most this many users. Values <= 0 "
             "retain the historical meaning of no cap.")
    parser.add_argument(
        "--workers", type=_bounded_int("--workers", 1, 64),
        default=DEFAULT_WORKERS,
        help=f"Threads for per-user /userEntitlements and /sessions calls "
             f"(default {DEFAULT_WORKERS}; allowed range 1-64).")

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the user-"
             "enumeration, userEntitlements, and sessions REST calls -- "
             "NOT the arcgis SDK's own internal connection, which always "
             "verifies TLS via connect_gis()) verifies TLS certificates. "
             "'standard' (default): certifi's bundled public CA list -- "
             "correct for AGOL and any Portal with a public CA cert. "
             "'os-trust-store': verify against the operating system's "
             "native certificate store (requires the 'truststore' "
             "package) -- use for an internal Enterprise Portal signed by "
             "an organizational CA already trusted at the OS level. "
             "'custom-ca-bundle': verify against an explicit PEM file "
             "passed via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")
    return parser


def _resolve_max_users(args, logger):
    if args.max_users is not None:
        if args.max_users <= 0:
            logger.info("--max-users <= 0 -> no cap.")
            return None
        logger.info("User cap: %d (explicit --max-users).", args.max_users)
        return args.max_users
    if args.test:
        logger.info("User cap: 50 (TEST mode default).")
        return 50
    logger.info("User cap: none (PRODUCTION mode).")
    return None


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
              "custom-ca-bundle.", file=sys.stderr)
        return 2

    timestamp = build_timestamp()
    finalize_config(args, required=("output-dir", "portal-url", "username",
                                    "keyring-service", "smtp-server",
                                    "email-to", "email-from"))
    max_email_mb = get_max_email_mb(args.target, args.config)
    ensure_output_dir(args.output_dir)
    org_prefix = args.org_name_safe

    configured_platform = get_platform(args.portal_url)
    try:
        _validate_target_platform(args.target, configured_platform,
                                  args.portal_url)
        _normalize_portal_base(args.portal_url)
        log_file_path, csv_template, run_stamp = _reserve_run_paths(
            args.output_dir, org_prefix, timestamp)
    except Exception as exc:
        print(f"Run ABORTED before logging: {redact_token(str(exc))}",
              file=sys.stderr)
        return 1

    platform = configured_platform
    csv_path = csv_template.format(platform=platform)
    logger = setup_logging(log_file_path, SCRIPT_NAME)
    start_wallclock, start_perf = start_timer()

    logger.info("=" * 70)
    logger.info("Named User License Inventory - run started")
    logger.info("=" * 70)
    logger.info("Organization: %s", args.org_name)
    logger.info("Config file: %s", args.config)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "PRODUCTION")
    logger.info("TLS mode: %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path: %s", args.ca_bundle_path)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("Output CSV: %s", csv_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s | from: %s", args.email_to, args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Assigned-only mode: %s", "yes" if args.assigned_only else "no")
    logger.info("Worker threads: %d", args.workers)
    logger.info("=" * 70)

    subject = subject_line(SUBJECT_PREFIX, run_stamp, platform,
                           org_name=args.org_name)
    if args.test:
        subject = "[TEST] " + subject

    def _abort(reason, *, include_traceback=False):
        safe = redact_token(str(reason))
        if include_traceback:
            logger.exception("Run ABORTED: %s", safe)
        else:
            logger.error("Run ABORTED: %s", safe)
        body = (
            f"{SUBJECT_PREFIX}\n\nResult: ABORTED\n"
            f"Organization: {args.org_name}\nPlatform: {platform}\n"
            f"Reason: {safe}\n\n"
            f"{timing_block(start_wallclock, start_perf)}"
        )
        send_email("[ABORTED] " + subject, body, [log_file_path],
                   args.smtp_server, args.email_to, args.email_from, logger)
        return 1

    rows = []
    users = []
    pro_count = 0
    incomplete_reasons = []
    entitlement_errors = []
    session_errors = []
    worker_pool = None
    main_session = None

    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        main_session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path,
            pool_connections=max(8, args.workers),
            pool_maxsize=max(16, args.workers * 2), retry_total=3,
            logger=logger)

        # This caller-level try also covers CA-file parsing, which the shared
        # handshake helper performs before entering its own socket try block.
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, main_session, logger=logger)
        if not handshake_ok:
            raise InventoryAbort(
                f"TLS handshake verification to {args.portal_url} FAILED: "
                f"{handshake_detail.get('error')}")

        ca_path = (args.ca_bundle_path
                   if mode is VerifyMode.CUSTOM_CA_BUNDLE else None)
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          verify_mode=mode, ca_bundle_path=ca_path)

        portal_url = _portal_base_url(gis, args.portal_url)
        authenticated_platform = get_platform(portal_url)
        _validate_target_platform(args.target, authenticated_platform,
                                  portal_url)
        platform = authenticated_platform
        csv_path = csv_template.format(platform=platform)

        token_provider = provider_for_gis(gis, platform)
        logger.info("Token acquired via %s (token present: %s).",
                    type(token_provider).__name__,
                    token_provider.is_present())

        org_id = _validate_org_id(get_org_id(gis, logger=logger))
        logger.info("Org id: %s", org_id)

        rest_client = PortalRestClient(
            portal_url, token_provider, main_session, gis, logger)

        is_agol = (args.target == "agol")
        pro_item_id, pro_lookup_error = find_pro_listing_item_id(
            gis, main_session, portal_url, token_provider, logger,
            is_agol=is_agol, rest_client=rest_client, return_error=True)

        max_users = _resolve_max_users(args, logger)
        users = enumerate_all_users(
            main_session, portal_url, token_provider, org_id, logger,
            max_users=max_users, rest_client=rest_client)
        usernames = [u.get("username") for u in users if u.get("username")]

        if users and pro_lookup_error:
            incomplete_reasons.append(pro_lookup_error)

        if usernames and pro_item_id:
            worker_pool = WorkerSessionPool(
                mode, ca_path, capacity=args.workers)

        entitlements_by_user, entitlement_errors = fetch_all_entitlements(
            main_session, portal_url, token_provider, pro_item_id, usernames,
            logger, workers=args.workers, rest_client=rest_client,
            session_pool=worker_pool, return_errors=True)

        sessions_last_used, session_errors = fetch_all_sessions(
            main_session, portal_url, token_provider, pro_item_id, usernames,
            logger, workers=args.workers, rest_client=rest_client,
            session_pool=worker_pool, return_errors=True)

        pro_sessions_fallback = {}
        usage_error = None
        if is_agol:
            tf = APP_USAGE_WINDOWS[args.app_usage_window]
            pro_sessions_fallback, usage_error = fetch_pro_session_usage(
                gis, tf, logger, org_id=org_id, return_error=True)
            needs_usage_fallback = any(
                not sessions_last_used.get(uname) for uname in usernames)
            if usage_error and needs_usage_fallback:
                incomplete_reasons.append(usage_error)
        else:
            logger.info("Skipping secondary Pro-desktop usage report: "
                        "Enterprise Portal has no usage_reports API. "
                        "pro_last_used_session relies on the sessions "
                        "endpoint and the userEntitlements.lastLogin "
                        "fallback for Portal targets.")

        role_names, role_error = build_role_name_map(
            gis, logger, return_error=True)
        if role_error:
            unresolved_custom_roles = []
            for user in users:
                if not isinstance(user, dict):
                    continue
                rid = user.get("role") or user.get("roleId")
                if rid and _standard_role_label(rid) == str(rid):
                    unresolved_custom_roles.append(str(rid))
            if unresolved_custom_roles:
                incomplete_reasons.append(role_error)

        entitlement_unknown_users = {
            err["subject"] for err in entitlement_errors
            if err.get("license_unknown")
        }
        if pro_lookup_error and not pro_item_id:
            entitlement_unknown_users.update(usernames)

        rows = build_rows(
            platform, users, entitlements_by_user, sessions_last_used,
            pro_sessions_fallback, role_names, pro_item_id, logger,
            assigned_only=args.assigned_only,
            entitlement_unknown_users=entitlement_unknown_users)
        pro_count = sum(
            1 for row in rows if row.get("has_pro_license") == "yes")

        if entitlement_errors:
            incomplete_reasons.append(
                f"{len(entitlement_errors)} user entitlement lookup(s) failed "
                "or returned malformed data."
            )
        if session_errors:
            incomplete_reasons.append(
                f"{len(session_errors)} user session lookup(s) failed or "
                "returned malformed data."
            )

    except InventoryAbort as exc:
        return _abort(exc)
    except Exception as exc:
        return _abort(exc, include_traceback=True)
    finally:
        if worker_pool is not None:
            worker_pool.close()
        if main_session is not None:
            try:
                main_session.close()
            except Exception:
                pass

    result = "INCOMPLETE" if incomplete_reasons else "COMPLETE"

    try:
        written_csv = write_dict_csv(
            rows, csv_path, fieldnames=CSV_FIELDS, logger=logger,
            label="license row", force_values={"PLATFORM": platform})
    except Exception as exc:
        return _abort(f"Could not write license inventory: {exc}",
                      include_traceback=True)

    timing = timing_block(start_wallclock, start_perf)
    attachments, attach_note = gather_attachments(
        log_file_path, [written_csv] if written_csv else [],
        args.output_dir, logger, cap_mb=max_email_mb, label="CSV")

    lookup_errors = entitlement_errors + session_errors
    error_counts = {
        "entitlements": len(entitlement_errors),
        "sessions": len(session_errors),
        "tls": sum(1 for err in lookup_errors if err.get("tls_error")),
        "other": max(0, len(incomplete_reasons)
                     - bool(entitlement_errors) - bool(session_errors)),
    }
    body = build_email_body(
        platform, args.org_name, args, row_count=len(rows),
        user_count=len(users), pro_count=pro_count, timing=timing,
        attach_note=attach_note, max_email_mb=max_email_mb,
        result=result, error_counts=error_counts,
        incomplete_reasons=incomplete_reasons)

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Result: %s", result)
    logger.info("Organization users enumerated: %d", len(users))
    logger.info("Rows emitted: %d", len(rows))
    logger.info("Users with a Pro license: %d", pro_count)
    logger.info("Entitlement lookup errors: %d", len(entitlement_errors))
    logger.info("Session lookup errors: %d", len(session_errors))
    logger.info("TLS/CERT lookup errors: %d", error_counts["tls"])
    logger.info("Output CSV: %s", written_csv or "(none - no rows)")
    logger.info("=" * 70)

    completion_subject = subject if result == "COMPLETE" else \
        "[INCOMPLETE] " + subject
    logger.info("Emailing results to configured recipient(s) ...")
    send_email(completion_subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    logger.info("%s %s. %d row(s) across %d org user(s); %d with a Pro "
                "license.", SCRIPT_NAME, result.lower(), len(rows),
                len(users), pro_count)
    if result == "INCOMPLETE":
        logger.warning(
            "%s completed with %d incomplete reason(s); returning exit "
            "status 0 because the inventory workflow ran to completion. "
            "The report remains marked INCOMPLETE because some license "
            "details could not be fully resolved.",
            SCRIPT_NAME, len(incomplete_reasons),
        )
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
