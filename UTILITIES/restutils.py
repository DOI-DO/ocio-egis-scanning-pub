"""
Shared REST / HTTP helpers used across the PORTAL_* scripts.

Everything here was previously copy-pasted into individual scripts:

  * tune_gis_session / build_pooled_session - a retrying, connection-pooled
    requests.Session (either tuning the GIS connection's own session or
    building a standalone one).
  * rest_search_page - one paged /sharing/rest/search call (per bucket).
  * search_hosted_feature_service_dicts / _items - hosted-feature-service
    enumeration via the shared bucketed search.
  * get_json / count_query - low-level GET-as-JSON with ArcGIS REST error
    parsing, and the cheapest proof-of-life count query.
  * get_org_id - resolve the authenticated org id (for orgid: scoping).
  * epoch_to_str / epoch_to_date - epoch-ms -> formatted date string.

CONSOLIDATION NOTE
------------------
This module also now carries three small, single-purpose helpers that
previously lived in their own one-function files (platform_detect.py,
timestamps.py, runtime.py). They had no dependencies of their own and were
used everywhere restutils already is, so folding them in here reduces the
number of files in UTILITIES without changing any behavior or call signature:

  * get_platform(portal_url)              (was platform_detect.py)
  * build_timestamp()                     (was timestamps.py)
  * now_wallclock/start_timer/format_elapsed/elapsed_since/timing_block
                                           (was runtime.py)

Every function keeps its original name and signature, so
`from UTILITIES import get_platform, build_timestamp, start_timer, ...`
continues to work unchanged via __init__.py.

*** HARDENED REST CONSOLIDATION (this revision) ***
-------------------------------------------------------------------------
The hardened direct-REST machinery that was copy-pasted verbatim into 10-12
scanner scripts (ITEM_INVENTORY.py, GET_RELATED_ITEMS.py, GROUPS_SCAN.py,
EMPTY_FEATURE_SERVICES.py, CORRELATE_SQLITE_REPLICAS.py,
PORTAL_SHARED_CREDENTIALS_SCAN.py, FIELD_NAME_SCAN.py,
SCAN_BROKEN_HOSTED_SERVICES.py, ...) now lives here ONCE:

  * RestCallError        - sanitized direct-REST failure carrying structured
                           classification flags (tls_error, auth_error,
                           policy_error, transient_error, transport_error,
                           status_code, rest_code). This is the UNION of every
                           per-script RestCallError: any flag any script set is
                           preserved here, so no caller loses a classification
                           it relied on.
  * WorkerSessionPool    - one pre-built, TLS-consistent requests.Session per
                           worker thread (OS-trust-store injection is
                           process-global, so first-use construction is
                           serialized). Supports an optional initial_session
                           pinned to the creating thread (as ITEM_INVENTORY /
                           GET_RELATED_ITEMS / SCAN_BROKEN_HOSTED_SERVICES did)
                           AND a borrow()/context-manager lending model (as
                           NAMED_USER_LICENSE_INVENTORY did). Both access styles
                           are supported so every former caller keeps working.
  * FixedSessionPool     - trivial adapter exposing .get() for a single,
                           already-built Session (used by the backward-compat
                           wrappers in several scripts).
  * ArcGISRestClient     - strict JSON client with explicit size caps,
                           redirect validation, cross-origin refusal, bounded
                           retries, and single token-refresh-and-retry on auth
                           failure. It validates every token-bearing URL
                           through a caller-supplied TrustedUrlPolicy (from
                           UTILITIES.secure_session), so SSRF / token-
                           exfiltration protection is identical across scripts.
  * _safe_org_id         - validate an org id string before it is interpolated
                           into a Lucene query (prevents an empty/garbage org
                           id from silently broadening a scan).
  * _normalize_portal_base - canonicalize a Portal/AGOL base URL (HTTPS only,
                           no credentials/query/fragment, strip trailing
                           /home), the shared pre-flight every script applied.

These raise ScanAbort (from UTILITIES.auth) for hard, run-invalidating
problems, exactly as the per-script copies did.

DECOUPLING FROM THE SEARCH API
------------------------------
Different callers search differently:
  * gis.content.search(...) -> returns arcgis Item objects
  * raw /sharing/rest/search -> returns dicts
So the actual search is supplied by the caller as a callback:

    search_fn(query: str, cap: int) -> list

The callback receives a Lucene query string (already including the date range
and any caller-supplied extra clause) and the cap, and returns a list of
results. The module only handles the partitioning, recursion, progress, and
de-duplication. The caller also supplies id_fn to extract an id from a result.

SESSION BUILDING
----------------
build_pooled_session() is the single shared entry point for a standalone
pooled + retrying requests.Session. It accepts backoff_factor and pool sizes
so scripts that previously hand-rolled their own session (e.g. a heavier
backoff for very large deployment-wide scans, or verify=False for internal
self-signed certs) can use the shared builder instead of duplicating the
HTTPAdapter/Retry wiring. Defaults reproduce the historical behavior, so
existing callers are unaffected.

ORG SCOPING
-----------
bucketed_hosted_fs() can restrict its enumeration to a single organization via
an 'orgid:<id>' Lucene clause. This matters for AGOL, where an unscoped search
can return Feature Service items owned by OTHER organizations (federated /
public content). On Enterprise Portal every item is already in the local org,
so scoping is a harmless no-op there. Pass org_only=True (and optionally an
explicit org_id; it is auto-resolved via get_org_id() when omitted) to enable
it.

DATE HELPERS
------------
Two functions, kept separate on purpose:

  * epoch_to_str(epoch_ms, fmt, utc) - the original, epoch-MILLISECONDS-only
    converter used across nearly every script. UNCHANGED; every existing caller
    behaves exactly as before.
  * epoch_to_date(value, fmt, logger) - a SUPERSET converter that also accepts
    datetime/date objects, epoch-SECONDS, numeric strings, and passes
    non-numeric strings through unchanged. It consolidates the three
    near-identical local date helpers that previously lived in
    PORTAL_GROUPS_INVENTORY.py, PORTAL_INVENTORY_FS_REPLICAS.py, and
    DATA_STORE_INVENTORY_SDE_REPLICAS.py. Use this when the incoming value's
    shape is not guaranteed to be epoch-ms.
"""

