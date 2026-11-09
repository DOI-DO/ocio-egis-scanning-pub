"""
Shared SMTP email sender with attachments (multi-recipient aware), plus the
run-scaffolding helpers (output dir, CSV writing) that used to live in the
separate reporting.py module.

CONSOLIDATION NOTE
------------------
reporting.py has been folded into this module so UTILITIES has one fewer file.
Everything it exported (ensure_output_dir, gather_attachments, write_dict_csv,
MB) is still available with the SAME name and signature from here (and via
UTILITIES/__init__.py, unchanged). Only the *internal* location moved.

ATTACHMENT SIZE POLICY (NEW)
-----------------------------
The email size ceiling is now a CONFIGURABLE setting, resolved from
config.toml's 'max-email-mb' (see config.py / config.toml), instead of the
previous hard-coded MAX_ATTACHMENTS_MB constant. MAX_ATTACHMENTS_MB is kept as
a fallback DEFAULT (used only if config.toml has no value and the caller
passes nothing explicit), so any code that still imports MAX_ATTACHMENTS_MB
keeps working.

Behavior of gather_attachments(), per the new spec:
  1. Try attaching the output file(s) in their ORIGINAL, UNZIPPED form. If the
     combined size (log + originals) is within the cap, attach them as-is
     (this is unchanged from before).
  2. If the originals are too big, ZIP just the output/report files (the log
     file is never zipped) and see if the zip fits under the cap instead. If
     it does, attach the zip (plus the log) instead of the originals.
  3. If even the zipped size still exceeds the cap, attach NOTHING but the
     log, and the returned `note` text explicitly says the file was too large
     even zipped, and states the on-disk path(s) where it can be found.

This is implemented entirely in this file (no new helper module), per request.
"""
import csv
import os
import smtplib
import sys
import zipfile
from email.message import EmailMessage

SMTP_PORT = 25  # standard SMTP relay port

# Fallback cap (MB) on the total size of CSV/other attachments to email, used
# only when config.toml has no 'max-email-mb' value and the caller does not
# pass cap_mb explicitly. Historically this was the ONLY cap; it is now the
# safety-net default. Every PORTAL_* script should prefer resolving the real
# value from config (see cli.finalize_config / config.get_config_value,
# key 'max-email-mb') and passing it into gather_attachments()/send_email().
MAX_ATTACHMENTS_MB = 20

MB = 1024 * 1024


# ==========================================================================
# Recipient parsing
# ==========================================================================
def normalize_recipients(email_to):
    """
    Turn an email-to value into a clean list of address strings.

    Accepts either:
      * a single address string ("a@x.com"),
      * a comma- (or semicolon-) separated string ("a@x.com, b@y.com"), or
      * an already-split list/tuple of addresses.

    Returns a de-duplicated list preserving first-seen order, with blanks
    removed. This lets the shared config carry one address OR many.
    """
    if email_to is None:
        return []

    if isinstance(email_to, (list, tuple, set)):
        raw = []
        for part in email_to:
            raw.extend(str(part).replace(";", ",").split(","))
    else:
        raw = str(email_to).replace(";", ",").split(",")

    seen = set()
    out = []
    for addr in raw:
        a = addr.strip()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


# ==========================================================================
# Output-dir / CSV helpers (formerly reporting.py)
# ==========================================================================
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


# ==========================================================================
# Attachment sizing / zipping
# ==========================================================================
def _total_mb(paths):
    """Sum of on-disk sizes (MB) for the given paths; missing files count 0."""
    total = 0.0
    for p in paths:
        try:
            total += os.path.getsize(p) / MB
        except OSError:
            pass
    return total


def _zip_files(paths, zip_path, logger=None):
    """
    Compress `paths` into a single zip at zip_path (flat, by basename).
    Returns zip_path on success, or None on any failure (logged if a logger is
    given). Never raises.
    """
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zf:
            for p in paths:
                if p and os.path.exists(p):
                    zf.write(p, arcname=os.path.basename(p))
        return zip_path
    except Exception as exc:
        if logger is not None:
            logger.warning("Could not create zip %s: %s", zip_path, exc)
        return None


