"""Shared argparse building blocks, subject-line helper, and config merge.

*** MULTI-ORG CHANGE ***
-------------------------------------------------------------------------
Every script now REQUIRES a --config <path-to-org-toml> argument (added by
add_config_arg(), called first in every script's parse_args()). This selects
WHICH organization's settings file is read -- e.g.:

    python SCRIPT.py --config org1.config.toml --target portal
    python SCRIPT.py --config org2.config.toml --target agol

finalize_config() loads that org's toml (via UTILITIES.config.load_config()),
fills in the shared connection/email/output settings exactly as before, AND
additionally resolves+validates the new mandatory 'org-name' setting from
[common], storing the SANITIZED (filename-safe) form on args.org_name_safe and
the RAW (human-readable) form on args.org_name. Scripts use org_name_safe when
building output file names and org_name in the email subject/body.

The connection / email / output options remain OPTIONAL on the command line.
Any value not supplied is filled in from the org's config file by
finalize_config(). A value passed on the command line always overrides the
config; the config in turn overrides nothing (it is the fallback).

The target ("portal" or "agol") remains a REQUIRED user parameter (--target)
for every multi-target script, added by add_target_arg(). It selects which
section of the CHOSEN org's toml is read. Portal-only tools (no --target flag)
still get --config and org-name; see those scripts for details.

*** CONSOLIDATION PHASE 3 (CLI scaffolding) ***
-------------------------------------------------------------------------
This revision adds three pieces of shared scaffolding that were previously
copy-pasted, with small variations, into 10-15 individual PORTAL_*/scanner
scripts. Per the consolidation plan, the UNION of every script's behavior is
kept -- nothing that any script relied on has been dropped.

  * _reserve_run_paths(output_dir, log_prefix, timestamp, *, extra_paths=(),
                       log_suffix=".log")
        Atomically reserve a collision-free run timestamp/log file, trying
        candidate paths in order (base timestamp, then "_01", "_02", ...)
        until BOTH the log path can be exclusively created (O_CREAT|O_EXCL)
        AND every path in `extra_paths` (e.g. one or more output CSVs) does
        not already exist. This is the union of the many slightly different
        "_reserve_run_paths"/"_reserve_log_path" helpers scattered across the
        project: some scripts only reserved a log file, some reserved a log
        + one CSV, some reserved a log + several CSVs (main/violations/
        all-rows/etc). This single helper covers all of those call shapes
        via the `extra_paths` callable-or-list parameter (see docstring
        below for the exact contract), returning the resolved run_stamp plus
        the final log path and the final extra paths (with the resolved
        run_stamp substituted in, if extra_paths was given as a template
        callable).

  * _bounded_future_results(executor, items, worker, max_in_flight)
        The bounded-concurrency submission/consumption generator duplicated
        (under various names: _bounded_future_results, _bounded_futures,
        _bounded_results, _bounded_map) in roughly a dozen scripts. Submits
        up to `max_in_flight` jobs at a time from `items`, yielding
        (item, future) pairs as they complete and backfilling the queue.
        This is the most general of the duplicated shapes (plain
        (item, future) yield); scripts that want (item, result, exception)
        triples can trivially wrap one call to future.result() themselves.

  * build_verified_gis(args, mode, logger, *, pool_connections=None,
                       pool_maxsize=None, retry_total=2, ca_bundle_path=None,
                       session_kwargs=None)
        The "build a secure session -> verify the TLS handshake against
        args.portal_url -> connect_gis() with the SAME verify_mode -> return
        everything the caller needs" sequence that appears, nearly
        verbatim, at the top of almost every hardened scanner's main(). On
        any failure this raises UTILITIES.auth.ScanAbort with a clear,
        already-redacted message, exactly matching the control-flow pattern
        every calling script already uses (catch ScanAbort / its own local
        ScanAbort subclass, log it, send an abort email, exit 1) -- scripts
        that still define their OWN local ScanAbort subclass continue to
        work unchanged, since Python exception handling matches by MRO and
        UTILITIES.auth.ScanAbort is now the common base every local
        ScanAbort should (and, going forward, does) derive from.

        Returns a BuiltGis namedtuple: (session, gis, token_provider,
        verify_mode, ca_bundle_path). The caller remains responsible for
        closing `session` (and any additional worker sessions it builds) --
        this function does not own a WorkerSessionPool, since the right
        pool size/shape still varies per script.

These are intentionally still prefixed with a leading underscore (except
build_verified_gis, which is the primary public entry point scripts are
expected to call) to signal "shared internal helper, not part of the original
narrow per-script CLI surface" -- but they are imported exactly the same way
as every other UTILITIES.cli name, i.e. `from UTILITIES.cli import
_reserve_run_paths, _bounded_future_results, build_verified_gis` or via the
package-level `from UTILITIES import ...` once __init__.py is updated in a
later step to re-export them.
"""

