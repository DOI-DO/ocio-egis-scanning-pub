"""
UTILITIES/secure_session.py
==============================================================================
Explicit, caller-decided TLS/SSL verification for a script's own direct
requests.Session (separate from the arcgis SDK's own connection, which
gis_connect.connect_gis() always secures on its own).

Three explicit modes -- no silent "just build me a session" default:
    VerifyMode.STANDARD_CA      certifi bundled CA list (AGOL, public CA)
    VerifyMode.OS_TRUST_STORE   verify via the OS certificate store
                                (requires the 'truststore' package)
    VerifyMode.CUSTOM_CA_BUNDLE verify against an explicit PEM file

allow_insecure_verify_false(reason, ...) is a documented, always-logged 
escape hatch for time-boxed debugging only; it never suppresses
InsecureRequestWarning.

verify_tls_handshake(url, session, ...) runs ONE real TLS handshake at
startup (not per-request) using the same verification config as `session`,
and logs protocol/cipher/certificate detail plus a PASS/FAIL verdict.

*** CONSOLIDATION CHANGE ***
-------------------------------------------------------------------------
The following were previously copy-pasted (verbatim, with minor per-script
drift) into 10-12 scanner scripts and are now shared here so every caller
uses one audited implementation:

  * _TLS_MODE_CHOICES / tls_mode_from_string() - the string<->VerifyMode map
    every script rebuilt from scratch, plus a validating resolver. The map
    accepts BOTH "standard" and "standard-ca" as aliases for STANDARD_CA so
    the two spellings that existed across scripts both keep working.

  * URL-safety helpers - split_https_url / split_service_url, origin tuple
    extraction, path validation (encoded-slash / traversal / control-char /
    credential-like rejection), portal-base normalization, and federation
    base-path normalization. These are the union of the per-script copies;
    the strictest check from any script is applied for everyone.

  * TrustedUrlPolicy - decides which ArcGIS service URLs may receive the
    organization token. It unifies the AGOL-suffix, Portal same-origin,
    federation-base, Portal sharing-proxy, and explicit-origin rules that
    were scattered across the scripts. Behavior is the UNION: a URL is
    trusted if ANY registered rule would have trusted it.

No functionality was removed and no hardening was weakened; the shared
helpers enforce the strictest variant observed in any single script.

Public API
----------
    VerifyMode
    build_secure_session(mode, *, ca_bundle_path=None, pool_connections=64,
                         pool_maxsize=128, retry_total=2, backoff_factor=0.3,
                         logger=None) -> requests.Session
    verify_tls_handshake(url, session, logger=None, timeout=10)
        -> (ok: bool, detail: dict)
    allow_insecure_verify_false(reason, logger=None, ...) -> requests.Session

    _TLS_MODE_CHOICES                       (dict: label -> VerifyMode)
    tls_mode_from_string(label)             -> VerifyMode
    AGOL_TRUSTED_SUFFIX

    UrlPolicyError                          (ValueError subclass)
    split_url(raw, *, allowed_schemes=..., require_https=False)
    split_https_url(raw)
    split_service_url(raw)
    origin_tuple(raw) / origin_tuple_from_parts(parts)
    validated_path(parts)
    normalize_portal_base(raw)
    normalize_federation_base_path(path)
    TrustedUrlPolicy
==============================================================================
"""

import enum
import socket
import ssl
from urllib.parse import unquote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BACKOFF_FACTOR = 0.3
DEFAULT_HANDSHAKE_TIMEOUT = 10

# Esri-owned parent domain for AGOL hosted content. Hosted feature services
# legitimately live on services*.arcgis.com / tiles*.arcgis.com /
# <org>.maps.arcgis.com, which are different hosts from the portal URL, so a
# strict same-origin check is wrong for AGOL -- membership in this domain is
# the correct trust rule there. This is a structural fact about how Esri names
# AGOL infrastructure, not an organization-specific literal.
AGOL_TRUSTED_SUFFIX = ".arcgis.com"
AGOL_TRUSTED_APEX = "arcgis.com"


