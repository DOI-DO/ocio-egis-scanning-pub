"""
Shared run-scaffolding helpers common to every PORTAL_* script:

  * ensure_output_dir - create the output directory or abort to stderr.
  * gather_attachments - given the log path + a set of CSV paths, decide which
    fit under the shared MAX_ATTACHMENTS_MB cap, and return (attachments,
    note_text) so callers stop re-implementing the identical size-gating block.
  * write_dict_csv - write a list of dict rows to a CSV with a given (or
    inferred) header, returning the path or None when there are no rows.
"""
import csv
import os
import sys

from .emailer import MAX_ATTACHMENTS_MB

MB = 1024 * 1024


def ensure_output_dir(output_dir):
    """
    Create output_dir (and parents) if needed. On failure, write a clear
    message to stderr and exit(1) - logging is typically not configured yet
    when this runs, which is exactly why every script did this by hand.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as exc:
        sys.stderr.write(f"Run ABORTED. Could not create output dir "
                         f"'{output_dir}': {exc}\n")
        sys.exit(1)


def gather_attachments(log_file_path, csv_paths, output_dir, logger,
                       cap_mb=MAX_ATTACHMENTS_MB, label="CSV"):
    """
    Build the email attachment list from the log file plus any CSVs that fit
    under the shared size cap.

    Returns (attachments, note) where:
      * attachments always starts with the log file, and includes the CSVs
        only if their combined size is <= cap_mb.
      * note is '' when everything is attached, or an explanatory paragraph to
        append to the email body when the CSVs were left on disk instead.

    This replaces the near-identical size-gating block that appeared in every
    script's main().
    """
    attachments = [log_file_path]
    written = [p for p in (csv_paths or []) if p]
    if not written:
        return attachments, ""

    total_mb = 0.0
    for p in written:
        try:
            total_mb += os.path.getsize(p) / MB
        except OSError:
            pass

    if total_mb <= cap_mb:
        attachments.extend(written)
        logger.info("Attaching %d %s(s) to email (%.2f MB total).",
                    len(written), label, total_mb)
        return attachments, ""

    logger.warning(
        "%s attachments total %.2f MB, exceeding the %d MB limit; they will "
        "NOT be attached but remain on disk in: %s",
        label, total_mb, cap_mb, output_dir)
    note = (
        f"\nNOTE: The output {label}(s) totaled {total_mb:.2f} MB, exceeding "
        f"the {cap_mb} MB email attachment limit and were NOT attached. They "
        f"remain on disk in:\n{output_dir}\n"
    )
    return attachments, note


def write_dict_csv(rows, path, fieldnames=None, logger=None, label="row",
                   force_values=None):
    """
    Write a list of dict rows to a CSV. If fieldnames is None it is taken from
    the first row's keys. Returns path if written, else None (no rows).

    force_values: optional dict of {column: value} written onto every row
    (used to guarantee, e.g., the PLATFORM column is always populated).
    """
    if not rows:
        if logger is not None:
            logger.info("No %s(s) to write for %s.", label, path)
        return None

    if fieldnames is None:
        fieldnames = list(rows[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            if force_values:
                row.update(force_values)
            writer.writerow(row)
    if logger is not None:
        logger.info("Wrote %d %s(s) to %s", len(rows), label, path)
    return path