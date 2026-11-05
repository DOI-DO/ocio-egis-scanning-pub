"""
Date-range "bucketing" search, shared across PORTAL_* scripts.

WHY THIS EXISTS
---------------
ArcGIS Online and ArcGIS Enterprise Portal cap a single search at 10,000
results (start + num may not exceed 10,000, and some Portal builds have no
usable 'start' at all). The only reliable way to enumerate MORE than 10,000
items is to PARTITION the catalog into buckets that each return fewer than the
cap, then union the buckets.

APPROACH
--------
Partition on a date field (default 'created', epoch milliseconds). Walk the
full time span in CONTIGUOUS, gap-free MONTHLY windows so nothing is missed.
If any monthly window still hits the cap, recursively split it in half by time
(month -> halves -> ... -> down to 1 ms) until every leaf window is under the
cap. Results are merged, de-duplicated by item id, so overlap between adjacent
windows collapses automatically.

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

*** STRICT PAGER CONSOLIDATION (this revision) ***
-------------------------------------------------------------------------
Across 10+ scanner scripts, a nearly-identical block of code was copy-pasted
to do strict, org-scoped, completeness-verified item discovery:

  * Single page: consume every result in one query (if the declared total is
    below the cap) with strict validation (stable total across pages, no
    repeated IDs, no cross-org items, no empty mid-page).
  * Bucketed: subdivide on created-date ranges when a single-page result
    reaches the cap, with the same strict validation in every leaf bucket.

Both variants now live here ONCE:

  strict_search_page(client, query, cap, org_id, ...)
      Consume one bucket strictly (raises ScanAbort on any integrity failure).
      Used by callers whose search fits in one cap-sized window, and as the
      leaf call inside strict_bucketed_search.

  strict_bucketed_search(client, extra_clause, org_id, max_items, on_item,
                         logger, ...)
      Full org-scoped date-range bucketing with strict page validation.
      Calls on_item(item_id, raw_result) for every discovered item so the
      caller can write directly to a disk-backed store rather than accumulating
      a list in memory. Raises ScanAbort if a bucket cannot be proven complete.

Both functions depend on ArcGISRestClient (from restutils) and ScanAbort
(from auth), so they can only be called after a verified GIS connection is
established. The original SDK-callback bucketed_search() is UNCHANGED below
and remains the right choice when an arcgis SDK gis.content.search() callback
is more convenient than a direct REST client.

The strict pager validates (union of all per-script checks):
  * total is an integer >= 0 (never Boolean, never negative)
  * total is stable across all pages of one bucket (never changes mid-page)
  * nextStart is an integer, advances forward, never loops
  * page result is a list of dicts, never contains non-objects
  * no item is returned twice within one bucket's page sequence
  * every item has a non-empty string id
  * every item's explicit orgId (when present) matches the expected org id
  * an empty result page is only accepted when it exactly matches the
    declared total (never treated as "pagination finished" early)
  * reaching the cap in a one-millisecond bucket is a hard ScanAbort, not a
    silent truncation
"""

import datetime

from .auth import ScanAbort

# Hard result cap enforced by AGOL / Portal search.
DEFAULT_CAP = 10000

# How many items to request per search page in strict mode.
_STRICT_PAGE_SIZE = 100

# Span start for date-range bucketing (any item created after 2000-01-01 is
# captured; items created before that are rare enough to be out of scope for
# all current scanners).
_SEARCH_START = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


# ==========================================================================
# Original SDK-callback bucketed_search (UNCHANGED)
# ==========================================================================

def _default_id_fn(result):
    """
    Extract an id from a result. Handles both arcgis Item objects (``.id``)
    and raw REST dicts (``{'id': ...}``).
    """
    if isinstance(result, dict):
        return result.get("id")
    return getattr(result, "id", None)