class VerifyMode(enum.Enum):
    STANDARD_CA = "standard_ca"
    OS_TRUST_STORE = "os_trust_store"
    CUSTOM_CA_BUNDLE = "custom_ca_bundle"


# ==========================================================================
# TLS-mode string <-> VerifyMode resolution (was rebuilt in every script)
# ==========================================================================
# Both "standard" and "standard-ca" appeared across scripts as the label for
# STANDARD_CA; both are accepted so no script's CLI changes meaning.
_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "standard-ca": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


def tls_mode_from_string(label):
    """
    Resolve a CLI --tls-mode string to a VerifyMode member.

    Accepts the labels used anywhere in this project ("standard",
    "standard-ca", "os-trust-store", "custom-ca-bundle"), case-insensitively.
    Raises ValueError on anything else so a typo'd flag fails loudly rather
    than silently falling back to a default verification strategy.
    """
    key = str(label or "").strip().lower()
    if key not in _TLS_MODE_CHOICES:
        valid = ", ".join(sorted(_TLS_MODE_CHOICES))
        raise ValueError(
            f"Unknown TLS mode {label!r}; expected one of: {valid}.")
    return _TLS_MODE_CHOICES[key]


def _build_retry(total, backoff):
    return Retry(
        total=total,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        allowed_methods={"GET", "POST"},
    )


def _mount_adapter(session, pool_connections, pool_maxsize, retry_total,
                   backoff_factor):
    adapter = HTTPAdapter(pool_connections=pool_connections,
                          pool_maxsize=pool_maxsize,
                          max_retries=_build_retry(retry_total, backoff_factor))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _enable_os_trust_store(logger=None):
    """
    Inject the `truststore` package into Python's ssl module so all TLS
    verification checks the OS-native certificate store instead of
    certifi's bundle. Raises ImportError if 'truststore' is not installed
    -- never silently degrades to an unverified connection.
    """
    try:
        import truststore
    except ImportError as exc:
        raise ImportError(
            "VerifyMode.OS_TRUST_STORE requires the 'truststore' package, "
            "which is not installed in this environment. Install it with:\n"
            "    pip install truststore\n"
            "This mode verifies TLS certificates against the OPERATING "
            "SYSTEM's certificate store rather than certifi's bundled CA "
            "list. Refusing to fall back to an unverified connection."
        ) from exc

    truststore.inject_into_ssl()
    if logger is not None:
        logger.info(
            "  [secure-session] TLS verification mode: OS_TRUST_STORE "
            "(truststore injected).")


def build_secure_session(mode, *, ca_bundle_path=None, pool_connections=64,
                         pool_maxsize=128, retry_total=2,
                         backoff_factor=DEFAULT_BACKOFF_FACTOR, logger=None):
    """
    Build a pooled + retrying requests.Session with an EXPLICIT, caller-
    chosen TLS verification strategy. `mode` is REQUIRED -- there is no
    default.
    """
    if not isinstance(mode, VerifyMode):
        raise TypeError(
            f"mode must be a UTILITIES.secure_session.VerifyMode member, "
            f"got {mode!r}. Callers must explicitly choose a TLS "
            f"verification strategy; there is no default.")

    session = requests.Session()

    if mode is VerifyMode.STANDARD_CA:
        session.verify = True
        if logger is not None:
            logger.info(
                "  [secure-session] TLS verification mode: STANDARD_CA "
                "(certifi bundled CA list).")

    elif mode is VerifyMode.OS_TRUST_STORE:
        _enable_os_trust_store(logger=logger)
        session.verify = True

    elif mode is VerifyMode.CUSTOM_CA_BUNDLE:
        if not ca_bundle_path:
            raise ValueError(
                "mode=CUSTOM_CA_BUNDLE requires ca_bundle_path (path to a "
                "PEM CA bundle file).")
        session.verify = ca_bundle_path
        if logger is not None:
            logger.info(
                "  [secure-session] TLS verification mode: "
                "CUSTOM_CA_BUNDLE (%s).", ca_bundle_path)

    return _mount_adapter(session, pool_connections, pool_maxsize,
                          retry_total, backoff_factor)


