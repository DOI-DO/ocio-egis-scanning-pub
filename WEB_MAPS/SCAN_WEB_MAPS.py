#!/usr/bin/env python3
"""Scan AGOL or Portal for web maps with broken layers and email a report.

The scan keeps the historical web-map/layer classifications and output columns.
Catalog discovery is organization-scoped by default, strictly paged, and fails
closed rather than silently omitting failed search buckets or unresolved items.
Layer probes remain anonymous-first; the organization token is sent only by
POST to an exact HTTPS Portal/federation scope or an exact ArcGIS Online
service/item-data path on Esri-controlled *.arcgis.com infrastructure. The
security-critical layer URL probing implementation is contained in this file;
no additional project helper module is required.

Common usage:
    python SCAN_WEB_MAPS.py --config org1.config.toml --target portal
    python SCAN_WEB_MAPS.py --config org2.config.toml --target agol --no-test --workers 24
    python SCAN_WEB_MAPS.py --config org1.config.toml --target agol --no-test --all-owners
    python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test \
        --exclude-group-id abc123,def456
    python SCAN_WEB_MAPS.py --config org1.config.toml --target portal \
        --tls-mode os-trust-store
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import socket
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock, local
from typing import Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidProxyURL
from requests.utils import prepend_scheme_if_needed, select_proxy
from urllib3.util import parse_url


def _locate_project_root():
    """Locate this project's real UTILITIES package without importing it."""
    probe = os.path.dirname(os.path.abspath(__file__))
    for _ in range(7):
        utilities_dir = os.path.join(probe, "UTILITIES")
        init_path = os.path.join(utilities_dir, "__init__.py")
        if os.path.isdir(utilities_dir) and os.path.isfile(init_path):
            return os.path.realpath(probe), os.path.realpath(utilities_dir)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    raise ImportError(
        "Could not locate this project's UTILITIES directory in the script "
        "directory or its first six parent directories."
    )


_PROJECT_ROOT, _EXPECTED_UTILITIES_DIR = _locate_project_root()
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_UTILITIES_SPEC = importlib.util.find_spec("UTILITIES")
_UTILITIES_ORIGIN = (
    os.path.normcase(os.path.realpath(_UTILITIES_SPEC.origin))
    if _UTILITIES_SPEC is not None and _UTILITIES_SPEC.origin else ""
)
_EXPECTED_UTILITIES_INIT = os.path.normcase(os.path.realpath(
    os.path.join(_EXPECTED_UTILITIES_DIR, "__init__.py")
))
if _UTILITIES_ORIGIN != _EXPECTED_UTILITIES_INIT:
    raise ImportError(
        "Refusing to import UTILITIES from an unexpected location: "
        f"{_UTILITIES_ORIGIN or '(not found)'}; expected "
        f"{_EXPECTED_UTILITIES_INIT}."
    )

from UTILITIES import (  # noqa: E402
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
    filter_excluded,
    finalize_config,
    gather_attachments,
    get_max_email_mb,
    get_org_id,
    get_platform,
    provider_for_gis,
    redact_token,
    resolve_excluded_item_ids,
    send_email,
    setup_logging,
    start_timer,
    subject_line,
    timing_block,
    tune_gis_session,
    verify_tls_handshake,
    write_dict_csv,
)

# ============================================================================
# Web-map layer URL parsing and safe probing
#
# Kept local by deployment policy: SCAN_WEB_MAPS.py must remain a single
# deployable Python file. Generic connection, TLS, authentication, logging,
# configuration, exclusion, email, and report helpers continue to come from
# the existing UTILITIES package.
# ============================================================================

AUTH_FAILURE_CODES = frozenset({401, 403, 498, 499})
UNTRUSTED_WEB_PORTS = frozenset({80, 443})
MAX_REDIRECTS = 4
MAX_LAYER_URL_LENGTH = 8192
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_LAYER_REFERENCES_PER_MAP = 100_000
MAX_LAYER_TREE_DEPTH = 128
DEFAULT_PROBE_WORKERS = 24

_ENTERPRISE_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

_SERVICE_TYPE_RE = re.compile(
    r"/(?:FeatureServer|MapServer|ImageServer|SceneServer|"
    r"VectorTileServer|StreamServer|GeoDataServer|GlobeServer|GPServer)"
    r"(?=/|$)",
    re.IGNORECASE,
)
_LOCAL_SECRET_RE = re.compile(
    r"(?i)(\b(?:token|access[_-]?token|refresh[_-]?token|sig|sas|"
    r"password|passwd|pwd|client[_-]?secret|api[_-]?key)\s*=\s*)"
    r"[^&\s'\"#]+"
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@:\s]+:)[^/@\s]*@"
)

class _URLPolicyError(ValueError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class _Origin:
    scheme: str
    host: str
    port: int


@dataclass(frozen=True)
class _NormalizedURL:
    url: str
    scheme: str
    host: str
    port: int
    path: str
    policy_path: str
    origin: _Origin


@dataclass(frozen=True)
class _LayerReference:
    section: str
    title: str
    url: str
    item_id: str = ""
    precheck_error: Optional[str] = None


@dataclass(frozen=True)
class _TrustedScope:
    origin: _Origin
    path_prefix: str
    label: str


@dataclass(frozen=True)
class _TrustedServiceRoot:
    origin: _Origin
    path: str
    label: str


@dataclass(frozen=True)
class _TrustPolicy:
    home_origins: FrozenSet[_Origin]
    private_scopes: Tuple[_TrustedScope, ...]
    auth_scopes: Tuple[_TrustedScope, ...]
    auth_service_roots: Tuple[_TrustedServiceRoot, ...]
    auth_origins: FrozenSet[_Origin] = frozenset()
    allowed_private_hosts: FrozenSet[str] = frozenset()
    referer: str = ""

    @classmethod
    def empty(cls):
        return cls(
            frozenset(), tuple(), tuple(), tuple(), frozenset(),
            frozenset(), ""
        )

    @staticmethod
    def _scope_matches(target, scope):
        if target.origin != scope.origin:
            return False
        base = scope.path_prefix.rstrip("/") or "/"
        return (
            target.policy_path == base
            or base == "/"
            or target.policy_path.startswith(base + "/")
        )

    def private_addresses_allowed(self, target):
        if target.host.casefold() in self.allowed_private_hosts:
            return True
        return any(self._scope_matches(target, scope)
                   for scope in self.private_scopes)

    def auth_label(self, target):
        if target.scheme != "https":
            return None

        if target.origin in self.auth_origins:
            return "explicitly approved HTTPS origin"

        for root in self.auth_service_roots:
            if target.origin != root.origin:
                continue
            if (target.policy_path == root.path
                    or target.policy_path.startswith(root.path + "/")):
                return root.label

        for scope in self.auth_scopes:
            if self._scope_matches(target, scope):
                return scope.label
        return None

    def agol_redirect_label(self, previous, target):
        """Allow a same-resource redirect between HTTPS *.arcgis.com hosts."""
        if (previous.scheme != "https" or previous.port != 443
                or target.scheme != "https" or target.port != 443):
            return None
        if (not _is_arcgis_online_host(previous.host)
                or not _is_arcgis_online_host(target.host)):
            return None

        # AGOL item resources can legitimately move between Esri hostnames
        # during a REST request (for example an org maps host and
        # www.arcgis.com). The redirect is trusted only when the resource is
        # still the SAME 32-hex item. This is more tolerant than requiring the
        # exact /data path while remaining much narrower than trusting arbitrary
        # *.arcgis.com redirects.
        def item_id(path):
            match = re.match(
                r"^/sharing/rest/content/items/([0-9a-f]{32})(?:/|$)",
                path,
                re.IGNORECASE,
            )
            return match.group(1).lower() if match else None

        previous_item = item_id(previous.policy_path)
        target_item = item_id(target.policy_path)
        if previous_item and target_item and previous_item == target_item:
            return "same AGOL item resource redirect on *.arcgis.com"

        previous_root = _service_root(previous)
        target_root = _service_root(target)
        if (previous_root is not None and target_root is not None
                and previous_root.policy_path == target_root.policy_path):
            return "same AGOL service root on *.arcgis.com"
        return None


class _TrustPolicyBuilder:
    def __init__(self):
        self.home_origins = set()
        self.private_scopes = {}
        self.auth_scopes = {}
        self.auth_service_roots = {}
        self.auth_origins = set()
        self.allowed_private_hosts = set()
        self.referer = ""

    def _add_private_scope(self, origin, path_prefix, label):
        path = path_prefix.rstrip("/") or "/"
        key = (origin, path)
        self.private_scopes[key] = _TrustedScope(origin, path, label)

    def add_private_host(self, host, label):
        try:
            normalized_host = _normalize_hostname(host)
        except _URLPolicyError:
            return False
        if not normalized_host:
            return False
        self.allowed_private_hosts.add(normalized_host.casefold())
        return True

    def add_home(self, url, label="authenticated home Portal"):
        try:
            target = _normalize_layer_url(url)
        except _URLPolicyError:
            return False
        self.home_origins.add(target.origin)
        app_path = target.policy_path.rstrip("/") or "/"
        if not self.referer:
            self.referer = _portal_application_root(target.url)

        proxy_prefix = (
            app_path.rstrip("/") + "/sharing/servers/"
            if app_path != "/" else "/sharing/servers/"
        )
        key = (target.origin, proxy_prefix.rstrip("/"))
        scope = _TrustedScope(
            target.origin, proxy_prefix, label + " sharing proxy"
        )
        self.auth_scopes[key] = scope
        # Portal itself does not host map services. Permit private-address layer
        # probes only under its registered sharing proxy, not every path on the
        # Portal web origin.
        self.private_scopes[key] = scope
        return True

    def add_federated_server(self, url, label):
        try:
            target = _normalize_layer_url(url)
        except _URLPolicyError:
            return False

        path = target.policy_path.rstrip("/")
        marker = "/rest/services"
        marker_match = re.search(r"/rest/services(?:/|$)", path)
        if marker_match:
            path = path[:marker_match.start() + len(marker)]
        elif path.endswith("/admin"):
            path = path[:-len("/admin")] + marker
        elif path.endswith("/rest"):
            path = path + "/services"
        else:
            path = path + marker
        path = path.rstrip("/") or "/"

        key = (target.origin, path)
        scope = _TrustedScope(target.origin, path, label)
        self.auth_scopes[key] = scope
        self.private_scopes[key] = scope
        return True

    def add_auth_origin(self, target, label):
        self.auth_origins.add(target.origin)
        return True

    def add_service_root(self, target, label, allow_private=True):
        key = (target.origin, target.policy_path)
        self.auth_service_roots[key] = _TrustedServiceRoot(
            target.origin, target.policy_path, label
        )
        if allow_private:
            self._add_private_scope(target.origin, target.policy_path, label)
        return True

    def freeze(self):
        return _TrustPolicy(
            home_origins=frozenset(self.home_origins),
            private_scopes=tuple(self.private_scopes.values()),
            auth_scopes=tuple(self.auth_scopes.values()),
            auth_service_roots=tuple(self.auth_service_roots.values()),
            auth_origins=frozenset(self.auth_origins),
            allowed_private_hosts=frozenset(self.allowed_private_hosts),
            referer=self.referer,
        )


def _safe_text(value):
    try:
        text = redact_token(str(value))
        text = _URL_USERINFO_RE.sub(r"\1<REDACTED>@", text)
        return _LOCAL_SECRET_RE.sub(r"\1<REDACTED>", text)
    except Exception:
        return "<diagnostic suppressed because safe redaction failed>"


def _safe_text_with_secret(value, secret):
    try:
        text = str(value)
        if secret not in (None, ""):
            text = text.replace(str(secret), "<REDACTED>")
        return _safe_text(text)
    except Exception:
        return "<diagnostic suppressed because safe redaction failed>"


def _safe_url_for_display(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host + (f":{port}" if port else "")
        if parsed.username is not None or parsed.password is not None:
            netloc = "<credentials-redacted>@" + netloc
        safe = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return _safe_text(safe)
    except Exception:
        head = raw.split("#", 1)[0].split("?", 1)[0]
        return _safe_text(head)


def _normalize_hostname(host):
    host = (host or "").strip().rstrip(".")
    if not host:
        raise _URLPolicyError("BLOCKED_UNSAFE_URL", "URL has no hostname")
    if "%" in host:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "IPv6 zone identifiers not permitted"
        )
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        try:
            normalized = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise _URLPolicyError(
                "BLOCKED_UNSAFE_URL", "hostname is not valid IDNA text"
            ) from exc
        labels = normalized.split(".")
        labels_valid = all(
            label
            and len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            and re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        )
        if not normalized or len(normalized) > 253 or not labels_valid:
            raise _URLPolicyError(
                "BLOCKED_UNSAFE_URL", "hostname is malformed or too long"
            )
        return normalized


def _policy_path(path):
    decoded = path or "/"
    for _ in range(8):
        newer = unquote(decoded)
        if newer == decoded:
            break
        decoded = newer
    else:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "URL path is excessively encoded"
        )

    if "\\" in decoded or any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "URL path contains prohibited character"
        )
    if _LOCAL_SECRET_RE.search(decoded):
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "URL path contains credential-like data"
        )
    segments = decoded.split("/")
    for segment in segments:
        routing_segment = segment.split(";", 1)[0]
        if routing_segment in (".", ".."):
            raise _URLPolicyError(
                "BLOCKED_UNSAFE_URL", "URL path contains a dot-segment"
            )
    return re.sub(r"/+", "/", decoded).rstrip("/").lower() or "/"