import os
from collections import namedtuple
from concurrent.futures import FIRST_COMPLETED, wait

from . import config as _config
from .auth import ScanAbort, provider_for_gis
from .secure_session import (
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from .gis_connect import connect_gis


def add_config_arg(parser):
    """
    Add the REQUIRED --config argument selecting WHICH org's toml file is
    read for connection/email/output/org-name settings.

    *** MULTI-ORG CHANGE ***: this is new and MANDATORY on every script, even
    when you only manage a single organization -- this keeps every script's
    invocation explicit and unambiguous about which org it is scanning,
    rather than silently depending on a fixed default file.

        --config org1.config.toml
        --config org2.config.toml

    Call this FIRST in parse_args(), before add_target_arg()/others, so
    --help lists it up top as the primary required input.
    """
    parser.add_argument(
        "--config",
        required=True,
        type=str,
        metavar="ORG_TOML_PATH",
        help="REQUIRED. Path to the org-specific config toml file (e.g. "
             "org1.config.toml or org2.config.toml). Selects which "
             "organization's connection/email/output settings and org-name "
             "prefix this run uses. There is no default -- every run must "
             "name its org config explicitly.",
    )


def add_target_arg(parser):
    """
    Add the REQUIRED --target argument selecting which section of the chosen
    --config org toml is read for connection settings.

        --target portal   -> reads the [portal] section (+ [common])
        --target agol      -> reads the [agol]   section (+ [common])

    The value is normalized to lowercase and validated by argparse choices.
    Explicit --portal-url/--username/--keyring-service still override whatever
    the chosen section provides.
    """
    parser.add_argument(
        "--target",
        required=True,
        type=str.lower,
        choices=["portal", "agol"],
        help="REQUIRED. Which environment to run against: 'portal' or 'agol'. "
             "Selects the matching section of the --config org toml file for "
             "connection/email/output settings.",
    )


def add_common_portal_args(parser):
    """
    Add --portal-url / --username / --keyring-service.

    These are OPTIONAL now: if omitted, finalize_config() fills them from the
    org toml section chosen by --target (then [common]). A command-line
    value always wins.
    """
    group = parser.add_argument_group("Portal connection (config fallback)")
    group.add_argument(
        "--portal-url",
        default=None,
        help="Portal/AGOL URL, e.g. "
             '"https://org.maps.arcgis.com/home" or '
             '"https://enterprise.com/portal". If omitted, taken from '
             "the --config org toml (the --target section's 'portal-url').",
    )
    group.add_argument(
        "--username",
        default=None,
        help="Login username, e.g. AGOL_ADMIN or PORTAL_ADMIN. If "
             "omitted, taken from the --config org toml (the --target "
             "section's 'username').",
    )
    group.add_argument(
        "--keyring-service",
        default=None,
        help="Keyring service name used to look up the password (e.g. AGOL or "
             "PORTAL). If omitted, taken from the --config org toml (the "
             "--target section's 'keyring-service').",
    )


def add_email_args(parser):
    """
    Add --smtp-server / --email-to / --email-from.

    These are OPTIONAL now: if omitted, finalize_config() fills them from the
    --config org toml. A command-line value always wins.
    """
    group = parser.add_argument_group("Email (config fallback)")
    group.add_argument(
        "--smtp-server",
        default=None,
        help="SMTP server used to send the completion email. If omitted, taken "
             "from the --config org toml ('smtp-server').",
    )
    group.add_argument(
        "--email-to",
        default=None,
        help="Recipient address for the completion email. If omitted, taken "
             "from the --config org toml ('email-to').",
    )
    group.add_argument(
        "--email-from",
        default=None,
        help="Sender address for the completion email. If omitted, taken from "
             "the --config org toml ('email-from').",
    )


def add_output_dir_arg(parser):
    """
    Add the shared --output-dir option (OPTIONAL; config fallback).

    Scripts that take a positional output path instead do not call this; they
    resolve 'output-dir' from config themselves when the positional is absent.
    """
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where the timestamped output CSV is written. If "
             "omitted, taken from the --config org toml ('output-dir').",
    )


