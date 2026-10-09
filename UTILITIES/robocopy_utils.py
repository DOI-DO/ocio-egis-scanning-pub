r"""
UTILITIES/robocopy_utils.py
==============================================================================
Shared log-collection helpers: date-filter selection and robocopy invocation,
factored out of the retired LOG_DUMP.bat so PORTAL_STACK_LOG_DUMP.py can
drive the same UNC-share robocopy log collection that batch file did, but
with the machine list and destination root supplied dynamically (discovered
live from a Portal deployment via UTILITIES.stack_discovery).

DATE FILTER MODES (unchanged from LOG_DUMP.bat)
------------------------------------------------
  1. Previous Day    -> robocopy /MAXAGE:1               (last 24 hours)
  2. Date Range      -> robocopy /MAXAGE:<n> /MINAGE:<m>  (computed from two
                         YYYYMMDD calendar dates, inclusive)
  3. Previous N Days -> robocopy /MAXAGE:<n>

ROBOCOPY OPTION SET (unchanged from LOG_DUMP.bat)
--------------------------------------------------
    /E /Z /MT:32 /R:1 /W:1 /NFL /NDL /NP /XJ <date-filter-args>

SUBPROCESS ENVIRONMENT
----------------------
Robocopy receives a copy of the parent environment with password, token,
secret, SAS, cloud-key, and connection-string variables removed. This keeps
unrelated credential fallbacks used by the Python process from being inherited
by the child while preserving ordinary Windows/PATH/UNC behavior.

Public API
----------
    ROBOCOPY_BASE_OPTS
        The fixed option list (excluding date-filter flags), as a list of
        strings ready to append to a subprocess command line.

    prompt_date_filter() -> dict
        Interactively prompts exactly as LOG_DUMP.bat did. Returns
        {"mode": "AGE"|"RANGE", "maxage": int|None, "minage": int|None,
         "range_start": str|None, "range_end": str|None}.

    date_filter_from_args(mode, previous_n_days=None, range_start=None,
                          range_end=None) -> dict
        Non-interactive equivalent for scripting/CLI-flag-driven runs. Same
        return shape as prompt_date_filter().

    build_date_filter_opts(filter_info) -> list[str]
        Turns the dict from either function above into the robocopy
        /MAXAGE[/MINAGE] argument list.

    run_robocopy(source, dest, extra_opts, logger=None) -> int
        Runs one robocopy call (source -> dest) with ROBOCOPY_BASE_OPTS +
        extra_opts, mirroring the .bat file's per-path robocopy invocation.
        Returns robocopy's exit code (robocopy exit codes <8 are "success
        with informational bits set", not failure -- see
        ROBOCOPY_SUCCESS_MAX below). Never raises; a launch failure (e.g.
        robocopy.exe not found) is logged and reported as exit code -1.

    ROBOCOPY_SUCCESS_MAX
        Robocopy exit codes 0-7 all indicate some degree of success (bit
        flags for "files copied", "extra files", etc.); only 8+ indicates a
        real failure. Callers should compare an exit code against this
        constant rather than treating any nonzero code as an error.

    copy_group(category, server_short_name, domain_suffix, path_pairs,
              dest_root, date_filter_opts, logger=None) -> list[int]
        Replicates the .bat file's :CopyGroup subroutine for ONE machine:
        builds \\<server_short_name>.<domain_suffix>\D$\<relpath> source UNC
        paths (the D$ share is required here -- it's the actual remote admin
        share) and mirrors them locally under
        <dest_root>\<category>\<fqdn>\<relpath> -- NOTE: the destination
        path deliberately OMITS the D$ segment (there is no such share
        locally; keeping it only added a meaningless extra folder layer to
        every destination path). Runs one robocopy per relative path pair.
        Returns the list of robocopy exit codes (one per path). `path_pairs`
        is a list of relative-path strings (e.g. ["arcgisportal\\logs",
        r"Program Files\\Portal\\framework\\runtime\\tomcat\\logs"]) -- NOT
        hard-coded here, since different categories use different log
        locations.
==============================================================================
"""

import os
import re
import subprocess


# Fixed robocopy options carried over verbatim from LOG_DUMP.bat, EXCLUDING
# the date-filter flags (/MAXAGE, /MINAGE), which vary per run and are built
# separately by build_date_filter_opts().
ROBOCOPY_BASE_OPTS = [
    "/E", "/Z", "/MT:32", "/R:1", "/W:1", "/NFL", "/NDL", "/NP", "/XJ",
]

# Robocopy exit codes 0-7 are informational success bit-flags (0 = nothing to
# copy, 1 = files copied, 2 = extra files, 4 = mismatched files, and
# combinations thereof); 8 and above indicate at least one real failure.
ROBOCOPY_SUCCESS_MAX = 7

_YYYYMMDD_RE = re.compile(r"^\d{8}$")