def _format_netloc(host, port, scheme):
    host_text = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return host_text if port == default else f"{host_text}:{port}"


def _normalize_layer_url(value):
    raw = str(value or "").strip()
    if not raw:
        raise _URLPolicyError("BLOCKED_UNSAFE_URL", "layer URL is blank")
    if len(raw) > MAX_LAYER_URL_LENGTH:
        raise _URLPolicyError("BLOCKED_UNSAFE_URL", "layer URL is too long")
    # Literal spaces in ArcGIS service names are common in legacy web-map URLs.
    # Encode ordinary spaces as %20; continue rejecting tabs/newlines and other
    # control characters that can alter request parsing.
    if any((ord(ch) < 32 or ord(ch) == 127) for ch in raw):
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "layer URL contains control characters"
        )

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", f"layer URL could not be parsed: {_safe_text(exc)}"
        ) from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "only http:// and https:// URLs permitted"
        )
    if parsed.username is not None or parsed.password is not None:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", "URL-embedded usernames/passwords not permitted"
        )

    try:
        parsed_host = parsed.hostname
    except ValueError as exc:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", f"URL contains invalid hostname: {_safe_text(exc)}"
        ) from exc
    host = _normalize_hostname(parsed_host)
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL", f"URL contains invalid port: {_safe_text(exc)}"
        ) from exc
    if not 1 <= port <= 65535:
        raise _URLPolicyError("BLOCKED_UNSAFE_URL", "URL port is out of range")

    path = (parsed.path or "/").replace(" ", "%20")
    if _LOCAL_SECRET_RE.search(path):
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL",
            "URL path contains credential-like data",
        )
    canonical_path = _policy_path(path)
    netloc = _format_netloc(host, port, scheme)

    safe_url = urlunsplit((scheme, netloc, path, "", ""))
    return _NormalizedURL(
        url=safe_url,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        policy_path=canonical_path,
        origin=_Origin(scheme, host, port),
    )


def _service_root(target):
    match = _SERVICE_TYPE_RE.search(target.path)
    if not match:
        return None
    root_path = target.path[:match.end()]
    try:
        return _normalize_layer_url(
            urlunsplit((
                target.scheme,
                _format_netloc(target.host, target.port, target.scheme),
                root_path,
                "",
                "",
            ))
        )
    except _URLPolicyError:
        return None


def _is_arcgis_online_host(host):
    normalized = str(host or "").strip().lower().rstrip(".")
    return normalized.endswith(_AGOL_HOST_SUFFIX)


def _canonical_agol_https_target(target):
    """Return the safe HTTPS equivalent for a default-port AGOL URL.

    ArcGIS Online web maps created years ago commonly retain http:// service
    URLs.  Anonymous probing may redirect those URLs to HTTPS, but trust must be
    registered against the HTTPS destination before an organization token can
    be sent.  Non-default ports are intentionally not rewritten.
    """
    if not _is_arcgis_online_host(target.host):
        return None
    if target.scheme == "https":
        if target.port != 443:
            return None
    elif target.scheme == "http":
        if target.port != 80:
            return None
    else:
        return None

    try:
        return _normalize_layer_url(urlunsplit((
            "https",
            _format_netloc(target.host, 443, "https"),
            target.path,
            "",
            "",
        )))
    except _URLPolicyError:
        return None


def _agol_authenticated_scope_for_reference(target):
    """Return one exact AGOL HTTPS path that may receive the org token.

    Only Esri-controlled ``*.arcgis.com`` destinations are considered.  A
    normal ArcGIS REST service reference is reduced to its exact service root;
    an AGOL item-data reference is accepted only when its path contains a valid
    32-hex item id.  External domains remain anonymous-only.
    """
    canonical = _canonical_agol_https_target(target)
    if canonical is None:
        return None, None

    root = _service_root(canonical)
    if (root is not None
            and "/rest/services/" in root.policy_path):
        return root, "service"

    if _AGOL_ITEM_DATA_PATH_RE.fullmatch(canonical.policy_path):
        return canonical, "item-data"
    return None, None


def _is_ignored_resource_media_jpeg(raw_url):
    """Return True for local web-map resource media JPEG references."""
    text = str(raw_url or "").strip().replace("\\", "/")
    lowered = text.casefold()
    marker = "./resources/media/"
    if marker not in lowered:
        return False
    resource_name = lowered.rsplit("/", 1)[-1]
    return resource_name.endswith(".jpeg") and resource_name != ".jpeg"


def _prepare_layer_reference(section, title, raw_url, item_id=""):
    display = _safe_url_for_display(raw_url)
    if _is_ignored_resource_media_jpeg(raw_url):
        return _LayerReference(
            section=section,
            title=str(title),
            url=display,
            item_id=str(item_id or "").strip(),
            precheck_error=(
                "IGNORED_RESOURCE_MEDIA_JPEG: local ./resources/media/*.jpeg "
                "reference is not a service URL"
            ),
        )
    try:
        target = _normalize_layer_url(raw_url)
        return _LayerReference(
            section=section,
            title=str(title),
            url=target.url,
            item_id=str(item_id or "").strip(),
        )
    except _URLPolicyError as exc:
        return _LayerReference(
            section=section,
            title=str(title),
            url=display,
            item_id=str(item_id or "").strip(),
            precheck_error=f"{exc.status}: {exc.detail}",
        )


def _collect_layer_references(wm_data):
    """Return operational/basemap URL references in historical depth-first order.

    The iterative walk preserves the original ordering while bounding nesting and
    reference count so a malformed web-map document cannot exhaust recursion or
    memory indefinitely.
    """
    found = []

    def walk(root_layers, section):
        stack = [
            (layer, "", 1)
            for layer in reversed(list(root_layers or []))
            if isinstance(layer, dict)
        ]
        while stack:
            layer, inherited_item_id, depth = stack.pop()
            if depth > MAX_LAYER_TREE_DEPTH:
                raise ValueError(
                    f"web map layer nesting exceeded {MAX_LAYER_TREE_DEPTH} levels"
                )
            title = layer.get("title") or layer.get("id") or "Untitled Layer"
            item_id = (
                layer.get("itemId")
                or layer.get("itemID")
                or layer.get("serviceItemId")
                or inherited_item_id
                or ""
            )
            url = layer.get("url")
            if url:
                found.append(_prepare_layer_reference(
                    section, title, url, item_id=item_id,
                ))
                if len(found) > MAX_LAYER_REFERENCES_PER_MAP:
                    raise ValueError(
                        "web map contained more than "
                        f"{MAX_LAYER_REFERENCES_PER_MAP:,} layer URL references"
                    )
            children = layer.get("layers") or []
            for child in reversed(children):
                if isinstance(child, dict):
                    stack.append((child, item_id, depth + 1))

    walk(wm_data.get("operationalLayers"), "operationalLayer")
    basemap = wm_data.get("baseMap") or {}
    walk(basemap.get("baseMapLayers"), "baseMapLayer")
    return found


def _portal_application_root(url):
    target = _normalize_layer_url(url)
    path = target.path.rstrip("/")
    lower = path.lower()
    for suffix in ("/sharing/rest", "/sharing", "/home"):
        if lower.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((
        target.scheme,
        _format_netloc(target.host, target.port, target.scheme),
        path,
        "",
        "",
    )).rstrip("/")





def _is_enterprise_private(address):
    return any(address in network for network in _ENTERPRISE_PRIVATE_NETWORKS)


