#!/usr/bin/env python3
r"""
UTILITIES/config.py
==============================================================================
Loader for a shared configuration file (an "org config toml").

*** MULTI-ORG CHANGE ***
-------------------------------------------------------------------------
This project now supports MULTIPLE organizations, each with its OWN toml file
(e.g. org1.config.toml, org2.config.toml, ...). There is NO default/implicit
config path anymore -- every script REQUIRES the caller to pass --config
<path-to-org-toml> (wired in cli.py via add_config_arg()/finalize_config()).
This file's load_config()/get_config_value()/etc. all operate on WHICHEVER
toml path they are given; CONFIG_PATH (the old fixed default) has been
REMOVED on purpose so a script can never silently fall back to the wrong
org's settings.

Each org's toml still uses the SAME [portal]/[agol]/[common] section layout as
before. What's new is a single MANDATORY key in [common]:

    org-name = "ORG1"     # short label; prefixes every output file & email

org-name has NO default -- if it is missing/blank, finalize_config() aborts
with a clear message, exactly like a missing smtp-server does today. Use
sanitize_org_name() (below) to turn the raw toml value into a filesystem/
filename-safe token for building output file names; the RAW value (not
sanitized) is used in email subject lines/bodies so it can stay
human-readable there.

PRECEDENCE (unchanged, highest wins):
    1. Command-line argument
    2. chosen target section ([portal] or [agol])
    3. [common] section

Public API
----------
    load_config(path)                   -> dict (raw parsed TOML)  ** path is
                                            now REQUIRED, no default **
    get_config_value(key, target, ...)  -> resolved value for a single key
    resolve(args, key, target, ...)     -> merge CLI namespace value with config
    apply_to_args(args, target, ...)    -> fill unset args from config
    require(args, keys, ...)            -> list of still-missing required keys
    normalize_target(value)            -> "portal" | "agol" (validated)
    sanitize_org_name(raw)              -> filename-safe org label
==============================================================================
"""

import os
import re

# tomllib is stdlib on Python 3.11+; fall back to the tomli backport if needed.
try:
    import tomllib as _toml            # Python 3.11+
    _TOML_MODE = "rb"
except ModuleNotFoundError:            # pragma: no cover
    import tomli as _toml              # pip install tomli (Python <= 3.10)
    _TOML_MODE = "rb"

# The keys the scripts look up in the config. 'org-name' is now MANDATORY
# (see require_org_name() below) and is part of this shared key list so
# apply_to_args() will copy it onto args.org_name automatically whenever a
# script defines that attribute (via add_config_arg(); see cli.py).
CONFIG_KEYS = (
    "org-name",
    "portal-url",
    "username",
    "keyring-service",
    "output-dir",
    "smtp-server",
    "email-to",
    "email-from",
    "max-email-mb",
)

# Cache so repeated lookups within one run don't re-read the file. Keyed by
# the RESOLVED ABSOLUTE PATH of the toml file, so org1/org2 configs (or any
# number of orgs) never collide in the cache.
_CACHE = {}

VALID_TARGETS = ("portal", "agol")

# Characters allowed verbatim in a sanitized org name; everything else becomes
# an underscore. Keeps output filenames Windows/UNC-safe.
_ORG_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def load_config(path):
    """
    Parse and return the raw config dict for the given org toml path.

    *** path is now REQUIRED. *** There is no implicit/default config file --
    every script must be told exactly which org's toml to read (via the
    mandatory --config CLI argument; see cli.add_config_arg()). Passing None
    or an empty string raises ValueError immediately rather than silently
    reading nothing.

    A path that does not exist on disk raises FileNotFoundError with a clear
    message (rather than returning {} as the old single-file version did),
    since a missing org config is almost always a typo the user should fix
    right away, not a "run with no config" situation.
    """
    if not path:
        raise ValueError(
            "A config file path is required (pass --config <org.toml>). "
            "There is no default config file in the multi-org setup."
        )
    abs_path = os.path.abspath(path)
    if abs_path in _CACHE:
        return _CACHE[abs_path]

    if not os.path.isfile(abs_path):
        raise FileNotFoundError(
            f"Config file not found: {abs_path}\n"
            f"Pass --config with the path to your org's toml file, e.g. "
            f"--config org1.config.toml"
        )

    with open(abs_path, _TOML_MODE) as fh:
        cfg = _toml.load(fh)

    _CACHE[abs_path] = cfg
    return cfg


