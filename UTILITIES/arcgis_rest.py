"""
UTILITIES/arcgis_rest.py
==============================================================================
Fast, raw-REST helpers for attachment/layer scanning, shared across PORTAL_*
scripts. These avoid the heavy arcgis SDK object graph (FeatureLayerCollection,
Item, FeatureLayer) for the hot paths, replacing them with a small number of
direct /f=json calls against a pooled requests.Session.

Everything here is designed to:
  * make ONE service-definition call per service (reads every sub-layer's
    hasAttachments at once) instead of building SDK layers,
  * enumerate attachments with queryAttachments (POST + small objectId batches
    on AGOL; a direct where=1=1 fast path on Portal),
  * trust the size returned by returnMetadata=true, only falling back to a
    per-attachment /attachments listing for rows that still lack a size,
  * be safe to call from many threads sharing one pooled Session.

Nothing here raises on a per-service/per-layer failure; callers get empty
results or (data, error) tuples so a single bad layer never aborts a scan.

BATCHED ITEM METADATA / OWNER RESOLUTION
----------------------------------------
resolve_items_metadata() resolves title/owner/created/modified for many item
ids using batched /sharing/rest/search calls (id:(a OR b OR ...)), collapsing
thousands of per-item content.get() calls into ~N/batch_size searches.

resolve_owners_batched() is a thin owner-only wrapper over the same batched
search (previously it lived in the now-removed UTILITIES/owner_resolve.py).
Because the search already returns owner alongside the other metadata fields,
the owner-only path issues NO extra HTTP round-trips -- it simply extracts the
owner from the shared result. Its return contract is unchanged from the old
module: {item_id: owner} with EVERY input id present (ids the search did not
return map to "").

Public API
----------
    get_service_definition(session, service_url, token, timeout)  -> dict
    iter_service_sublayers(defn)                                  -> list[dict]
    list_object_ids(session, layer_url, token, page_size, timeout)-> list[int]
    query_attachments(session, layer_url, token, is_agol, ...)    -> list[dict]
    resolve_items_metadata(session, portal_url, token, item_ids,  -> dict
                           batch_size, timeout, logger)
    resolve_owners_batched(session, portal_url, token, item_ids,  -> dict
                           batch_size, timeout, logger)
==============================================================================
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from .restutils import get_json, DEFAULT_TIMEOUT

# queryAttachments objectId batch size. AGOL rejects a very large objectIds
# list packed into a request; keep each batch small so it works on both AGOL
# and Portal.
QA_OID_BATCH = 100

# Page size for objectId enumeration.
DEFAULT_OID_PAGE = 5000


# ==========================================================================
# Service definition (ONE call reads every sub-layer's hasAttachments)
# ==========================================================================
def get_service_definition(session, service_url, token, timeout=DEFAULT_TIMEOUT):
    """
    Fetch a Feature/Map service's definition JSON once. Returns the raw dict
    (with 'layers' and 'tables' arrays) or {} on any failure.

    service_url is the SERVICE ROOT (…/FeatureServer), not a sub-layer URL.
    """
    data, err = get_json(session, service_url.rstrip("/"), token,
                         timeout=timeout)
    if err or not isinstance(data, dict):
        return {}
    return data


def iter_service_sublayers(defn):
    """
    Given a service-definition dict, return a flat list of sub-layer/table
    descriptors: [{'id': int, 'name': str, 'hasAttachments': bool,
                   'is_table': bool}, ...].

    The service-root definition lists layers/tables with id + name but may not
    include hasAttachments; callers that need certainty can still probe, but in
    practice hosted services expose hasAttachments here.
    """
    out = []
    for key, is_table in (("layers", False), ("tables", True)):
        for lyr in (defn.get(key) or []):
            if not isinstance(lyr, dict):
                continue
            out.append({
                "id": lyr.get("id"),
                "name": lyr.get("name"),
                "hasAttachments": bool(lyr.get("hasAttachments", False)),
                "is_table": is_table,
            })
    return out


def get_sublayer_definition(session, layer_url, token, timeout=DEFAULT_TIMEOUT):
    """
    Fetch a single sub-layer definition (…/FeatureServer/3). Returns the dict
    or {} on failure. Used only when the service-root definition omitted
    hasAttachments and we must confirm per layer.
    """
    data, err = get_json(session, layer_url.rstrip("/"), token, timeout=timeout)
    if err or not isinstance(data, dict):
        return {}
    return data


# ==========================================================================
# ObjectId enumeration (only needed for the AGOL objectIds path)
# ==========================================================================
def list_object_ids(session, layer_url, token, page_size=DEFAULT_OID_PAGE,
                    timeout=DEFAULT_TIMEOUT):
    """
    Enumerate all object ids for a layer. Returns a list of ints (possibly
    empty).

    FIX (undercount): the previous implementation paged with
    resultOffset/resultRecordCount while ALSO passing returnIdsOnly=true. On
    ArcGIS Enterprise (Portal), a returnIdsOnly=true /query returns the FULL
    objectIds array in a single response and ignores the offset/record-count
    paging params; on the second loop the service returned an empty objectIds
    array, which tripped the `if not batch: break` guard and caused
    list_object_ids to return []. Every layer that fell through to this path
    (i.e. every Portal layer whose queryAttachments fast path came back empty)
    therefore reported ZERO attachments. This is the root cause of Stage 2's
    1,141 "no attachment rows" layers.

    New behavior:
      1. Try the single-call returnIdsOnly path first (the correct pattern for
         both Portal and AGOL). If the service returns the whole set, use it.
      2. Only if the service signals more data than one response can carry
         (exceededTransferLimit) do we page — and we page by OID watermark
         (objectId > last) rather than resultOffset, which returnIdsOnly does
         reliably honor. An empty page ENDS paging without discarding what we
         already collected.
    """
    q_url = f"{layer_url.rstrip('/')}/query"

    # --- Attempt 1: single call, whole ID set (correct for Portal + AGOL) ---
    data, err = get_json(session, q_url, token, timeout=timeout, params={
        "where": "1=1", "returnIdsOnly": "true"})
    if not err and isinstance(data, dict):
        ids = list(data.get("objectIds") or [])
        # If the service returned everything (the common case), we're done.
        if ids and not data.get("exceededTransferLimit"):
            return ids
        # If it returned some but flagged more, fall into watermark paging
        # below (seeded with what we have). If it returned nothing, fall
        # through to the count-bounded fallback.
        if ids and data.get("exceededTransferLimit"):
            return _page_ids_by_watermark(
                session, q_url, token, page_size, timeout, seed=ids,
                oid_field=data.get("objectIdFieldName"))

    # --- Attempt 2: count-bounded fallback (older behavior, corrected) ------
    # Some services only return ids when asked with explicit ordering/paging.
    cnt, err = get_json(session, q_url, token, timeout=timeout, params={
        "where": "1=1", "returnCountOnly": "true"})
    if err or not isinstance(cnt, dict):
        return []
    total = cnt.get("count", 0) or 0
    if not total:
        return []

    return _page_ids_by_watermark(
        session, q_url, token, page_size, timeout, seed=[], oid_field=None,
        expected_total=total)


def _page_ids_by_watermark(session, q_url, token, page_size, timeout,
                           seed=None, oid_field=None, expected_total=None):
    """
    Page objectIds using an OID watermark (objectId > last_seen) with
    returnIdsOnly=true, which Portal/AGOL honor reliably (unlike resultOffset
    on a returnIdsOnly query). Returns the accumulated, de-duplicated id list.

    seed          : ids already collected (from the single-call attempt).
    oid_field     : the layer's objectId field name if known; discovered if not.
    expected_total: optional count to bound the loop when seeding from nothing.
    """
    ids = list(seed or [])
    seen = set(ids)

    # Discover the OID field name if we weren't given one.
    if not oid_field:
        meta, err = get_json(session, q_url.rsplit("/query", 1)[0], token,
                             timeout=timeout, params={})
        if not err and isinstance(meta, dict):
            oid_field = meta.get("objectIdField") or "OBJECTID"
        else:
            oid_field = "OBJECTID"

    last = max(ids) if ids else None
    while True:
        where = "1=1" if last is None else f"{oid_field} > {last}"
        data, err = get_json(session, q_url, token, timeout=timeout, params={
            "where": where,
            "returnIdsOnly": "true",
            "orderByFields": oid_field,
            "resultRecordCount": page_size,
        })
        if err or not isinstance(data, dict):
            break
        batch = data.get("objectIds") or []
        # New ids only (guard against services that ignore the watermark).
        new = [i for i in batch if i not in seen]
        if not new:
            break
        ids.extend(new)
        seen.update(new)
        last = max(ids)
        if expected_total is not None and len(ids) >= expected_total:
            break
        if not data.get("exceededTransferLimit") and len(batch) < page_size:
            break
    return ids


# ==========================================================================
# Attachment enumeration with size, cross-platform
# ==========================================================================
def _post_json(session, url, token, data, timeout):
    """POST helper returning (dict, error_str). AGOL needs POST for large
    objectIds lists that would overflow a GET query string."""
    merged = {"f": "json", "token": token}
    merged.update(data)
    try:
        r = session.post(url, data=merged, timeout=timeout)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:  # noqa: BLE001 - callers only need pass/fail
        return None, str(exc)


def _rows_from_groups(groups):
    """Flatten attachmentGroups -> list of {oid, id, name, contentType, size}.
    size may be None when the service omitted it."""
    rows = []
    for g in groups or []:
        oid = g.get("parentObjectId")
        for info in (g.get("attachmentInfos") or []):
            rows.append({
                "oid": oid,
                "id": info.get("id"),
                "name": info.get("name"),
                "contentType": info.get("contentType"),
                "size": info.get("size"),
            })
    return rows


def query_attachments(session, layer_url, token, is_agol,
                      object_ids=None, timeout=DEFAULT_TIMEOUT,
                      oid_batch=QA_OID_BATCH):
    """
    Enumerate a layer's attachments (with size where the service supplies it).

    Portal fast path (is_agol False and object_ids None): one POST with
    where=1=1 returnMetadata=true grabs everything.

    AGOL / objectIds path: POST queryAttachments in small objectId batches so
    the request stays small enough for the hosted endpoint to accept.

    Returns a list of row dicts: {oid, id, name, contentType, size}. Never
    raises; a failed batch is simply skipped.

    FIX (undercount): the Portal fast path previously returned whatever it got
    as long as attachmentGroups was a list -- including an EMPTY list. On many
    Portal layers the where=1=1 returnMetadata=true form returns an empty (or
    absent) attachmentGroups even though the layer has attachments, so the
    layer was reported as having none. Now the fast path only returns early
    when it produces a NON-EMPTY result; an empty/absent attachmentGroups is
    treated as INCONCLUSIVE and we fall through to the authoritative objectIds
    enumeration (which is what actually finds the attachments).
    """
    qa_url = f"{layer_url.rstrip('/')}/queryAttachments"

    # Portal fast path: no objectId batching needed.
    if not is_agol and not object_ids:
        data, err = _post_json(session, qa_url, token, {
            "where": "1=1", "returnMetadata": "true"}, timeout)
        if not err and isinstance(data, dict) and "error" not in data:
            groups = data.get("attachmentGroups")
            if isinstance(groups, list) and groups:
                rows = _rows_from_groups(groups)
                if rows:
                    return rows
        # Empty / inconclusive / error -> fall through to objectIds path
        # instead of reporting zero attachments.

    # objectIds path (AGOL, or Portal fallback). Need ids first.
    if object_ids is None:
        object_ids = list_object_ids(session, layer_url, token, timeout=timeout)
    if not object_ids:
        return []

    rows = []
    for i in range(0, len(object_ids), oid_batch):
        chunk = object_ids[i:i + oid_batch]
        data, err = _post_json(session, qa_url, token, {
            "objectIds": ",".join(map(str, chunk)),
            "returnMetadata": "true",
        }, timeout)
        if err or not isinstance(data, dict) or "error" in data:
            continue
        rows.extend(_rows_from_groups(data.get("attachmentGroups")))
    return rows


def fill_missing_sizes(session, layer_url, token, rows, workers=8,
                       timeout=DEFAULT_TIMEOUT):
    """
    For any rows whose 'size' is None, fetch the per-object /attachments
    listing (which always includes size) and patch them. Listings are fetched
    in parallel across the distinct parent object ids that still need them.

    Mutates and returns rows.
    """
    need = {r["oid"] for r in rows if r.get("size") is None and r.get("oid") is not None}
    if not need:
        return rows

    def _listing(oid):
        u = f"{layer_url.rstrip('/')}/{oid}/attachments"
        data, err = get_json(session, u, token, timeout=timeout)
        if err or not isinstance(data, dict):
            return oid, {}
        by_id = {}
        for info in (data.get("attachmentInfos") or []):
            by_id[info.get("id")] = info.get("size")
        return oid, by_id

    size_by_oid_att = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_listing, oid) for oid in need]
        for fut in as_completed(futs):
            oid, by_id = fut.result()
            for att_id, size in by_id.items():
                size_by_oid_att[(oid, att_id)] = size

    for r in rows:
        if r.get("size") is None:
            r["size"] = size_by_oid_att.get((r.get("oid"), r.get("id")))
    return rows


# ==========================================================================
# Batched item metadata (title/owner/dates) — many ids per search call
# ==========================================================================
def resolve_items_metadata(session, portal_url, token, item_ids,
                           batch_size=100, timeout=DEFAULT_TIMEOUT,
                           logger=None):
    """
    Resolve title/owner/created/modified for many item ids using batched
    /sharing/rest/search calls (id:(a OR b OR ...)). Collapses thousands of
    per-item content.get() calls into ~N/batch_size searches.

    Returns {item_id: {'title','owner','created','modified'}}. Ids not returned
    by search are present with empty/None fields so callers always get a value.
    """
    ids = [str(i).strip() for i in item_ids if i and str(i).strip()]
    meta = {i: {"title": None, "owner": None, "created": None, "modified": None}
            for i in ids}
    if not ids:
        return meta

    search_url = portal_url.rstrip("/") + "/sharing/rest/search"
    total = len(ids)
    n_batches = (total + batch_size - 1) // batch_size
    if logger is not None:
        logger.info("Resolving item metadata for %d id(s) in %d search "
                    "call(s) ...", total, n_batches)

    for b in range(n_batches):
        batch = ids[b * batch_size:(b + 1) * batch_size]
        clause = " OR ".join(batch)
        data, err = get_json(session, search_url, token, timeout=timeout,
                             params={"q": f"id:({clause})",
                                     "num": len(batch), "start": 1})
        if err or not isinstance(data, dict):
            if logger is not None:
                logger.warning("  item-metadata batch %d/%d failed: %s",
                               b + 1, n_batches, err)
            continue
        for res in (data.get("results") or []):
            if isinstance(res, dict) and res.get("id") in meta:
                meta[res["id"]] = {
                    "title": res.get("title"),
                    "owner": res.get("owner"),
                    "created": res.get("created"),
                    "modified": res.get("modified"),
                }
    return meta


def resolve_owners_batched(session, portal_url, token, item_ids,
                           batch_size=100, timeout=DEFAULT_TIMEOUT,
                           logger=None):
    """
    Resolve portal item OWNER for many item ids using batched search calls.

    This is an owner-only wrapper over resolve_items_metadata(): the batched
    /sharing/rest/search already returns owner alongside title/created/modified,
    so this issues NO extra HTTP round-trips -- it simply extracts the owner
    field from the shared batched result. (Formerly UTILITIES/owner_resolve.py.)

    Parameters
    ----------
    session : requests.Session
        Pooled session (verify already set by caller as needed).
    portal_url : str
        Portal base URL (e.g. gis.url).
    token : str
        Portal token.
    item_ids : iterable of str
        Distinct item ids to resolve. Blanks/None are ignored.
    batch_size : int
        Ids per search call. 100 is a safe default; the Lucene query length
        grows with this, so keep it moderate.
    timeout : int
        Per-request timeout (seconds).
    logger : logging.Logger, optional
        Progress + batch-failure logging.

    Returns
    -------
    dict
        {item_id: owner}. Every input id is present; ids the search did not
        return (or that had no owner) map to "" so callers always get a value.
    """
    # Normalize exactly as the metadata resolver does, so the set of keys in
    # the returned dict matches the (cleaned) input ids one-for-one.
    ids = [str(i).strip() for i in item_ids if i and str(i).strip()]
    owners = {i: "" for i in ids}
    if not ids:
        return owners

    meta = resolve_items_metadata(
        session, portal_url, token, ids, batch_size=batch_size,
        timeout=timeout, logger=logger)

    resolved = 0
    for iid in owners:
        owner = (meta.get(iid) or {}).get("owner") or ""
        owners[iid] = owner
        if owner:
            resolved += 1

    if logger is not None:
        logger.info("Service owners resolved: %d / %d had an owner "
                    "(via batched search).", resolved, len(ids))
    return owners