def _resolve_and_validate_target(target, policy, *, authenticated=False):
    explicit_private_allowed = policy.private_addresses_allowed(target)

    # Anonymous web-map probes are intentionally allowed to follow public URL
    # names that resolve to RFC1918/private space. In environments with split
    # DNS, a public service name can legitimately resolve to an internal address.
    # This does NOT grant the address an authentication scope: organization
    # credentials are still withheld unless the target is separately approved by
    # the trust policy. Loopback/link-local/multicast/reserved addresses remain
    # prohibited.
    anonymous_private_web = (
        not authenticated and target.port in UNTRUSTED_WEB_PORTS
    )
    private_allowed = explicit_private_allowed or anonymous_private_web

    if not private_allowed and target.port not in UNTRUSTED_WEB_PORTS:
        raise _URLPolicyError(
            "BLOCKED_UNSAFE_URL",
            f"untrusted URL uses non-web port {target.port}",
        )

    try:
        literal = ipaddress.ip_address(target.host)
        raw_addresses = [literal.compressed]
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                target.host, target.port, type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise _URLPolicyError(
                "BROKEN_DNS", f"DNS resolution failed for {target.host}: {_safe_text(exc)}"
            ) from exc
        raw_addresses = [info[4][0].split("%", 1)[0] for info in infos]

    addresses = []
    seen = set()
    for raw in raw_addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    if not addresses:
        raise _URLPolicyError(
            "BROKEN_DNS", f"DNS returned no usable address for {target.host}"
        )

    prohibited = []
    for address in addresses:
        always_blocked = (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        )
        if always_blocked:
            prohibited.append(address)
            continue
        if explicit_private_allowed:
            if not address.is_global and not _is_enterprise_private(address):
                prohibited.append(address)
        elif anonymous_private_web:
            # Anonymous probe only: permit a non-global web destination because
            # split-horizon/public service DNS can resolve to an internal address.
            # The always_blocked checks above still reject loopback/link-local/etc.
            pass
        elif not address.is_global:
            prohibited.append(address)

    if prohibited:
        rendered = ", ".join(str(value) for value in prohibited)
        reason = (
            "resolved to prohibited address(es) " + rendered
            if len(prohibited) == len(addresses)
            else "returned mixed safe/prohibited DNS answer including " + rendered
        )
        raise _URLPolicyError("BLOCKED_UNSAFE_ADDRESS", reason)

    return tuple(address.compressed for address in addresses)


class _PinnedDNSAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._local = local()

    def send(self, request, *args, **kwargs):
        self._local.original_hostname = getattr(
            request, "_safe_original_hostname", None
        )
        try:
            return super().send(request, *args, **kwargs)
        finally:
            self._local.original_hostname = None

    def get_connection_with_tls_context(self, request, verify, proxies=None,
                                        cert=None):
        original_hostname = getattr(request, "_safe_original_hostname", None)
        if not original_hostname:
            return super().get_connection_with_tls_context(
                request, verify, proxies=proxies, cert=cert
            )

        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert
        )
        if host_params.get("scheme") == "https":
            pool_kwargs["assert_hostname"] = original_hostname
            pool_kwargs["server_hostname"] = original_hostname

        proxy = select_proxy(request.url, proxies)
        if proxy:
            proxy = prepend_scheme_if_needed(proxy, "http")
            proxy_url = parse_url(proxy)
            if not proxy_url.host:
                raise InvalidProxyURL(
                    "Configured proxy URL is malformed or has no hostname."
                )
            manager = self.proxy_manager_for(proxy)
        else:
            manager = self.poolmanager
        return manager.connection_from_host(
            **host_params, pool_kwargs=pool_kwargs
        )

    def get_connection(self, url, proxies=None):
        hostname = getattr(self._local, "original_hostname", None)
        if not hostname:
            return super().get_connection(url, proxies=proxies)

        parsed_target = parse_url(url)
        pool_kwargs = {}
        if parsed_target.scheme == "https":
            pool_kwargs["assert_hostname"] = hostname
            pool_kwargs["server_hostname"] = hostname

        proxy = select_proxy(url, proxies)
        if proxy:
            proxy = prepend_scheme_if_needed(proxy, "http")
            proxy_url = parse_url(proxy)
            if not proxy_url.host:
                raise InvalidProxyURL(
                    "Configured proxy URL is malformed or has no hostname."
                )
            manager = self.proxy_manager_for(proxy)
        else:
            manager = self.poolmanager
        return manager.connection_from_host(
            scheme=parsed_target.scheme,
            host=parsed_target.host,
            port=parsed_target.port,
            pool_kwargs=pool_kwargs,
        )


def _build_probe_session(base_session, pool_size):
    if getattr(base_session, "verify", True) is False:
        raise RuntimeError(
            "Refusing to construct layer probes from an unverified TLS session."
        )

    probe_session = requests.Session()
    probe_session.verify = getattr(base_session, "verify", True)
    probe_session.cert = getattr(base_session, "cert", None)
    probe_session.trust_env = getattr(base_session, "trust_env", True)
    base_headers = getattr(base_session, "headers", {}) or {}
    if base_headers.get("User-Agent"):
        probe_session.headers["User-Agent"] = base_headers["User-Agent"]
    probe_session.proxies.update(getattr(base_session, "proxies", {}))

    base_adapter = base_session.adapters.get("https://")
    max_retries = getattr(base_adapter, "max_retries", 0)
    adapter_kwargs = {
        "pool_connections": max(2, min(int(pool_size), 8)),
        "pool_maxsize": max(2, min(int(pool_size), 8)),
        "max_retries": max_retries,
    }
    probe_session.mount("https://", _PinnedDNSAdapter(**adapter_kwargs))
    probe_session.mount("http://", _PinnedDNSAdapter(**adapter_kwargs))
    return probe_session


class _ProbeSessionPool:
    """Allocate one cookie-isolated requests session per worker thread."""

    def __init__(self, base_session, pool_size):
        self._base_session = base_session
        self._pool_size = pool_size
        self._local = local()
        self._lock = Lock()
        self._sessions = []

    def get(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        with self._lock:
            session = _build_probe_session(self._base_session, self._pool_size)
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self):
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


class _ResponseTooLarge(RuntimeError):
    pass


def _read_limited_json(response, limit=MAX_RESPONSE_BYTES):
    length = response.headers.get("Content-Length")
    if length:
        try:
            declared = int(length)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > limit:
            raise _ResponseTooLarge(
                f"response declared {declared:,} bytes; limit is {limit:,}"
            )

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode(response.encoding or "utf-8", errors="replace")
        total += len(chunk)
        if total > limit:
            raise _ResponseTooLarge(
                f"response exceeded {limit:,} decoded bytes"
            )
        chunks.append(chunk)
    payload = b"".join(chunks)
    try:
        text = payload.decode(response.encoding or "utf-8-sig")
    except (LookupError, UnicodeDecodeError):
        text = payload.decode("utf-8-sig", errors="replace")
    try:
        return json.loads(text), ""
    except ValueError as exc:
        snippet = text[:160].replace("\r", " ").replace("\n", " ")
        return None, _safe_text(
            f"non-JSON response ({type(exc).__name__}): {snippet!r}"
        )


def _token_fingerprint(value):
    if value in (None, ""):
        return None
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True)
class _ProbeOutcome:
    status: str
    detail: str
    final_url: str
    auth_code: Optional[int] = None
    token_fingerprint: Optional[str] = None


