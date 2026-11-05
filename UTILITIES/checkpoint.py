"""
UTILITIES/checkpoint.py
==============================================================================
Shared per-service checkpointing for long-running scan scripts.

WHY THIS EXISTS
---------------
Most PORTAL_* scans (e.g. FIELD_NAME_SCAN.py) are cheap enough per-service
(one service-definition REST call) that "just re-run the whole thing from
scratch" is an acceptable failure-recovery strategy even at org scale. Scripts
that also pull and analyze RECORD data (e.g. a PII content scan running
Presidio over every string field of every layer) are meaningfully more
expensive per service, so a crash/interruption partway through a multi-hour
org-wide run should not throw away everything already completed.

GRANULARITY
-----------
Checkpointing is per-SERVICE, not per-layer or per-record. A service that was
mid-scan when the process died is simply re-scanned in full on resume -- this
matches the existing convention (in FIELD_NAME_SCAN.py) of treating "one
service" as the natural unit of work per thread, and avoids the much harder
problem of resuming mid-layer or mid-record-page.

MODE SAFETY
-----------
A checkpoint file records which MODE produced it (TEST vs FULL) and, for TEST
mode, the exact --test-count used. Resuming a checkpoint under a DIFFERENT
mode or a different test_count is refused (hard abort) unless the caller
passes --fresh-start -- silently resuming a stale TEST checkpoint into a FULL
run (or vice versa) would produce a report that LOOKS complete but silently
skipped most of the org. See CheckpointModeMismatch below.

FILE FORMAT
-----------
Plain JSON, one file per script+org+target+mode, e.g.:
    C:\\temp\\reports\\ORG_PII_SCAN_agol_checkpoint.json          (FULL mode)
    C:\\temp\\reports\\ORG_PII_SCAN_agol_TEST_checkpoint.json     (TEST mode)

    {
      "run_started": "2026-08-30T14:22:00",
      "mode": "TEST",
      "test_count": 25,
      "completed_service_ids": ["abc123...", "def456..."],
      "total_services": 25
    }

Written after EVERY completed service (append + flush), so a crash never
loses more than the one in-flight service. Deleted automatically on a clean
full completion -- a checkpoint file left on disk always means an incomplete
run.

Public API
----------
    checkpoint_path(output_dir, org_prefix, script_tag, target, mode)
        -> str
        Builds the checkpoint file path using the shared naming convention.

    load_or_init_checkpoint(path, mode, test_count, logger=None)
        -> dict
        Loads an existing checkpoint file, OR initializes a fresh in-memory
        checkpoint dict if none exists. Raises CheckpointModeMismatch (never
        SystemExit itself -- the caller decides how to report/abort) if an
        existing checkpoint's mode/test_count does not match the current run.

    save_checkpoint(path, state, logger=None)
        Writes the full checkpoint dict to disk (overwrite). Called after
        every completed service so progress is never lost.

    mark_service_complete(state, item_id)
        Adds item_id to the in-memory state's completed set (mutates state).
        Caller is responsible for calling save_checkpoint() afterward.

    is_service_complete(state, item_id) -> bool
        True if item_id is already recorded as completed in state.

    clear_checkpoint(path, logger=None)
        Deletes the checkpoint file (used on clean completion, or when
        --fresh-start is passed). Never raises if the file doesn't exist.

    CheckpointModeMismatch(Exception)
        Raised by load_or_init_checkpoint() when an existing checkpoint's
        mode/test_count does not match the requested run. Callers should
        catch this, log a clear error explaining --fresh-start, and exit.
==============================================================================
"""

import json
import os

from .restutils import now_wallclock


class CheckpointModeMismatch(Exception):
    """
    Raised when an existing checkpoint file's mode (TEST/FULL) or test_count
    does not match the current run. Callers should catch this, log a clear
    message telling the user to pass --fresh-start, and exit -- this module
    never calls sys.exit() itself so it stays usable outside a CLI context.
    """
    def __init__(self, path, existing_mode, existing_test_count,
                requested_mode, requested_test_count):
        self.path = path
        self.existing_mode = existing_mode
        self.existing_test_count = existing_test_count
        self.requested_mode = requested_mode
        self.requested_test_count = requested_test_count
        msg = (
            f"Existing checkpoint {path!r} was created in "
            f"{existing_mode!r} mode"
            + (f" (test_count={existing_test_count})"
               if existing_mode == "TEST" else "")
            + f", but this run requested {requested_mode!r} mode"
            + (f" (test_count={requested_test_count})"
               if requested_mode == "TEST" else "")
            + ". Resuming across a mode/test-count change is refused because "
              "it would silently under-scan the org. Pass --fresh-start to "
              "discard the existing checkpoint and start over, or re-run "
              "with the original mode/test-count to resume it as-is."
        )
        super().__init__(msg)


