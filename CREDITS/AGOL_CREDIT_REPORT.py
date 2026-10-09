#!/usr/bin/env python3
r"""
AGOL_CREDIT_REPORT.py

Generate an ArcGIS Online credit-usage report for a chosen period, clean it
into a by-user CSV, and roll it up into by-app and by-user-summary CSVs.
AGOL only. This script's own direct requests.Session (used for the report
request/poll/download REST calls, separate from the arcgis SDK's own
connection) uses an explicit, caller-chosen TLS verification strategy
(--tls-mode), with a one-time startup handshake check before any real API
traffic. The auth token is held in a managed TokenProvider (UTILITIES.auth)
whose repr()/str() never expose the value; the token acquisition and
representation are controlled, though the raw string is necessarily returned
by get() at each REST call site.

Depends on the shared .\UTILITIES package (imported as a sibling directory
of this script). See readme.md for full configuration and usage details.

Example usage:
    python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week
    python AGOL_CREDIT_REPORT.py --config org2.config.toml --period month
    python AGOL_CREDIT_REPORT.py --config org1.config.toml --period month --timeout-minutes 45
    python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week --output-dir "C:\temp\reports"
    python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week --tls-mode os-trust-store
    python AGOL_CREDIT_REPORT.py --help
"""

import argparse
import csv
import datetime
import io
import math
import os
import sys
import time
import zipfile

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from UTILITIES import (   # noqa: E402
    build_timestamp,
    setup_logging,
    connect_gis,
    send_email,
    get_platform,
    start_timer,
    timing_block,
    subject_line,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_config_arg,
    finalize_config,
    get_max_email_mb,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    provider_for_gis,
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
    redact_token,
)
from UTILITIES.restutils import get_json, DEFAULT_TIMEOUT   # noqa: E402

EMAIL_SUBJECT_PREFIX = "AGOL Credit Report"
FORCED_TARGET = "agol"
PLATFORM_LABEL = "AGOL"

REQUIRED_SETTINGS = (
    "portal-url", "username", "keyring-service", "output-dir",
    "smtp-server", "email-to", "email-from",
)

REPORT_POLL_SECONDS = 5
DEFAULT_TIMEOUT_MINUTES = 10
REPORT_INITIAL_DELAY = 3
REQUEST_TIMEOUT = 60

# Bounded number of auth-related failures tolerated while polling before the
# run is declared a terminal authentication failure (rather than continuing
# to poll to the nominal timeout). Each such failure triggers a token
# refresh; if refresh does not resolve it within this many attempts, the
# poll loop aborts as a real error.
MAX_POLL_AUTH_FAILURES = 3

DUPLICATE_REPORT_PHRASE = "already generated"
_DUP_ID_PATTERN = r"report item id\s*:\s*([0-9a-fA-F]{16,})"

META_LINE_PREFIXES = ("organization:", "starttime:", "endtime:")

BY_APP_FIELDS = ["PLATFORM", "app", "credits"]
BY_USER_SUMMARY_FIELDS = ["PLATFORM", "user", "total_credits"]

USER_ID_COLUMN_CANDIDATES = ("username", "user name", "user", "full name",
                             "fullname", "name")

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


def _start_of_day_utc(dt):
    return datetime.datetime(dt.year, dt.month, dt.day,
                             tzinfo=datetime.timezone.utc)


def _yesterday_bounds(now_utc):
    today0 = _start_of_day_utc(now_utc)
    y_start = today0 - datetime.timedelta(days=1)
    y_end = today0 - datetime.timedelta(seconds=1)
    return y_start, y_end


def _last_week_bounds(now_utc):
    _y_start, y_end = _yesterday_bounds(now_utc)
    week_end_day = _start_of_day_utc(y_end)
    while week_end_day.weekday() != 4:
        week_end_day -= datetime.timedelta(days=1)
    week_start = week_end_day - datetime.timedelta(days=6)
    week_end = (week_end_day + datetime.timedelta(days=1)
                - datetime.timedelta(seconds=1))
    return week_start, week_end


def _last_month_bounds(now_utc):
    today0 = _start_of_day_utc(now_utc)
    first_of_this_month = today0.replace(day=1)
    last_month_end = first_of_this_month - datetime.timedelta(seconds=1)
    last_month_start = last_month_end.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return last_month_start, last_month_end


def resolve_period(args, logger):
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    if args.period == "week":
        start, end = _last_week_bounds(now_utc)
        slug = f"week_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    elif args.period == "month":
        start, end = _last_month_bounds(now_utc)
        slug = f"month_{start.strftime('%Y%m')}"
    else:
        logger.error("Unknown period %r.", args.period)
        raise SystemExit(2)
    start_ms = int(start.timestamp() * 1000)
    return {"label": args.period, "slug": slug, "start": start, "end": end,
            "start_ms": start_ms}