class _SafeLayerProber:
    def __init__(self, base_session, token_provider, policy, timeout,
                 pool_size=DEFAULT_PROBE_WORKERS):
        self.sessions = _ProbeSessionPool(base_session, pool_size)
        self.token_provider = token_provider
        self.policy = policy
        self.timeout = timeout
        self._refresh_lock = Lock()

    def close(self):
        self.sessions.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def _send_pinned(self, target, pinned_ip, authenticated):
        session = self.sessions.get()
        try:
            session.cookies.clear()
        except Exception:
            pass

        method = "GET"
        request_kwargs = {"params": {"f": "json"}}
        token_fp = None
        if authenticated:
            method = "POST"
            token_used = self.token_provider.get()
            token_fp = _token_fingerprint(token_used)
            request_kwargs = {"data": {"f": "json", "token": token_used}}

        request = requests.Request(
            method,
            target.url,
            headers={
                "Accept": "application/json",
                **({"Referer": self.policy.referer}
                   if authenticated and self.policy.referer else {}),
            },
            **request_kwargs,
        )
        prepared = session.prepare_request(request)
        prepared._safe_original_hostname = target.host
        original_safe_url = target.url

        parsed = urlsplit(prepared.url)
        pinned_netloc = _format_netloc(pinned_ip, target.port, target.scheme)
        prepared.url = urlunsplit((
            parsed.scheme, pinned_netloc, parsed.path, parsed.query, ""
        ))
        prepared.headers["Host"] = _format_netloc(
            target.host, target.port, target.scheme
        )

        settings = session.merge_environment_settings(
            original_safe_url, {}, True, session.verify, session.cert
        )
        # Environment proxy selection is retained, but REQUESTS_CA_BUNDLE and
        # CURL_CA_BUNDLE must not override the explicitly selected TLS policy.
        settings["verify"] = session.verify
        settings["cert"] = session.cert
        settings["stream"] = True
        try:
            response = session.send(
                prepared,
                timeout=self.timeout,
                allow_redirects=False,
                **settings,
            )
        except Exception:
            prepared.body = None
            raise
        finally:
            try:
                session.cookies.clear()
            except Exception:
                pass

        response.url = original_safe_url
        if response.request is not None:
            response.request.url = original_safe_url
            response.request.body = None
        prepared.body = None
        return response, token_fp, (token_used if authenticated else None)

    @staticmethod
    def _rest_error(data, exact_secret=None):
        if not isinstance(data, dict):
            return None, None
        if "error" not in data:
            if str(data.get("status", "")).strip().lower() == "error":
                return None, _safe_text_with_secret(
                    f"ArcGIS endpoint returned status=error: {data.get('messages')!r}",
                    exact_secret,
                )
            return None, None
        err = data.get("error")
        if not isinstance(err, dict):
            return None, _safe_text_with_secret(
                f"REST error: {err!r}", exact_secret
            )
        try:
            code = int(err.get("code"))
        except (TypeError, ValueError):
            code = None
        message = err.get("message") or "ArcGIS REST error"
        details_value = err.get("details") or []
        if isinstance(details_value, list):
            details = "; ".join(str(value) for value in details_value)
        else:
            details = str(details_value)
        text = f"REST error {code if code is not None else '?'}: {message}"
        if details:
            text += f" ({details})"
        return code, _safe_text_with_secret(text, exact_secret)

    def _request_chain(self, start_url, authenticated):
        try:
            current = _normalize_layer_url(start_url)
        except _URLPolicyError as exc:
            return _ProbeOutcome(exc.status, exc.detail,
                                 _safe_url_for_display(start_url))

        for hop in range(MAX_REDIRECTS + 1):
            if authenticated and not self.policy.auth_label(current):
                return _ProbeOutcome(
                    "BLOCKED_REDIRECT" if hop else "UNVERIFIABLE_AUTH",
                    "authenticated request target not approved HTTPS scope",
                    current.url,
                )

            try:
                pinned_addresses = _resolve_and_validate_target(
                    current, self.policy, authenticated=authenticated
                )
            except _URLPolicyError as exc:
                return _ProbeOutcome(exc.status, exc.detail, current.url)

            response = None
            response_token_fp = None
            response_token_value = None
            last_connect_error = None
            for pinned_ip in pinned_addresses:
                try:
                    response, response_token_fp, response_token_value = self._send_pinned(
                        current, pinned_ip, authenticated=authenticated
                    )
                    break
                except requests.exceptions.SSLError as exc:
                    return _ProbeOutcome(
                        "BROKEN_TLS", _safe_text(exc), current.url
                    )
                except requests.exceptions.Timeout:
                    last_connect_error = _ProbeOutcome(
                        "BROKEN_TIMEOUT", f"timeout after {self.timeout}s", current.url
                    )
                except requests.exceptions.ConnectionError as exc:
                    last_connect_error = _ProbeOutcome(
                        "BROKEN_CONNECTION", _safe_text(exc), current.url
                    )
                except requests.exceptions.RequestException as exc:
                    return _ProbeOutcome(
                        "BROKEN_REQUEST",
                        f"{type(exc).__name__}: {_safe_text(exc)}",
                        current.url,
                    )
            if response is None:
                return last_connect_error or _ProbeOutcome(
                    "BROKEN_CONNECTION", "all validated addresses failed", current.url
                )

            try:
                status = response.status_code
                if 300 <= status < 400:
                    location = response.headers.get("Location")
                    if not location:
                        return _ProbeOutcome(
                            "BROKEN_HTTP",
                            f"HTTP {status} redirect without Location header",
                            current.url,
                        )
                    if hop >= MAX_REDIRECTS:
                        return _ProbeOutcome(
                            "BLOCKED_REDIRECT",
                            f"redirect limit of {MAX_REDIRECTS} exceeded",
                            current.url,
                        )
                    next_value = urljoin(current.url, location)
                    try:
                        next_target = _normalize_layer_url(next_value)
                    except _URLPolicyError as exc:
                        return _ProbeOutcome("BLOCKED_REDIRECT", exc.detail, current.url)
                    if authenticated and not self.policy.auth_label(next_target):
                        redirect_label = self.policy.agol_redirect_label(current, next_target)
                        if redirect_label is None:
                            return _ProbeOutcome(
                                "BLOCKED_REDIRECT",
                                "authenticated redirect left approved scope",
                                next_target.url,
                            )
                    current = next_target
                    continue

                if status in AUTH_FAILURE_CODES:
                    return _ProbeOutcome(
                        "AUTH_REQUIRED",
                        _safe_text_with_secret(
                            f"HTTP {status} {response.reason or ''}".strip(),
                            response_token_value,
                        ),
                        current.url,
                        auth_code=status,
                        token_fingerprint=response_token_fp,
                    )
                if status != 200:
                    return _ProbeOutcome(
                        "BROKEN_HTTP",
                        _safe_text_with_secret(
                            f"HTTP {status} {response.reason or ''}".strip(),
                            response_token_value,
                        ),
                        current.url,
                    )

                try:
                    data, parse_error = _read_limited_json(response)
                except _ResponseTooLarge as exc:
                    return _ProbeOutcome(
                        "BROKEN_RESPONSE", _safe_text(exc), current.url
                    )
                if parse_error:
                    safe_parse_error = _safe_text_with_secret(
                        parse_error, response_token_value
                    )
                    # Many legitimate ArcGIS/public layer endpoints return a
                    # non-JSON success body (HTML/text) even though the URL is
                    # reachable. Do not turn the common JSONDecodeError case
                    # into a broken-layer finding. It is intentionally excluded
                    # from the broken report. Other malformed/non-JSON cases
                    # retain the normal BROKEN_RESPONSE classification.
                    if "JSONDecodeError" in str(parse_error):
                        return _ProbeOutcome(
                            "IGNORED_NON_JSON",
                            safe_parse_error,
                            current.url,
                        )
                    return _ProbeOutcome(
                        "BROKEN_RESPONSE",
                        safe_parse_error,
                        current.url,
                    )

                code, error_text = self._rest_error(
                    data, exact_secret=response_token_value
                )
                if error_text:
                    if code in AUTH_FAILURE_CODES:
                        return _ProbeOutcome(
                            "AUTH_REQUIRED", error_text, current.url,
                            auth_code=code,
                            token_fingerprint=response_token_fp,
                        )
                    return _ProbeOutcome(
                        "BROKEN_REST", error_text, current.url, auth_code=code
                    )
                if not isinstance(data, dict):
                    return _ProbeOutcome(
                        "BROKEN_RESPONSE", "JSON response was not an object", current.url
                    )
                # Preserve the historical success rule: any JSON object with no
                # ArcGIS error payload proves the referenced layer URL is readable.
                # Web maps can legitimately reference non-ArcGIS JSON resources
                # (for example GeoJSON), so requiring ArcGIS-specific keys would
                # change successful classifications.
                return _ProbeOutcome("OK", "", current.url)
            finally:
                response_token_value = None
                response.close()

        return _ProbeOutcome(
            "BLOCKED_REDIRECT", "redirect limit exceeded", current.url
        )

    @staticmethod
    def _render(outcome):
        return f"{outcome.status}: {outcome.detail}"

    def _refresh_if_stale(self, failed_fingerprint):
        with self._refresh_lock:
            current = self.token_provider.get()
            if (failed_fingerprint is not None
                    and _token_fingerprint(current) != failed_fingerprint):
                return
            self.token_provider.refresh()

    def probe(self, layer_url):
        anonymous = self._request_chain(layer_url, authenticated=False)
        if anonymous.status == "OK":
            return None
        if anonymous.status != "AUTH_REQUIRED":
            return self._render(anonymous)

        try:
            final_target = _normalize_layer_url(anonymous.final_url)
        except _URLPolicyError as exc:
            return f"{exc.status}: {exc.detail}"

        # A legacy HTTP ArcGIS URL may return 499 without redirecting.  Upgrade
        # only when the exact HTTPS equivalent was already approved by policy;
        # the organization token is never sent over HTTP.
        if final_target.scheme == "http":
            https_target = _canonical_agol_https_target(final_target)
            if (https_target is not None
                    and self.policy.auth_label(https_target)):
                final_target = https_target

        if not self.policy.auth_label(final_target):
            if final_target.scheme != "https":
                reason = "credentials withheld (authenticated probes require HTTPS)"
            else:
                reason = (
                    "credentials withheld (endpoint not an approved "
                    "Portal/federated/AGOL scope)"
                )
            return f"UNVERIFIABLE_AUTH: {anonymous.detail}; {reason}"

        authenticated = self._request_chain(final_target.url, authenticated=True)
        if authenticated.status == "OK":
            return None

        if (authenticated.status == "AUTH_REQUIRED"
                and authenticated.auth_code in (498, 499)):
            try:
                self._refresh_if_stale(authenticated.token_fingerprint)
            except Exception as exc:
                return (
                    "UNVERIFIABLE_AUTH: token refresh failed for trusted "
                    f"endpoint ({_safe_text(exc)})"
                )
            authenticated = self._request_chain(
                final_target.url, authenticated=True
            )
            if authenticated.status == "OK":
                return None

        if authenticated.status == "AUTH_REQUIRED":
            return (
                f"UNVERIFIABLE_AUTH: {authenticated.detail}; authenticated "
                f"retry did not establish access"
            )
        return self._render(authenticated)


EMAIL_SUBJECT_PREFIX = "Web Map Layer Scan"
REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

MAX_ITEMS = 2_000_000
TEST_MODE_ITEM_LIMIT = 250
DEFAULT_WORKERS = 12
DEFAULT_URL_WORKERS = 24
DEFAULT_RESOLVE_WORKERS = 12
MAX_WORKERS = 64
REQUEST_TIMEOUT = 30
URL_PROBE_TIMEOUT = 15
MAX_URL_TIMEOUT = 300
SEARCH_PAGE_SIZE = 100
SEARCH_CAP = 10_000
MAX_IN_FLIGHT_FACTOR = 3

_ITEM_ID_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_AGOL_HOST_SUFFIX = ".arcgis.com"
_AGOL_ITEM_DATA_PATH_RE = re.compile(
    r"^/sharing/rest/content/items/[0-9a-f]{32}/data$",
    re.IGNORECASE,
)
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)

BROKEN_FIELDS = [
    "platform", "title", "item_id", "owner", "date_created", "date_modified",
    "section", "layer_name", "layer_url", "error",
]

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


class ScanAbort(RuntimeError):
    """A controlled failure that invalidates the required scan."""


class UsageError(ValueError):
    """Invalid startup arguments that should return command-line status 2."""


@dataclass
class _UnresolvedWebMap:
    id: str
    title: str
    owner: str
    created: object
    modified: object
    resolution_error: str

    def get_data(self):
        raise RuntimeError(self.resolution_error)


