"""
UTILITIES/exclusions.py
==============================================================================
Shared "exclude this group's items from the scan" feature.

WHY THIS EXISTS
---------------
Several scan scripts need the ability to skip items that belong to one or
more groups the org considers "legitimate exceptions" (e.g. a group used to
stage intentionally-duplicated test services, or a group of known-broken demo
content that IT doesn't want flagged every run). Rather than each script
re-implementing "look up a group's members, then filter my results," this one
module provides both pieces so the logic is written ONCE:

    add_exclude_group_arg(parser)        -> adds the optional CLI flag
    resolve_excluded_item_ids(...)       -> group id(s) -> set of item ids
    filter_excluded(...)                 -> generic list filter using that set

USAGE PATTERN (see any of the 7 scripts that use this)
-------------------------------------------------------------------------
    add_exclude_group_arg(parser)                 # in parse_args()
    ...
    excluded_ids = resolve_excluded_item_ids(
        gis, args.exclude_group_id, logger)       # after connect_gis()
    ...
    items = filter_excluded(
        items, excluded_ids, id_getter=lambda it: it.id,
        logger=logger, label="hosted feature service item")

`id_getter` makes this work identically whether the caller has arcgis SDK
Item objects (`lambda it: it.id`), raw REST dicts (`lambda d: d.get("id")`),
or already-built row dicts keyed differently (`lambda r: r["item_id"]`).

MULTIPLE GROUPS
---------------
--exclude-group-id accepts a COMMA-SEPARATED list of group ids, matching the
existing convention used by 'email-to' elsewhere in this project:

    --exclude-group-id abc123def456...
    --exclude-group-id abc123...,def456...,789fed...

Every named group's membership is unioned into a single excluded-id set.
"""


def add_exclude_group_arg(parser):
    """
    Add the OPTIONAL --exclude-group-id flag.

    Accepts one group id or a comma-separated list of group ids. Items
    belonging to ANY of the named group(s) are excluded from that script's
    scan results. Omit this flag entirely to scan everything (the default,
    unchanged behavior).
    """
    parser.add_argument(
        "--exclude-group-id",
        default=None,
        metavar="GROUP_ID[,GROUP_ID,...]",
        help="Optional. One group id, or a comma-separated list of group "
             "ids, whose member items are EXCLUDED from this scan's results "
             "(e.g. a group of known/approved exceptions). Omit to scan "
             "everything (default).",
    )


def _parse_group_ids(raw):
    """Split a comma-separated --exclude-group-id value into a clean list."""
    if not raw:
        return []
    return [g.strip() for g in str(raw).split(",") if g.strip()]


def resolve_excluded_item_ids(gis, exclude_group_id, logger=None):
    """
    Resolve one or more group ids (from --exclude-group-id) into the UNION of
    every item id those groups contain. Returns an empty set() if
    exclude_group_id is None/blank (the common case -- no exclusion
    requested), so callers can unconditionally pass the result to
    filter_excluded() without an extra "if exclusions requested" branch.

    Each group's content() is queried exactly once. A group id that can't be
    resolved (bad id, no access) is logged as a warning and simply
    contributes no ids -- it never aborts the run, since a typo'd exclusion
    group should not block an otherwise-valid scan.
    """
    group_ids = _parse_group_ids(exclude_group_id)
    if not group_ids:
        return set()

    excluded = set()
    for gid in group_ids:
        try:
            grp = gis.groups.get(gid)
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    "--exclude-group-id: could not resolve group '%s' (%s); "
                    "no items excluded for this group.", gid, exc)
            continue
        if grp is None:
            if logger is not None:
                logger.warning(
                    "--exclude-group-id: group '%s' not found; no items "
                    "excluded for this group.", gid)
            continue
        try:
            members = grp.content()
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    "--exclude-group-id: could not read content of group "
                    "'%s' (%s); no items excluded for this group.", gid, exc)
            continue
        ids = {getattr(it, "id", None) for it in members}
        ids.discard(None)
        excluded |= ids
        if logger is not None:
            logger.info(
                "--exclude-group-id: group '%s' (%s) contributes %d "
                "excluded item id(s).", gid, getattr(grp, "title", "?"),
                len(ids))

    if logger is not None and excluded:
        logger.info(
            "Exclusion group(s) resolved: %d total unique item id(s) will "
            "be skipped by this scan.", len(excluded))
    return excluded


def filter_excluded(records, excluded_ids, id_getter, logger=None,
                    label="item"):
    """
    Return the subset of `records` whose id (via id_getter(record)) is NOT in
    `excluded_ids`. If excluded_ids is empty, `records` is returned UNCHANGED
    (as the same list object is not guaranteed, but content is identical) with
    no logging noise -- this keeps the common "no exclusion requested" path
    a cheap no-op.

    Parameters
    ----------
    records : list
        Any list of items -- arcgis SDK Item objects, raw REST dicts, or
        already-built row dicts.
    excluded_ids : set
        The set returned by resolve_excluded_item_ids().
    id_getter : callable
        id_getter(record) -> the item id to test, e.g. `lambda it: it.id` for
        SDK Items or `lambda d: d.get("id")` for REST dicts.
    logger : logging.Logger, optional
        If given, logs how many records were dropped.
    label : str
        Human-readable noun used in the log line (e.g. "hosted feature
        service", "web map item").

    Returns
    -------
    list
        The filtered records, preserving original order.
    """
    if not excluded_ids:
        return records

    kept = []
    dropped = 0
    for rec in records:
        try:
            rid = id_getter(rec)
        except Exception:
            rid = None
        if rid is not None and rid in excluded_ids:
            dropped += 1
            continue
        kept.append(rec)

    if logger is not None and dropped:
        logger.info(
            "Excluded %d %s(s) belonging to the --exclude-group-id "
            "group(s); %d remain to be scanned.", dropped, label, len(kept))
    return kept