def checkpoint_path(output_dir, org_prefix, script_tag, target, mode):
    """
    Build the checkpoint file path using the shared naming convention:
        <output_dir>/<org_prefix>_<script_tag>_<target>_checkpoint.json        (FULL)
        <output_dir>/<org_prefix>_<script_tag>_<target>_TEST_checkpoint.json   (TEST)

    Parameters
    ----------
    output_dir : str
    org_prefix : str
        args.org_name_safe from finalize_config().
    script_tag : str
        Short script identifier, e.g. "PII_SCAN".
    target : str
        "portal" or "agol".
    mode : str
        "TEST" or "FULL".
    """
    mode = str(mode).strip().upper()
    suffix = "_TEST_checkpoint.json" if mode == "TEST" else "_checkpoint.json"
    fname = f"{org_prefix}_{script_tag}_{target}{suffix}"
    return os.path.join(output_dir, fname)


def _fresh_state(mode, test_count):
    return {
        "run_started": now_wallclock(),
        "mode": str(mode).strip().upper(),
        "test_count": test_count,
        "completed_service_ids": [],
        "total_services": None,
    }


def load_or_init_checkpoint(path, mode, test_count, logger=None):
    """
    Load an existing checkpoint file at `path`, or return a fresh in-memory
    state dict if none exists.

    Raises CheckpointModeMismatch if a file exists but its recorded mode (or,
    for TEST mode, its test_count) differs from the current run's mode /
    test_count. This function never deletes or overwrites anything -- callers
    handle --fresh-start by calling clear_checkpoint() themselves BEFORE
    calling this function.

    Returns
    -------
    dict
        {"run_started", "mode", "test_count", "completed_service_ids"
         (list), "total_services"}
    """
    mode = str(mode).strip().upper()

    if not os.path.isfile(path):
        if logger is not None:
            logger.info("No existing checkpoint at %s; starting fresh.", path)
        return _fresh_state(mode, test_count)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "Could not read existing checkpoint %s (%s); treating as no "
                "checkpoint and starting fresh. If this file is corrupt, "
                "consider passing --fresh-start.", path, exc)
        return _fresh_state(mode, test_count)

    existing_mode = str(state.get("mode", "")).strip().upper()
    existing_test_count = state.get("test_count")

    mismatch = existing_mode != mode
    if mode == "TEST" and not mismatch:
        mismatch = existing_test_count != test_count

    if mismatch:
        raise CheckpointModeMismatch(
            path, existing_mode, existing_test_count, mode, test_count)

    completed = state.get("completed_service_ids") or []
    if logger is not None:
        logger.info(
            "Resuming from existing checkpoint %s: %d service(s) already "
            "completed (run started %s, mode=%s%s).",
            path, len(completed), state.get("run_started", "?"),
            existing_mode,
            f", test_count={existing_test_count}" if mode == "TEST" else "")

    # Normalize shape defensively in case of a hand-edited/older file.
    state["completed_service_ids"] = list(completed)
    state["mode"] = existing_mode
    state["test_count"] = existing_test_count
    state.setdefault("total_services", None)
    state.setdefault("run_started", now_wallclock())
    return state


def save_checkpoint(path, state, logger=None):
    """
    Write the full checkpoint state to disk, overwriting any existing file.
    Called after every completed service so a crash never loses more than the
    one in-flight service. Failures are logged but never raised -- a
    checkpoint WRITE failure should not abort an otherwise-successful scan.
    """
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp_path, path)  # atomic on both POSIX and Windows
    except Exception as exc:
        if logger is not None:
            logger.warning("Could not write checkpoint %s: %s", path, exc)


def mark_service_complete(state, item_id):
    """Record item_id as completed in the in-memory state (mutates state)."""
    if item_id and item_id not in state["completed_service_ids"]:
        state["completed_service_ids"].append(item_id)


def is_service_complete(state, item_id):
    """True if item_id is already recorded as completed."""
    return item_id in (state.get("completed_service_ids") or [])


def clear_checkpoint(path, logger=None):
    """
    Delete the checkpoint file. Used on clean full completion, or when
    --fresh-start is passed before a new run begins. Never raises if the file
    is already absent.
    """
    try:
        if os.path.isfile(path):
            os.remove(path)
            if logger is not None:
                logger.info("Checkpoint %s cleared.", path)
    except Exception as exc:
        if logger is not None:
            logger.warning("Could not remove checkpoint %s: %s", path, exc)