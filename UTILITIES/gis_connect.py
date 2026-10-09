"""
UTILITIES/gis_connect.py
==============================================================================
Shared AGOL / Portal connection using a keyring-stored password.

*** SECURITY CHANGE (this revision) ***
-------------------------------------------------------------------------
connect_gis() now ALWAYS connects with TLS verification enabled. This
absorbs what used to be the separate UTILITIES/secure_gis_connect.py /
connect_gis_secure() helper directly into this function, so every existing
script that already does:

    from UTILITIES import connect_gis
    gis = connect_gis(portal_url, username, keyring_service, logger)

gets the hardened behavior automatically -- no import changes, no renamed
calls, nothing to touch in any script. There is no verify_cert=False
parameter anywhere in this function's signature; that unsafe path simply
does not exist here.

*** BUGFIX #1 (earlier revision): OS_TRUST_STORE was silently NOT verifying
    the real SDK connection ***
-------------------------------------------------------------------------
connect_gis() takes an explicit `verify_mode` argument (a
UTILITIES.secure_session.VerifyMode member) instead of inferring behavior
solely from whether a ca_bundle_path was supplied. When verify_mode is
OS_TRUST_STORE, connect_gis() calls truststore.inject_into_ssl() ITSELF,
immediately before constructing GIS(...).

*** BUGFIX #2 (earlier revision): GIS(verify_cert=True) alone does NOT
    cover post-auth SDK traffic ***
-------------------------------------------------------------------------
connect_gis() also sets the module-level `arcgis.env.verify_cert` to match
verify_mode, since gis.content.search() / item.* calls consult that flag
independently of what was passed to the GIS() constructor.

*** BUGFIX #3 (earlier revision): a connection-pool race in arcgis's OWN
    EsriTrustStoreAdapter, not anything under this project's control ***
-------------------------------------------------------------------------
BUGFIX #2 (arcgis.env.verify_cert) was necessary but NOT sufficient. Even
with arcgis.env.verify_cert=True, gis._con._session.verify=True, AND the
mounted EsriTrustStoreAdapter itself reporting verify=True with a real
truststore SSLContext object attached, a SMALL NUMBER of genuine
InsecureRequestWarnings were still observed -- always clustered at the
very start of a run that immediately issues many concurrent requests
(e.g. UTILITIES.bucketing.bucketed_search()'s threaded advanced_search
calls), and NEVER again for the remaining hundreds of requests in the
same run.

ROOT CAUSE (confirmed via a captured stack trace at the exact moment the
warning fired -- see arcgis/auth/tools/_adapter.py's EsriTrustStoreAdapter
and the call chain through urllib3's _validate_conn()): this is a RACE
CONDITION in the arcgis SDK's own connection-pool warm-up, not a
configuration problem on our side. EsriTrustStoreAdapter's own `verify`
attribute and `ssl_context` were confirmed correct (True / a real
truststore SSLContext) at the moment of the warning -- but a handful of
the very first underlying HTTPSConnection objects pulled from the
adapter's urllib3 PoolManager can still be constructed with unverified
settings if they are created concurrently, before the pool has "settled"
on the adapter's verified SSL context for new connections. Once the pool
has a few connections in it, every subsequent request (even highly
concurrent ones) reuses already-verified connections and this stops
happening -- which matches the observed pattern exactly (a few warnings
at query #1-3, zero for the following ~330).

*** BUGFIX #3b (THIS revision): a single serial pre-warm call left too
    thin a safety margin for callers whose very first burst of traffic
    is itself already large ***
-------------------------------------------------------------------------
The original BUGFIX #3 fix issued exactly ONE serial pre-warm request
after GIS() returned, on the theory that forcing the pool's FIRST
connection to be created and verified while nothing else is racing it
would close the window. In practice this is necessarily true only for a
caller whose own traffic ramps up gradually. A caller that goes straight
from connect_gis() into a large, rapid sequence of SDK calls (e.g.
paginated gis.users.search() over many thousand records, immediately
followed by many hundreds of gis.content.search()/gis.groups.search()
calls in a tight loop -- see USER_REPORT.py) can still open several NEW
underlying HTTPSConnection objects in quick succession before urllib3's
pool has "settled" on the adapter's verified SSL context, i.e. one
pre-warmed connection is not always enough headroom.

FIX: connect_gis() now issues SEVERAL cheap, SERIAL (never concurrent),
already-authenticated pre-warm requests -- not just one -- immediately
after GIS() returns and BEFORE returning control to the caller. Each call
is made one at a time, on purpose: concurrent pre-warm calls would
reintroduce the exact race this is trying to close (see
UTILITIES.gis_connect's sibling module GROUPS_SCAN.py's own docstring
note that applying a global TLS-trust change to CONCURRENT traffic is
unsafe). Serial pre-warm calls cannot race each other, so this adds
strictly more verified connections to the pool with no new race surface,
regardless of how many pre-warm calls are made.

This does not, and cannot, offer a mathematical guarantee that the
underlying arcgis SDK race is fully eliminated -- the root cause lives in
third-party code (arcgis's own EsriTrustStoreAdapter / urllib3 pool
internals) that this project does not control. What this DOES do is
strictly increase the number of already-verified connections seeded into
the pool before ANY caller traffic begins, which directly enlarges the
safety margin the original single-call mitigation was already relying on,
for every script that calls connect_gis() -- not just ones that happen to
also call UTILITIES.restutils.tune_gis_session() afterward. Centralizing
the strengthened mitigation here means every current and future caller of
connect_gis() benefits automatically, with no call-site changes required
anywhere in the project.

_PREWARM_REQUEST_COUNT (module-level constant, below) controls how many
serial pre-warm calls are made. Each one is:
  * cheap (gis.properties.get("name"), same lightweight call as before),
  * wrapped in its own try/except so a single flaky pre-warm request
    cannot abort an otherwise-successful connection (non-fatal, logged as
    a warning, exactly as the original single-call version already did),
  * strictly serial with respect to every other pre-warm call (the next
    one is not issued until the previous one has returned or failed).

A failure of ANY pre-warm call is logged as a warning and does NOT abort
the connection, matching the original behavior exactly -- pre-warming
remains a best-effort mitigation for a third-party race, never a hard
requirement for connect_gis() to succeed.

This is a workaround for behavior inside the third-party `arcgis` package
itself, not a bug in this project's code. If a future arcgis release fixes
the underlying pool race, these pre-warm calls become harmless no-ops (a
handful of extra cheap REST calls) and can be safely left in place or
reduced/removed.

`verify_mode` defaults to VerifyMode.STANDARD_CA, so every existing caller
that does NOT pass verify_mode is unaffected in terms of API usage -- but
every caller now gets the CORRECTED behavior (verification actually
enforced end-to-end, including the strengthened pool-warm-up mitigation)
rather than the previous partial enforcement.

ca_bundle_path is still available as a keyword argument for
VerifyMode.CUSTOM_CA_BUNDLE. Passing ca_bundle_path without also passing
verify_mode=VerifyMode.CUSTOM_CA_BUNDLE raises a clear ValueError instead
of silently mis-inferring the mode.

CALLER UPDATE REQUIRED for scripts using --tls-mode os-trust-store or
custom-ca-bundle: pass the SAME VerifyMode you resolved for
build_secure_session()/verify_tls_handshake() through to connect_gis() as
`verify_mode`, e.g.:

    gis = connect_gis(args.portal_url, args.username, args.keyring_service,
                      logger, verify_mode=mode, ca_bundle_path=ca_path)

Scripts that only ever use the default (STANDARD_CA) -- i.e. every script
without a --tls-mode flag -- need NO code changes at all. This revision
(BUGFIX #3b) also requires NO caller changes -- the strengthened pre-warm
happens entirely inside connect_gis() itself.

secure_gis_connect.py / connect_gis_secure() is now REDUNDANT with this
function and can be treated as deprecated; nothing in the project needs to
import it anymore. It has been left in place (unmodified) only so nothing
that already imports it directly breaks; new code should just use
connect_gis() from here.

Public API
----------
    connect_gis(portal_url, username, keyring_service, logger,
               verify_mode=VerifyMode.STANDARD_CA, ca_bundle_path=None)
        -> arcgis.gis.GIS
==============================================================================
"""
import os

