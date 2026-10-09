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
"""

import datetime


# Hard result cap enforced by AGOL / Portal search.
DEFAULT_CAP = 10000


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
        Defaults: 2000-01-01 (UTC) through now + 365 days (UTC), matching the
        reference implementation. Naive datetimes are treated as-is by
        ``.timestamp()``; passing tz-aware UTC datetimes is recommended.
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