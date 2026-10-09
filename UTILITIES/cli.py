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
"""

from . import config as _config


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