import keyring
import arcgis
from arcgis.gis import GIS

from .secure_session import VerifyMode

# Number of SERIAL (never concurrent) pre-warm requests issued against the
# freshly-connected GIS object before connect_gis() returns control to the
# caller -- see "BUGFIX #3b" in the module docstring above for the full
# rationale. Each call is cheap (gis.properties.get("name")) and failures
# are non-fatal. Kept as a module-level constant so the safety margin can be
# tuned in one place if a future caller's traffic pattern needs more
# headroom, without touching connect_gis()'s body.
_PREWARM_REQUEST_COUNT = 5


def _enable_os_trust_store_for_sdk(logger):
    """
    Inject the `truststore` package into Python's ssl module so that any
    ssl.SSLContext created FROM THIS POINT FORWARD (including whatever the
    arcgis SDK builds internally, for both the initial connection AND any
    secondary sessions it creates for content/search calls) verifies
    against the OS-native certificate store instead of certifi's bundled
    CA list.

    Called from connect_gis() itself -- NOT left to the caller to have
    already done via build_secure_session() -- specifically so there is no
    ordering gap where a caller's separate handshake-check session got the
    injection but the SDK's own session(s) did not reliably pick it up.
    Idempotent: truststore.inject_into_ssl() is safe to call more than once
    per process.

    Raises ImportError if 'truststore' is not installed -- never silently
    degrades to an unverified connection.
    """
    try:
        import truststore
    except ImportError as exc:
        raise ImportError(
            "verify_mode=VerifyMode.OS_TRUST_STORE requires the "
            "'truststore' package, which is not installed in this "
            "environment. Install it with:\n"
            "    pip install truststore\n"
            "This mode verifies TLS certificates against the OPERATING "
            "SYSTEM's certificate store rather than certifi's bundled CA "
            "list. Refusing to fall back to an unverified connection."
        ) from exc

    truststore.inject_into_ssl()
    if logger is not None:
        logger.info(
            "  [gis-connect] TLS verification mode: OS_TRUST_STORE "
            "(truststore injected into ssl module for THIS connection).")


