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

*** MULTI-ORG PLATFORM-DETECTION BUGFIX (this revision) ***
-------------------------------------------------------------------------
get_platform() previously matched a portal_url against a small, HARD-CODED
dict of exactly two known URLs -- one fixed Enterprise Portal URL and one
fixed AGOL org URL, both belonging to a single organization, LITERALLY
WRITTEN INTO THIS FILE. Every org's toml carries its OWN [portal].portal-url
and [agol].portal-url (e.g. "https://OTHER_ORGldt.maps.arcgis.com/home" for one
org, "https://org.maps.arcgis.com/home" for another). Any org whose URL
wasn't one of those two exact hard-coded strings fell through to the
"UNKNOWN" branch -- which is exactly why running a scan against a second
AGOL org produced "UNKNOWN" in the CSV/report instead of "AGOL".

FIX: get_platform() now classifies GENERICALLY, from the shape of whatever
URL it is handed at runtime -- there is NO org-specific or hard-coded URL
literal anywhere in this module (or anywhere else in this project; portal-url
is ALWAYS read from the chosen org's --config toml via finalize_config(), and
threaded through as a plain function parameter from there):

  * Any hostname ending in ".arcgis.com" (case-insensitive) -> "AGOL".
    Every ArcGIS Online organization URL takes the form
    "https://<org-short-name>.maps.arcgis.com/...", so matching the parent
    domain ".arcgis.com" (not just "maps.arcgis.com", in case of other
    Esri-hosted subdomains) correctly classifies ANY org's AGOL URL without
    this module needing to know that org's short name in advance.
  * Anything else with a resolvable hostname -> "PORTAL". Enterprise Portal
    deployments are self-hosted and never live on Esri's arcgis.com domain,
    so "not AGOL" is a safe, permanent proxy for "Portal" here.
  * Blank/unparseable input -> "UNKNOWN" (this is a real "we were given
    nothing/garbage" case -- e.g. a missing config value -- not a
    new-org-not-in-my-list case, which is what the old hard-coded version
    incorrectly produced).

This is backward compatible: both this project's existing org URLs
(enterprise.com/portal -> PORTAL, org.maps.arcgis.com -> AGOL) still
classify exactly as before, and every NEW org's AGOL/Portal URL now also
classifies correctly with zero code changes required per org -- because no
code anywhere has to be told about a new org's URL. It's read from that
org's toml at runtime, every time.

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
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .bucketing import bucketed_search, DEFAULT_CAP

# Sensible shared defaults; callers may override per call.
DEFAULT_TIMEOUT = 30
DEFAULT_RETRY_TOTAL = 3
DEFAULT_PAGE_SIZE = 100
DEFAULT_BACKOFF_FACTOR = 0.3

# Standard hosted-feature-service Lucene clause.
HOSTED_FS_CLAUSE = 'type:"Feature Service" AND typekeywords:"Hosted Service"'


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
        parsed = urlparse(normalized if "://" in normalized
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