def gather_attachments(log_file_path, csv_paths, output_dir, logger,
                       cap_mb=None, label="CSV"):
    """
    Build the email attachment list from the log file plus any output/report
    files (CSVs, etc.), applying the three-tier size policy described in the
    module docstring:

      1. Original files fit under cap_mb (with the log)  -> attach as-is.
      2. They don't fit, but a ZIP of just the output files (plus the log,
         unzipped) fits under cap_mb                       -> attach the zip.
      3. Even the zip doesn't fit                          -> attach only the
         log; `note` explains the files were too large even zipped and gives
         their on-disk path(s).

    Parameters
    ----------
    cap_mb : float or None
        The size ceiling in MB. If None, falls back to MAX_ATTACHMENTS_MB.
        Callers should normally resolve the real value from config.toml's
        'max-email-mb' key (see config.get_config_value) and pass it here.

    Returns
    -------
    (attachments, note) exactly as before: `attachments` always starts with
    the log file; `note` is '' when everything that could be attached was
    attached in its best available form, or an explanatory paragraph to
    append to the email body otherwise.
    """
    cap_mb = MAX_ATTACHMENTS_MB if cap_mb is None else cap_mb

    attachments = [log_file_path]
    written = [p for p in (csv_paths or []) if p]
    if not written:
        return attachments, ""

    # --- Tier 1: originals, unzipped -----------------------------------
    originals_mb = _total_mb(written)
    if originals_mb <= cap_mb:
        attachments.extend(written)
        if logger is not None:
            logger.info("Attaching %d %s(s) to email in original form "
                        "(%.2f MB total).", len(written), label, originals_mb)
        return attachments, ""

    # --- Tier 2: zip just the output files, check again -----------------
    if logger is not None:
        logger.info("%s attachment(s) total %.2f MB (> %d MB cap); "
                    "attempting to zip before giving up ...",
                    label, originals_mb, cap_mb)

    zip_name = f"{os.path.splitext(os.path.basename(written[0]))[0]}_attachments.zip"
    zip_path = os.path.join(output_dir, zip_name)
    zipped = _zip_files(written, zip_path, logger=logger)

    if zipped:
        zip_mb = _total_mb([zipped])
        if zip_mb <= cap_mb:
            attachments.append(zipped)
            if logger is not None:
                logger.info(
                    "Zipped %s attachment(s): %.2f MB -> %.2f MB, now under "
                    "the %d MB cap; attaching the zip instead of the "
                    "originals.", label, originals_mb, zip_mb, cap_mb)
            return attachments, ""

        # --- Tier 3: even the zip is too big --------------------------
        if logger is not None:
            logger.warning(
                "Zipped %s attachment(s) still %.2f MB (> %d MB cap); "
                "NOT attaching. Original file(s) remain on disk in: %s",
                label, zip_mb, cap_mb, output_dir)
        note = (
            f"\nNOTE: The output {label}(s) totaled {originals_mb:.2f} MB "
            f"({zip_mb:.2f} MB even after zipping), exceeding the {cap_mb} MB "
            f"email attachment limit. Neither the original file(s) nor a "
            f"zipped copy could be attached. They remain on disk here:\n"
            + "\n".join(f"  {p}" for p in written) + "\n"
        )
        return attachments, note

    # Zip creation itself failed -> fall back to the original "too big" note.
    if logger is not None:
        logger.warning(
            "%s attachments total %.2f MB, exceeding the %d MB limit, and a "
            "zip could not be created; they will NOT be attached but remain "
            "on disk in: %s", label, originals_mb, cap_mb, output_dir)
    note = (
        f"\nNOTE: The output {label}(s) totaled {originals_mb:.2f} MB, "
        f"exceeding the {cap_mb} MB email attachment limit and were NOT "
        f"attached (a zipped copy could not be created either). They remain "
        f"on disk in:\n{output_dir}\n"
    )
    return attachments, note


# ==========================================================================
# Sending
# ==========================================================================
def send_email(subject, body, attachments, smtp_server, email_to, email_from,
               logger):
    """
    Send a completion email with the given attachments (list of file paths) via
    the given SMTP server. Failure to email is logged but does not crash the
    caller. Missing attachment files are skipped with a warning.

    email_to may be a single address, a comma/semicolon-separated string, or a
    list of addresses; in every case the message is delivered to all of them.

    Attachment MIME subtype is 'csv' for .csv files, 'zip' (application/zip)
    for .zip files, and 'plain' otherwise.
    """
    try:
        recipients = normalize_recipients(email_to)
        if not recipients:
            logger.error("No valid recipient address(es) in email-to: %r",
                         email_to)
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        for path in attachments:
            if not path or not os.path.exists(path):
                logger.warning("Attachment not found, skipping: %s", path)
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            lower = path.lower()
            if lower.endswith(".zip"):
                msg.add_attachment(
                    data, maintype="application", subtype="zip",
                    filename=os.path.basename(path))
            else:
                subtype = "csv" if lower.endswith(".csv") else "plain"
                msg.add_attachment(
                    data, maintype="text", subtype=subtype,
                    filename=os.path.basename(path))

        logger.info("Sending email to %s via %s:%d ...",
                    ", ".join(recipients), smtp_server, SMTP_PORT)
        with smtplib.SMTP(smtp_server, SMTP_PORT, timeout=60) as server:
            # Pass an explicit recipient list so every address is delivered to,
            # regardless of how the To header is formatted.
            server.send_message(msg, from_addr=email_from, to_addrs=recipients)
        logger.info("Email sent successfully.")
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)