import datetime
import json
import re
import time
from threading import Lock, local, get_ident
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .bucketing import bucketed_search, DEFAULT_CAP
from .auth import ScanAbort
from .secure_session import build_secure_session

# Sensible shared defaults; callers may override per call.
DEFAULT_TIMEOUT = 30
DEFAULT_RETRY_TOTAL = 3
DEFAULT_PAGE_SIZE = 100
DEFAULT_BACKOFF_FACTOR = 0.3

# Standard hosted-feature-service Lucene clause.
HOSTED_FS_CLAUSE = 'type:"Feature Service" AND typekeywords:"Hosted Service"'

# Hardened-client shared limits (union of the per-script constants).
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_REDIRECTS = 3

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:401|403|498|499)\b|invalid\s+token|"
    r"token\s+(?:is\s+)?expired|token\s+required|"
    r"authentication\s+token\s+required)"
)


# ==========================================================================
# Platform label (was platform_detect.py)
# ==========================================================================
# *** NO ORG-SPECIFIC OR HARD-CODED URLS HERE, EVER ***
# There used to be two literal URL constants here (one org's Portal URL, one
# org's AGOL URL) that get_platform() matched against. That is exactly the
# pattern this project must never repeat: portal-url is ALWAYS read at
# runtime from the org's --config toml (via finalize_config() ->
# args.portal_url), never written into any .py file. A literal URL baked
# into source is (a) wrong the moment a second org is added -- which is
# precisely how the original AGOL "UNKNOWN" bug happened -- and (b) a
# maintenance trap even if left in "only as an unused reference" constant,
# since it invites some future script to import and depend on it instead of
# reading config. get_platform() below classifies GENERICALLY, from the
# shape of whatever URL it is given, with no org-specific literals at all.

# Any hostname ending in one of these suffixes (case-insensitive) is an Esri-
# hosted ArcGIS Online organization, regardless of the org's short name.
# ".arcgis.com" (rather than just "maps.arcgis.com") is matched so other
# Esri-hosted AGOL subdomains are also caught. This is a STRUCTURAL fact
# about how Esri names AGOL orgs, not a specific organization's URL, so it
# is not subject to the "never hard-code a URL" rule above.
_AGOL_HOST_SUFFIX = ".arcgis.com"


def get_platform(portal_url):
    """
    Return the PLATFORM label ('PORTAL' or 'AGOL') for a given portal URL.

    Classification is GENERIC (host-suffix based), not a lookup against a
    fixed list of known organization URLs -- see the bugfix note in the
    module docstring for why. `portal_url` must be supplied by the caller,
    always sourced from the chosen org's --config toml at runtime (never a
    literal in source). Any URL whose hostname ends in ".arcgis.com" (e.g.
    "https://org.maps.arcgis.com/home", "https://OTHER_ORGldt.maps.arcgis.com/home",
    or any other org's AGOL URL) is "AGOL". Any other URL with a resolvable
    hostname (e.g. an Enterprise Portal URL like
    "https://enterprise.com/portal") is "PORTAL", since Enterprise Portal
    is self-hosted and never lives on Esri's arcgis.com domain. A blank or
    unparseable URL returns "UNKNOWN" so downstream output still has a value
    in the PLATFORM column.
    """
    normalized = (portal_url or "").strip()
    if not normalized:
        return "UNKNOWN"

    try:
        parsed = urlsplit(normalized if "://" in normalized
                          else f"https://{normalized}")
        host = (parsed.hostname or "").strip().lower()
    except Exception:
        host = ""

    if not host:
        return "UNKNOWN"

    if host.endswith(_AGOL_HOST_SUFFIX):
        return "AGOL"

    return "PORTAL"