def bucketed_search(
    search_fn,
    span_start=None,
    span_end=None,
    date_field="created",
    extra_clause=None,
    cap=DEFAULT_CAP,
    id_fn=_default_id_fn,
    logger=None,
    log_every=20,
):
    """
    Enumerate all items matching an optional clause by date-range bucketing.

    Parameters
    ----------
    search_fn : callable
        ``search_fn(query, cap) -> list``. Runs one search for a Lucene query
        and returns up to ``cap`` results (Item objects or dicts).
    span_start, span_end : datetime.datetime, optional
        Inclusive lower / exclusive upper bound of the time span to cover.
        Defaults: 2000-01-01 (UTC) through now + 365 days (UTC). Naive
        datetimes are treated as-is by ``.timestamp()``; passing tz-aware UTC
        datetimes is recommended.
    date_field : str
        The date field to partition on (e.g. 'created' or 'modified').
    extra_clause : str, optional
        An additional Lucene clause AND-ed into every query, e.g.
        ``'type:"File Geodatabase"'``. If None, only the date range is used.
    cap : int
        The search result cap (default 10,000).
    id_fn : callable
        ``id_fn(result) -> hashable id`` used to de-duplicate. Defaults to a
        helper that reads ``.id`` (Item) or ``['id']`` (dict).
    logger : logging.Logger, optional
        If supplied, progress is logged via ``logger.info`` / ``.debug`` /
        ``.warning``. If None, no logging is emitted.
    log_every : int
        Emit a progress line every N bucket queries.

    Returns
    -------
    dict
        ``{id: result}`` for every unique item found across all buckets.
    """
    if span_start is None:
        span_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    if span_end is None:
        span_end = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=365))

    items_by_id = {}
    stats = {"queries": 0}

    def _log(level, msg, *a):
        if logger is not None:
            getattr(logger, level)(msg, *a)

    def _build_query(lo_ms, hi_ms):
        range_clause = f"{date_field}:[{lo_ms} TO {hi_ms}]"
        if extra_clause:
            return f"{extra_clause} AND {range_clause}"
        return range_clause

    def search_range(lo_ms, hi_ms):
        """Fetch [lo_ms, hi_ms]; split in half by time if it hits the cap."""
        query = _build_query(lo_ms, hi_ms)
        stats["queries"] += 1

        try:
            results = search_fn(query, cap)
        except Exception as exc:
            _log("warning", "  bucket ERROR on range [%d TO %d]: %s",
                 lo_ms, hi_ms, exc)
            return

        n = len(results)

        # Too big and still splittable -> halve the time window and recurse.
        if n >= cap and hi_ms > lo_ms:
            mid = lo_ms + (hi_ms - lo_ms) // 2
            if mid <= lo_ms:            # guard against no-progress recursion
                mid = lo_ms + 1
            search_range(lo_ms, mid)
            search_range(mid + 1, hi_ms)
            return

        if n >= cap and hi_ms <= lo_ms:
            _log("warning",
                 "  bucket [%d TO %d] hit the cap but cannot be split "
                 "further (identical timestamps); some items may be missed.",
                 lo_ms, hi_ms)

        for result in results:
            iid = id_fn(result)
            if iid is not None:
                items_by_id[iid] = result

        if log_every and stats["queries"] % log_every == 0:
            _log("info", "  %d bucket queries run, %d unique item(s) so far.",
                 stats["queries"], len(items_by_id))

    _log("info",
         "Bucketing '%s' dates from %s to %s (monthly, auto-subdividing) ...",
         date_field, span_start.date(), span_end.date())

    # Walk the span in contiguous MONTHLY windows.
    cursor = span_start
    while cursor < span_end:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)

        lo_ms = int(cursor.timestamp() * 1000)
        hi_ms = int(nxt.timestamp() * 1000) - 1     # inclusive upper bound
        search_range(lo_ms, hi_ms)
        cursor = nxt

    _log("info", "Bucketed search complete: %d bucket queries, %d unique item(s).",
         stats["queries"], len(items_by_id))

    return items_by_id


# ==========================================================================
# Strict page-level helpers (shared by both strict entry points below)
# ==========================================================================

def _strict_total(data, label):
    """Parse and validate the 'total' field from a search response dict."""
    value = data.get("total")
    if isinstance(value, bool):
        raise ScanAbort(f"{label} search returned a Boolean total.")
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"{label} search returned an invalid total: {value!r}"
        ) from exc
    if total < 0:
        raise ScanAbort(f"{label} search returned a negative total: {total}")
    return total


def _strict_next_start(data, fallback, label):
    """Parse nextStart; return fallback when the field is absent."""
    value = data.get("nextStart")
    if value is None:
        return fallback
    if isinstance(value, bool):
        raise ScanAbort(f"{label} search returned a Boolean nextStart.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(
            f"{label} search returned an invalid nextStart: {value!r}"
        ) from exc


def _strict_results(data, label):
    """Extract and validate the results list from a search response dict."""
    value = data.get("results")
    if value is None:
        value = data.get("items", [])
    if not isinstance(value, list):
        raise ScanAbort(
            f"{label} search returned a malformed results list "
            f"(got {type(value).__name__})."
        )
    if any(not isinstance(entry, dict) for entry in value):
        raise ScanAbort(
            f"{label} search returned a non-object entry in its results list."
        )
    return value