_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?i)(?:^|_)(?:password|passwd|secret|token|sas|api_key|access_key|"
    r"private_key|account_key|storage_key|connection_string|credentials?)"
    r"(?:$|_)"
)
_EXPLICIT_SECRET_ENV_NAMES = {
    "PORTAL_PASSWORD",
    "DATASTORE_PASSWORD",
    "AZCOPY_SPA_CLIENT_SECRET",
    "AZCOPY_SAS_TOKEN",
    "AZURE_CLIENT_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "DATABASE_URL",
    "SSLKEYLOGFILE",
    "KRB5CCNAME",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
}


def _sanitized_robocopy_env():
    """Return a normal child environment without unrelated credentials."""
    clean = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if (upper in _EXPLICIT_SECRET_ENV_NAMES
                or _SENSITIVE_ENV_NAME_RE.search(upper)):
            continue
        rendered_value = str(value)
        lowered_value = rendered_value.lower()
        if ("sig=" in lowered_value and
                ("sv=" in lowered_value or "se=" in lowered_value)):
            continue
        if re.search(r"(?i)^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",
                     rendered_value):
            continue
        clean[name] = value
    return clean


def _validate_yyyymmdd(value, label):
    if not value or not _YYYYMMDD_RE.match(str(value).strip()):
        raise ValueError(
            f"{label} must be exactly 8 digits in YYYYMMDD form, e.g. "
            f"20260824. Got: {value!r}")
    return str(value).strip()


def _days_between(d1, d2):
    """Whole days between two YYYYMMDD strings (d2 - d1), via datetime."""
    import datetime
    a = datetime.datetime.strptime(d1, "%Y%m%d")
    b = datetime.datetime.strptime(d2, "%Y%m%d")
    return (b - a).days


def prompt_date_filter():
    """
    Interactively prompt for a date filter, replicating LOG_DUMP.bat's
    "Select a date filter" menu exactly (Previous Day / Date Range /
    Previous N Days), including the YYYYMMDD format validation loop for
    Date Range.

    Returns
    -------
    dict
        {"mode": "AGE"|"RANGE", "maxage": int|None, "minage": int|None,
         "range_start": str|None, "range_end": str|None}
    """
    print("Select a date filter:")
    print("  1. Previous Day   (last 24 hours)")
    print("  2. Date Range     (YYYYMMDD to YYYYMMDD)")
    print("  3. Previous N Days")
    print()
    choice = input("Enter choice (1, 2, or 3): ").strip()

    if choice == "1":
        print("\nFilter: Files modified within the last 24 hours.")
        return {"mode": "AGE", "maxage": 1, "minage": None,
               "range_start": None, "range_end": None}

    if choice == "2":
        while True:
            start = input("Enter START date (YYYYMMDD): ").strip()
            if _YYYYMMDD_RE.match(start):
                break
            print("  Invalid format - please enter exactly 8 digits, e.g. "
                 "20260824.")
        while True:
            end = input("Enter END date (YYYYMMDD): ").strip()
            if _YYYYMMDD_RE.match(end):
                break
            print("  Invalid format - please enter exactly 8 digits, e.g. "
                 "20260826.")
        print(f"\nFilter: Files with a last-modified date between {start} "
             f"and {end} (inclusive).")
        return date_filter_from_args("RANGE", range_start=start,
                                     range_end=end)

    if choice == "3":
        ndays = input("Enter number of days back from today: ").strip()
        try:
            n = int(ndays)
        except ValueError:
            raise ValueError(
                f"Number of days must be an integer. Got: {ndays!r}")
        print(f"\nFilter: Files modified within the last {n} day(s).")
        return {"mode": "AGE", "maxage": n, "minage": None,
               "range_start": None, "range_end": None}

    raise ValueError(f"Invalid choice: {choice!r}. Exiting.")


def date_filter_from_args(mode, previous_n_days=None, range_start=None,
                          range_end=None):
    """
    Non-interactive equivalent of prompt_date_filter(), for CLI-flag-driven
    runs. `mode` is one of "PREVIOUS_DAY", "PREVIOUS_N", "RANGE" (case
    insensitive), or the internal "AGE"/"RANGE" values.

    Returns the same dict shape as prompt_date_filter().
    """
    m = str(mode).strip().upper()

    if m in ("PREVIOUS_DAY", "DAY"):
        return {"mode": "AGE", "maxage": 1, "minage": None,
               "range_start": None, "range_end": None}

    if m in ("PREVIOUS_N", "AGE"):
        if previous_n_days is None:
            raise ValueError("previous_n_days is required for Previous N "
                            "Days mode.")
        return {"mode": "AGE", "maxage": int(previous_n_days), "minage": None,
               "range_start": None, "range_end": None}

    if m == "RANGE":
        start = _validate_yyyymmdd(range_start, "range_start")
        end = _validate_yyyymmdd(range_end, "range_end")
        # Mirrors the .bat's PowerShell computation:
        #   DAYS_TO_START = (Now - start_date).TotalDays, rounded
        #   DAYS_TO_END   = max(0, (Now - (end_date + 1 day)).TotalDays), rounded
        import datetime
        now = datetime.datetime.now()
        start_dt = datetime.datetime.strptime(start, "%Y%m%d")
        end_dt = datetime.datetime.strptime(end, "%Y%m%d") + datetime.timedelta(days=1)
        days_to_start = round((now - start_dt).total_seconds() / 86400)
        days_to_end = max(0, round((now - end_dt).total_seconds() / 86400))
        return {"mode": "RANGE", "maxage": days_to_start,
               "minage": days_to_end, "range_start": start,
               "range_end": end}

    raise ValueError(f"Unknown date filter mode: {mode!r}")