def add_test_flag(parser, help_text):
    """
    Add the shared --no-test flag (test mode is the default in both scripts).
    Each script supplies its own help text describing what test mode means.
    """
    parser.add_argument(
        "--no-test",
        dest="test",
        action="store_false",
        help=help_text,
    )
    parser.set_defaults(test=True)  # TEST MODE is the default


def subject_line(prefix, timestamp, platform=None, org_name=None):
    """
    Build the shared email subject string.

    *** MULTI-ORG CHANGE ***: an optional org_name (the RAW, human-readable
    value from the org's toml -- see finalize_config()) is now prepended as a
    single bracketed tag alongside the platform, so completion emails from
    different orgs are instantly distinguishable in an inbox even when
    subjects otherwise look identical:

        subject_line("Related Items", ts, "PORTAL", "ORG1")
            -> "[ORG1-PORTAL] Related Items <ts>"
        subject_line("Related Items", ts, "PORTAL")
            -> "[PORTAL] Related Items <ts>"                  (unchanged)
        subject_line("Orphan Items Backup and Delete", ts, None, "ORG1")
            -> "[ORG1] Orphan Items Backup and Delete <ts>"
        subject_line("Orphan Items Backup and Delete", ts)
            -> "Orphan Items Backup and Delete <ts>"          (unchanged)

    Passing org_name=None preserves the exact legacy behavior for any caller
    not yet updated, so this change is backward compatible.
    """
    tag_parts = [p for p in (org_name, platform) if p]
    if tag_parts:
        return f"[{'-'.join(tag_parts)}] {prefix} {timestamp}"
    return f"{prefix} {timestamp}"