def _resolve_host_port(url):
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    host = parsed.hostname
    port = parsed.port or (443 if (parsed.scheme or "https") == "https" else 80)
    return host, port


def _build_ssl_context_like_session(session):
    """
    Build an ssl.SSLContext mirroring the given session's verification
    behavior, so the one-time handshake check exercises the SAME trust
    decision every subsequent real request will use.
    """
    verify = getattr(session, "verify", True)

    if verify is False:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx, False

    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify), True

    return ssl.create_default_context(), True


def verify_tls_handshake(url, session, logger=None,
                         timeout=DEFAULT_HANDSHAKE_TIMEOUT):
    """
    ONE-TIME startup check: open a real TLS connection to `url`'s host
    using the SAME verification configuration as `session`, and log a
    clear PASS/FAIL summary plus protocol/cipher/certificate detail.

    Returns (ok, detail).
    """
    host, port = _resolve_host_port(url)
    detail = {
        "host": host, "port": port, "verify_enabled": None,
        "protocol": None, "cipher": None,
        "cert_subject": None, "cert_issuer": None,
        "cert_not_before": None, "cert_not_after": None,
        "error": None,
    }

    if not host:
        detail["error"] = f"could not parse a hostname out of url={url!r}"
        if logger is not None:
            logger.error("  [tls-handshake-check] %s", detail["error"])
        return False, detail

    ctx, verify_enabled = _build_ssl_context_like_session(session)
    detail["verify_enabled"] = verify_enabled

    if logger is not None:
        logger.info(
            "  [tls-handshake-check] Opening a one-time verification TLS "
            "connection to %s:%d (verification %s) ...",
            host, port, "ENABLED" if verify_enabled else "DISABLED")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                detail["protocol"] = tls_sock.version()
                cipher = tls_sock.cipher()
                detail["cipher"] = cipher[0] if cipher else None

                cert = None
                try:
                    cert = tls_sock.getpeercert()
                except ssl.SSLError:
                    cert = None

                if cert:
                    subject = dict(x[0] for x in cert.get("subject", []))
                    issuer = dict(x[0] for x in cert.get("issuer", []))
                    detail["cert_subject"] = subject.get("commonName")
                    detail["cert_issuer"] = issuer.get("commonName")
                    detail["cert_not_before"] = cert.get("notBefore")
                    detail["cert_not_after"] = cert.get("notAfter")

    except ssl.SSLCertVerificationError as exc:
        detail["error"] = f"certificate verification FAILED: {exc}"
        if logger is not None:
            logger.error(
                "  [tls-handshake-check] *** FAIL *** %s:%d -- %s",
                host, port, detail["error"])
        return False, detail
    except Exception as exc:
        detail["error"] = f"could not complete TLS handshake: {exc}"
        if logger is not None:
            logger.error(
                "  [tls-handshake-check] *** FAIL *** %s:%d -- %s",
                host, port, detail["error"])
        return False, detail

    if logger is not None:
        if verify_enabled:
            logger.info(
                "  [tls-handshake-check] *** PASS *** %s:%d -- handshake "
                "completed AND certificate verification succeeded.",
                host, port)
        else:
            logger.warning(
                "  [tls-handshake-check] *** HANDSHAKE OK BUT UNVERIFIED "
                "*** %s:%d -- certificate verification is DISABLED for "
                "this session.", host, port)
        logger.info(
            "  [tls-handshake-check] Protocol=%s Cipher=%s",
            detail["protocol"], detail["cipher"])
        logger.info(
            "  [tls-handshake-check] Certificate subject=%r issuer=%r "
            "validNotBefore=%r validNotAfter=%r",
            detail["cert_subject"], detail["cert_issuer"],
            detail["cert_not_before"], detail["cert_not_after"])

    return True, detail