class _WorkerSessionPool:
    """Provide one ordinary verified requests session per worker thread."""

    def __init__(self, base_session, capacity):
        self._base = base_session
        self._capacity = max(1, int(capacity))
        self._local = local()
        self._lock = Lock()
        self._sessions = []

    def _new_session(self):
        session = requests.Session()
        session.verify = getattr(self._base, "verify", True)
        if session.verify is False:
            raise RuntimeError("Refusing to clone an unverified HTTP session.")
        session.cert = getattr(self._base, "cert", None)
        session.trust_env = getattr(self._base, "trust_env", True)
        session.proxies.update(getattr(self._base, "proxies", {}))
        base_headers = getattr(self._base, "headers", {}) or {}
        if base_headers.get("User-Agent"):
            session.headers["User-Agent"] = base_headers["User-Agent"]
        base_adapter = self._base.adapters.get("https://")
        retries = getattr(base_adapter, "max_retries", 0)
        per_thread_pool = max(2, min(self._capacity, 8))
        adapter = HTTPAdapter(
            pool_connections=per_thread_pool,
            pool_maxsize=per_thread_pool,
            max_retries=retries,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def get(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        with self._lock:
            session = self._new_session()
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self):
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass


_PORTAL_TOKEN_REFRESH_LOCK = Lock()


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


def _item_value(item, name, default=""):
    try:
        value = getattr(item, name, default)
    except Exception:
        value = default
    return default if value is None else value


def _item_dates(item):
    created = epoch_to_str(_item_value(item, "created", None),
                           fmt="%Y/%m/%d", utc=True)
    modified = epoch_to_str(_item_value(item, "modified", None),
                            fmt="%Y/%m/%d", utc=True)
    return created, modified


def _safe_org_id(value):
    text = str(value or "").strip()
    if not text or not _ORG_ID_RE.fullmatch(text):
        raise ScanAbort(f"Invalid or unavailable organization id: {text!r}")
    return text


def _refresh_provider_if_stale(token_provider, token_used):
    with _PORTAL_TOKEN_REFRESH_LOCK:
        current = token_provider.get()
        if current == token_used:
            token_provider.refresh()


def _portal_json(session, url, token_provider, params=None,
                 timeout=REQUEST_TIMEOUT):
    """POST to an authoritative Portal endpoint with bounded JSON handling."""
    last_error = None
    for attempt in range(2):
        response = None
        token_used = token_provider.get()
        form = {"f": "json", "token": token_used}
        form.update(params or {})
        try:
            response = session.post(
                url,
                data=form,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                verify=session.verify,
                cert=session.cert,
            )
            status = response.status_code
            if status in (401, 498, 499) and attempt == 0:
                _refresh_provider_if_stale(token_provider, token_used)
                continue
            if 300 <= status < 400:
                raise ScanAbort(
                    f"authoritative Portal endpoint returned unhandled redirect "
                    f"(HTTP {status})"
                )
            try:
                data, parse_error = _read_limited_json(
                    response, limit=MAX_RESPONSE_BYTES
                )
            except Exception as exc:
                raise ScanAbort(f"Portal response could not be read safely: {_safe_text(exc)}") from exc
            if parse_error:
                raise ScanAbort(_safe_text_with_secret(parse_error, token_used))
            if not isinstance(data, dict):
                raise ScanAbort("Portal endpoint returned a non-object JSON response.")

            err = data.get("error")
            if isinstance(err, dict):
                try:
                    code = int(err.get("code"))
                except (TypeError, ValueError):
                    code = None
                message = err.get("message") or "ArcGIS REST error"
                details_value = err.get("details") or []
                if isinstance(details_value, list):
                    details = "; ".join(str(v) for v in details_value)
                else:
                    details = str(details_value)
                last_error = f"REST error {code or '?'}: {message}"
                if details:
                    last_error += f" ({details})"
                last_error = _safe_text_with_secret(last_error, token_used)
                if code in (401, 498, 499) and attempt == 0:
                    _refresh_provider_if_stale(token_provider, token_used)
                    continue
                raise ScanAbort(_safe_text(last_error))
            if status >= 400:
                raise ScanAbort(_safe_text_with_secret(
                    f"HTTP {status} {response.reason or ''}".strip(),
                    token_used,
                ))
            if str(data.get("status", "")).strip().lower() == "error":
                raise ScanAbort(
                    f"Portal endpoint returned status=error: "
                    f"{_safe_text_with_secret(data.get('messages'), token_used)}"
                )
            return data
        except ScanAbort:
            raise
        except requests.RequestException as exc:
            last_error = _safe_text_with_secret(exc, token_used)
            raise ScanAbort(last_error) from exc
        finally:
            form["token"] = None
            token_used = None
            if response is not None:
                if response.request is not None:
                    response.request.body = None
                response.close()
    raise ScanAbort(last_error or "unknown Portal REST error")


def _search_results(data):
    value = data.get("results") if "results" in data else data.get("items", [])
    if not isinstance(value, list):
        raise ScanAbort("Web-map search returned a malformed results list.")
    if any(not isinstance(entry, dict) for entry in value):
        raise ScanAbort("Web-map search returned a non-object result.")
    return value


def _search_total(data):
    value = data.get("total")
    if isinstance(value, bool):
        raise ScanAbort("Web-map search returned a Boolean total.")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"Web-map search returned an invalid total: {value!r}"
        ) from exc
    if total < 0:
        raise ScanAbort("Web-map search returned a negative total.")
    return total


def _search_next_start(data, fallback):
    value = data.get("nextStart")
    if value is None:
        return fallback
    if isinstance(value, bool):
        raise ScanAbort("Web-map search returned a Boolean nextStart.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"Web-map search returned an invalid nextStart: {value!r}"
        ) from exc


def _consume_search_bucket(session, search_url, token_provider, query,
                           org_id, enforce_org, items_by_id, max_items,
                           search_stats):
    params = {
        "q": query,
        "sortField": "modified",
        "sortOrder": "desc",
        "start": 1,
        "num": SEARCH_PAGE_SIZE,
    }
    first = _portal_json(
        session, search_url, token_provider, params=params,
        timeout=REQUEST_TIMEOUT,
    )
    search_stats["requests"] += 1
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
                f"Web-map search total changed during paging "
                f"({total} -> {page_total})."
            )
        page = _search_results(data)
        if not page and received < total:
            raise ScanAbort(
                f"Web-map search returned an empty page after "
                f"{received} of {total} results."
            )
        for result in page:
            item_id = str(result.get("id") or "").strip()
            if not _ITEM_ID_RE.fullmatch(item_id):
                raise ScanAbort(
                    f"Web-map search returned an invalid item id: {item_id!r}."
                )
            if item_id in range_ids:
                raise ScanAbort(
                    f"Web-map search repeated id {item_id!r} within one query."
                )
            range_ids.add(item_id)
            if enforce_org:
                explicit_org = result.get("orgId") or result.get("orgid")
                if (explicit_org not in (None, "")
                        and str(explicit_org).lower() != org_id.lower()):
                    raise ScanAbort(
                        f"Web-map search returned cross-organization item "
                        f"{item_id} (orgId={explicit_org!r}, expected "
                        f"{org_id!r})."
                    )
            is_new = item_id not in items_by_id
            items_by_id[item_id] = result
            if is_new and len(items_by_id) >= max_items:
                if max_items >= MAX_ITEMS:
                    raise ScanAbort(
                        f"Full web-map discovery reached the {MAX_ITEMS:,}-item "
                        "safety ceiling; refusing to write a silently capped report."
                    )
                return False, True
        received += len(page)
        if received > total:
            raise ScanAbort(
                "Web-map search returned more records than its declared total."
            )
        next_start = _search_next_start(
            data, start + len(page) if page else -1
        )
        if next_start <= 0:
            if received != total or len(range_ids) != total:
                raise ScanAbort(
                    f"Web-map search paging mismatch: expected {total}, read "
                    f"{received}, unique {len(range_ids)}."
                )
            break
        if next_start <= start:
            raise ScanAbort("Web-map search pagination did not advance.")
        if next_start > SEARCH_CAP:
            raise ScanAbort("Web-map search pagination exceeded the API cap.")
        start = next_start
        params["start"] = start
        data = _portal_json(
            session, search_url, token_provider, params=params,
            timeout=REQUEST_TIMEOUT,
        )
        search_stats["requests"] += 1
    return False, False


def _enumerate_web_map_dicts(session, portal_root, token_provider, max_items,
                             org_id, org_only, logger):
    clause = 'type:"Web Map"'
    if org_only:
        clause = f"{clause} AND orgid:{org_id}"
        logger.info(
            "Restricting search to org-owned content (orgid:%s).", org_id
        )
    else:
        logger.warning(
            "--all-owners selected: web-map discovery is intentionally not "
            "organization-scoped."
        )

    search_url = portal_root.rstrip("/") + "/sharing/rest/search"
    items_by_id = {}
    search_stats = {"requests": 0}
    stopped = False

    def search_range(lo_ms, hi_ms):
        nonlocal stopped
        if stopped:
            return
        query = f"{clause} AND created:[{lo_ms} TO {hi_ms}]"
        hit_cap, reached_limit = _consume_search_bucket(
            session, search_url, token_provider, query, org_id, org_only,
            items_by_id, max_items, search_stats,
        )
        if reached_limit:
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
                f"A one-millisecond web-map search bucket [{lo_ms} TO {hi_ms}] "
                f"reached the {SEARCH_CAP:,}-item API cap."
            )

    span_end = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=365)
    )
    cursor = _SEARCH_START
    last_logged = 0
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
        if search_stats["requests"] - last_logged >= 20:
            last_logged = search_stats["requests"]
            logger.info(
                "  %d strict web-map search request(s), %d unique item(s) "
                "selected so far.",
                search_stats["requests"], len(items_by_id),
            )

    logger.info(
        "Strict web-map discovery complete: %d request(s), %d unique item(s)%s.",
        search_stats["requests"], len(items_by_id),
        " (test-mode limit reached)" if stopped else "",
    )
    return items_by_id


def _unresolved_item(item_id, metadata, detail):
    return _UnresolvedWebMap(
        id=item_id,
        title=str(metadata.get("title") or item_id),
        owner=str(metadata.get("owner") or ""),
        created=metadata.get("created"),
        modified=metadata.get("modified"),
        resolution_error=_safe_text(detail),
    )


def search_web_maps(gis, base_url, max_items, org_only, resolve_workers, logger,
                    session=None, token_provider=None):
    """Strictly discover and resolve web maps without silently dropping any."""
    portal_root = _portal_application_root(base_url)
    session = session or getattr(getattr(gis, "_con", None), "_session", None)
    if session is None:
        raise ScanAbort("The GIS connection did not expose an HTTP session.")
    token_provider = token_provider or provider_for_gis(
        gis, get_platform(portal_root)
    )

    org_id = str(get_org_id(gis, logger=logger) or "").strip()
    if org_only:
        org_id = _safe_org_id(org_id)
    metadata_by_id = _enumerate_web_map_dicts(
        session, portal_root, token_provider, max_items,
        org_id, bool(org_only), logger,
    )

    entries = list(metadata_by_id.items())
    logger.info(
        "Resolving %d web map item(s) with %d worker(s) ...",
        len(entries), resolve_workers,
    )

    def resolve_entry(entry):
        item_id, metadata = entry
        try:
            item = gis.content.get(item_id)
        except Exception as exc:
            return _unresolved_item(
                item_id, metadata,
                f"failed to resolve item through ArcGIS SDK: {exc}",
            )
        if item is None:
            return _unresolved_item(
                item_id, metadata,
                "ArcGIS SDK returned no item for an id present in search results",
            )
        returned_id = str(_item_value(item, "id", "") or "").strip()
        if returned_id != item_id:
            return _unresolved_item(
                item_id, metadata,
                f"ArcGIS SDK returned item id {returned_id!r} instead of "
                f"{item_id!r}",
            )
        return item

    resolved_by_id = {}
    failures = 0
    with ThreadPoolExecutor(max_workers=resolve_workers) as executor:
        for entry, future in _bounded_future_results(
                executor, entries, resolve_entry,
                resolve_workers * MAX_IN_FLIGHT_FACTOR):
            item_id, _metadata = entry
            try:
                item = future.result()
            except Exception as exc:
                item = _unresolved_item(
                    item_id, metadata_by_id[item_id],
                    f"unexpected item-resolution exception: {exc}",
                )
            if isinstance(item, _UnresolvedWebMap):
                failures += 1
                logger.warning(
                    "Could not resolve web map %s: %s",
                    item_id, item.resolution_error,
                )
            resolved_by_id[item_id] = item

    items = [resolved_by_id[item_id] for item_id in metadata_by_id]
    logger.info(
        "Found %d web map item(s) to scan (%d retained as unreadable after "
        "resolution failure).", len(items), failures,
    )
    return items