def build_date_filter_opts(filter_info):
    """
    Turn the dict from prompt_date_filter()/date_filter_from_args() into the
    robocopy date-filter argument list.
    """
    opts = [f"/MAXAGE:{filter_info['maxage']}"]
    if filter_info.get("minage") is not None:
        opts.append(f"/MINAGE:{filter_info['minage']}")
    return opts


def run_robocopy(source, dest, extra_opts, logger=None):
    """
    Run one robocopy call: robocopy <source> <dest> <ROBOCOPY_BASE_OPTS>
    <extra_opts>. Mirrors the .bat file's per-path invocation exactly
    (options, order aside, are equivalent).

    Returns the robocopy process exit code, or -1 if robocopy could not be
    launched at all (e.g. not found / permissions), which is logged as an
    error rather than raised so one bad path never aborts an entire run.

    Exit codes 0-7 (ROBOCOPY_SUCCESS_MAX) are success; 8+ is a real failure.
    """
    cmd = ["robocopy", source, dest] + ROBOCOPY_BASE_OPTS + list(extra_opts)
    if logger is not None:
        logger.info("Copying: %s", source)
        logger.info("    To: %s", dest)
        logger.debug("  robocopy command: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=_sanitized_robocopy_env(),
        )
        code = result.returncode
    except Exception as exc:
        if logger is not None:
            logger.error("  Could not launch robocopy for %s -> %s: %s",
                        source, dest, exc)
        return -1

    if logger is not None:
        if code > ROBOCOPY_SUCCESS_MAX:
            logger.warning("  robocopy exit code %d (failure) for %s -> %s",
                          code, source, dest)
        else:
            logger.info("  robocopy exit code %d for %s -> %s", code,
                        source, dest)
    return code


def copy_group(category, server_short_name, domain_suffix, path_pairs,
               dest_root, date_filter_opts, logger=None):
    r"""
    Replicate the .bat file's :CopyGroup subroutine for ONE machine.

    For each relative path in `path_pairs`, builds:
        source = \\<server_short_name>.<domain_suffix>\D$\<relpath>
        dest   = <dest_root>\<category>\<fqdn>\<relpath>
    and runs one robocopy call per path.

    NOTE ON THE DESTINATION PATH: the source UNC path still goes through the
    D$ admin share (that's the actual remote share and can't be dropped --
    there is no other way to reach that folder over the network). The LOCAL
    destination path, however, deliberately does NOT include a "D$" segment
    -- there is no such share on the local disk, and keeping it there only
    added a pointless extra folder layer to every destination path, e.g.
        C:\Temp\20260829_1131\east\big_serverGISAP20.enterprise.com\arcgisserver\logs
    instead of the old, needlessly deep
        C:\Temp\20260829_1131\east\big_serverGISAP20.enterprise.com\D$\arcgisserver\logs

    Parameters
    ----------
    category : str
        "Portal", "Hosting", "DataStore", or a federated site name (matches
        the .bat's grouping, extended to cover federated sites).
    server_short_name : str
        The short (non-FQDN) machine name, e.g. "big_serverGISAP32". Use
        UTILITIES.stack_discovery.short_hostname() to derive this from a
        discovered FQDN.
    domain_suffix : str
        The domain to append to form the FQDN used for the UNC path, e.g.
        "enterprise.com" (matches the .bat's hard-coded ".enterprise.com").
    path_pairs : list[str]
        Relative paths (under the D$ share) to copy for this category, e.g.
        ["arcgisportal\logs",
         r"Program Files\Portal\framework\runtime\tomcat\logs"].
    dest_root : str
        The destination root directory (e.g. C:\Temp\<timestamp>).
    date_filter_opts : list[str]
        Output of build_date_filter_opts().
    logger : logging.Logger, optional

    Returns
    -------
    list[int]
        One robocopy exit code per path in `path_pairs`, in order.
    """
    fqdn = f"{server_short_name}.{domain_suffix}"

    if logger is not None:
        logger.info("-------------------------------------------------------")
        logger.info("%s - %s", category, server_short_name)
        logger.info("-------------------------------------------------------")

    codes = []
    for relpath in path_pairs:
        source = f"\\\\{fqdn}\\D$\\{relpath}"
        dest = os.path.join(dest_root, category, fqdn, relpath)
        code = run_robocopy(source, dest, date_filter_opts, logger=logger)
        codes.append(code)
    return codes