# ==========================================================================
# Timestamp (was timestamps.py)
# ==========================================================================
def build_timestamp():
    """Return the shared YYYYMMDD_HHMM timestamp string used everywhere."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M")


# ==========================================================================
# Run timing (was runtime.py)
# ==========================================================================
# Human-readable wall-clock format used in email bodies, e.g.
# "2026-08-15 13:30:07".
WALLCLOCK_FMT = "%Y-%m-%d %H:%M:%S"


def now_wallclock():
    """Return the current local wall-clock time formatted for display."""
    return datetime.datetime.now().strftime(WALLCLOCK_FMT)


def start_timer():
    """
    Begin timing a run. Returns (start_wallclock_str, start_perf) where
    start_perf is a high-resolution counter to pair with elapsed_since().
    """
    return now_wallclock(), time.perf_counter()


def format_elapsed(seconds):
    """
    Convert a number of seconds into a friendly H:MM:SS.s string,
    e.g. 207.4 -> "0:03:27.4".
    """
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{int(hrs)}:{int(mins):02d}:{secs:04.1f}"


def elapsed_since(start_perf):
    """Return seconds elapsed since a start_perf from start_timer()."""
    return time.perf_counter() - start_perf


def timing_block(start_wallclock, start_perf):
    """
    Build the shared three-line timing block for an email body:

        Start time    : 2026-08-15 13:30:07
        End time      : 2026-08-15 13:33:34
        Elapsed       : 0:03:27.4

    Captures the end time and elapsed at the moment it is called, so call it
    right before composing the email.
    """
    end_wallclock = now_wallclock()
    elapsed = format_elapsed(elapsed_since(start_perf))
    return (
        f"Start time    : {start_wallclock}\n"
        f"End time      : {end_wallclock}\n"
        f"Elapsed       : {elapsed}\n"
    )


# ==========================================================================
# Sessions
# ==========================================================================
def _build_retry(total, backoff=DEFAULT_BACKOFF_FACTOR):
    """
    Build a urllib3 Retry. `backoff` is exposed so callers that want a heavier
    backoff (e.g. large deployment-wide scans that hit throttling) can raise it
    without hand-rolling their own Retry. Defaults to the historical 0.3.
    """
    return Retry(
        total=total,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        allowed_methods={"GET", "POST"},
    )


def tune_gis_session(gis, workers, timeout=DEFAULT_TIMEOUT,
                     retry_total=DEFAULT_RETRY_TOTAL,
                     backoff_factor=DEFAULT_BACKOFF_FACTOR):
    """
    Tune the GIS connection's underlying requests.Session with retry +
    connection pooling sized for the worker pool. Returns the session.
    """
    session = gis._con._session
    adapter = HTTPAdapter(pool_connections=workers,
                          pool_maxsize=workers * 2,
                          max_retries=_build_retry(retry_total, backoff_factor))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    gis._con.timeout = timeout
    return session


def build_pooled_session(pool_connections=64, pool_maxsize=128,
                         retry_total=2, verify=None,
                         backoff_factor=DEFAULT_BACKOFF_FACTOR):
    """
    Build a standalone pooled + retrying requests.Session.

    Parameters
    ----------
    pool_connections, pool_maxsize : int
        HTTPAdapter connection-pool sizes. Size these to the worker count so
        concurrent requests don't queue behind a starved pool.
    retry_total : int
        Total retry attempts (retries on 429/500/502/503/504, GET/POST).
    verify : bool or None
        TLS verification. Left unset (None) keeps requests' default; pass
        False for internal self-signed certs.
    backoff_factor : float
        urllib3 backoff factor. Raise it (e.g. 1.5) for very large scans that
        provoke throttling; the default 0.3 matches the historical behavior.
    """
    session = requests.Session()
    if verify is not None:
        session.verify = verify
    adapter = HTTPAdapter(pool_connections=pool_connections,
                          pool_maxsize=pool_maxsize,
                          max_retries=_build_retry(retry_total, backoff_factor))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ==========================================================================
# Hardened direct-REST infrastructure (consolidated from the scanner scripts)
# ==========================================================================
def _safe_org_id(value):
    """
    Validate an organization id before it is interpolated into a Lucene query
    or a trust decision. A blank or malformed org id must never be allowed to
    silently broaden a scan to cross-organization content, so this fails
    closed with ScanAbort rather than returning a usable-looking empty value.
    """
    text = str(value or "").strip()
    if not text or not _ORG_ID_RE.fullmatch(text):
        raise ScanAbort(f"Invalid or unavailable organization id: {text!r}")
    return text


def _validated_path(parts):
    """
    Validate and canonicalize a URL path for safety (shared by the client and
    _normalize_portal_base). Rejects encoded slashes/backslashes, control
    characters, and traversal; returns the path with a trailing slash removed.
    Raises RestCallError on an unsafe path so the caller classifies it as a
    policy error rather than a transport failure.
    """
    if parts.query or parts.fragment:
        raise RestCallError("Request URL must not contain a query or fragment.",
                            policy_error=True)
    raw_path = parts.path or ""
    lowered = raw_path.lower()
    if "%2f" in lowered or "%5c" in lowered:
        raise RestCallError(
            "Request URL contains an encoded slash or backslash.",
            policy_error=True)
    decoded = unquote(raw_path)
    if "\\" in decoded or any(ord(ch) < 32 for ch in decoded):
        raise RestCallError("Request URL contains unsafe path characters.",
                            policy_error=True)
    segments = [segment for segment in decoded.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise RestCallError("Request URL contains path traversal.",
                            policy_error=True)
    return raw_path.rstrip("/")


def _normalize_portal_base(raw):
    """
    Canonicalize a Portal/AGOL base URL exactly as every scanner did before
    talking to it: HTTPS only, no embedded credentials, no query/fragment, no
    unsafe path, with a trailing '/home' stripped. Raises ScanAbort on a URL
    that cannot be made safe.
    """
    text = str(raw or "").strip()
    if not text:
        raise ScanAbort("Portal URL is blank.")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ScanAbort(f"Portal URL could not be parsed: {text!r}") from exc
    if parts.scheme.lower() != "https":
        raise ScanAbort(f"Portal URL must use HTTPS: {text!r}")
    if not parts.hostname:
        raise ScanAbort(f"Portal URL contains no hostname: {text!r}")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ScanAbort(f"Portal URL contains an invalid port: {text!r}") from exc
    if parts.username is not None or parts.password is not None:
        raise ScanAbort("Portal URL must not contain user-info credentials.")
    if parts.query or parts.fragment:
        raise ScanAbort("Portal URL must not contain a query or fragment.")
    try:
        path = _validated_path(parts)
    except RestCallError as exc:
        raise ScanAbort(f"Portal URL is unsafe: {exc}") from exc
    if path.lower().endswith("/home"):
        path = path[:-5]
    return urlunsplit(("https", parts.netloc, path, "", "")).rstrip("/")


def _origin_tuple(raw):
    """Return (scheme, host, port) for a URL, or None if it cannot be parsed."""
    try:
        parts = urlsplit(str(raw or "").strip())
        if not parts.hostname:
            return None
        return (parts.scheme.lower(),
                parts.hostname.lower().rstrip("."),
                parts.port or 443)
    except Exception:
        return None


class RestCallError(RuntimeError):
    """
    Sanitized direct-REST failure carrying structured classification metadata.

    This is the UNION of every per-script RestCallError seen across the
    project: callers may inspect any of the flags below. A caller that only
    cared about auth_error still works; a caller that also branched on
    policy_error / transient_error / transport_error / status_code / rest_code
    keeps that behavior. The message is passed through redact at the call site
    (scripts already wrap str(exc) in redact_token), so the raw message is
    retained here verbatim for those wrappers.
    """

    def __init__(self, message, *, tls_error=False, auth_error=False,
                 policy_error=False, transient_error=False,
                 transport_error=False, status_code=None, rest_code=None):
        super().__init__(str(message))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)
        self.policy_error = bool(policy_error)
        self.transient_error = bool(transient_error)
        self.transport_error = bool(transport_error)
        self.status_code = status_code
        self.rest_code = rest_code


class FixedSessionPool:
    """
    Trivial adapter exposing .get() for a single, already-built Session.

    Used by the backward-compatible wrapper functions in several scripts, which
    pass an existing requests.Session where an ArcGISRestClient expects a pool.
    """

    def __init__(self, session):
        self._session = session

    def get(self):
        return self._session


class WorkerSessionPool:
    """
    One pre-built, TLS-consistent requests.Session per worker thread.

    OS-trust-store injection (VerifyMode.OS_TRUST_STORE) mutates process-global
    SSL state, so first-use session construction is serialized under a lock.
    This class supports BOTH access patterns found across the scripts, so every
    former caller keeps working:

      1. Thread-pinned .get() (ITEM_INVENTORY, GET_RELATED_ITEMS,
         SCAN_BROKEN_HOSTED_SERVICES, CORRELATE_SQLITE_REPLICAS,
         PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR):
         each worker thread is assigned one session for its lifetime. An
         optional `initial_session` is pinned to the creating thread (so the
         already-verified startup session is reused on the main thread), and up
         to `capacity` additional sessions are pre-built for the workers.

      2. borrow() context manager (NAMED_USER_LICENSE_INVENTORY):
         sessions are lent one task at a time from a fixed pool and returned,
         never shared concurrently. Use this when tasks are short and you want
         strict one-session-per-active-task semantics.

    Do not mix the two styles on the same pool instance; pick the one the
    calling script used.
    """

    def __init__(self, mode, ca_bundle_path=None, initial_session=None,
                 capacity=1):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._create_lock = Lock()

        # Thread-pinned state.
        self._local = local()
        self._assignment_lock = Lock()
        self._assignments = {}
        self._next = 0

        # All live sessions (for close()).
        self._sessions = []

        # Optional initial session pinned to the creating thread.
        self._initial_session = initial_session
        self._initial_thread = get_ident() if initial_session is not None else None
        if initial_session is not None:
            self._sessions.append(initial_session)

        # Pre-built worker sessions (shared by both access styles).
        self._worker_sessions = []
        try:
            for _ in range(max(1, int(capacity))):
                session = self._new_session()
                self._worker_sessions.append(session)
                self._sessions.append(session)
        except Exception:
            self.close()
            raise

        # borrow() queue is created lazily from the pre-built worker sessions.
        self._borrow_queue = None

    def _new_session(self):
        # Serialize construction so OS-trust-store injection is never raced by
        # multiple first-use worker threads.
        with self._create_lock:
            return build_secure_session(
                self._mode,
                ca_bundle_path=self._ca_bundle_path,
                pool_connections=2,
                pool_maxsize=4,
                retry_total=3,
                logger=None,
            )

    # -- Access style 1: thread-pinned -----------------------------------
    def get(self):
        thread_id = get_ident()
        if (self._initial_session is not None
                and thread_id == self._initial_thread):
            return self._initial_session
        with self._assignment_lock:
            assigned = self._assignments.get(thread_id)
            if assigned is not None:
                return assigned
            if self._next < len(self._worker_sessions):
                assigned = self._worker_sessions[self._next]
            else:
                # More distinct worker threads than pre-built sessions: round
                # robin as a defensive fallback (never shared concurrently in
                # practice because the thread pool's live-worker count is
                # bounded by the pool's configured maximum).
                assigned = self._worker_sessions[
                    self._next % len(self._worker_sessions)]
            self._next += 1
            self._assignments[thread_id] = assigned
            return assigned

    # -- Access style 2: borrow()/return ---------------------------------
    def _ensure_borrow_queue(self):
        if self._borrow_queue is None:
            from queue import LifoQueue
            queue = LifoQueue()
            for session in self._worker_sessions:
                queue.put(session)
            self._borrow_queue = queue
        return self._borrow_queue

    def borrow(self):
        from contextlib import contextmanager

        @contextmanager
        def _lease():
            queue = self._ensure_borrow_queue()
            session = queue.get()
            try:
                yield session
            finally:
                queue.put(session)

        return _lease()

    def close(self):
        seen = set()
        for session in self._sessions:
            if id(session) in seen:
                continue
            seen.add(id(session))
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()
        self._worker_sessions.clear()
        self._assignments.clear()
        self._borrow_queue = None


class ArcGISRestClient:
    """
    Strict JSON client with size caps, redirect validation, cross-origin
    refusal, bounded retries, and single token-refresh-and-retry on an auth
    failure. This is the consolidated UNION of the per-script hardened clients.

    A caller-supplied TrustedUrlPolicy (from UTILITIES.secure_session) decides
    which URLs may receive the organization token. Two request entry points are
    provided:

      * portal_json(path, ...)   - path is an absolute "/sharing/rest/..."
                                   (or "/portaladmin/...") path; the policy
                                   validates it against the authenticated
                                   Portal origin.
      * service_json(url, ...)   - url is a full service URL; the policy
                                   validates it is an approved service origin
                                   (same-origin Portal, registered federated
                                   server, or an Esri *.arcgis.com AGOL host).

    Both apply: explicit Content-Length / streamed-size caps (MAX_JSON_BYTES),
    at most MAX_REDIRECTS redirects each re-validated through the policy, a hard
    refusal of cross-origin redirects for token-bearing requests, ArcGIS REST
    error parsing, and exactly one token refresh-and-retry when the first
    attempt fails with an authentication error.

    The policy object must expose:
        validate_portal_path(path) -> url          (or validate_portal_url)
        validate_portal_url(url)   -> url
        validate_service_url(url)  -> url
    Scripts whose policies use slightly different method names can adapt with a
    thin wrapper, but the names above are the consolidated convention.
    """

    def __init__(self, policy, session_pool, token_provider, gis=None,
                 logger=None):
        self.policy = policy
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.gis = gis
        self.logger = logger
        self._refresh_lock = Lock()

    # -- public entry points ---------------------------------------------
    def portal_json(self, path, params=None, timeout=DEFAULT_TIMEOUT):
        if hasattr(self.policy, "validate_portal_path"):
            url = self.policy.validate_portal_path(path)
        else:
            # Policies that only expose validate_portal_url expect a full URL;
            # build one against the policy's portal_base when available.
            base = getattr(self.policy, "portal_base", "")
            full = path if str(path).startswith("http") else base + path
            url = self.policy.validate_portal_url(full)
        return self._request_with_refresh(
            url, params, timeout, self.policy.validate_portal_url)

    def service_json(self, url, params=None, timeout=DEFAULT_TIMEOUT):
        safe_url = self.policy.validate_service_url(url)
        return self._request_with_refresh(
            safe_url, params, timeout, self.policy.validate_service_url)

    # -- token refresh ---------------------------------------------------
    def _refresh_if_stale(self, failed_token):
        with self._refresh_lock:
            current = self.token_provider.get()
            if current != failed_token:
                return current
            if self.gis is not None:
                try:
                    _ = self.gis.properties.get("name")
                except Exception:
                    pass
            try:
                self.token_provider.refresh()
                refreshed = self.token_provider.get()
            except Exception as exc:
                raise RestCallError(
                    f"token refresh failed: {exc}", auth_error=True) from exc
            if not refreshed:
                raise RestCallError(
                    "token refresh returned no token", auth_error=True)
            return refreshed

    def _request_with_refresh(self, url, params, timeout, redirect_validator):
        token = self.token_provider.get()
        try:
            return self._request_once(
                url, params, timeout, token, redirect_validator)
        except RestCallError as exc:
            if not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._request_once(
            url, params, timeout, refreshed, redirect_validator)

    # -- core request ----------------------------------------------------
    def _request_once(self, url, params, timeout, token, redirect_validator):
        merged = {"f": "json"}
        if params:
            merged.update(params)
        # A caller cannot smuggle or replace the token through params.
        merged.pop("token", None)
        merged["token"] = token

        original_origin = _origin_tuple(url)
        current_url = url
        response = None
        for redirect_count in range(MAX_REDIRECTS + 1):
            try:
                response = self.session_pool.get().get(
                    current_url,
                    params=merged,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.Timeout as exc:
                raise RestCallError(
                    f"timeout after {timeout}s",
                    transient_error=True, transport_error=True) from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True, transport_error=True) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(
                    f"request failed: {exc}", transport_error=True) from exc

            status = response.status_code
            if not 300 <= status < 400:
                break
            location = response.headers.get("Location")
            response.close()
            response = None
            if redirect_count >= MAX_REDIRECTS:
                raise RestCallError(
                    "Too many redirects for a token-bearing request.",
                    policy_error=True)
            if not location:
                raise RestCallError(
                    f"HTTP {status} redirect supplied no Location header.",
                    transport_error=True)
            candidate = redirect_validator(urljoin(current_url, location))
            if _origin_tuple(candidate) != original_origin:
                raise RestCallError(
                    "Refusing a cross-origin redirect for a token-bearing "
                    "request.", policy_error=True)
            current_url = candidate

        if response is None:
            raise RestCallError("Request did not produce a response.",
                                transport_error=True)

        status = response.status_code
        reason = response.reason or ""
        auth_status = status in (401, 403, 498, 499)
        transient_status = status == 429 or status >= 500

        # Size cap: declared Content-Length.
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except (TypeError, ValueError) as exc:
                response.close()
                raise RestCallError(
                    f"invalid Content-Length header: {declared!r}",
                    transport_error=True) from exc
            if declared_size < 0 or declared_size > MAX_JSON_BYTES:
                response.close()
                raise RestCallError(
                    f"JSON response exceeded the {MAX_JSON_BYTES // (1024 * 1024)} "
                    "MB limit", policy_error=True)

        # Size cap: streamed body.
        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_JSON_BYTES:
                    raise RestCallError(
                        f"JSON response exceeded the "
                        f"{MAX_JSON_BYTES // (1024 * 1024)} MB limit",
                        policy_error=True)
                chunks.append(chunk)
        except RestCallError:
            raise
        except requests.exceptions.SSLError as exc:
            raise RestCallError(
                f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
                tls_error=True, transport_error=True) from exc
        except requests.exceptions.RequestException as exc:
            raise RestCallError(
                f"request failed while reading: {exc}",
                transport_error=True) from exc
        finally:
            response.close()

        body = b"".join(chunks)
        if not body:
            raise RestCallError(
                f"HTTP {status} {reason}: empty response body",
                auth_error=auth_status, transient_error=transient_status,
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status)
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RestCallError(
                f"non-JSON response (HTTP {status})",
                auth_error=auth_status, transient_error=transient_status,
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status) from exc
        if not isinstance(data, dict):
            raise RestCallError(
                "endpoint returned a non-object JSON response",
                status_code=status)

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
                transient_error=(code == "429"
                                 or (code.isdigit() and int(code) >= 500)),
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status, rest_code=code)
        if status >= 400:
            raise RestCallError(
                f"HTTP {status} {reason}",
                auth_error=auth_status, transient_error=transient_status,
                transport_error=(status in (408, 425) or status >= 500),
                status_code=status)
        if str(data.get("status", "")).strip().lower() == "error":
            messages = data.get("messages")
            rendered = (f"endpoint returned status=error: "
                        f"{messages or 'no message supplied'}")
            raise RestCallError(
                rendered, auth_error=bool(_AUTH_ERROR_RE.search(rendered)))
        return data


# ==========================================================================
# Search paging
# ==========================================================================
def rest_search_page(gis, base_url, query, cap, session=None,
                     timeout=DEFAULT_TIMEOUT, page_size=DEFAULT_PAGE_SIZE):
    """
    Run one /sharing/rest/search for a Lucene query and page through it up to
    the cap. Returns a list of item dicts. This is the per-bucket search that
    the shared bucketed_search() calls once per (already small enough) window.
    """
    session = session or gis._con._session
    search_url = f"{base_url}/sharing/rest/search"
    results = []
    start = 1

    while len(results) < cap:
        num_to_get = min(page_size, cap - len(results))
        if start + num_to_get - 1 > cap:
            break
        params = {
            "f": "json",
            "token": gis._con.token,
            "q": query,
            "sortField": "modified",
            "sortOrder": "desc",
            "start": start,
            "num": num_to_get,
        }
        r = session.get(search_url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        items = data.get("results", []) or data.get("items", [])
        if not items:
            break
        results.extend(items)
        start += len(items)
        total = data.get("total", len(results))
        if len(results) >= total:
            break
    return results


def get_org_id(gis, logger=None):
    """Return the authenticated org id, or None if it cannot be determined."""
    org_id = None
    try:
        org_id = gis.properties.get("id")
    except Exception:
        org_id = None
    if not org_id:
        try:
            me = gis.users.me
            org_id = getattr(me, "orgId", None)
        except Exception:
            org_id = None
    return org_id


def bucketed_hosted_fs(gis, base_url, extra_clause=HOSTED_FS_CLAUSE,
                       session=None, logger=None, timeout=DEFAULT_TIMEOUT,
                       org_only=False, org_id=None):
    """
    Enumerate hosted feature service item dicts via the shared date-range
    bucketing utility (partitions on 'created'), returning {id: dict}.

    Org scoping
    -----------
    When org_only is True the search is restricted to a single organization by
    AND-ing an 'orgid:<id>' clause into every bucket query. This is important
    for AGOL, where an unscoped hosted-feature-service search can return items
    owned by OTHER organizations (federated / public content); scoping keeps the
    scan to the authenticated org only. On Enterprise Portal every item already
    belongs to the local org, so enabling this is a harmless no-op.

    The org id is taken from `org_id` when supplied, otherwise resolved via
    get_org_id(gis). If org_only is True but no org id can be determined, the
    scoping clause is skipped and a warning is logged (the scan still runs, just
    unscoped) so a lookup failure never silently returns nothing.
    """
    clause = extra_clause
    if org_only:
        resolved_org = org_id or get_org_id(gis, logger=logger)
        if resolved_org:
            org_clause = f"orgid:{resolved_org}"
            clause = (f"{extra_clause} AND {org_clause}" if extra_clause
                      else org_clause)
            if logger is not None:
                logger.info("Scoping hosted-FS search to org id %s.",
                            resolved_org)
        elif logger is not None:
            logger.warning(
                "org_only requested but no org id could be resolved; "
                "running the hosted-FS search UNSCOPED.")

    def search_fn(query, cap):
        return rest_search_page(gis, base_url, query, cap, session=session,
                                timeout=timeout)

    return bucketed_search(
        search_fn,
        date_field="created",
        extra_clause=clause,
        cap=DEFAULT_CAP,
        logger=logger,
    )


def resolve_items(gis, ids, logger=None, skip_quiet_substrings=()):
    """
    Resolve a list of item ids to arcgis Item objects via gis.content.get().

    Returns (items, skipped_count). Errors whose message contains any of
    `skip_quiet_substrings` are counted silently; all other errors are logged
    as warnings.
    """
    items = []
    skipped = 0
    for iid in ids:
        try:
            itm = gis.content.get(iid)
            if itm is not None:
                items.append(itm)
        except Exception as exc:
            msg = str(exc)
            if any(s in msg for s in skip_quiet_substrings):
                skipped += 1
            elif logger is not None:
                logger.warning("Could not resolve item %s: %s", iid, exc)
    return items, skipped


# ==========================================================================
# Low-level REST GET-as-JSON with ArcGIS error parsing
# ==========================================================================
def _format_rest_error(data, prefix=""):
    err = data.get("error") or {}
    code = err.get("code", "?")
    msg = err.get("message", "unknown REST error")
    details = "; ".join(err.get("details", []) or [])
    return f"{prefix}REST error {code}: {msg}" + (f" ({details})" if details else "")


def get_json(session, url, token, timeout=DEFAULT_TIMEOUT, params=None):
    """
    GET a REST endpoint as JSON. Returns (data_dict, error_str). Exactly one is
    meaningful: on success error_str is None; on failure data is None and
    error_str explains what went wrong (HTTP status, timeout, bad JSON, or a
    REST 'error' payload).
    """
    merged = {"f": "json", "token": token}
    if params:
        merged.update(params)
    try:
        r = session.get(url, params=merged, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, f"timeout after {timeout}s"
    except requests.exceptions.ConnectionError as exc:
        return None, f"connection error: {exc}"
    except requests.exceptions.RequestException as exc:
        return None, f"request error: {exc}"

    if r.status_code >= 400:
        return None, f"HTTP {r.status_code} {r.reason}"

    try:
        data = r.json()
    except ValueError:
        snippet = (r.text or "")[:160].replace("\n", " ")
        return None, f"non-JSON response (status {r.status_code}): {snippet!r}"

    if isinstance(data, dict) and "error" in data:
        return None, _format_rest_error(data)

    return data, None


def count_query(session, layer_url, token, where="1=1",
                timeout=DEFAULT_TIMEOUT):
    """
    Run the cheapest possible proof-of-life query against a layer: a
    returnCountOnly count. Returns (count_int, error_str). On any problem
    count_int is None and error_str explains it.
    """
    q_url = f"{layer_url.rstrip('/')}/query"
    try:
        r = session.get(q_url, params={
            "f": "json",
            "token": token,
            "where": where,
            "returnCountOnly": "true",
        }, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, f"query timeout after {timeout}s"
    except requests.exceptions.ConnectionError as exc:
        return None, f"query connection error: {exc}"
    except requests.exceptions.RequestException as exc:
        return None, f"query request error: {exc}"

    if r.status_code >= 400:
        return None, f"query HTTP {r.status_code} {r.reason}"

    try:
        data = r.json()
    except ValueError:
        snippet = (r.text or "")[:160].replace("\n", " ")
        return None, f"query non-JSON response: {snippet!r}"

    if isinstance(data, dict) and "error" in data:
        return None, _format_rest_error(data, prefix="query ")

    if not isinstance(data, dict) or "count" not in data:
        return None, f"query returned no 'count' field: {str(data)[:160]!r}"

    return data.get("count"), None


# ==========================================================================
# Dates
# ==========================================================================
def epoch_to_str(epoch_ms, fmt="%Y/%m/%d", utc=False):
    """
    Convert epoch MILLISECONDS to a formatted date string, or '' if empty.

    This is the original, widely-used converter. It assumes the input is epoch
    milliseconds (it divides by 1000) and does NOT handle datetime objects,
    epoch-seconds, or non-numeric strings. For inputs whose shape is not
    guaranteed to be epoch-ms, use epoch_to_date() instead. UNCHANGED so every
    existing caller behaves exactly as before.
    """
    if epoch_ms is None:
        return ""
    try:
        if utc:
            dt = datetime.datetime.utcfromtimestamp(int(epoch_ms) / 1000.0)
        else:
            dt = datetime.datetime.fromtimestamp(int(epoch_ms) / 1000.0)
        return dt.strftime(fmt)
    except Exception:
        return ""


def epoch_to_date(value, fmt="%Y/%m/%d", logger=None):
    """
    SUPERSET date renderer: turn any of the shapes a date field can arrive in
    into a human-readable string (default YYYY/MM/DD, UTC), or '' when there is
    nothing usable to show.

    Consolidates the three near-identical local helpers that previously lived in
    the scripts (PORTAL_GROUPS_INVENTORY.epoch_ms_to_date,
    PORTAL_INVENTORY_FS_REPLICAS.epoch_ms_to_date, and
    DATA_STORE_INVENTORY_SDE_REPLICAS._format_date). Handles, in order:

      * None / "" / 0 / "0"                      -> "" (blank cell)
      * datetime.datetime / datetime.date        -> strftime(fmt)
      * epoch MILLISECONDS (magnitude >= 1e11)   -> UTC date
      * epoch SECONDS      (magnitude >= 1e9)    -> UTC date
      * a smaller number                         -> passed through as text
      * a numeric string                         -> parsed as above
      * a non-numeric / already-formatted string -> returned stripped, as-is

    The epoch-ms vs epoch-seconds magnitude split (1e11 / 1e9) matches the SDE
    script's original heuristic. String PASSTHROUGH (returning an unrecognized
    string unchanged rather than blanking it) matches the replicas script's
    original behavior; it is the safe superset, since it never silently discards
    a value it doesn't understand. For inputs that are always epoch-ms or blank
    (e.g. the groups scan), passthrough only ever activates on data that would
    previously have produced '' anyway, so observable output is unchanged.

    UTC is used for epoch conversions to match every prior caller (all three
    local helpers rendered UTC).
    """
    # Blank / sentinel-zero values -> blank cell.
    if value in (None, "", 0, "0"):
        return ""

    # Native datetime / date objects (from ArcPy Replica.creationDate etc.).
    if isinstance(value, (datetime.datetime, datetime.date)):
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)

    # Numeric epoch values (int/float, or a numeric string).
    epoch = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if s == "":
            return ""
        try:
            epoch = float(s)
        except ValueError:
            # Not numeric -> already a formatted/text date; keep as-is.
            return s

    if epoch is not None:
        try:
            if abs(epoch) >= 1e11:          # epoch milliseconds
                dt = datetime.datetime.utcfromtimestamp(epoch / 1000.0)
            elif abs(epoch) >= 1e9:         # epoch seconds
                dt = datetime.datetime.utcfromtimestamp(epoch)
            else:
                # Too small to be a modern timestamp; return the raw text.
                return str(value).strip()
            return dt.strftime(fmt)
        except Exception as exc:
            if logger is not None:
                logger.warning("Could not convert date value %r: %s",
                               value, exc)
            return str(value).strip()

    return str(value).strip()