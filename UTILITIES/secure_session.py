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

Public API
----------
    VerifyMode
    build_secure_session(mode, *, ca_bundle_path=None, pool_connections=64,
                         pool_maxsize=128, retry_total=2, backoff_factor=0.3,
                         logger=None) -> requests.Session
    verify_tls_handshake(url, session, logger=None, timeout=10)
        -> (ok: bool, detail: dict)
    allow_insecure_verify_false(reason, logger=None, ...) -> requests.Session
==============================================================================
"""

import enum
import socket
import ssl
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BACKOFF_FACTOR = 0.3
DEFAULT_HANDSHAKE_TIMEOUT = 10


class VerifyMode(enum.Enum):
    STANDARD_CA = "standard_ca"
    OS_TRUST_STORE = "os_trust_store"
    CUSTOM_CA_BUNDLE = "custom_ca_bundle"


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
    parsed = urlparse(url if "://" in url else f"https://{url}")
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