def _read_web_map(item):
    date_created, date_modified = _item_dates(item)
    try:
        wm_data = item.get_data()
    except Exception as exc:
        return {
            "item": item,
            "date_created": date_created,
            "date_modified": date_modified,
            "unreadable": _safe_text(f"failed to read web map data: {exc}"),
            "layers": [],
        }

    if not isinstance(wm_data, dict) or not wm_data:
        return {
            "item": item,
            "date_created": date_created,
            "date_modified": date_modified,
            "unreadable": "web map returned no JSON data",
            "layers": [],
        }
    try:
        layers = _collect_layer_references(wm_data)
    except Exception as exc:
        return {
            "item": item,
            "date_created": date_created,
            "date_modified": date_modified,
            "unreadable": _safe_text(
                f"failed to parse web map layer references: {exc}"
            ),
            "layers": [],
        }
    return {
        "item": item,
        "date_created": date_created,
        "date_modified": date_modified,
        "unreadable": None,
        "layers": layers,
    }


def _home_url_candidates(gis, base_url):
    candidates = [base_url, getattr(gis, "url", None)]
    portal_obj = getattr(gis, "_portal", None)
    candidates.append(getattr(portal_obj, "url", None))
    out = []
    seen = set()
    for value in candidates:
        if not value:
            continue
        try:
            root = _portal_application_root(value)
        except _URLPolicyError:
            continue
        if root and root not in seen:
            seen.add(root)
            out.append(root)
    return out


def _resolve_agol_service_roots(map_reads, builder, logger):
    """Register exact ArcGIS Online destinations found in the web maps.

    The previous implementation required every layer reference to carry an
    accessible service-item id and then demanded an exact URL match against
    that item's metadata.  Older web maps frequently omit the item id, deleted
    service items can leave a still-valid service URL behind, and legacy HTTP
    references differ from the authoritative HTTPS item URL.  Those conditions
    caused legitimate AGOL layers to be reported as UNVERIFIABLE_AUTH.

    ArcGIS Online tokens are now permitted only for exact service roots or
    exact item-data paths already present in the scanned map, only after the
    destination is canonicalized to HTTPS, and only on Esri-controlled
    ``*.arcgis.com`` infrastructure.  DNS/address validation, redirect checks,
    POST-only token transport, and external-domain withholding remain enforced
    by ``_SafeLayerProber``.
    """
    approved = set()
    service_roots = 0
    item_data_paths = 0

    for map_read in map_reads:
        for ref in map_read.get("layers", []):
            if not isinstance(ref, _LayerReference) or ref.precheck_error:
                continue
            try:
                target = _normalize_layer_url(ref.url)
            except _URLPolicyError:
                continue

            auth_target, target_kind = _agol_authenticated_scope_for_reference(
                target
            )
            if auth_target is None:
                continue

            key = (auth_target.origin, auth_target.policy_path)
            if key in approved:
                continue
            approved.add(key)
            builder.add_service_root(
                auth_target,
                "exact ArcGIS Online "
                + ("service root" if target_kind == "service"
                   else "item-data path"),
                allow_private=False,
            )
            if target_kind == "service":
                service_roots += 1
            else:
                item_data_paths += 1

    # www.arcgis.com is an approved AGOL REST origin. Permit authenticated
    # requests anywhere on that HTTPS origin so normal AGOL redirects do not
    # get mistaken for an unsafe cross-origin redirect. Other *.arcgis.com
    # hosts remain limited to exact service roots or item-data paths discovered
    # in the scanned maps.
    try:
        www_arcgis = _normalize_layer_url("https://www.arcgis.com/")
        builder.add_auth_origin(www_arcgis, "approved AGOL origin https://www.arcgis.com")
        approved_origin = 1
    except _URLPolicyError:
        approved_origin = 0

    logger.info(
        "AGOL trusted-target discovery: %d exact HTTPS service root(s), "
        "%d exact HTTPS item-data path(s), and %d approved HTTPS origin(s); "
        "external destinations remain anonymous-only.",
        service_roots, item_data_paths, approved_origin,
    )
    return []

def _build_trust_policy(gis, session_pool, token_provider, base_url, platform,
                        map_reads, logger, allowed_private_hosts=None):
    builder = _TrustPolicyBuilder()
    incomplete_reasons = []
    home_roots = _home_url_candidates(gis, base_url)
    for root in home_roots:
        builder.add_home(root)

    connection = getattr(gis, "_con", None)
    sdk_referer = (
        getattr(connection, "_referer", None)
        or getattr(connection, "referer", None)
    )
    if not sdk_referer:
        sdk_session = getattr(connection, "_session", None)
        sdk_referer = (
            getattr(sdk_session, "headers", {}).get("Referer")
            if sdk_session is not None else None
        )
    if sdk_referer:
        try:
            builder.referer = _portal_application_root(sdk_referer)
        except _URLPolicyError:
            pass

    if not home_roots:
        raise ScanAbort("Could not establish the authenticated Portal origin.")
    portal_root = home_roots[0]

    for host in (allowed_private_hosts or []):
        if not builder.add_private_host(host, f"operator-approved private layer host: {host}"):
            incomplete_reasons.append(
                f"Configured private layer host {host!r} was invalid and was not allowed."
            )

    if str(platform).upper() == "PORTAL":
        federation_url = f"{portal_root}/portaladmin/federation/servers"
        try:
            data = _portal_json(
                session_pool.get(), federation_url, token_provider
            )
            servers = data.get("servers")
            if not isinstance(servers, list):
                raise ScanAbort(
                    "Portal federation inventory did not contain a server list."
                )
            for index, server in enumerate(servers):
                if not isinstance(server, dict):
                    incomplete_reasons.append(
                        f"Portal federation record {index + 1} was malformed."
                    )
                    continue
                label = (
                    server.get("name")
                    or server.get("serverRole")
                    or f"federated server {index + 1}"
                )
                added = False
                for key in ("url", "adminUrl", "serverUrl"):
                    if server.get(key):
                        if builder.add_federated_server(
                                server[key], f"confirmed {label}"):
                            added = True
                        else:
                            incomplete_reasons.append(
                                f"Portal federation record {label!r} supplied "
                                f"an invalid {key} value."
                            )
                if not added:
                    incomplete_reasons.append(
                        f"Portal federation record {label!r} exposed no URL."
                    )
            logger.info(
                "Trusted-target discovery: read %d federated server record(s).",
                len(servers),
            )
        except Exception as exc:
            incomplete_reasons.append(
                "Could not read Portal federation inventory: "
                + _safe_text(exc)
            )
    elif str(platform).upper() == "AGOL":
        incomplete_reasons.extend(_resolve_agol_service_roots(
            map_reads, builder, logger,
        ))

    policy = builder.freeze()
    logger.info(
        "Layer-probe trust policy: %d home origin(s), %d private path scope(s), "
        "%d federated/proxy auth scope(s), %d exact AGOL HTTPS auth scope(s).",
        len(policy.home_origins), len(policy.private_scopes),
        len(policy.auth_scopes), len(policy.auth_service_roots),
    )
    return policy, incomplete_reasons


def _new_scan_stats(item_count):
    return {
        "items_found": item_count,
        "maps_checked": 0,
        "maps_ok": 0,
        "maps_broken": 0,
        "maps_unreadable": 0,
        "layers_checked": 0,
        "layers_ok": 0,
        "layers_broken": 0,
        "layers_unverifiable_auth": 0,
        "layers_blocked_unsafe": 0,
        "layers_broken_transport": 0,
        "layers_ignored_non_json": 0,
        "layers_ignored_resource_media_jpeg": 0,
        "unique_urls": 0,
        "url_cache_hits": 0,
    }