def allow_insecure_verify_false(reason, logger=None, pool_connections=64,
                                pool_maxsize=128, retry_total=2,
                                backoff_factor=DEFAULT_BACKOFF_FACTOR):
    """
    Documented, explicit escape hatch for disabling TLS verification.
    Requires a non-blank `reason`; always logs a loud warning; never
    suppresses InsecureRequestWarning. Time-boxed debugging use only.
    """
    if not reason or not str(reason).strip():
        raise ValueError(
            "allow_insecure_verify_false() requires a non-blank `reason` "
            "explaining why TLS verification is being disabled.")

    msg = (
        "  [secure-session] *** TLS VERIFICATION IS DISABLED (verify=False) "
        f"*** Reason given: {reason!r}. Certificates will NOT be checked. "
        "InsecureRequestWarning is intentionally left ENABLED. This should "
        "be a temporary debugging measure only."
    )
    if logger is not None:
        logger.warning(msg)
    else:
        print(msg)

    session = requests.Session()
    session.verify = False
    return _mount_adapter(session, pool_connections, pool_maxsize,
                          retry_total, backoff_factor)


# ==========================================================================
# URL-safety helpers (consolidated from the scanner scripts)
# ==========================================================================
# These were copy-pasted -- with minor drift -- into ITEM_INVENTORY,
# GET_RELATED_ITEMS, CORRELATE_SQLITE_REPLICAS, EMPTY_FEATURE_SERVICES,
# FIELD_NAME_SCAN, SHARED_CREDENTIALS_SCAN, and others. The shared versions
# below apply the UNION of every safety check any single copy performed:
#
#   * scheme must be https (or http where a caller explicitly allows it),
#   * a hostname must be present,
#   * no embedded user-info credentials,
#   * no query string or fragment on a path meant to be exact,
#   * no percent-encoded slash/backslash (%2f / %5c),
#   * no backslash or ASCII control characters after unquoting,
#   * no "." / ".." path-traversal segments.
#
# UrlPolicyError is a ValueError subclass so existing `except ValueError`
# call sites keep catching it.


class UrlPolicyError(ValueError):
    """A URL failed a structural safety check before any network access."""