def _report_time_params(period):
    if period["label"] == "week":
        return {"timeDuration": "weekly", "startTime": period["start_ms"]}
    return {"timeDuration": "monthly", "startTime": period["start_ms"]}


def _post_report_request(session, base_url, username, token_provider, period,
                         logger):
    url = f"{base_url.rstrip('/')}/sharing/rest/community/users/{username}/report"
    params = {"f": "json", **token_provider.auth_params(), "reportType": "org",
              "reportSubType": "credits"}
    params.update(_report_time_params(period))
    try:
        r = session.post(url, data=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.SSLError as exc:
        return None, {"message": redact_token(
            f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}")}
    except Exception as exc:
        return None, {"message": redact_token(f"request failed: {exc}")}
    if isinstance(data, dict) and "error" in data:
        return None, (data.get("error") or {})
    item_id = (data.get("itemId") if isinstance(data, dict) else None) or \
        (data.get("id") if isinstance(data, dict) else None)
    if not item_id:
        return None, {"message": f"no item id in response: {str(data)[:300]}"}
    return item_id, None


def _delete_report_item(gis, item_id, username, logger):
    """
    Permanently delete an existing credit-report item created by a prior run.

    SAFETY GUARD: before deleting, the item is validated to actually look
    like this user's own credit-report item -- it must be owned by the same
    `username` this run authenticated as, and its type must look like a
    report. This prevents a mis-parsed id (or a maliciously/accidentally
    crafted "already generated" message pointing at an unrelated id) from
    causing this script to permanently delete arbitrary org content. If the
    item does not pass validation, deletion is REFUSED and the caller aborts
    regeneration rather than destroying something it should not.
    """
    try:
        item = gis.content.get(item_id)
    except Exception as exc:
        logger.error("Could not resolve existing report item %s to delete: %s",
                     item_id, exc)
        return False
    if item is None:
        logger.warning("Existing report item %s not found; it may already be "
                       "gone.", item_id)
        return True

    title = (getattr(item, "title", "") or "")
    owner = (getattr(item, "owner", "") or "")
    itype = (getattr(item, "type", "") or "")

    # Only delete an item that plausibly IS our own credit report. Owner must
    # match the authenticated user; type must look report-ish. Anything else
    # is refused loudly rather than destroyed.
    if owner != username:
        logger.error(
            "REFUSING to delete report item %s: it is owned by %r, not the "
            "authenticated user %r. Aborting rather than deleting content "
            "this run does not own. (item type=%r, title=%r)",
            item_id, owner or "(none)", username, itype, title)
        return False
    if "report" not in itype.lower():
        logger.error(
            "REFUSING to delete report item %s: its type %r does not look "
            "like a credit-report item (expected a type containing "
            "'report'). Aborting rather than deleting an unexpected item. "
            "(owner=%r, title=%r)",
            item_id, itype or "(none)", owner, title)
        return False

    logger.info("Permanently deleting existing report item %s (owner=%s, "
                "type=%s, title=%s) ...", item_id, owner, itype,
                title or "untitled")
    try:
        ok = item.delete(permanent=True)
    except TypeError:
        try:
            ok = item.delete()
        except Exception as exc:
            logger.error("Failed to delete report item %s: %s", item_id, exc)
            return False
    except Exception as exc:
        logger.error("Failed to permanently delete report item %s: %s",
                     item_id, exc)
        return False
    if ok:
        logger.info("Deleted report item %s.", item_id)
        return True
    logger.error("Delete call for report item %s returned falsy.", item_id)
    return False


def _extract_existing_report_id(message):
    import re
    m = re.search(_DUP_ID_PATTERN, str(message or ""), re.I)
    return m.group(1) if m else None


def create_credit_report(gis, session, base_url, username, token_provider,
                         period, logger):
    logger.info("Requesting credits report (%s) for %s ... window %s -> %s "
                "(timeDuration=%s).", period["label"], username,
                period["start"].date(), period["end"].date(),
                _report_time_params(period).get("timeDuration"))
    item_id, err = _post_report_request(session, base_url, username,
                                        token_provider, period, logger)
    if item_id:
        logger.info("Report item created: %s", item_id)
        return item_id
    message = str((err or {}).get("message", ""))
    if "TLS/CERTIFICATE VERIFICATION FAILED" in message:
        logger.error("Report request TLS/CERTIFICATE VERIFICATION FAILED: %s",
                     message)
        return None
    if DUPLICATE_REPORT_PHRASE in message.lower():
        existing_id = _extract_existing_report_id(message)
        if not existing_id:
            logger.error("Report already exists but its item id could not be "
                         "parsed from the message: %s", message)
            return None
        logger.warning("A %s report already exists (item %s); deleting it and "
                       "regenerating.", period["label"], existing_id)
        if not _delete_report_item(gis, existing_id, username, logger):
            logger.error("Could not delete the existing report; aborting.")
            return None
        item_id, err = _post_report_request(session, base_url, username,
                                            token_provider, period, logger)
        if item_id:
            logger.info("Report item created on retry: %s", item_id)
            return item_id
    logger.error("Report request returned an error: %s", err)
    return None


def _classify_poll_error(err):
    """
    Classify a get_json() error STRING (get_json converts requests
    exceptions, including SSLError which is a ConnectionError subclass, into
    an error string, so an outer `except SSLError` never sees them). Returns
    one of "tls", "auth", or "transient".

    This is deliberately string-based because get_json()'s contract is a
    (data, error_str) tuple, and this script must not change that shared
    helper's behavior. Worst case of a mis-classification here is an extra
    poll or an earlier abort -- never a silently wrong report.
    """
    low = str(err or "").lower()
    if any(t in low for t in ("certificate", "ssl", "tls")):
        return "tls"
    if any(t in low for t in ("token", "498", "499", "expired",
                              "invalid token", "unauthorized", "403",
                              "not authorized")):
        return "auth"
    return "transient"


def wait_for_report(session, base_url, username, token_provider, item_id,
                    timeout_minutes, logger):
    status_url = (f"{base_url.rstrip('/')}/sharing/rest/content/users/"
                  f"{username}/items/{item_id}/status")
    max_tries = max(1, int(round(timeout_minutes * 60 / REPORT_POLL_SECONDS)))
    # Real wall-clock deadline: max_tries is only a safety ceiling on the
    # number of iterations; the deadline is what actually bounds total run
    # time, since each poll can itself take up to REQUEST_TIMEOUT plus any
    # session-level retries and would otherwise let the run run far past
    # --timeout-minutes.
    deadline = time.monotonic() + (timeout_minutes * 60)
    logger.info("Waiting for report to finish (polling every %ds, up to %d "
                "tries and a hard ~%g min wall-clock deadline) ...",
                REPORT_POLL_SECONDS, max_tries, timeout_minutes)

    auth_failures = 0
    for attempt in range(1, max_tries + 1):
        # Wall-clock guard: stop as soon as the real deadline passes,
        # regardless of how many attempts remain.
        if time.monotonic() >= deadline:
            logger.error("Wall-clock timeout (~%g min) reached after %d "
                         "poll(s) waiting for the report to finish; raise "
                         "--timeout-minutes if reports for this org take "
                         "longer.", timeout_minutes, attempt - 1)
            return False

        try:
            data, err = get_json(session, status_url, token_provider.get(),
                                 timeout=REQUEST_TIMEOUT,
                                 params={"jobType": "output"})
        except requests.exceptions.SSLError as exc:
            # Retained as defense-in-depth for any path that does not route
            # through get_json()'s own exception handling.
            logger.error("TLS/CERTIFICATE VERIFICATION FAILED while polling "
                         "report status: %s", redact_token(str(exc)))
            return False

        if err:
            kind = _classify_poll_error(err)
            if kind == "tls":
                # A certificate failure mid-run is terminal, not "not ready".
                logger.error("TLS/CERTIFICATE VERIFICATION FAILED while "
                             "polling report status: %s", err)
                return False
            if kind == "auth":
                auth_failures += 1
                # Force token re-acquisition; a token that expired mid-run
                # would otherwise stay cached and every poll would keep
                # failing until the nominal timeout.
                try:
                    token_provider.refresh()
                except Exception as refresh_exc:  # noqa: BLE001
                    logger.warning("Token refresh after auth error failed: %s",
                                   redact_token(str(refresh_exc)))
                logger.warning("Authentication error while polling (attempt "
                               "%d/%d, auth failure %d/%d); refreshed token "
                               "and will retry: %s", attempt, max_tries,
                               auth_failures, MAX_POLL_AUTH_FAILURES, err)
                if auth_failures >= MAX_POLL_AUTH_FAILURES:
                    logger.error("Repeated authentication failures (%d) while "
                                 "polling report status; treating as a "
                                 "terminal authentication failure rather "
                                 "than continuing to poll. Last error: %s",
                                 auth_failures, err)
                    return False
            else:
                logger.debug("  status poll %d/%d: %s", attempt, max_tries, err)
        elif isinstance(data, dict):
            status = str(data.get("status", "")).lower()
            if status in ("completed", "complete"):
                logger.info("Report completed after %d poll(s) (%ds).",
                            attempt, attempt * REPORT_POLL_SECONDS)
                return True
            if status in ("failed", "error", "partial"):
                logger.error("Report generation did not succeed (status=%s): "
                             "%s", status, data)
                return False
            logger.debug("  status poll %d/%d: status=%s", attempt, max_tries,
                         status or "(none)")
        if attempt < max_tries:
            time.sleep(REPORT_POLL_SECONDS)
    logger.error("Timed out after ~%d min (%d poll(s)) waiting for the report "
                 "to finish; raise --timeout-minutes if reports for this org "
                 "take longer.", timeout_minutes, max_tries)
    return False


def download_report_csv(gis, session, base_url, username, token_provider,
                        item_id, output_dir, logger):
    downloaded_path = None
    try:
        item = gis.content.get(item_id)
        if item is not None:
            logger.info("Downloading report CSV via the ArcGIS item API ...")
            downloaded_path = item.download(save_path=output_dir)
            if downloaded_path and os.path.isfile(downloaded_path):
                logger.info("Report downloaded: %s", downloaded_path)
                text = _read_csv_file(downloaded_path, logger)
                if text:
                    return text
            else:
                logger.warning("item.download() did not return a file path; "
                               "falling back to a direct request.")
        else:
            logger.warning("Could not resolve report item %s via the item API; "
                           "falling back to a direct request.", item_id)
    except Exception as exc:
        logger.warning("ArcGIS item download failed (%s); falling back to a "
                       "direct request.", redact_token(str(exc)))
    finally:
        if downloaded_path and os.path.isfile(downloaded_path):
            try:
                os.remove(downloaded_path)
            except OSError:
                logger.debug("Could not remove temp download %s (leaving it).",
                             downloaded_path)

    data_url = (f"{base_url.rstrip('/')}/sharing/rest/content/users/"
                f"{username}/items/{item_id}/data")
    logger.info("Downloading report data directly for item %s ...", item_id)
    try:
        r = session.get(data_url, params={"token": token_provider.get()},
                        timeout=REQUEST_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
    except requests.exceptions.SSLError as exc:
        logger.error("TLS/CERTIFICATE VERIFICATION FAILED downloading report "
                     "data: %s", redact_token(str(exc)))
        return None
    except Exception as exc:
        logger.error("Report download failed: %s", redact_token(str(exc)))
        return None
    content = r.content or b""
    if not content:
        logger.error("Report download returned an empty body.")
        return None
    return _decode_report_bytes(content, r.headers.get("Content-Type"), logger)


def _read_csv_file(path, logger):
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as exc:
        logger.error("Could not read downloaded report file %s: %s", path, exc)
        return None
    return _decode_report_bytes(content, None, logger)


def _decode_report_bytes(content, content_type, logger):
    if content[:4] == b"PK\x03\x04":
        logger.info("Report data is a ZIP archive; extracting the CSV ...")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_names = [n for n in zf.namelist()
                             if n.lower().endswith(".csv")]
                if not csv_names:
                    logger.error("ZIP contained no .csv entry (entries: %s).",
                                 ", ".join(zf.namelist()) or "(none)")
                    return None
                with zf.open(csv_names[0]) as fh:
                    content = fh.read()
                logger.info("Extracted %s from the report ZIP.", csv_names[0])
        except zipfile.BadZipFile as exc:
            logger.error("Report data looked like a ZIP but could not be "
                         "opened: %s", exc)
            return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    if not text.strip():
        logger.error("Report CSV was empty after decoding.")
        return None
    head = text.lstrip()[:200].lower()
    ctype = (content_type or "").lower()
    if head.startswith("<") or "<html" in head or "json" in ctype or \
            head.startswith("{"):
        logger.error("Report download returned a non-CSV body (HTML/JSON); the "
                     "report may not be downloadable with this request. First "
                     "bytes: %s", text[:200])
        return None
    return text


def _is_metadata_line(stripped):
    low = stripped.lower()
    if any(low.startswith(p) for p in META_LINE_PREFIXES):
        return True
    colon = stripped.find(":")
    comma = stripped.find(",")
    if colon > 0 and (comma == -1 or colon < comma):
        left = stripped[:colon].strip()
        if left and "," not in left and len(left.split()) <= 2:
            return True
    return False


def _strip_metadata_rows(raw_text, logger):
    lines = raw_text.splitlines()
    header_idx = -1
    stripped_count = 0
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        if _is_metadata_line(stripped):
            stripped_count += 1
            continue
        header_idx = i
        break
    if header_idx < 0:
        preview = " | ".join(
            l.strip() for l in lines[:6] if l.strip())[:300]
        logger.error("Could not locate the CSV header row after stripping "
                     "metadata lines. First lines were: %s", preview or "(none)")
        return None
    logger.info("Stripped %d metadata line(s); real header at line %d.",
                stripped_count, header_idx + 1)
    return "\n".join(lines[header_idx:])


def _csv_safe(value):
    """
    Neutralize spreadsheet-formula injection in a cell value.

    A CSV cell whose first character is one of = + - @ (or a leading tab /
    carriage return followed by one of those) can be interpreted as a
    formula by Excel/Sheets when the file is opened. Prefix such values with
    a single quote so they are shown literally. Non-string values are
    returned unchanged. This only affects how a spreadsheet DISPLAYS the
    value; the underlying data is preserved (with the guarding quote).
    """
    if value is None:
        return value
    if not isinstance(value, str):
        return value
    stripped = value.lstrip("\t\r ")
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def parse_and_write_by_user(cleaned_text, csv_path, platform, logger):
    reader = csv.reader(io.StringIO(cleaned_text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        logger.warning("Cleaned report contained no rows.")
        return None, [], []
    header = [h.strip() for h in rows[0]]
    data = rows[1:]
    out_fields = ["PLATFORM"] + header
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(out_fields)
            for r in data:
                writer.writerow([platform] + [_csv_safe(c) for c in r])
    except Exception as exc:
        logger.error("Failed to write by-user CSV: %s", exc)
        return None, header, []
    logger.info("Wrote %d credit-by-user row(s) to %s", len(data), csv_path)
    dict_rows = []
    for r in data:
        padded = list(r) + [""] * (len(header) - len(r))
        dict_rows.append(dict(zip(header, padded)))
    return csv_path, header, dict_rows


def _looks_like_credit_column(name):
    identity = {
        "username", "user name", "user", "full name", "fullname", "name",
        "email", "user type", "usertype", "role",
    }
    return name.strip().lower() not in identity


def _parse_credit_value(raw, column, logger, malformed_counter):
    """
    Parse a single credit cell into a float. Returns (value, ok). A blank
    cell is (0.0, True) and does not count as malformed. A non-numeric,
    non-blank cell is counted in malformed_counter and returns (0.0, False)
    so the caller can decide, but the running total is unaffected either way
    (callers add only on ok). This centralizes what used to be a silent
    `except ValueError: continue`, so malformed numeric data is at least
    surfaced in the log rather than vanishing without trace.
    """
    text = (raw or "").strip().replace(",", "")
    if not text:
        return 0.0, True
    try:
        return float(text), True
    except ValueError:
        malformed_counter[column] = malformed_counter.get(column, 0) + 1
        return 0.0, False


def build_by_app_rows(header, dict_rows, platform, logger):
    if not header:
        return []
    app_cols = [c for c in header[1:] if _looks_like_credit_column(c)]
    totals = {c: 0.0 for c in app_cols}
    malformed = {}
    for row in dict_rows:
        for c in app_cols:
            value, ok = _parse_credit_value(row.get(c), c, logger, malformed)
            if ok:
                totals[c] += value
    if malformed:
        detail = ", ".join(f"{col}={cnt}" for col, cnt in sorted(
            malformed.items()))
        logger.warning("build_by_app: ignored non-numeric credit value(s) in "
                       "%d column(s): %s", len(malformed), detail)
    by_app = [{"PLATFORM": platform, "app": c, "credits": round(totals[c], 2)}
              for c in app_cols]
    logger.info("Rolled up credits across %d app column(s).", len(by_app))
    return by_app


def _find_user_id_column(header):
    lower_map = {h.strip().lower(): h for h in header}
    for candidate in USER_ID_COLUMN_CANDIDATES:
        if candidate in lower_map:
            return lower_map[candidate]
    return header[0] if header else None


def build_by_user_summary_rows(header, dict_rows, platform, logger):
    if not header or not dict_rows:
        return []
    user_col = _find_user_id_column(header)
    if not user_col:
        logger.warning("Could not identify a user column for the per-user "
                       "summary; skipping.")
        return []
    app_cols = [c for c in header[1:] if _looks_like_credit_column(c)]
    app_cols = [c for c in app_cols if c != user_col]
    malformed = {}
    summary = []
    for row in dict_rows:
        user = (row.get(user_col) or "").strip()
        if not user:
            continue
        total = 0.0
        for c in app_cols:
            value, ok = _parse_credit_value(row.get(c), c, logger, malformed)
            if ok:
                total += value
        summary.append({"PLATFORM": platform, "user": _csv_safe(user),
                        "total_credits": round(total, 2)})
    if malformed:
        detail = ", ".join(f"{col}={cnt}" for col, cnt in sorted(
            malformed.items()))
        logger.warning("build_by_user_summary: ignored non-numeric credit "
                       "value(s) in %d column(s): %s", len(malformed), detail)
    summary.sort(key=lambda r: r["total_credits"], reverse=True)
    logger.info("Rolled up per-user credit summary across %d user(s).",
                len(summary))
    return summary


def build_email_body(platform, org_name, period, by_user_count, by_app_rows,
                     by_user_summary_rows, by_user_csv, by_app_csv,
                     by_user_summary_csv, tls_mode, timing, attach_note):
    total_credits = round(sum(r.get("credits", 0.0) for r in by_app_rows), 2)
    top_apps = sorted(by_app_rows, key=lambda r: r.get("credits", 0.0),
                      reverse=True)[:10]
    top_users = by_user_summary_rows[:10]
    lines = [
        f"{EMAIL_SUBJECT_PREFIX} for platform [{platform}].",
        "",
        f"Organization: {org_name}",
        f"Period: {period['label']}",
        f"Window (UTC): {period['start'].date()} to {period['end'].date()}",
        f"TLS mode: {tls_mode}",
        f"Credit-by-user rows: {by_user_count}",
        f"App columns summed: {len(by_app_rows)}",
        f"Users summarized: {len(by_user_summary_rows)}",
        f"Total credits (period): {total_credits}",
        f"By-user CSV: {by_user_csv or '(NOT WRITTEN)'}",
        f"By-app CSV: {by_app_csv or '(NOT WRITTEN)'}",
        f"By-user summary CSV: {by_user_summary_csv or '(NOT WRITTEN)'}",
        "",
        "Top app credit usage (this period):",
    ]
    if top_apps:
        for r in top_apps:
            lines.append(f"  {r['app']}: {r['credits']}")
    else:
        lines.append("  (no credit usage found)")
    lines += ["", "Top user credit usage (this period):"]
    if top_users:
        for r in top_users:
            lines.append(f"  {r['user']}: {r['total_credits']}")
    else:
        lines.append("  (no credit usage found)")
    lines += [
        "",
        "The default Esri credits report prepends Organization/StartTime/"
        "EndTime rows that make the CSV unusable; those rows are stripped in "
        "the attached by-user CSV. The by-app CSV sums each app column across "
        "all users, and the by-user summary CSV sums each user's credits "
        "across all app columns.",
        "",
        timing,
    ]
    if attach_note:
        lines.append(attach_note)
    return "\n".join(lines)


def positive_float(value):
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "--timeout-minutes must be a positive number.")
    # Reject NaN and infinity: comparisons with NaN are always False, so a
    # bare `<= 0` check lets "nan"/"inf" through and they later blow up in
    # round()/int() or create an effectively unbounded loop.
    if not math.isfinite(fvalue):
        raise argparse.ArgumentTypeError(
            "--timeout-minutes must be a finite number (not nan/inf).")
    if fvalue <= 0:
        raise argparse.ArgumentTypeError(
            "--timeout-minutes must be greater than 0.")
    return fvalue


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate an AGOL credit-usage report for a chosen time "
                    "period. AGOL only. TLS verification strategy for this "
                    "script's own direct REST calls is explicit (--tls-mode); "
                    "the arcgis SDK connection (via connect_gis()) always "
                    "verifies TLS regardless.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python AGOL_CREDIT_REPORT.py --config org1.config.toml "
            "--period week\n"
            "  python AGOL_CREDIT_REPORT.py --config org2.config.toml "
            "--period month\n"
            "  python AGOL_CREDIT_REPORT.py --config org1.config.toml "
            "--period week --tls-mode os-trust-store\n"
        ),
    )
    add_config_arg(parser)
    add_common_portal_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    parser.add_argument(
        "--period", required=True, type=str.lower, choices=["week", "month"],
        help="REQUIRED. Reporting window: 'week' (previous Saturday->Friday) "
             "or 'month' (last calendar month).",
    )
    parser.add_argument(
        "--timeout-minutes", type=positive_float,
        default=DEFAULT_TIMEOUT_MINUTES,
        help=f"Minutes to wait for report generation (default: "
             f"{DEFAULT_TIMEOUT_MINUTES}). Polled every {REPORT_POLL_SECONDS}s, "
             f"with a hard wall-clock deadline at the specified number of "
             f"minutes.",
    )

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How this script's own HTTP session (used for the credit-report "
             "request/poll/download REST calls -- NOT the arcgis SDK's own "
             "internal connection, which always verifies TLS via "
             "connect_gis()) verifies TLS certificates. 'standard' "
             "(default): certifi's bundled public CA list -- correct for "
             "AGOL. 'os-trust-store': verify against the operating system's "
             "native certificate store (requires the 'truststore' package). "
             "'custom-ca-bundle': verify against an explicit PEM file passed "
             "via --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle file. REQUIRED when --tls-mode is "
             "custom-ca-bundle; ignored otherwise. Also passed through to "
             "connect_gis() so the arcgis SDK connection trusts the same "
             "CA.")
    return parser.parse_args()


def subject_line_for(period, timestamp, org_name):
    prefix = f"{EMAIL_SUBJECT_PREFIX} ({period['label']})"
    return subject_line(prefix, timestamp, PLATFORM_LABEL, org_name=org_name)


def main():
    args = parse_args()
    args.target = FORCED_TARGET

    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
             "custom-ca-bundle.", file=sys.stderr)
        sys.exit(2)

    timestamp = build_timestamp()
    start_wallclock, start_perf = start_timer()

    finalize_config(args, required=REQUIRED_SETTINGS)
    max_email_mb = get_max_email_mb(args.target, args.config)

    # ENFORCE AGOL-ONLY. Previously any non-AGOL URL was silently relabeled
    # as "AGOL", which meant an Enterprise Portal (or a typo'd/blank URL)
    # would be scanned with AGOL-specific endpoints, mislabeled, and could
    # run the destructive duplicate-report logic against the wrong
    # environment. Fail closed instead: a correctly configured AGOL run
    # still returns "AGOL" here and proceeds unchanged; anything else aborts
    # before any connection is made.
    platform = get_platform(args.portal_url)
    if platform != PLATFORM_LABEL:
        print(
            f"This script is AGOL-only, but the portal-url resolved from "
            f"--config classifies as {platform!r} (portal-url="
            f"{args.portal_url!r}). Refusing to run AGOL-specific reporting "
            f"against a non-AGOL (or unrecognized) URL. Check the [agol] "
            f"section of your org config.", file=sys.stderr)
        sys.exit(2)
    org_prefix = args.org_name_safe

    ensure_output_dir(args.output_dir)

    log_file_path = os.path.join(
        args.output_dir, f"{org_prefix}_AGOL_CREDIT_REPORT_{timestamp}.log")
    logger = setup_logging(log_file_path, "agol_credit_report")

    logger.info("=" * 70)
    logger.info("AGOL Credit Report - run started")
    logger.info("Org config: %s", args.config)
    logger.info("Org name: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("Portal URL: %s", args.portal_url)
    logger.info("Username: %s", args.username)
    logger.info("Keyring service: %s", args.keyring_service)
    logger.info("Output dir: %s", args.output_dir)
    logger.info("Log file: %s", log_file_path)
    logger.info("SMTP server: %s", args.smtp_server)
    logger.info("Email to: %s", args.email_to)
    logger.info("Email from: %s", args.email_from)
    logger.info("Max email attachment size (MB): %s", max_email_mb)
    logger.info("Requested period: %s", args.period)
    logger.info("Report wait timeout: %g minute(s)", args.timeout_minutes)
    logger.info("=" * 70)

    period = resolve_period(args, logger)
    logger.info("Resolved window (UTC): %s -> %s (startTime=%d ms).",
                period["start"], period["end"], period["start_ms"])

    subject = subject_line_for(period, timestamp, args.org_name)

    by_user_csv = None
    by_app_csv = None
    by_user_summary_csv = None
    by_user_count = 0
    by_app_rows = []
    by_user_summary_rows = []
    # Track whether the run actually reached a clean, complete report so the
    # final exit status and "Success" log are honest. A partial/failed run
    # must NOT log Success or exit 0.
    report_ok = False

    try:
        mode = _TLS_MODE_CHOICES[args.tls_mode]
        session = build_secure_session(
            mode, ca_bundle_path=args.ca_bundle_path, retry_total=3,
            logger=logger)
    except (ImportError, ValueError) as exc:
        logger.error("Could not build a secure session: %s",
                     redact_token(str(exc)))
        send_email(subject,
                   f"Run ABORTED. Could not establish TLS verification "
                   f"strategy '{args.tls_mode}': {redact_token(str(exc))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    # verify_tls_handshake() builds its SSL context (e.g.
    # ssl.create_default_context(cafile=...)) BEFORE its own try/except, so a
    # missing/unreadable/invalid custom CA PEM can raise straight out of the
    # call. Wrap it here so such a failure becomes the normal abort-email
    # path instead of an uncaught traceback with no notification.
    try:
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, session, logger=logger)
    except Exception as exc:  # noqa: BLE001
        handshake_ok = False
        handshake_detail = {"error": redact_token(str(exc))}
        logger.error("TLS handshake verification raised before returning "
                     "(likely an invalid/unreadable CA bundle): %s",
                     handshake_detail["error"])
    if not handshake_ok:
        logger.error(
            "Aborting: TLS handshake verification to %s FAILED (%s).",
            args.portal_url, handshake_detail.get("error"))
        send_email(subject,
                   f"Run ABORTED. TLS handshake verification to "
                   f"{args.portal_url} failed: "
                   f"{redact_token(str(handshake_detail.get('error')))}\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    try:
        # Pass verify_mode through so the arcgis SDK connection uses the SAME
        # TLS strategy as this script's direct session. Without it,
        # custom-ca-bundle raises inside connect_gis() (ca_bundle_path given
        # but verify_mode left at its STANDARD_CA default), and os-trust-store
        # is only honored by accident.
        gis = connect_gis(args.portal_url, args.username,
                          args.keyring_service, logger,
                          verify_mode=mode, ca_bundle_path=ca_path)
    except Exception as exc:
        logger.error("Could not connect to AGOL: %s", redact_token(str(exc)))
        send_email(subject,
                   f"Run ABORTED. Could not connect: {redact_token(str(exc))}"
                   "\n\n"
                   + timing_block(start_wallclock, start_perf),
                   [log_file_path], args.smtp_server, args.email_to,
                   args.email_from, logger)
        sys.exit(1)

    token_provider = provider_for_gis(gis, platform)
    logger.info("Token acquired via %s (token present: %s).",
                type(token_provider).__name__, token_provider.is_present())

    try:
        base_url = gis._portal.url
        username = args.username

        item_id = create_credit_report(gis, session, base_url, username,
                                       token_provider, period, logger)
        raw_text = None
        if item_id:
            time.sleep(REPORT_INITIAL_DELAY)
            if wait_for_report(session, base_url, username, token_provider,
                               item_id, args.timeout_minutes, logger):
                raw_text = download_report_csv(
                    gis, session, base_url, username, token_provider, item_id,
                    args.output_dir, logger)

        if raw_text:
            cleaned = _strip_metadata_rows(raw_text, logger)
            if cleaned:
                by_user_path = os.path.join(
                    args.output_dir,
                    f"{org_prefix}_AGOL_CREDIT_REPORT_BY_USER_"
                    f"{period['slug']}_{timestamp}.csv")
                by_user_csv, header, dict_rows = parse_and_write_by_user(
                    cleaned, by_user_path, platform, logger)
                by_user_count = len(dict_rows)

                by_app_rows = build_by_app_rows(header, dict_rows, platform,
                                                logger)
                if by_app_rows:
                    by_app_path = os.path.join(
                        args.output_dir,
                        f"{org_prefix}_AGOL_CREDIT_REPORT_BY_APP_"
                        f"{period['slug']}_{timestamp}.csv")
                    by_app_csv = write_dict_csv(
                        by_app_rows, by_app_path, fieldnames=BY_APP_FIELDS,
                        logger=logger, label="app credit row",
                        force_values={"PLATFORM": platform})

                by_user_summary_rows = build_by_user_summary_rows(
                    header, dict_rows, platform, logger)
                if by_user_summary_rows:
                    by_user_summary_path = os.path.join(
                        args.output_dir,
                        f"{org_prefix}_AGOL_CREDIT_REPORT_BY_USER_SUMMARY_"
                        f"{period['slug']}_{timestamp}.csv")
                    by_user_summary_csv = write_dict_csv(
                        by_user_summary_rows, by_user_summary_path,
                        fieldnames=BY_USER_SUMMARY_FIELDS, logger=logger,
                        label="user credit summary row",
                        force_values={"PLATFORM": platform})

                # A clean run is one that produced the primary by-user CSV
                # from a successfully generated, downloaded, and parsed
                # report.
                report_ok = bool(by_user_csv)
    except Exception as exc:
        logger.exception("Fatal error during credit report generation: %s",
                         redact_token(str(exc)))
        report_ok = False

    logger.info("=" * 70)
    logger.info("RUN SUMMARY")
    logger.info("Organization: %s", args.org_name)
    logger.info("Platform: %s", platform)
    logger.info("TLS mode: %s", args.tls_mode)
    logger.info("Period: %s (%s -> %s)", period["label"],
                period["start"].date(), period["end"].date())
    logger.info("By-user rows: %d", by_user_count)
    logger.info("By-app rows: %d", len(by_app_rows))
    logger.info("By-user summary rows: %d", len(by_user_summary_rows))
    logger.info("By-user CSV: %s", by_user_csv or "(NOT WRITTEN)")
    logger.info("By-app CSV: %s", by_app_csv or "(NOT WRITTEN)")
    logger.info("By-user summary CSV: %s",
                by_user_summary_csv or "(NOT WRITTEN)")
    logger.info("Overall result: %s", "SUCCESS" if report_ok else "FAILED / "
                "INCOMPLETE")
    logger.info("=" * 70)

    timing = timing_block(start_wallclock, start_perf)
    written = [by_user_csv, by_app_csv, by_user_summary_csv]

    attachments, note = gather_attachments(
        log_file_path, written, args.output_dir, logger, cap_mb=max_email_mb)

    body = build_email_body(
        platform, args.org_name, period, by_user_count, by_app_rows,
        by_user_summary_rows, by_user_csv, by_app_csv, by_user_summary_csv,
        args.tls_mode, timing, note)
    # Make the emailed result honest about a partial/failed run rather than
    # sending a normal-looking report email that silently omits data.
    if not report_ok:
        body = ("WARNING: THIS RUN DID NOT COMPLETE SUCCESSFULLY. The credit "
                "report was not generated, downloaded, or parsed into a "
                "by-user CSV. The figures below (if any) are incomplete and "
                "should not be treated as an authoritative credit report. See "
                "the attached log for the specific failure.\n\n") + body

    logger.info("Emailing results to configured recipient(s) ...")
    send_email(subject, body, attachments, args.smtp_server,
               args.email_to, args.email_from, logger)

    if report_ok:
        logger.info("Success")
        sys.exit(0)
    logger.error("Run did not complete successfully; exiting with status 1.")
    sys.exit(1)


if __name__ == "__main__":
    main()