def _read_map_documents(items, workers, logger):
    logger.info(
        "Phase 1/3: reading JSON for %d web map(s) with %d worker(s) ...",
        len(items), workers,
    )
    map_reads_by_id: Dict[str, Dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for item, future in _bounded_future_results(
                executor, items, _read_web_map,
                workers * MAX_IN_FLIGHT_FACTOR):
            item_id = str(_item_value(item, "id", ""))
            try:
                map_read = future.result()
            except Exception as exc:
                created, modified = _item_dates(item)
                map_read = {
                    "item": item,
                    "date_created": created,
                    "date_modified": modified,
                    "unreadable": _safe_text(
                        f"unexpected read exception: {exc}"
                    ),
                    "layers": [],
                }
            map_reads_by_id[item_id] = map_read
            done += 1
            if done % 100 == 0 or done == len(items):
                logger.info(
                    "Phase 1 progress: %d / %d map(s) read.", done, len(items)
                )
    return [
        map_reads_by_id[str(_item_value(item, "id", ""))] for item in items
    ]


def _index_layer_references(map_reads):
    unique_refs = {}
    url_status: Dict[str, Optional[str]] = {}
    for map_read in map_reads:
        normalized_refs = []
        for value in map_read.get("layers", []):
            if isinstance(value, _LayerReference):
                ref = value
            else:
                try:
                    section, title, url = value[:3]
                except Exception:
                    continue
                ref = _prepare_layer_reference(section, title, url)
            normalized_refs.append(ref)
            unique_refs.setdefault(ref.url, ref)
            if ref.precheck_error:
                url_status.setdefault(ref.url, ref.precheck_error)
        map_read["layers"] = normalized_refs
    return unique_refs, url_status


def _probe_unique_layer_urls(session, token_provider, policy, unique_refs,
                             url_status, url_workers, url_timeout, logger):
    to_probe = [ref for url, ref in unique_refs.items() if url not in url_status]
    logger.info(
        "Phase 2/3: evaluating %d unique layer URL(s) with %d worker(s) "
        "(timeout %ds; %d rejected before network access) ...",
        len(unique_refs), url_workers, url_timeout, len(url_status),
    )
    probed = len(url_status)
    if not to_probe:
        return

    with _SafeLayerProber(
            session, token_provider, policy, url_timeout,
            pool_size=url_workers) as prober:

        def probe_reference(ref):
            return ref.url, prober.probe(ref.url)

        with ThreadPoolExecutor(max_workers=url_workers) as executor:
            for ref, future in _bounded_future_results(
                    executor, to_probe, probe_reference,
                    url_workers * MAX_IN_FLIGHT_FACTOR):
                try:
                    url, verdict = future.result()
                except Exception as exc:
                    url = ref.url
                    verdict = _safe_text(
                        f"BROKEN_REQUEST: probe exception: {exc}"
                    )
                url_status[url] = verdict
                probed += 1
                if probed % 250 == 0 or probed == len(unique_refs):
                    logger.info(
                        "Phase 2 progress: %d / %d URL(s) evaluated.",
                        probed, len(unique_refs),
                    )


def _broken_row(platform, item, map_read, *, section, layer_name,
                layer_url, error):
    return {
        "platform": platform,
        "title": _item_value(item, "title", ""),
        "item_id": _item_value(item, "id", ""),
        "owner": _item_value(item, "owner", ""),
        "date_created": map_read["date_created"],
        "date_modified": map_read["date_modified"],
        "section": section,
        "layer_name": layer_name,
        "layer_url": layer_url,
        "error": _safe_text(error),
    }


def _assemble_scan_rows(map_reads, url_status, stats, platform, logger):
    logger.info("Phase 3/3: assembling per-map results ...")
    broken_rows: List[Dict] = []
    for map_read in map_reads:
        item = map_read["item"]
        stats["maps_checked"] += 1
        if map_read["unreadable"]:
            stats["maps_broken"] += 1
            stats["maps_unreadable"] += 1
            broken_rows.append(_broken_row(
                platform, item, map_read,
                section="WEBMAP", layer_name="", layer_url="",
                error=map_read["unreadable"],
            ))
            continue

        layers = map_read["layers"]
        if not layers:
            stats["maps_ok"] += 1
            continue

        layers_broken = 0
        for ref in layers:
            stats["layers_checked"] += 1
            verdict = ref.precheck_error or url_status.get(ref.url)
            if ref.url not in url_status and not ref.precheck_error:
                verdict = "BROKEN_REQUEST: URL was not evaluated"
            if verdict is None:
                stats["layers_ok"] += 1
                continue
            if verdict.startswith("IGNORED_NON_JSON:"):
                stats["layers_ignored_non_json"] += 1
                continue
            if verdict.startswith("IGNORED_RESOURCE_MEDIA_JPEG:"):
                stats["layers_ignored_resource_media_jpeg"] += 1
                continue

            if verdict.startswith("UNVERIFIABLE_AUTH:"):
                stats["layers_unverifiable_auth"] += 1
            elif verdict.startswith("BLOCKED_"):
                stats["layers_blocked_unsafe"] += 1
            else:
                stats["layers_broken_transport"] += 1
            layers_broken += 1
            stats["layers_broken"] += 1
            broken_rows.append(_broken_row(
                platform, item, map_read,
                section=ref.section,
                layer_name=ref.title,
                layer_url=_safe_url_for_display(ref.url),
                error=verdict,
            ))

        if layers_broken:
            stats["maps_broken"] += 1
        else:
            stats["maps_ok"] += 1
    return broken_rows


def _finalize_incomplete_reasons(initial_reasons, stats):
    reasons = list(initial_reasons)
    if stats["maps_unreadable"]:
        reasons.append(
            f"{stats['maps_unreadable']} web map document(s) could not be read"
        )
    if stats["layers_unverifiable_auth"]:
        reasons.append(
            f"{stats['layers_unverifiable_auth']} layer URL(s) could not be "
            "authenticated within the approved trust boundary"
        )
    if stats["layers_blocked_unsafe"]:
        reasons.append(
            f"{stats['layers_blocked_unsafe']} layer URL(s) were blocked by "
            "the network-safety policy"
        )

    deduplicated = []
    seen = set()
    for reason in reasons:
        safe = _safe_text(reason)
        if safe not in seen:
            seen.add(safe)
            deduplicated.append(safe)
    return deduplicated


def scan_web_maps(gis, session, token_provider, base_url, platform,
                  output_dir, timestamp, max_items, workers, url_workers,
                  url_timeout, resolve_workers, org_only, logger,
                  excluded_ids, org_prefix, broken_csv_path=None,
                  allowed_private_hosts=None):
    items = search_web_maps(
        gis, base_url, max_items, org_only, resolve_workers, logger,
        session=session, token_provider=token_provider,
    )
    items = filter_excluded(
        items, excluded_ids, id_getter=lambda item: _item_value(item, "id", None),
        logger=logger, label="web map item",
    )
    stats = _new_scan_stats(len(items))
    map_reads = _read_map_documents(items, workers, logger)

    portal_sessions = _WorkerSessionPool(
        session, max(DEFAULT_RESOLVE_WORKERS, workers)
    )
    try:
        policy, trust_errors = _build_trust_policy(
            gis, portal_sessions, token_provider, base_url, platform,
            map_reads, logger, allowed_private_hosts=allowed_private_hosts,
        )
    finally:
        portal_sessions.close()

    unique_refs, url_status = _index_layer_references(map_reads)
    stats["unique_urls"] = len(unique_refs)
    _probe_unique_layer_urls(
        session, token_provider, policy, unique_refs, url_status,
        url_workers, url_timeout, logger,
    )
    broken_rows = _assemble_scan_rows(
        map_reads, url_status, stats, platform, logger
    )
    stats["url_cache_hits"] = max(
        0, stats["layers_checked"] - stats["unique_urls"]
    )
    incomplete_reasons = _finalize_incomplete_reasons(trust_errors, stats)

    csv_path = broken_csv_path or os.path.join(
        output_dir,
        f"{org_prefix}_{platform}_WEBMAP_BROKEN_LAYERS_{timestamp}.csv",
    )
    broken_csv = write_dict_csv(
        broken_rows, csv_path, fieldnames=BROKEN_FIELDS, logger=logger,
        label="broken layer row", force_values={"platform": platform},
    )
    if broken_rows and not broken_csv:
        raise ScanAbort("Broken-layer rows existed, but the report was not written.")

    logger.info(
        "Scan diagnostics: items=%d maps_checked=%d ok=%d broken=%d "
        "unreadable=%d | layers_checked=%d ok=%d broken=%d "
        "unverifiable_auth=%d blocked_unsafe=%d transport_failure=%d "
        "ignored_non_json=%d | unique_urls=%d cache_hits=%d",
        stats["items_found"], stats["maps_checked"], stats["maps_ok"],
        stats["maps_broken"], stats["maps_unreadable"],
        stats["layers_checked"], stats["layers_ok"], stats["layers_broken"],
        stats["layers_unverifiable_auth"], stats["layers_blocked_unsafe"],
        stats["layers_broken_transport"], stats["layers_ignored_non_json"],
        stats["unique_urls"],
        stats["url_cache_hits"],
    )
    return {
        "broken_csv": broken_csv,
        "stats": stats,
        "incomplete_reasons": incomplete_reasons,
    }

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Scan AGOL or Portal for web maps with broken layers and email a "
            "report. TLS verification is explicit; layer probes are "
            "anonymous-first with DNS pinning and redirect validation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TEST MODE IS THE DEFAULT (scans only 250 web maps). Pass "
            "--no-test to scan everything.\n\n"
            "Examples:\n"
            "  python SCAN_WEB_MAPS.py --config org1.config.toml --target portal\n"
            "  python SCAN_WEB_MAPS.py --config org2.config.toml --target agol "
            "--no-test --workers 24\n"
            "  python SCAN_WEB_MAPS.py --config org1.config.toml --target portal "
            "--tls-mode os-trust-store\n"
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
            "FULL mode: scan all web maps (up to the safety ceiling). If "
            "omitted, scan only the first 250 web maps."
        ),
    )
    parser.add_argument(
        "--all-owners", dest="org_only", action="store_false",
        help=(
            "Scan every reachable web map, not just content owned by the "
            "authenticated organization. By default both Portal and AGOL "
            "searches are organization-scoped."
        ),
    )
    parser.set_defaults(org_only=True)
    parser.add_argument(
        "--allow-private-layer-host",
        dest="allowed_private_hosts",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "Explicitly allow this exact layer hostname to be probed even when "
            "DNS resolves it to a private address. May be supplied more than "
            "once. This permits anonymous probes only; organization credentials "
            "are still withheld from external non-AGOL hosts."
        ),
    )
    parser.add_argument(
        "--workers",
        type=_bounded_int("--workers", 1, MAX_WORKERS),
        default=DEFAULT_WORKERS,
        help=(
            f"Concurrent workers for map JSON reads/item resolution "
            f"(default: {DEFAULT_WORKERS}; maximum: {MAX_WORKERS})."
        ),
    )
    parser.add_argument(
        "--url-workers",
        type=_bounded_int("--url-workers", 1, MAX_WORKERS),
        default=DEFAULT_URL_WORKERS,
        help=(
            f"Concurrent workers for unique layer URL probes "
            f"(default: {DEFAULT_URL_WORKERS}; maximum: {MAX_WORKERS})."
        ),
    )
    parser.add_argument(
        "--url-timeout",
        type=_bounded_int("--url-timeout", 1, MAX_URL_TIMEOUT),
        default=URL_PROBE_TIMEOUT,
        help=(
            f"Per-URL timeout in seconds (default: {URL_PROBE_TIMEOUT}; "
            f"maximum: {MAX_URL_TIMEOUT})."
        ),
    )

    tls_group = parser.add_argument_group("TLS verification")
    tls_group.add_argument(
        "--tls-mode", choices=sorted(_TLS_MODE_CHOICES), default="standard",
        help=(
            "TLS verification strategy: 'standard' (default), "
            "'os-trust-store', or 'custom-ca-bundle'."
        ),
    )
    tls_group.add_argument(
        "--ca-bundle-path", default=None,
        help=(
            "Path to a readable PEM CA bundle. Valid only when --tls-mode is "
            "custom-ca-bundle."
        ),
    )
    return parser.parse_args(argv)


def _validate_tls_arguments(args):
    path = str(args.ca_bundle_path or "").strip()
    if args.tls_mode == "custom-ca-bundle":
        if not path:
            raise UsageError(
                "--ca-bundle-path is required when --tls-mode is custom-ca-bundle."
            )
        if not os.path.isfile(path) or not os.access(path, os.R_OK):
            raise UsageError(
                f"--ca-bundle-path must name a readable file: {path!r}."
            )
    elif path:
        raise UsageError(
            "--ca-bundle-path is valid only with --tls-mode custom-ca-bundle."
        )


def _validate_portal_target(target, portal_url):
    raw = str(portal_url or "").strip()
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ScanAbort("portal-url contains whitespace or control characters.")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ScanAbort(f"portal-url could not be parsed: {_safe_text(exc)}") from exc
    if parsed.scheme.lower() != "https":
        raise ScanAbort("portal-url must use HTTPS.")
    if not parsed.hostname:
        raise ScanAbort("portal-url must contain a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ScanAbort("portal-url must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ScanAbort("portal-url must not contain a query string or fragment.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ScanAbort(f"portal-url contains an invalid port: {_safe_text(exc)}") from exc
    try:
        _portal_application_root(raw)
    except _URLPolicyError as exc:
        raise ScanAbort(f"portal-url is unsafe: {exc.detail}") from exc
    platform = get_platform(portal_url)
    expected = "AGOL" if str(target).lower() == "agol" else "PORTAL"
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} requires {expected}, but portal-url "
            f"{portal_url!r} was classified as {platform}."
        )
    return platform


def _reserve_run_paths(output_dir, org_prefix, platform, timestamp):
    for sequence in range(1000):
        stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        log_path = os.path.join(
            output_dir, f"{org_prefix}_{platform}_web_map_scan_{stamp}.log"
        )
        csv_path = os.path.join(
            output_dir,
            f"{org_prefix}_{platform}_WEBMAP_BROKEN_LAYERS_{stamp}.csv",
        )
        if os.path.exists(csv_path):
            continue
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return stamp, log_path, csv_path
    raise ScanAbort("Could not reserve a unique output name after 1,000 attempts.")