def finalize_config(args, required=(), logger=None):
    """
    Load the org toml named by --config, fill any unset connection / email /
    output args from it (using the section chosen by args.target, when the
    script has a target), resolve+validate the mandatory 'org-name' setting,
    then verify every `required` config key ended up with a value.

    Call this once, right after parse_args(). It reads args.config (added by
    add_config_arg()) to decide WHICH org toml to load, and args.target (added
    by add_target_arg(), when present) to decide which section within that
    toml to pull from. Mutates `args` in place so the rest of the script can
    use args.portal_url, args.smtp_server, args.org_name, args.org_name_safe,
    etc. without caring whether a value came from the command line or the
    config file.

    Portal-only scripts with NO --target flag should set args.target = "portal"
    themselves BEFORE calling this (matching the existing pattern those
    scripts already use for the shared CONFIG_KEYS machinery).

    Parameters
    ----------
    args : argparse.Namespace
        The parsed arguments (mutated in place). Must have a `config`
        attribute (the org toml path) and a `target` attribute ("portal" or
        "agol").
    required : iterable of str
        Config keys in dash form (e.g. "portal-url") the script cannot run
        without. 'org-name' is ALWAYS required in addition to whatever is
        passed here -- a run can never proceed without a valid org label.
    logger : logging.Logger, optional
        Used to report problems. If None (logging not yet configured), the
        message is printed to stderr instead.

    Returns
    -------
    argparse.Namespace
        The same `args`, with config values applied.
    """
    import sys

    config_path = getattr(args, "config", None)
    if not config_path:
        msg = ("--config is required (path to your org's toml file, e.g. "
               "--config org1.config.toml).")
        if logger is None:
            print(msg, file=sys.stderr)
        else:
            logger.error("%s", msg)
        raise SystemExit(2)

    # Determine the target section to read from.
    try:
        target = _config.normalize_target(getattr(args, "target", None))
    except ValueError as exc:
        msg = f"{exc} Pass --target portal  or  --target agol."
        if logger is None:
            print(msg, file=sys.stderr)
        else:
            logger.error("%s", msg)
        raise SystemExit(2)

    # Store the normalized value back so callers/logging see a clean label.
    args.target = target

    # Load the CHOSEN org's toml up front so a bad/missing path fails fast
    # with a clear message, before anything else is attempted.
    try:
        _config.load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        if logger is None:
            print(str(exc), file=sys.stderr)
        else:
            logger.error("%s", exc)
        raise SystemExit(2)

    # Fill every known config-backed attribute that the CLI left unset, from
    # the chosen target's section (then [common]) OF THE SELECTED ORG TOML.
    # NOTE: apply_to_args() only sets attributes that ALREADY exist on args
    # (e.g. via add_email_args()), so keys with no matching --flag (like
    # 'max-email-mb' or 'org-name') are copied only when the script pre-
    # declares that attribute -- org-name is handled explicitly below instead,
    # so it is validated regardless of whether the script declared an
    # --org-name flag (none do; it is config-only, never a CLI override).
    _config.apply_to_args(args, target, path=config_path)

    # --- Resolve + validate the mandatory org-name (config-only setting) ---
    raw_org_name = _config.get_config_value("org-name", target, path=config_path)
    safe_org_name = _config.sanitize_org_name(raw_org_name)
    if not safe_org_name:
        msg = (
            "Missing required setting 'org-name' in the [common] section of "
            f"the --config org toml ({config_path}). Every org config file "
            "must define a short org-name used to prefix output files and "
            "emails, e.g.:\n"
            "    [common]\n"
            '    org-name = "ORG1"\n'
        )
        if logger is None:
            print(msg, file=sys.stderr)
        else:
            logger.error("%s", msg)
        raise SystemExit(2)
    args.org_name = str(raw_org_name).strip()   # raw, human-readable form
    args.org_name_safe = safe_org_name          # filename-safe form

    # Validate the ones this script declared as required (org-name is already
    # validated above and is not part of this generic loop).
    missing = _config.require(args, required, logger=logger)
    if missing:
        msg = ("Missing required setting(s) with no value on the command line "
               "or in the [" + target + "] / [common] section of "
               + config_path + ": "
               + ", ".join("--" + k for k in missing)
               + f"\nEdit {config_path} or pass the flag(s) explicitly.")
        if logger is None:
            print(msg, file=sys.stderr)
        else:
            logger.error("%s", msg)
        raise SystemExit(2)
    return args


def get_max_email_mb(target, config_path, default=None):
    """
    Resolve 'max-email-mb' for the given target within the chosen org toml
    (dash-key only; there is no --max-email-mb CLI flag, since this is a
    mail-server characteristic, not a per-run choice). Returns a float, or
    `default` if unset/blank/invalid.

    *** MULTI-ORG CHANGE ***: now requires config_path (the org toml selected
    via --config) since there is no default file to fall back to.

    Every PORTAL_* script's main() should call this once, right after
    finalize_config(), passing args.config, and pass the result as
    gather_attachments(..., cap_mb=...) so the configured mail-server limit is
    honored. If this returns None (nothing configured), gather_attachments()
    falls back to its own built-in default (emailer.MAX_ATTACHMENTS_MB = 20).

    Example
    -------
        finalize_config(args, required=REQUIRED_SETTINGS)
        max_email_mb = get_max_email_mb(args.target, args.config)
        ...
        attachments, note = gather_attachments(
            log_file_path, [csv_path], args.output_dir, logger,
            cap_mb=max_email_mb)
    """
    raw = _config.get_config_value("max-email-mb", target, path=config_path)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def normalize_target(value):
    """Validate/normalize a target label to "portal" or "agol"."""
    return _config.normalize_target(value)


# ==========================================================================
# CONSOLIDATION PHASE 3: shared CLI/run scaffolding
# ==========================================================================