def split_url(raw, *, allowed_schemes=("https",), require_https=False):
    """
    Parse and structurally validate a URL, returning the urlsplit result.

    Raises UrlPolicyError if the URL is blank, unparseable, uses a scheme
    not in `allowed_schemes` (or is non-HTTPS when require_https is set),
    has no hostname, carries user-info credentials, or has an invalid port.
    Path safety is validated separately by validated_path().
    """
    text = str(raw or "").strip()
    if not text:
        raise UrlPolicyError("URL is blank.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise UrlPolicyError(f"URL could not be parsed: {text!r}") from exc

    scheme = (parts.scheme or "").lower()
    allowed = {s.lower() for s in allowed_schemes}
    if require_https:
        allowed = {"https"}
    if scheme not in allowed:
        rendered = " or ".join(sorted(s.upper() for s in allowed))
        raise UrlPolicyError(f"URL must use {rendered}: {text!r}")

    if not parts.hostname:
        raise UrlPolicyError(f"URL contains no hostname: {text!r}")

    try:
        _ = parts.port
    except ValueError as exc:
        raise UrlPolicyError(f"URL contains an invalid port: {text!r}") from exc

    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError("URL must not contain user-info credentials.")

    return parts


def split_https_url(raw):
    """Parse a URL that MUST be HTTPS. Raises UrlPolicyError otherwise."""
    return split_url(raw, require_https=True)


def split_service_url(raw):
    """Parse a URL that may be HTTP or HTTPS (registered-service discovery)."""
    return split_url(raw, allowed_schemes=("http", "https"))


def origin_tuple_from_parts(parts):
    """Return (scheme, host, port) for an already-parsed urlsplit result."""
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower().rstrip(".")
    default_port = 443 if scheme == "https" else 80
    return scheme, host, parts.port or default_port


def origin_tuple(raw, *, allowed_schemes=("https",)):
    """
    Return (scheme, host, port) for a raw URL, or None if it is unsafe.

    Never raises -- callers use this to compare origins without having to
    guard every call. Use split_url() directly when a hard failure is wanted.
    """
    try:
        return origin_tuple_from_parts(
            split_url(raw, allowed_schemes=allowed_schemes))
    except UrlPolicyError:
        return None


def validated_path(parts, *, allow_query_fragment=False):
    """
    Validate and return the path of an already-parsed urlsplit result.

    Rejects a query string / fragment (unless explicitly allowed), any
    percent-encoded slash or backslash, any backslash or control character
    after unquoting, and any "." / ".." traversal segment. Returns the raw
    (still-encoded) path with trailing slashes stripped -- the strictest
    common behavior across the scripts.
    """
    if not allow_query_fragment and (parts.query or parts.fragment):
        raise UrlPolicyError(
            "Request URL must not contain a query string or fragment.")
    raw_path = parts.path or ""
    lowered = raw_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise UrlPolicyError(
            "Request URL contains an encoded slash or backslash.")
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise UrlPolicyError("Request URL contains unsafe path characters.")
    if any(segment in (".", "..") for segment in decoded.split("/")):
        raise UrlPolicyError("Request URL contains path traversal.")
    return raw_path.rstrip("/")


def normalize_portal_base(raw):
    """
    Normalize a Portal/AGOL base URL to an HTTPS origin + safe path with any
    trailing '/home' and trailing slashes removed. Raises UrlPolicyError if
    the URL is unsafe (non-HTTPS, credentials, query/fragment, traversal,
    encoded slashes, etc.).
    """
    parts = split_https_url(raw)
    if parts.query or parts.fragment:
        raise UrlPolicyError("Portal URL must not contain a query or fragment.")
    path = validated_path(parts)
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def normalize_federation_base_path(path):
    """
    Normalize a federated-server URL path down to its site base, stripping a
    trailing '/rest/services...', '/admin', or '/rest' suffix. Matches the
    per-script helpers of the same name.
    """
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


# ==========================================================================
# TrustedUrlPolicy (consolidated token-destination gate)
# ==========================================================================
class TrustedUrlPolicy:
    """
    Decide which ArcGIS service URLs may receive the organization token.

    This unifies the per-script trust gates. A URL is trusted if ANY of the
    registered rules would accept it (UNION semantics -- never the
    intersection):

      * Portal target: the authenticated Portal origin itself, plus its
        '/sharing/servers/<id>/rest/services' proxy path.
      * AGOL target: any https host within Esri's '.arcgis.com' domain.
      * Any explicit HTTPS origin the caller registered (exact origin, no
        path) via the constructor's `explicit_origins`.
      * Any federated-server base registered with add_federation_base():
        a service URL under '<base>/rest/services/...' on the same origin.

    `portal_base` must be a safe HTTPS Portal/AGOL base (it is normalized
    here). `target` is "portal" or "agol" (case-insensitive). Construction
    raises UrlPolicyError if any explicit origin is not an exact HTTPS
    origin (no path/query/fragment).
    """

    def __init__(self, portal_base, target, explicit_origins=None,
                 logger=None):
        self.portal_base = normalize_portal_base(portal_base)
        self.target = str(target or "").lower()
        self.logger = logger
        portal_parts = split_https_url(self.portal_base)
        self.portal_origin = origin_tuple_from_parts(portal_parts)
        self.portal_path = (portal_parts.path or "").rstrip("/")
        self.explicit_origins = set()
        self.federation_bases = []

        import re
        self._portal_proxy_re = re.compile(
            r"(?i)/sharing/servers/[A-Za-z0-9_-]{1,128}/rest/services/"
        )

        for raw in explicit_origins or []:
            parts = split_https_url(raw)
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                raise UrlPolicyError(
                    "An explicitly trusted origin must be an exact HTTPS "
                    f"origin without a path, query, or fragment: {raw!r}")
            self.explicit_origins.add(origin_tuple_from_parts(parts))

    def add_federation_base(self, raw):
        """
        Register a federated-server URL as a trusted token destination.

        The URL's origin plus normalized site base are retained; later a
        service URL is trusted when it is under '<base>/rest/services/...'
        on that same origin. Ignores a blank value. Raises UrlPolicyError
        if the URL is unsafe.
        """
        if not raw:
            return
        parts = split_https_url(str(raw))
        if parts.query or parts.fragment:
            raise UrlPolicyError(
                f"Federation URL contains a query or fragment: {raw!r}")
        origin = origin_tuple_from_parts(parts)
        validated_path(parts)
        base_path = normalize_federation_base_path(parts.path)
        entry = (origin, base_path)
        if entry not in self.federation_bases:
            self.federation_bases.append(entry)

    def validate_portal_path(self, path):
        """Return an absolute Portal URL for a '/sharing/rest/...' path."""
        if not isinstance(path, str) or not path.startswith("/sharing/rest/"):
            raise UrlPolicyError(
                "Portal REST path must begin with '/sharing/rest/'.")
        if "?" in path or "#" in path or "\\" in path:
            raise UrlPolicyError("Portal REST path contains unsafe characters.")
        if any(part in (".", "..") for part in path.split("/")):
            raise UrlPolicyError("Portal REST path contains traversal.")
        return self.portal_base + path

    def validate_portal_url(self, raw):
        """
        Validate a Portal REST URL (used to re-check a redirect target):
        must be same-origin as the authenticated Portal and stay under its
        '/sharing/rest/' boundary. Returns the normalized URL.
        """
        try:
            parts = split_https_url(raw)
        except UrlPolicyError:
            raise
        path = validated_path(parts)
        if origin_tuple_from_parts(parts) != self.portal_origin:
            raise UrlPolicyError("Portal REST redirect changed origin.")
        required_prefix = self.portal_path + "/sharing/rest/"
        if not path.startswith(required_prefix):
            raise UrlPolicyError(
                "Portal REST redirect left the authenticated sharing/rest "
                "path.")
        return urlunsplit(("https", parts.netloc, path, "", ""))

    def validate_service_url(self, raw):
        """
        Validate a service URL destined to receive the organization token.

        Raises UrlPolicyError unless the URL is HTTPS and trusted by at least
        one registered rule (explicit origin, AGOL suffix, Portal sharing
        proxy, or a registered federation base). Returns the normalized URL
        on success.
        """
        try:
            parts = split_https_url(raw)
        except UrlPolicyError:
            raise
        path = validated_path(parts)
        origin = origin_tuple_from_parts(parts)
        host = origin[1]

        allowed = origin in self.explicit_origins

        if not allowed and self.target == "agol":
            if host == AGOL_TRUSTED_APEX or host.endswith(AGOL_TRUSTED_SUFFIX):
                allowed = True

        if not allowed and origin == self.portal_origin:
            if self._portal_proxy_re.search(path):
                allowed = True

        if not allowed:
            for registered_origin, base_path in self.federation_bases:
                if origin != registered_origin:
                    continue
                expected = (base_path + "/rest/services/").lower()
                if path.lower().startswith(expected):
                    allowed = True
                    break

        if not allowed:
            raise UrlPolicyError(
                "Refusing to send the organization token to an untrusted "
                f"service origin: {parts.scheme}://{parts.netloc}")

        return urlunsplit(("https", parts.netloc, path, "", ""))

    def is_trusted_service_url(self, raw):
        """Non-raising convenience: True if validate_service_url() accepts raw."""
        try:
            self.validate_service_url(raw)
            return True
        except UrlPolicyError:
            return False

    def render_trusted(self):
        """Return a sorted, human-readable list of trusted destinations."""
        rendered = set()
        for scheme, host, port in self.explicit_origins:
            rendered.add(f"{scheme}://{host}"
                         + (f":{port}" if port != 443 else ""))
        for (scheme, host, port), path in self.federation_bases:
            rendered.add(f"{scheme}://{host}"
                         + (f":{port}" if port != 443 else "") + path)
        if self.target == "agol":
            rendered.add("https://*.arcgis.com")
        rendered.add(self.portal_base + "/sharing/servers/<id>/rest/services")
        return sorted(rendered)