def normalize_target(value):
    """
    Validate and normalize a target label to lowercase "portal" or "agol".

    Raises ValueError on anything else. Scripts pass argparse choices so this
    is mostly a safety net (and lets us accept mixed case).
    """
    if value is None:
        raise ValueError("A target ('portal' or 'agol') is required.")
    tgt = str(value).strip().lower()
    if tgt not in VALID_TARGETS:
        raise ValueError(
            f"Invalid target {value!r}; expected one of {VALID_TARGETS}."
        )
    return tgt


def get_config_value(key, target, cfg=None, path=None):
    """
    Resolve a single config key for the given target using the precedence:
        chosen target section -> [common] -> None.

    `target` is required ("portal" or "agol"). `cfg` (a pre-loaded dict) or
    `path` (an org toml path to load) must be supplied by the caller -- this
    function itself has no default file to fall back to. Returns None (never
    raises for a missing KEY) if the key is absent everywhere or blank.
    """
    cfg = cfg if cfg is not None else load_config(path)
    target = normalize_target(target)

    # 1. Chosen target section.
    section = cfg.get(target, {}) or {}
    if key in section and _nonblank(section[key]):
        return section[key]

    # 2. [common] section.
    common = cfg.get("common", {}) or {}
    if key in common and _nonblank(common[key]):
        return common[key]

    return None


def resolve(args, key, target, cfg=None, path=None):
    """
    Merge a parsed-args value with the config for a single key and target.

    `key` is the config key ("portal-url"); the matching argparse attribute is
    the same name with dashes turned into underscores ("portal_url"). A value
    supplied on the command line always wins; otherwise the config is used.

    Returns the resolved value or None if neither source provides one.
    """
    attr = key.replace("-", "_")
    cli_val = getattr(args, attr, None)
    if _nonblank(cli_val):
        return cli_val
    return get_config_value(key, target, cfg=cfg, path=path)


def apply_to_args(args, target, keys=CONFIG_KEYS, path=None):
    """
    Convenience: for each key, fill the corresponding args attribute from the
    config (for the given target) when the command line did not supply it.
    Mutates and returns `args`.

    Only sets keys that exist as attributes on `args` (so a script that never
    defines --output-dir, say, is unaffected). `path` is the org toml path
    (REQUIRED -- see load_config()); callers normally pass args.config here.
    """
    cfg = load_config(path)
    tgt = normalize_target(target)
    for key in keys:
        attr = key.replace("-", "_")
        if not hasattr(args, attr):
            continue
        if not _nonblank(getattr(args, attr, None)):
            setattr(args, attr, get_config_value(key, tgt, cfg=cfg))
    return args


def require(args, keys, logger=None):
    """
    Ensure every key in `keys` has a value after config resolution. Returns a
    list of the config keys that are still missing (empty list == all present).
    Callers can log/abort on a non-empty result.
    """
    missing = []
    for key in keys:
        attr = key.replace("-", "_")
        if not _nonblank(getattr(args, attr, None)):
            missing.append(key)
    if missing and logger is not None:
        logger.error(
            "Missing required setting(s) with no value on the command line "
            "or in config.toml: %s", ", ".join("--" + k for k in missing)
        )
    return missing


def sanitize_org_name(raw):
    """
    Turn a raw org-name toml value into a filesystem/filename-safe token used
    to prefix output files (e.g. "National Park Service" -> "National_Park_
    Service"). The RAW (unsanitized) value should still be used verbatim in
    email subjects/bodies, where spaces and punctuation are fine to read.

    Rules: strip leading/trailing whitespace, collapse any run of characters
    OTHER than letters/digits/dot/dash/underscore into a single underscore,
    and strip leading/trailing underscores left over from that collapse.
    Returns "" if raw is None/blank (caller should treat that as a hard
    config error -- see require_org_name() in cli.py).
    """
    if not raw or not str(raw).strip():
        return ""
    cleaned = _ORG_NAME_SAFE_RE.sub("_", str(raw).strip())
    return cleaned.strip("_")


def _nonblank(val):
    """True if val is not None and not an empty/whitespace-only string."""
    if val is None:
        return False
    if isinstance(val, str) and val.strip() == "":
        return False
    return True