def _reserve_run_paths(output_dir, log_prefix, timestamp, *, extra_paths=(),
                       log_suffix=".log", max_attempts=1000):
    """
    Atomically reserve a unique run timestamp, together with its log path and
    any number of additional "must not already exist" output paths (e.g. one
    or more report CSVs), using the SAME run_stamp for all of them.

    This is the union of every "_reserve_run_paths" / "_reserve_log_path" /
    "_reserve_run_stamp" variant previously copy-pasted across the project:

      * Some scripts reserved only a log file (e.g. USER_REPORT.py's
        _reserve_log_path, NAMED_USER_LICENSE_INVENTORY.py's
        _reserve_run_paths).
      * Some reserved a log + exactly one CSV (e.g. GROUPS_SCAN.py,
        EMPTY_FEATURE_SERVICES.py, SCAN_WEB_MAPS.py, GET_RELATED_ITEMS.py).
      * Some reserved a log + SEVERAL CSVs that must all avoid collision
        together (e.g. FEATURE_CLASS_NAME_SCAN.py's violations + all-rows
        CSVs, ITEM_INVENTORY.py's main/feature/summary CSVs).

    This helper covers all three shapes via `extra_paths`, which may be:

      * a plain iterable of COMPLETE file path templates, each containing a
        single '{stamp}' placeholder, e.g.
            extra_paths=[
                os.path.join(output_dir, f"{prefix}_MAIN_{{stamp}}.csv"),
                os.path.join(output_dir, f"{prefix}_SUMMARY_{{stamp}}.csv"),
            ]
        Each is formatted with the CANDIDATE run_stamp on every attempt, and
        the attempt only succeeds once NONE of the formatted paths exist yet
        (checked with os.path.exists) AND the log file can be exclusively
        created.

      * empty/omitted, for the log-only case.

    Parameters
    ----------
    output_dir : str
        Directory the log (and any extra_paths, once formatted) live in.
        Only used to build the default log path; extra_paths are expected to
        already be fully-qualified templates (they may live in a different
        directory than output_dir, e.g. a user-supplied report path).
    log_prefix : str
        Filename prefix for the log, e.g. "{org}_{platform}_my_scan". The
        final log filename is f"{log_prefix}_{run_stamp}{log_suffix}".
    timestamp : str
        The base timestamp (from build_timestamp()) to try FIRST, before
        falling back to "{timestamp}_01", "{timestamp}_02", etc.
    extra_paths : iterable of str, optional
        '{stamp}'-templated complete file paths that must also not already
        exist for a candidate run_stamp to be accepted. Default: none (the
        log file is the only thing reserved).
    log_suffix : str, optional
        Extension for the log file. Default ".log".
    max_attempts : int, optional
        How many "{timestamp}_NN" suffixes to try before giving up.

    Returns
    -------
    (run_stamp, log_path, resolved_extra_paths) : (str, str, list[str])
        run_stamp is the accepted timestamp/suffix combination; log_path is
        the exclusively-created log file path (already exists as an empty
        0600 file -- the caller should open it for logging, e.g. via
        UTILITIES.logging_setup.setup_logging(log_path, ...)); and
        resolved_extra_paths is extra_paths with '{stamp}' substituted by
        run_stamp, in the SAME order they were supplied, ready for the
        caller to pass straight to write_dict_csv()/open().

    Raises
    ------
    RuntimeError
        If no collision-free run_stamp could be found within max_attempts.
    """
    extra_templates = list(extra_paths or ())

    for attempt in range(max_attempts):
        run_stamp = timestamp if attempt == 0 else f"{timestamp}_{attempt:02d}"
        log_path = os.path.join(output_dir, f"{log_prefix}_{run_stamp}{log_suffix}")

        resolved_extra = [tmpl.format(stamp=run_stamp) for tmpl in extra_templates]
        if any(os.path.exists(p) for p in resolved_extra):
            continue

        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)

        # Re-check the extra paths after reserving the log: a parallel run
        # could have created one of them in the narrow window above. If so,
        # release the log reservation and try the next candidate stamp.
        if any(os.path.exists(p) for p in resolved_extra):
            try:
                os.remove(log_path)
            except OSError:
                pass
            continue

        return run_stamp, log_path, resolved_extra

    raise RuntimeError(
        f"Could not reserve a unique run timestamp under {output_dir!r} "
        f"(prefix {log_prefix!r}) after {max_attempts} attempts."
    )