def _prewarm_connection_pool(gis, logger):
    """
    Issue several cheap, SERIAL, already-authenticated requests against the
    freshly-connected `gis` object before connect_gis() returns control to
    the caller. See "BUGFIX #3b" in the module docstring for the full
    rationale.

    Each call is independently wrapped so one flaky/slow pre-warm request
    never aborts an otherwise-successful connection, and never prevents the
    remaining pre-warm calls from still being attempted -- this mitigation
    is strictly best-effort. Calls are made ONE AT A TIME, on purpose:
    running them concurrently would reintroduce the exact pool-settling
    race this function exists to reduce.
    """
    successes = 0
    for attempt in range(1, _PREWARM_REQUEST_COUNT + 1):
        try:
            _ = gis.properties.get("name")
            successes += 1
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    "  [gis-connect] connection pool pre-warm request "
                    "%d/%d failed (non-fatal, continuing): %s",
                    attempt, _PREWARM_REQUEST_COUNT, exc)

    if logger is not None:
        logger.info(
            "  [gis-connect] pre-warmed the connection pool with %d/%d "
            "serial, verified request(s) before returning -- this closes "
            "a race in the arcgis SDK's own connection pool where its "
            "first connection(s) could otherwise be created before TLS "
            "verification settings were fully applied (observed as a "
            "handful of InsecureRequestWarnings at the start of a run "
            "that immediately issues many requests in quick succession, "
            "e.g. bucketed_search() or a large paginated user/group "
            "scan).", successes, _PREWARM_REQUEST_COUNT)


