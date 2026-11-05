"""
UTILITIES/datastore_config.py
==============================================================================
Helper for the Data Store / hosting-server config keys that live in the
[portal] section of an org's config toml but are NOT part of the shared
CONFIG_KEYS (so cli.finalize_config does not touch them).

These keys are read directly from the chosen --target section (falling back to
[common], then to the caller-supplied default), exactly as documented in
config.toml:

    hosting-url        Hosting GIS Server admin URL.
    db-host            Data Store host.
    db-port            Data Store port (default 9876).
    dbname             Data Store database name.
    db-user            DB admin to CONNECT AS.
    keyring-service-db Keyring service holding the DB password (user = db-user).
    arcgisinput        Hosting server's arcgisinput folder.
    db-prefix          Optional; defaults to dbname.

*** MULTI-ORG BUGFIX (this revision) ***
-------------------------------------------------------------------------
config.load_config() REQUIRES a `path` argument -- there is no default/
implicit config file anymore in the multi-org setup (see config.py). The
PREVIOUS version of this module called `_config.load_config()` with NO
arguments, which raised:

    TypeError: load_config() missing 1 required positional argument: 'path'

FIX: get_datastore_settings() now takes a REQUIRED `config_path` positional
parameter (the org toml path already resolved onto args.config by
cli.finalize_config()) and passes it through to load_config(). `logger` is
now KEYWORD-ONLY (note the bare `*` before it in the signature below) so that
a stale caller passing the old 3-argument call shape
(`get_datastore_settings(args, target, logger=logger)`) fails LOUDLY and
immediately with a clear "missing required positional argument: config_path"
error, instead of silently binding `config_path`'s old slot to something
unexpected and producing a confusing "multiple values for argument 'logger'"
error.

Every caller must now pass the org config path explicitly, e.g.:

    settings = get_datastore_settings(args, args.target, args.config,
                                      logger=logger)

This matches the pattern already used elsewhere in this project (e.g.
cli.get_max_email_mb(target, config_path, default=None)), so the whole
project is now consistent about explicitly threading the chosen org's config
path through every config-reading helper, rather than any module having its
own implicit/default file.

Public API
----------
    get_datastore_settings(args, target, config_path, *, logger=None) -> dict
        Resolve every Data Store key with precedence
        CLI arg -> target section -> [common] -> default.

    add_datastore_args(parser)
        Add the matching OPTIONAL command-line overrides.

USAGE EXAMPLE
--------------
Wire the overrides into an argparse parser, then resolve after --target is
known (typically right after cli.finalize_config()):

    import argparse
    from UTILITIES.cli import add_target_arg, add_config_arg, finalize_config
    from UTILITIES.datastore_config import (
        add_datastore_args, get_datastore_settings, missing_datastore_keys,
    )

    parser = argparse.ArgumentParser()
    add_config_arg(parser)            # required --config org_toml_path
    add_target_arg(parser)            # required --target portal|agol
    add_datastore_args(parser)        # optional --db-host/--db-port/...
    args = parser.parse_args()

    # e.g. command line:
    #   python SCRIPT.py --config ORG.config.toml --target portal
    #   python SCRIPT.py --config ORG.config.toml --target portal --db-host db.example.com --db-port 9876
    finalize_config(args, required=("output-dir",))   # fills shared keys

    settings = get_datastore_settings(args, args.target, args.config,
                                      logger=logger)
    # -> {'db-host': 'datatore_fqdn', 'db-port': 9876,
    #     'dbname': 'hosting_datastore_db_name', 'db-user': 'hosting_datastore_db_admin',
    #     'keyring-service-db': 'DATASTORE'}
    #  (values pulled from the [portal] section of the CHOSEN org's toml,
    #   unless a --db-* flag overrode one)

    missing = missing_datastore_keys(settings)
    if missing:
        logger.error("Missing DB settings: %s", ", ".join(missing))
        raise SystemExit(2)

Resolution precedence for every key (highest wins):
    1. Command-line flag   (--db-host db.example.com)
    2. --target section    ([portal] db-host = "...", in the chosen org toml)
    3. [common] section    (of the chosen org toml)
    4. Built-in default    (db-port -> 9876; others -> None)
==============================================================================
"""

from . import config as _config


# key -> default value when absent everywhere.
_DATASTORE_DEFAULTS = {
    "db-host": None,
    "db-port": 9876,
    "dbname": None,
    "db-user": None,
    "keyring-service-db": None,
}