def _validate_item_id(item, label):
    """Return a clean item id string or raise ScanAbort."""
    raw = item.get("id")
    if not raw:
        raise ScanAbort(f"{label} search returned an item without an id.")
    return str(raw).strip()


def _validate_item_org(item, item_id, org_id, label):
    """Raise ScanAbort when an item belongs to the wrong organization."""
    if not org_id:
        return
    explicit_org = item.get("orgId") or item.get("orgid")
    if (explicit_org not in (None, "")
            and str(explicit_org).strip().lower() != org_id.lower()):
        raise ScanAbort(
            f"{label} search returned cross-organization item {item_id!r} "
            f"(orgId={explicit_org!r}, expected {org_id!r})."
        )


# ==========================================================================
# Strict single-bucket consumer
# ==========================================================================

def strict_search_page(client, query, cap, org_id="", label="item",
                       page_size=_STRICT_PAGE_SIZE):
    """
    Consume one search bucket strictly using ArcGISRestClient.

    Every page is validated for integrity; the function raises ScanAbort on
    any problem so the caller never silently accepts a partial result. The
    function returns True when the bucket hit the cap (caller should
    subdivide), False otherwise.

    When not hitting the cap, every discovered item is returned in a list
    that the caller can process however it likes (insert into a store,
    accumulate in memory, etc.).

    Parameters
    ----------
    client : ArcGISRestClient
        Already-connected, policy-validated REST client.
    query : str
        Complete Lucene query (including date-range clause).
    cap : int
        The Portal/AGOL search cap.
    org_id : str
        When non-empty, every returned item's explicit orgId (if present) is
        checked against this value. Pass '' to skip org validation.
    label : str
        Human-readable noun for error messages (e.g. "web map", "item").
    page_size : int
        Number of results to request per page.

    Returns
    -------
    (hit_cap: bool, results: list[dict])
        hit_cap  – True means the caller must subdivide this query.
        results  – the de-duplicated items collected (empty when hit_cap).
    """
    results = []
    stable_total = None
    seen_ids = set()
    start = 1
    pages = 0
    max_pages = max(4, cap + 2)

    while len(results) < cap:
        pages += 1
        if pages > max_pages:
            raise ScanAbort(
                f"{label} search exceeded {max_pages} pages for one bucket; "
                "refusing to loop indefinitely."
            )

        num = min(page_size, cap - len(results))
        data = client.portal_json(
            "/sharing/rest/search",
            params={
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": num,
            },
        )

        page = _strict_results(data, label)
        total = _strict_total(data, label)

        # On the first page, check immediately whether this bucket is above cap.
        # Do this BEFORE recording any items so we never emit a partial bucket
        # that a subdivided retry will also emit.
        if stable_total is None:
            if total >= cap:
                return True, []
            stable_total = total
        elif total != stable_total:
            raise ScanAbort(
                f"{label} search total changed during paging "
                f"({stable_total} -> {total})."
            )

        if not page:
            expected = min(stable_total, cap)
            if len(results) < expected:
                raise ScanAbort(
                    f"{label} search returned an empty page after "
                    f"{len(results)} of {stable_total} expected results."
                )
            break

        new_on_page = 0
        for item in page:
            item_id = _validate_item_id(item, label)
            if item_id in seen_ids:
                raise ScanAbort(
                    f"{label} search repeated id {item_id!r} within one "
                    "bucket's page sequence."
                )
            seen_ids.add(item_id)
            _validate_item_org(item, item_id, org_id, label)
            results.append(item)
            new_on_page += 1

        if new_on_page == 0:
            raise ScanAbort(
                f"{label} search page at start={start} returned no new items."
            )

        if len(results) >= stable_total:
            break

        raw_next = data.get("nextStart")
        next_start = _strict_next_start(data, start + len(page), label)
        if raw_next is not None and next_start <= 0:
            # Portal signals end of results with nextStart == -1.
            if len(results) < stable_total:
                raise ScanAbort(
                    f"{label} search ended at nextStart={next_start} after "
                    f"{len(results)} of {stable_total} declared results."
                )
            break
        if next_start <= start:
            raise ScanAbort(
                f"{label} search pagination did not advance "
                f"(start={start}, nextStart={next_start})."
            )
        start = next_start

    return False, results[:cap]


# ==========================================================================
# Strict full org-scoped date-range bucketing
# ==========================================================================