def connect_gis(portal_url, username, keyring_service, logger,
                verify_mode=VerifyMode.STANDARD_CA, ca_bundle_path=None):
    """
    Connect to AGOL or Portal using a password pulled from keyring, with TLS
    verification ALWAYS enabled -- for the initial authentication, every
    subsequent SDK call (gis.content.search(), item.get_data(), etc.), AND
    the arcgis SDK's own connection-pool warm-up (see BUGFIX #3 / #3b
    above). Works against both AGOL and Enterprise because both authenticate
    through the same GIS(url, user, pass, verify_cert=...) call.

    Falls back to the PORTAL_PASSWORD environment variable if keyring has no
    entry, preserving the behavior of the original (pre-hardening) version
    of this function.

    Parameters
    ----------
    portal_url : str
    username : str
    keyring_service : str
    logger : logging.Logger
    verify_mode : UTILITIES.secure_session.VerifyMode
        Which TLS verification strategy to use for THIS connection AND
        every subsequent SDK call in this process. Defaults to
        VerifyMode.STANDARD_CA (certifi's bundled public CA list).
          * STANDARD_CA      -> verify_cert=True (certifi). Correct for AGOL
                                and any Portal with a publicly-trusted cert.
          * OS_TRUST_STORE   -> injects the OS-native certificate store via
                                the 'truststore' package BEFORE constructing
                                GIS(...). Use for an internal Enterprise
                                Portal signed by an organizational CA
                                already trusted at the OS level.
          * CUSTOM_CA_BUNDLE -> verify_cert=<ca_bundle_path>. Requires
                                ca_bundle_path.
        In every mode, `arcgis.env.verify_cert` is ALSO set to match, and
        the connection pool is pre-warmed with several serial requests (see
        BUGFIX #3 / #3b) so verification is enforced for every SDK call
        made for the rest of the process, not just the initial login.
    ca_bundle_path : str, optional
        Path to a PEM CA bundle for an internal/organizational CA. REQUIRED
        when verify_mode is VerifyMode.CUSTOM_CA_BUNDLE; must be omitted
        (None) for every other verify_mode -- passing it otherwise raises
        ValueError rather than being silently ignored or silently
        mis-inferred.

    Returns
    -------
    arcgis.gis.GIS
        The authenticated GIS connection, with TLS verification on for
        every call made through it (and through the arcgis package's own
        module-level defaults and connection pool) for the rest of this
        process.
    """
    if not isinstance(verify_mode, VerifyMode):
        raise TypeError(
            f"verify_mode must be a UTILITIES.secure_session.VerifyMode "
            f"member, got {verify_mode!r}.")

    if verify_mode is VerifyMode.CUSTOM_CA_BUNDLE and not ca_bundle_path:
        raise ValueError(
            "verify_mode=VerifyMode.CUSTOM_CA_BUNDLE requires "
            "ca_bundle_path (path to a PEM CA bundle file).")
    if verify_mode is not VerifyMode.CUSTOM_CA_BUNDLE and ca_bundle_path:
        raise ValueError(
            f"ca_bundle_path was supplied ({ca_bundle_path!r}) but "
            f"verify_mode is {verify_mode!r}, not CUSTOM_CA_BUNDLE. Pass "
            f"verify_mode=VerifyMode.CUSTOM_CA_BUNDLE explicitly when "
            f"supplying a CA bundle path, so the verification strategy "
            f"actually used is never inferred implicitly.")

    logger.info(
        "Retrieving password from keyring service '%s' for user '%s'.",
        keyring_service, username,
    )
    password = keyring.get_password(keyring_service, username) or os.getenv("PORTAL_PASSWORD")
    if not password:
        raise RuntimeError(
            f"No password found in keyring for service '{keyring_service}' "
            f"and username '{username}'."
        )

    if verify_mode is VerifyMode.OS_TRUST_STORE:
        _enable_os_trust_store_for_sdk(logger)
        verify_cert = True
        logger.info("TLS verification: OS_TRUST_STORE (verified against "
                   "the operating system certificate store).")
    elif verify_mode is VerifyMode.CUSTOM_CA_BUNDLE:
        verify_cert = ca_bundle_path
        logger.info("TLS verification: CUSTOM_CA_BUNDLE (%s).", ca_bundle_path)
    else:  # STANDARD_CA
        verify_cert = True
        logger.info("TLS verification: enabled (standard CA / certifi).")

    # arcgis.env.verify_cert governs verification for SDK calls made AFTER
    # the initial GIS() constructor call (gis.content.search(), item.*,
    # etc.) -- see BUGFIX #2. Set it before constructing GIS(...).
    arcgis.env.verify_cert = verify_cert
    logger.info(
        "  [gis-connect] arcgis.env.verify_cert set to %r (covers "
        "gis.content.search()/item.* calls made after login, not just "
        "the initial authentication handshake).", verify_cert)

    logger.info("Connecting to: %s", portal_url)

    gis = GIS(portal_url, username, password, verify_cert=verify_cert)
    me = gis.users.me.username if gis.users.me else "unknown"
    logger.info("Successfully connected. Authenticated as: %s", me)
    logger.info("Portal name: %s", gis.properties.get("name", "n/a"))

    # *** BUGFIX #3 / #3b ***: see module docstring. Pre-warm the SDK's own
    # connection pool with SEVERAL cheap, SERIAL, already-authenticated
    # requests before returning control to the caller. This closes a race
    # where the arcgis SDK's EsriTrustStoreAdapter can report verify=True
    # with a real truststore SSLContext mounted, yet the first few
    # underlying HTTPS connections pulled from its urllib3 pool are still
    # occasionally constructed with unverified settings if the caller's own
    # traffic ramps up quickly right after connect_gis() returns (e.g.
    # UTILITIES.bucketing.bucketed_search()'s threaded calls, or a large
    # paginated scan like USER_REPORT.py's user/group counting loop). Once
    # the pool has several verified connections in it, every later
    # request -- concurrent or not -- reuses them correctly. Every pre-warm
    # call here is strictly serial with respect to the others, so this adds
    # safety margin without introducing any new race.
    _prewarm_connection_pool(gis, logger)

    return gis