def add_datastore_args(parser):
    """
    Add OPTIONAL Data Store connection overrides. Any value omitted on the
    command line is filled from the [target]/[common] section of the chosen
    org's config toml by get_datastore_settings().
    """
    group = parser.add_argument_group("Data Store connection (config fallback)")
    group.add_argument(
        "--db-host", default=None,
        help="Data Store host. If omitted, taken from the --config org toml "
             "(the --target section's 'db-host').",
    )
    group.add_argument(
        "--db-port", default=None, type=int,
        help="Data Store port (default 9876). If omitted, taken from the "
             "--config org toml ('db-port').",
    )
    group.add_argument(
        "--dbname", default=None,
        help="Data Store database name. If omitted, taken from the --config "
             "org toml ('dbname').",
    )
    group.add_argument(
        "--db-user", default=None,
        help="DB admin to connect as (superuser / pg_monitor). If omitted, "
             "taken from the --config org toml ('db-user').",
    )
    group.add_argument(
        "--keyring-service-db", default=None,
        help="Keyring service holding the DB password (username = db-user). "
             "If omitted, taken from the --config org toml "
             "('keyring-service-db').",
    )


def _resolve_one(args, key, target, cfg, default):
    """CLI arg -> target section -> [common] -> default."""
    attr = key.replace("-", "_")
    cli_val = getattr(args, attr, None)
    if _config._nonblank(cli_val):
        return cli_val
    val = _config.get_config_value(key, target, cfg=cfg)
    if _config._nonblank(val):
        return val
    return default


def get_datastore_settings(args, target, config_path, *, logger=None):
    """
    Resolve all Data Store connection keys for the given target, from the
    chosen org's config toml.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed arguments; any of --db-host/--db-port/--dbname/--db-user/
        --keyring-service-db present here override the config file.
    target : str
        "portal" or "agol" (normalized internally).
    config_path : str
        *** REQUIRED ***. Path to the org's config toml (normally
        args.config, the same path already resolved by
        cli.finalize_config()). There is no default/implicit config file in
        the multi-org setup -- config.load_config() raises ValueError if this
        is empty/None.
    logger : logging.Logger, optional, KEYWORD-ONLY
        If given, the resolved settings are debug-logged. Keyword-only so a
        stale/old-style call passing logger positionally where config_path
        now belongs fails immediately and clearly, instead of silently
        misassigning arguments.

    Returns
    -------
    dict
        Keys: db-host, db-port, dbname, db-user, keyring-service-db. Missing
        required values are returned as None so the caller can log/abort with
        a precise message.

    Example
    -------
        settings = get_datastore_settings(args, "portal", args.config,
                                          logger=log)
        # settings["db-host"]  -> "datatore_fqdn"
        # settings["db-port"]  -> 9876   (int, even if config had a string)
        # settings["dbname"]   -> "hosting_datastore_db_name"
        # A CLI "--db-host other.com" would replace just db-host above.
    """
    target = _config.normalize_target(target)
    cfg = _config.load_config(config_path)

    settings = {}
    for key, default in _DATASTORE_DEFAULTS.items():
        settings[key] = _resolve_one(args, key, target, cfg, default)

    # Normalize port to int if it came through as a string.
    if settings["db-port"] is not None:
        try:
            settings["db-port"] = int(settings["db-port"])
        except (TypeError, ValueError):
            if logger is not None:
                logger.warning(
                    "Invalid db-port %r; falling back to default 9876.",
                    settings["db-port"])
            settings["db-port"] = 9876

    if logger is not None:
        logger.debug(
            "Resolved Data Store settings (from %s): host=%s port=%s "
            "dbname=%s user=%s keyring-service-db=%s",
            config_path, settings["db-host"], settings["db-port"],
            settings["dbname"], settings["db-user"],
            settings["keyring-service-db"],
        )
    return settings


def missing_datastore_keys(settings, required=("db-host", "dbname", "db-user",
                                               "keyring-service-db")):
    """
    Return the list of required Data Store keys still missing a value.

    Example
    -------
        settings = get_datastore_settings(args, "portal", args.config)
        missing = missing_datastore_keys(settings)
        if missing:
            # e.g. missing == ["dbname"]  ->  abort with a clear message
            raise SystemExit("Missing: " + ", ".join(missing))
    """
    return [k for k in required if not _config._nonblank(settings.get(k))]