def _close_gis_sessions(gis, logger=None):
    seen = set()
    candidates = []
    connection = getattr(gis, "_con", None)
    candidates.append(getattr(connection, "_session", None))
    portal = getattr(gis, "_portal", None)
    portal_connection = getattr(portal, "con", None)
    candidates.append(getattr(portal_connection, "_session", None))
    for session in candidates:
        if session is None or id(session) in seen:
            continue
        seen.add(id(session))
        try:
            session.close()
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    "Could not close an ArcGIS SDK HTTP session: %s",
                    _safe_text(exc),
                )


def _send_abort(subject, reason, log_path, args, logger,
                start_wallclock, start_perf):
    safe = _safe_text(reason)
    body = (
        f"{EMAIL_SUBJECT_PREFIX}\n\n"
        f"Result: ABORTED\n"
        f"Organization: {args.org_name}\n"
        f"Target: {args.target}\n"
        f"Reason: {safe}\n\n"
        f"{timing_block(start_wallclock, start_perf)}"
    )
    try:
        send_email(
            subject + " (ABORTED)", body, [log_path], args.smtp_server,
            args.email_to, args.email_from, logger,
        )
    except Exception as exc:
        logger.error("Could not send abort notification: %s", _safe_text(exc))


def _log_header(args, platform, run_stamp, log_path, max_email_mb, logger):
    logger.info("=" * 70)
    logger.info("Web Map Layer Scan - run started")
    logger.info("Organization: %s", args.org_name)
    logger.info("Target: %s", args.target)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST (250 web maps)" if args.test else "FULL")
    logger.info("Org-only scan: %s", bool(args.org_only))
    logger.info("Run timestamp: %s", run_stamp)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("Output directory: %s", args.output_dir)
    logger.info("Log file: %s", log_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s", args.email_to)
    logger.info("Email from: %s", args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Map-read workers: %d", args.workers)
    logger.info("URL-probe workers: %d", args.url_workers)
    logger.info("URL-probe timeout (seconds): %d", args.url_timeout)
    logger.info("Exclude group id(s): %s", args.exclude_group_id or "(none)")
    logger.info("Allowed private layer host(s): %s",
                ",".join(args.allowed_private_hosts) if args.allowed_private_hosts else "(none)")
    logger.info("=" * 70)


def _send_completion(args, platform, run_stamp, log_path, max_email_mb,
                     result, start_wallclock, start_perf, logger):
    broken_csv = result.get("broken_csv")
    stats = result.get("stats", {})
    incomplete_reasons = result.get("incomplete_reasons", [])
    status = "INCOMPLETE" if incomplete_reasons else "COMPLETE"

    logger.info("=" * 70)
    logger.info("RUN SUMMARY - %s", status)
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Mode: %s", "TEST" if args.test else "FULL")
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Org-only scan: %s", bool(args.org_only))
    logger.info("Maps checked: %d", stats.get("maps_checked", 0))
    logger.info("Maps OK: %d", stats.get("maps_ok", 0))
    logger.info("Maps broken: %d", stats.get("maps_broken", 0))
    logger.info("Maps unreadable: %d", stats.get("maps_unreadable", 0))
    logger.info("Layers checked: %d", stats.get("layers_checked", 0))
    logger.info("Layers OK: %d", stats.get("layers_ok", 0))
    logger.info("Layers broken: %d", stats.get("layers_broken", 0))
    logger.info("Unverifiable auth: %d", stats.get("layers_unverifiable_auth", 0))
    logger.info("Blocked unsafe: %d", stats.get("layers_blocked_unsafe", 0))
    logger.info("Other layer failure: %d", stats.get("layers_broken_transport", 0))
    logger.info("Ignored non-JSON responses: %d", stats.get("layers_ignored_non_json", 0))
    logger.info(
        "Ignored ./resources/media/*.jpeg references: %d",
        stats.get("layers_ignored_resource_media_jpeg", 0),
    )
    logger.info("Unique URLs checked: %d", stats.get("unique_urls", 0))
    logger.info("URL checks saved: %d", stats.get("url_cache_hits", 0))
    logger.info("Broken report: %s", broken_csv or "(none broken)")
    for reason in incomplete_reasons:
        logger.warning("Incomplete because: %s", reason)
    logger.info("=" * 70)

    body = (
        f"Web Map Layer Scan\n"
        f"Result: {status}\n"
        f"Organization: {args.org_name}\n"
        f"Platform: {platform}\n"
        f"Run timestamp: {run_stamp}\n"
        f"Mode: {'TEST (250 web maps)' if args.test else 'FULL'}\n"
        f"TLS mode: {args.tls_mode}\n"
        f"Org-only scan: {bool(args.org_only)}\n"
        f"Username: {args.username}\n"
        f"Excluded group(s): {args.exclude_group_id or '(none)'}\n\n"
        f"Maps checked: {stats.get('maps_checked', 0)}\n"
        f"Maps OK: {stats.get('maps_ok', 0)}\n"
        f"Maps broken: {stats.get('maps_broken', 0)}\n"
        f"Maps unreadable: {stats.get('maps_unreadable', 0)}\n"
        f"Layers checked: {stats.get('layers_checked', 0)}\n"
        f"Layers OK: {stats.get('layers_ok', 0)}\n"
        f"Layers broken: {stats.get('layers_broken', 0)}\n"
        f"Unverifiable auth: {stats.get('layers_unverifiable_auth', 0)}\n"
        f"Blocked unsafe: {stats.get('layers_blocked_unsafe', 0)}\n"
        f"Other failures: {stats.get('layers_broken_transport', 0)}\n"
        f"Ignored non-JSON responses: {stats.get('layers_ignored_non_json', 0)}\n"
        f"Ignored ./resources/media/*.jpeg references: "
        f"{stats.get('layers_ignored_resource_media_jpeg', 0)}\n"
        f"Unique URLs: {stats.get('unique_urls', 0)}\n"
        f"URL checks saved: {stats.get('url_cache_hits', 0)}\n\n"
        f"Broken report: {broken_csv or '(none broken)'}\n"
    )
    if incomplete_reasons:
        body += (
            "\nWARNING: Some maps/layers could not be conclusively checked. "
            "Confirmed findings are retained, but missing rows must not be "
            "interpreted as proof that an unchecked layer is healthy.\n"
            "Reasons:\n"
            + "\n".join(f"  - {reason}" for reason in incomplete_reasons)
            + "\n"
        )
    body += (
        f"\n{timing_block(start_wallclock, start_perf)}\n"
        "See the attached log file and report for full detail.\n"
    )

    attachments, note = gather_attachments(
        log_path, [broken_csv], args.output_dir, logger, cap_mb=max_email_mb
    )
    body += note
    prefix = EMAIL_SUBJECT_PREFIX if status == "COMPLETE" else \
        EMAIL_SUBJECT_PREFIX + " (INCOMPLETE)"
    subject = subject_line(
        prefix, run_stamp, platform, org_name=args.org_name
    )
    send_email(
        subject, body, attachments, args.smtp_server, args.email_to,
        args.email_from, logger,
    )
    if incomplete_reasons:
        logger.warning(
            "Web Map Layer Scan completed with incomplete detail(s); returning "
            "exit status 0 because the scheduled workflow ran to completion."
        )
    else:
        logger.info("Success")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        _validate_tls_arguments(args)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    finalize_config(args, required=REQUIRED_SETTINGS)
    ensure_output_dir(args.output_dir)
    base_timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()

    try:
        platform = _validate_portal_target(args.target, args.portal_url)
        run_stamp, log_path, broken_csv_path = _reserve_run_paths(
            args.output_dir, args.org_name_safe, platform, base_timestamp
        )
    except Exception as exc:
        print(f"Run ABORTED before logging: {_safe_text(exc)}", file=sys.stderr)
        return 1

    logger = setup_logging(log_path, "web_map_scan")
    max_email_mb = get_max_email_mb(args.target, args.config)
    _log_header(args, platform, run_stamp, log_path, max_email_mb, logger)
    base_subject = subject_line(
        EMAIL_SUBJECT_PREFIX, run_stamp, platform, org_name=args.org_name
    )

    direct_session = None
    gis = None
    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        ca_path = (
            args.ca_bundle_path
            if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
        )
        direct_session = build_secure_session(
            mode, ca_bundle_path=ca_path,
            pool_connections=max(8, args.url_workers),
            pool_maxsize=max(16, args.url_workers * 2),
            retry_total=2, logger=logger,
        )
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, direct_session, logger=logger
        )
        if not handshake_ok:
            raise ScanAbort(
                "TLS handshake verification failed: "
                + _safe_text(handshake_detail.get("error"))
            )

        gis = connect_gis(
            args.portal_url, args.username, args.keyring_service, logger,
            verify_mode=mode, ca_bundle_path=ca_path,
        )
        token_provider = provider_for_gis(gis, platform)
        logger.info(
            "Token acquired via %s (token present: %s).",
            type(token_provider).__name__, token_provider.is_present(),
        )

        excluded_ids = resolve_excluded_item_ids(
            gis, args.exclude_group_id, logger=logger
        )
        tune_gis_session(
            gis, max(args.workers, args.url_workers),
            timeout=REQUEST_TIMEOUT, retry_total=2,
        )
        base_url = _portal_application_root(
            getattr(gis, "url", None)
            or getattr(getattr(gis, "_portal", None), "url", None)
            or args.portal_url
        )

        result = scan_web_maps(
            gis, direct_session, token_provider, base_url, platform,
            args.output_dir, run_stamp,
            TEST_MODE_ITEM_LIMIT if args.test else MAX_ITEMS,
            args.workers, args.url_workers, args.url_timeout, args.workers,
            bool(args.org_only), logger, excluded_ids, args.org_name_safe,
            broken_csv_path=broken_csv_path,
            allowed_private_hosts=args.allowed_private_hosts,
        )
        return _send_completion(
            args, platform, run_stamp, log_path, max_email_mb, result,
            start_wallclock, start_perf, logger,
        )
    except ScanAbort as exc:
        logger.error("Run ABORTED: %s", _safe_text(exc))
        _send_abort(
            base_subject, exc, log_path, args, logger,
            start_wallclock, start_perf,
        )
        return 1
    except Exception as exc:
        logger.exception("Unexpected fatal error: %s", _safe_text(exc))
        _send_abort(
            base_subject, f"Unexpected fatal error: {_safe_text(exc)}",
            log_path, args, logger, start_wallclock, start_perf,
        )
        return 1
    finally:
        if direct_session is not None:
            try:
                direct_session.close()
            except Exception:
                pass
        if gis is not None:
            _close_gis_sessions(gis, logger=logger)


if __name__ == "__main__":
    raise SystemExit(main())