def strict_bucketed_search(client, extra_clause, org_id, max_items,
                           on_item, logger, label="item",
                           page_size=_STRICT_PAGE_SIZE,
                           cap=DEFAULT_CAP):
    """
    Full org-scoped date-range bucketed search with strict page validation.

    Calls ``on_item(item_id, raw_result)`` for every newly discovered item in
    first-seen order. The caller is responsible for de-duplication storage
    (a disk-backed store, a dict, etc.); this function never builds an
    in-memory list of all results so it stays memory-efficient for large orgs.

    The search is stopped gracefully (not aborted) when ``on_item`` returns
    True, allowing the caller to cap the discovery at ``max_items`` without
    touching this function's core loop.

    Parameters
    ----------
    client : ArcGISRestClient
        Already-connected, policy-validated REST client.
    extra_clause : str
        The item-type / org-scope part of the Lucene query, e.g.
        ``'orgid:abc123 AND type:"Web Map"'``. The date-range clause is
        appended automatically.
    org_id : str
        Every returned item's explicit orgId (when present) is validated
        against this value. Pass '' to skip; _safe_org_id() should be called
        by the caller before passing here.
    max_items : int
        Safety ceiling. When on_item has been called this many times the
        search stops.  For a truly unlimited full scan pass a large value
        (e.g. 2_000_000) and let on_item never return True; raising ScanAbort
        inside on_item for a hard stop is also valid.
    on_item : callable
        ``on_item(item_id: str, raw_result: dict) -> bool``
        Called for each new item. Return True to stop the scan (e.g. cap
        reached); return False/None to continue.
    logger : logging.Logger or None
        Progress logging.
    label : str
        Human-readable noun for log messages and ScanAbort messages.
    page_size : int
        Items per search page.
    cap : int
        Portal/AGOL search result cap (default 10,000).

    Raises
    ------
    ScanAbort
        On any integrity problem: pagination regression, total instability,
        duplicate ids, cross-org items, or a one-millisecond bucket at cap.

    Returns
    -------
    dict
        ``{"requests": int, "items": int}`` diagnostic counters.
    """
    stats = {"requests": 0, "items": 0}
    stopped = False
    span_end = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=365)
    )

    def _search_range(lo_ms, hi_ms):
        nonlocal stopped
        if stopped:
            return

        query = f"{extra_clause} AND created:[{lo_ms} TO {hi_ms}]"
        stats["requests"] += 1
        hit_cap, page_items = strict_search_page(
            client, query, cap, org_id=org_id, label=label,
            page_size=page_size,
        )

        if hit_cap:
            if hi_ms <= lo_ms:
                raise ScanAbort(
                    f"{label} search: a one-millisecond bucket "
                    f"[{lo_ms} TO {hi_ms}] still hit the {cap:,}-item "
                    "cap; completeness cannot be guaranteed. Refusing to "
                    "silently truncate."
                )
            mid = lo_ms + (hi_ms - lo_ms) // 2
            if mid <= lo_ms:
                mid = lo_ms + 1
            _search_range(lo_ms, mid)
            _search_range(mid + 1, hi_ms)
            return

        for item in page_items:
            if stopped:
                return
            item_id = str(item.get("id") or "").strip()
            result = on_item(item_id, item)
            stats["items"] += 1
            if result or stats["items"] >= max_items:
                stopped = True
                return

        if logger is not None and stats["requests"] % 20 == 0:
            logger.info(
                "  %d strict %s search request(s), %d item(s) discovered "
                "so far.",
                stats["requests"], label, stats["items"],
            )

    if logger is not None:
        logger.info(
            "Strict org-scoped %s bucketing from %s to %s ...",
            label, _SEARCH_START.date(), span_end.date(),
        )

    cursor = _SEARCH_START
    while cursor < span_end and not stopped:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)
        lo_ms = int(cursor.timestamp() * 1000)
        hi_ms = int(nxt.timestamp() * 1000) - 1
        _search_range(lo_ms, hi_ms)
        cursor = nxt

    if logger is not None:
        logger.info(
            "Strict %s bucketing complete: %d request(s), %d item(s)%s.",
            label, stats["requests"], stats["items"],
            " (stopped early)" if stopped else "",
        )

    return stats


# ==========================================================================
# Convenience: month window generator (used by strict callers directly)
# ==========================================================================

def month_windows(span_start=None, span_end=None):
    """
    Yield (lo_ms, hi_ms) inclusive epoch-millisecond pairs for contiguous
    monthly windows covering [span_start, span_end).

    Both defaults match the bucketed_search defaults so callers that want
    to drive the outer loop themselves get identical coverage.
    """
    if span_start is None:
        span_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    if span_end is None:
        span_end = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=365))
    cursor = span_start
    while cursor < span_end:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)
        yield int(cursor.timestamp() * 1000), int(nxt.timestamp() * 1000) - 1
        cursor = nxt