def _bounded_future_results(executor, items, worker, max_in_flight):
    """
    Submit jobs from `items` to `executor` (running `worker(item)` for each),
    keeping at most `max_in_flight` futures pending at once, and yield
    (item, future) pairs as each one completes -- backfilling the pending set
    from `items` after every yield so the pool never starves or over-queues.

    This is the shared shape behind the many near-identical
    "_bounded_future_results" / "_bounded_futures" / "_bounded_results" /
    "_bounded_map" helpers duplicated across roughly a dozen scanner scripts
    (FEATURE_CLASS_NAME_SCAN.py, FIELD_NAME_SCAN.py, GROUPS_SCAN.py,
    ITEM_INVENTORY.py, NAMED_USER_LICENSE_INVENTORY.py,
    PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py, SCAN_BROKEN_HOSTED_SERVICES.py,
    SCAN_WEB_MAPS.py, GET_RELATED_ITEMS.py, CERTIFICATE_INVENTORY_SCAN.py,
    EMPTY_FEATURE_SERVICES.py, and others). The caller is left to call
    `future.result()` (and handle/convert exceptions) exactly as each script
    already does today -- this keeps the helper's contract minimal and lets
    call sites that want a (item, result, exception) triple build that with
    one extra line, matching whichever of the slightly different historical
    conventions that particular script used.

    Parameters
    ----------
    executor : concurrent.futures.Executor
        Normally a ThreadPoolExecutor already sized by the caller.
    items : iterable
        The work items to submit, lazily consumed (may be a generator).
    worker : callable
        worker(item) -> result. Submitted via executor.submit(worker, item).
    max_in_flight : int
        Maximum number of not-yet-completed futures kept pending at once.
        Callers typically pass something like `workers * 2` so the pool
        always has a little extra queued work ready to go.

    Yields
    ------
    (item, future) : tuple
        The original item and its (now-completed) Future. Call
        future.result() to get the worker's return value or raise its
        exception.
    """
    iterator = iter(items)
    pending = {}

    def _fill():
        while len(pending) < max_in_flight:
            try:
                item = next(iterator)
            except StopIteration:
                return
            pending[executor.submit(worker, item)] = item

    _fill()
    while pending:
        completed, _ = wait(set(pending), return_when=FIRST_COMPLETED)
        for future in completed:
            item = pending.pop(future)
            yield item, future
        _fill()


BuiltGis = namedtuple(
    "BuiltGis", ["session", "gis", "token_provider", "verify_mode", "ca_bundle_path"]
)


def build_verified_gis(args, mode, logger, *, pool_connections=None,
                       pool_maxsize=None, retry_total=2, ca_bundle_path=None,
                       session_kwargs=None):
    """
    Build this script's own direct requests.Session under an explicit TLS
    verification strategy, prove it against args.portal_url with a real
    one-time TLS handshake, then connect_gis() with the SAME verify_mode, and
    return everything the caller needs. Raises UTILITIES.auth.ScanAbort (with
    an already-redacted message) on any failure, matching the control-flow
    every calling script already implements for its own local ScanAbort.

    This consolidates the "build secure session -> verify TLS handshake ->
    connect_gis()" sequence duplicated, nearly verbatim, near the top of
    main()/_run() in essentially every hardened scanner in this project
    (SCAN_ATTACHMENTS_SIZE.py, DEPRECATED_CONTENT.py, DUPLICATE_LAYERS.py,
    FIELD_NAME_SCAN.py, GROUPS_SCAN.py, ITEM_INVENTORY.py,
    SCAN_BROKEN_HOSTED_SERVICES.py, SCAN_WEB_MAPS.py,
    PORTAL_SHARED_CREDENTIALS_SCAN.py, EMPTY_FEATURE_SERVICES.py, and more).

    Parameters
    ----------
    args : argparse.Namespace
        Must already have portal_url, username, keyring_service set (i.e.
        finalize_config() has already run). args.target is used to build the
        TokenProvider via UTILITIES.auth.provider_for_gis(), matched against
        get_platform(args.portal_url) the same way every existing script
        does it (platform is NOT re-derived here -- callers that need the
        platform label should call UTILITIES.restutils.get_platform()
        themselves and pass/compare it, exactly as before; this function
        only needs it to build the right TokenProvider subclass).
    mode : UTILITIES.secure_session.VerifyMode
        The TLS verification strategy, typically resolved from
        UTILITIES.secure_session._TLS_MODE_CHOICES[args.tls_mode].
    logger : logging.Logger
        Used for the usual "TLS verification mode: ..." / "Token acquired
        via ..." informational lines every script already logs.
    pool_connections, pool_maxsize : int, optional
        Passed through to build_secure_session(). If omitted, that
        function's own defaults are used (64/128).
    retry_total : int, optional
        Passed through to build_secure_session(). Default 2, matching most
        existing call sites.
    ca_bundle_path : str, optional
        Required when mode is VerifyMode.CUSTOM_CA_BUNDLE; must be omitted
        otherwise. Passed through to both build_secure_session() and
        connect_gis() so the direct REST session and the arcgis SDK
        connection trust the exact same CA.
    session_kwargs : dict, optional
        Any additional keyword arguments to forward to build_secure_session()
        (e.g. a script-specific pool shape not covered by pool_connections/
        pool_maxsize/retry_total above).

    Returns
    -------
    BuiltGis
        Namedtuple of (session, gis, token_provider, verify_mode,
        ca_bundle_path). `session` is the verified direct requests.Session
        the caller should use for its own REST calls (and should .close()
        when done, or wrap in a WorkerSessionPool); `gis` is the connected
        arcgis.gis.GIS object; `token_provider` is already built via
        provider_for_gis() for args.target.

    Raises
    ------
    UTILITIES.auth.ScanAbort
        If the TLS strategy could not be established, the handshake check
        failed, or connect_gis() itself failed. The message is already
        passed through redact_sensitive_data(), matching every existing
        script's own abort-path handling.
    """
    if not isinstance(mode, VerifyMode):
        raise TypeError(
            f"mode must be a UTILITIES.secure_session.VerifyMode member, "
            f"got {mode!r}."
        )
    if mode is VerifyMode.CUSTOM_CA_BUNDLE and not ca_bundle_path:
        raise ScanAbort(
            "mode=CUSTOM_CA_BUNDLE requires ca_bundle_path (path to a PEM "
            "CA bundle file)."
        )
    if mode is not VerifyMode.CUSTOM_CA_BUNDLE and ca_bundle_path:
        raise ScanAbort(
            f"ca_bundle_path was supplied but mode is {mode!r}, not "
            f"CUSTOM_CA_BUNDLE. Pass mode=VerifyMode.CUSTOM_CA_BUNDLE "
            f"explicitly when supplying a CA bundle path."
        )

    build_kwargs = dict(session_kwargs or {})
    if pool_connections is not None:
        build_kwargs.setdefault("pool_connections", pool_connections)
    if pool_maxsize is not None:
        build_kwargs.setdefault("pool_maxsize", pool_maxsize)
    build_kwargs.setdefault("retry_total", retry_total)

    try:
        session = build_secure_session(
            mode, ca_bundle_path=ca_bundle_path, logger=logger, **build_kwargs
        )
    except Exception as exc:
        raise ScanAbort(
            f"Could not establish TLS verification strategy {mode!r}: {exc}"
        ) from exc

    try:
        handshake_ok, handshake_detail = verify_tls_handshake(
            args.portal_url, session, logger=logger
        )
    except Exception as exc:
        try:
            session.close()
        except Exception:
            pass
        raise ScanAbort(
            f"TLS handshake verification setup for {args.portal_url} "
            f"raised an error: {exc}"
        ) from exc

    if not handshake_ok:
        try:
            session.close()
        except Exception:
            pass
        raise ScanAbort(
            f"TLS handshake verification to {args.portal_url} failed: "
            f"{handshake_detail.get('error')}"
        )

    try:
        gis = connect_gis(
            args.portal_url,
            args.username,
            args.keyring_service,
            logger,
            verify_mode=mode,
            ca_bundle_path=ca_bundle_path,
        )
    except Exception as exc:
        try:
            session.close()
        except Exception:
            pass
        raise ScanAbort(f"Could not connect to the target GIS: {exc}") from exc

    from .restutils import get_platform  # local import avoids a cycle at module load

    platform = get_platform(args.portal_url)
    token_provider = provider_for_gis(gis, platform)
    logger.info(
        "Token acquired via %s (token present: %s).",
        type(token_provider).__name__, token_provider.is_present(),
    )

    return BuiltGis(
        session=session,
        gis=gis,
        token_provider=token_provider,
        verify_mode=mode,
        ca_bundle_path=ca_bundle_path,
    )