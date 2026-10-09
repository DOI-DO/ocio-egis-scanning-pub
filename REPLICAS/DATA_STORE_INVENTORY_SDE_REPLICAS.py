#!/usr/bin/env python3
r"""
DATA_STORE_INVENTORY_SDE_REPLICAS.py

Inventories every feature-service replica in the ArcGIS Data Store's managed
enterprise geodatabase (SDE), via an on-the-fly database connection.
Portal-only. The live PRIMARY Data Store machine is auto-detected on every
run (HA-aware) unless --db-host forces a specific host. Connects to SDE using
the MANAGED SCHEMA OWNER account (db-user-mu), since ListReplicas needs
schema-owner visibility to see replicas at all.

TLS: Data Store discovery calls the hosting server's admin REST API over a
session built by UTILITIES.secure_session.build_secure_session() with an
EXPLICIT --tls-mode (no implicit/default verification). The Portal token
used for that call is wrapped in a UTILITIES.auth.StaticTokenProvider
immediately on acquisition, so it is never exposed to logging/printing by
accident. No token is ever written to the log file, console, email body, or
CSV.

Examples:
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config ORG.config.toml
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config ORG.config.toml --no-test
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config ORG.config.toml --no-test --output-dir "D:\reports"
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org.config.toml --db-host datastore-primary.example.com
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org.config.toml --db-user-mu managed_owner --keyring-service-db-mu DATASTORE_MANAGED_OWNER
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config ORG.config.toml --tls-mode os-trust-store
    python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config ORG.config.toml --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
"""

import argparse
import datetime
import ipaddress
import os
import sys
import tempfile
import time
from urllib.parse import urlparse


def _locate_utilities_on_path():
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while True:
        candidate = os.path.join(cur, "UTILITIES", "__init__.py")
        if os.path.isfile(candidate):
            if cur not in sys.path:
                sys.path.insert(0, cur)
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


_locate_utilities_on_path()

import arcpy
import keyring

from UTILITIES import (
    build_timestamp,
    setup_logging,
    send_email,
    start_timer,
    timing_block,
    ensure_output_dir,
    gather_attachments,
    write_dict_csv,
    add_common_portal_args,
    add_email_args,
    add_output_dir_arg,
    add_test_flag,
    add_config_arg,
    subject_line,
    finalize_config,
    get_max_email_mb,
)
from UTILITIES import config as _config
from UTILITIES.datastore_config import (
    add_datastore_args,
    get_datastore_settings,
    missing_datastore_keys,
)
from UTILITIES.stack_discovery import (
    discover_data_store,
    short_hostname,
    looks_like_fqdn,
)
from UTILITIES.secure_session import (
    VerifyMode,
    build_secure_session,
    verify_tls_handshake,
)
from UTILITIES.auth import StaticTokenProvider

CONFIG_TARGET = "portal"
PLATFORM_LABEL = "PORTAL"

STAGE_DATASTORE = "DATASTORE_DISCOVERY"
STAGE_PORTAL_TOKEN = "PORTAL_TOKEN"

TEST_LIMIT = 50

DATE_FMT = "%Y/%m/%d"

TYPE_MAP = {0: "checkout", 1: "one-way", 2: "two-way",
            3: "one-way-child-to-parent"}
ROLE_MAP = {0: "none", 1: "parent", 2: "child"}
REL_MAP = {0: "in-x", 1: "intersects", 2: "undefined"}

EXTRA_KEYS = ["guid", "created", "lastReceive", "lastSend"]

CSV_HEADER = [
    "platform",
    "name", "ID", "type", "role", "service_owner", "version", "isSender",
    "datasets", "hasConflicts", "replica_id", "service_url",
    "spatialRelation", "unRegisterFlag", "useArchiving", "useGeometry",
    "guid", "date_created", "lastReceive", "lastSend",
]

_TLS_MODE_MAP = {
    "standard-ca": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


class StepError(Exception):
    def __init__(self, stage, msg):
        super().__init__(msg)
        self.stage = stage


def get_db_password(keyring_service_db, db_user, logger):
    logger.info(
        "Retrieving DB password from keyring service '%s' for user '%s'.",
        keyring_service_db, db_user)
    password = (keyring.get_password(keyring_service_db, db_user)
                or os.getenv("DATASTORE_PASSWORD"))
    if not password:
        raise RuntimeError(
            f"No DB password found in keyring for service "
            f"'{keyring_service_db}' / user '{db_user}', and the "
            f"DATASTORE_PASSWORD environment variable is not set. Store it "
            f"with: python -c \"import keyring; keyring.set_password("
            f"'{keyring_service_db}','{db_user}','<pw>')\""
        )
    return password


def get_keyring_password(service, user, env_fallback, label, logger):
    password = None
    try:
        logger.info(
            "Retrieving %s password from keyring service '%s' for user "
            "'%s'.", label, service, user)
        password = keyring.get_password(service, user)
    except Exception as exc:
        logger.warning("keyring lookup for %s failed (%s); will try the "
                       "environment variable %s.", label, exc, env_fallback)

    if not password:
        password = os.getenv(env_fallback)
        if password:
            logger.info("Using %s password from environment variable %s.",
                        label, env_fallback)

    if not password:
        raise StepError(
            STAGE_PORTAL_TOKEN,
            f"No {label} password found. Store it in keyring (service "
            f"'{service}', username '{user}') or set the {env_fallback} "
            f"environment variable.")
    return password


def get_portal_token_provider(portal_url, user, password, session, referer,
                              logger):
    url = f"{portal_url.rstrip('/')}/sharing/rest/generateToken"
    try:
        r = session.post(url, data={"username": user, "password": password,
                                    "client": "referer", "referer": referer,
                                    "expiration": 120, "f": "json"},
                         timeout=30)
        r.raise_for_status()
    except Exception as exc:
        raise StepError(STAGE_PORTAL_TOKEN,
                        f"Token request to {url} failed: {exc}")

    try:
        j = r.json()
    except ValueError:
        raise StepError(STAGE_PORTAL_TOKEN,
                        f"Token endpoint {url} returned non-JSON (HTTP "
                        f"{r.status_code}). Is the portal URL correct?")
    if "token" not in j:
        raise StepError(STAGE_PORTAL_TOKEN,
                        f"Token request failed (bad credentials / referer?): "
                        f"{j.get('error') or j}")

    provider = StaticTokenProvider(j["token"], label="portal-discovery")
    logger.info("Token acquired via StaticTokenProvider (token present: %s).",
               provider.is_present())
    return provider


def _domain_suffix_from_host(host_value):
    if not _config._nonblank(host_value):
        return ""

    raw = str(host_value).strip()
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        host = (parsed.hostname or "").strip().rstrip(".")
    except Exception:
        host = ""

    if not host:
        host = raw.rstrip(".")

    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass

    if "." not in host:
        return ""
    return host.split(".", 1)[1]


def resolve_runtime_domain_suffix(args):
    if _config._nonblank(args.domain_suffix):
        return (str(args.domain_suffix).strip().strip("."),
                "explicit --domain-suffix override")

    configured = _config.get_config_value(
        "domain-suffix", args.target, path=args.config)
    if _config._nonblank(configured):
        return (str(configured).strip().strip("."),
                "runtime config 'domain-suffix'")

    configured_db_host = _config.get_config_value(
        "db-host", args.target, path=args.config)
    derived = _domain_suffix_from_host(configured_db_host)
    if derived:
        return derived, "derived from runtime config 'db-host'"

    return "", ("not configured and not derivable from runtime config "
                "'db-host'")


def resolve_db_host_fqdn(machine_name, domain_suffix, logger=None):
    if not machine_name:
        return machine_name

    if looks_like_fqdn(machine_name):
        if logger is not None:
            logger.info(
                "Discovered machine name '%s' already looks like a full FQDN; "
                "using it as-is.", machine_name)
        return machine_name

    short = short_hostname(machine_name)
    if not domain_suffix:
        if logger is not None:
            logger.warning(
                "Discovered machine name '%s' has no domain, and no domain "
                "suffix was supplied by --domain-suffix or the runtime config "
                "TOML (nor derivable from its 'db-host'). Using short "
                "hostname '%s' unchanged.", machine_name, short)
        return short

    fqdn = f"{short}.{domain_suffix}"
    if logger is not None:
        logger.info(
            "Discovered machine name '%s' has no domain; built FQDN '%s' "
            "using runtime-resolved domain suffix '%s'.", machine_name, fqdn,
            domain_suffix)
    return fqdn


def resolve_live_db_host(args, hosting_url, logger):
    if args.db_host:
        logger.warning(
            "--db-host was explicitly given ('%s'); SKIPPING live primary "
            "auto-detection entirely. This bypasses HA-awareness.",
            args.db_host)
        return args.db_host, "explicit --db-host override"

    if not hosting_url:
        raise StepError(
            STAGE_DATASTORE,
            "No hosting server admin URL is available -- cannot discover "
            "the live Data Store topology. Provide --hosting-url, or set "
            "'hosting-url' in the [portal]/[common] section of the chosen "
            "--config org toml, or pass --db-host explicitly.")

    tls_mode = _TLS_MODE_MAP[args.tls_mode]
    if tls_mode is VerifyMode.CUSTOM_CA_BUNDLE and not args.ca_bundle_path:
        raise StepError(
            STAGE_DATASTORE,
            "--tls-mode custom-ca-bundle requires --ca-bundle-path.")

    session = build_secure_session(
        tls_mode, ca_bundle_path=args.ca_bundle_path, logger=logger)

    ok, _detail = verify_tls_handshake(args.portal_url, session,
                                       logger=logger)
    if not ok:
        raise StepError(
            STAGE_DATASTORE,
            f"TLS handshake/verification to {args.portal_url} failed. See "
            "the log above for details. Refusing to proceed with an "
            "unverified connection.")

    p = urlparse(args.portal_url)
    referer = f"{p.scheme}://{p.netloc}"

    try:
        portal_password = get_keyring_password(
            args.keyring_service, args.username,
            "PORTAL_PASSWORD", "portal", logger)
        token_provider = get_portal_token_provider(
            args.portal_url, args.username, portal_password, session,
            referer, logger)
    except StepError:
        raise
    except Exception as exc:
        raise StepError(
            STAGE_PORTAL_TOKEN,
            f"Unexpected error acquiring a Portal token for Data Store "
            f"discovery: {exc}")

    ds = discover_data_store(session, hosting_url, token_provider.get(),
                             logger=logger)

    if ds.get("error"):
        raise StepError(
            STAGE_DATASTORE,
            "Could not discover the Data Store topology from the hosting "
            f"server admin API at {hosting_url}. Underlying error: "
            f"{ds['error']}\n\nPass --db-host explicitly to bypass "
            "discovery if you need to force a specific machine.")

    if not ds.get("items"):
        raise StepError(
            STAGE_DATASTORE,
            "The hosting server's admin API reported NO registered "
            f"enterpriseDatabases item(s) at {hosting_url}"
            "/admin/data/findItems. Verify the Data Store is registered "
            "with this hosting server, or pass --db-host explicitly.")

    if ds.get("ambiguous") or not ds.get("host"):
        ambiguous_items = [it for it in ds["items"] if it.get("ambiguous")]
        detail_lines = []
        for it in ds["items"]:
            detail_lines.append(
                f"    - item '{it['name']}': machines={it['all_machines']} "
                f"primary={it['primary']} standby={it['standby']} "
                f"ambiguous={it['ambiguous']}")
        raise StepError(
            STAGE_DATASTORE,
            "Could not confidently determine the LIVE PRIMARY Data Store "
            "machine. "
            + (f"{len(ambiguous_items)} registered item(s) reported "
               "multiple machines with no 'PRIMARY' role information. "
               if ambiguous_items else
               "No item resolved a primary machine at all. ")
            + "Refusing to guess.\n\nDiscovered Data Store item(s):\n"
            + "\n".join(detail_lines)
            + "\n\nIf you know exactly which machine is live, pass "
            "--db-host explicitly to bypass auto-detection.")

    db_host_machine = ds["host"]
    logger.info(
        "Live PRIMARY Data Store machine resolved: '%s' (from %d "
        "registered enterpriseDatabases item(s)).",
        db_host_machine, len(ds["items"]))
    for it in ds["items"]:
        logger.info(
            "  item '%s': all_machines=%s primary=%s standby=%s",
            it["name"], it["all_machines"], it["primary"], it["standby"])

    db_host = resolve_db_host_fqdn(db_host_machine, args.domain_suffix,
                                   logger=logger)
    return db_host, "auto-detected LIVE PRIMARY (HA-aware)"


def create_temp_sde(settings, password, logger):
    db_host = settings["db-host"]
    db_port = settings["db-port"]
    dbname = settings["dbname"]
    db_user = settings["db-user"]

    instance = f"{db_host},{db_port}" if db_port else db_host

    tmp_dir = tempfile.mkdtemp(prefix="ds_sde_")
    conn_name = "datastore_onthefly.sde"
    sde_path = os.path.join(tmp_dir, conn_name)

    logger.info(
        "Creating on-the-fly SDE connection: instance='%s' database='%s' "
        "user='%s' -> %s",
        instance, dbname, db_user, sde_path)

    try:
        arcpy.management.CreateDatabaseConnection(
            out_folder_path=tmp_dir,
            out_name=conn_name,
            database_platform="POSTGRESQL",
            instance=instance,
            account_authentication="DATABASE_AUTH",
            username=db_user,
            password=password,
            save_user_pass="SAVE_USERNAME",
            database=dbname,
        )
    except arcpy.ExecuteError:
        logger.error("arcpy.ExecuteError while creating the SDE connection.")
        logger.error("ArcPy messages:\n%s", arcpy.GetMessages(2))
        logger.error(
            "Connection parameters were: platform=POSTGRESQL instance='%s' "
            "database='%s' user='%s' (auth=DATABASE_AUTH).",
            instance, dbname, db_user)
        _safe_cleanup(tmp_dir, logger)
        raise
    except Exception as exc:
        logger.error("Unexpected error creating the SDE connection: %s", exc)
        logger.error("ArcPy messages (if any):\n%s", arcpy.GetMessages(2))
        _safe_cleanup(tmp_dir, logger)
        raise

    if not os.path.exists(sde_path):
        logger.error(
            "CreateDatabaseConnection reported success but no .sde file was "
            "produced at %s. ArcPy messages:\n%s",
            sde_path, arcpy.GetMessages())
        _safe_cleanup(tmp_dir, logger)
        raise RuntimeError("SDE connection file was not created.")

    logger.info("Validating the new SDE connection with arcpy.Describe ...")
    describe_seconds = 0.0
    describe_t0 = time.perf_counter()
    try:
        desc = arcpy.Describe(sde_path)
        describe_seconds = time.perf_counter() - describe_t0
        wt = getattr(desc, "workspaceType", "unknown")
        logger.info("SDE connection OK (workspaceType=%s).", wt)
        logger.info("Describe Data Store connection took %.2f s.",
                    describe_seconds)
    except arcpy.ExecuteError:
        describe_seconds = time.perf_counter() - describe_t0
        logger.error("SDE connection created but failed validation "
                     "(after %.2f s).", describe_seconds)
        logger.error("ArcPy messages:\n%s", arcpy.GetMessages(2))
        _safe_cleanup(tmp_dir, logger)
        raise
    except Exception as exc:
        describe_seconds = time.perf_counter() - describe_t0
        logger.error(
            "SDE connection created but could not be described after %.2f s "
            "(likely a bad password, blocked port %s, wrong dbname '%s', or "
            "the user '%s' lacks access): %s",
            describe_seconds, db_port, dbname, db_user, exc)
        _safe_cleanup(tmp_dir, logger)
        raise

    return sde_path, tmp_dir, describe_seconds


def _safe_cleanup(path, logger):
    if not path:
        return
    try:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path, topdown=False):
                for fn in files:
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        pass
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        elif os.path.exists(path):
            os.remove(path)
        logger.debug("Cleaned up temporary path: %s", path)
    except Exception as exc:
        logger.warning("Could not fully clean up temp path %s: %s", path, exc)


def _format_date(value, fmt=DATE_FMT):
    if value is None:
        return ""
    if isinstance(value, (datetime.datetime, datetime.date)):
        try:
            return value.strftime(fmt)
        except Exception:
            return str(value)

    epoch = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if s == "":
            return ""
        try:
            epoch = float(s)
        except ValueError:
            return s

    if epoch is not None:
        try:
            if abs(epoch) >= 1e11:
                dt = datetime.datetime.utcfromtimestamp(epoch / 1000.0)
            elif abs(epoch) >= 1e9:
                dt = datetime.datetime.utcfromtimestamp(epoch)
            else:
                return str(value).strip()
            return dt.strftime(fmt)
        except Exception:
            return str(value).strip()

    return str(value).strip()


def load_replica_details(sde_path, logger):
    extra_by_name = {}
    candidate_queries = [
        "SELECT NAME, REPLICA_GUID, CREATION_DATE, LAST_RECEIVE, LAST_SEND "
        "FROM SDE.GDB_REPLICAS",
        "SELECT NAME, REPLICAGUID, CREATIONDATE, LASTRECEIVEDATE, LASTSENDDATE "
        "FROM SDE.GDB_REPLICAS",
        "SELECT NAME, REPLICA_GUID, CREATION_DATE, LAST_RECEIVE, LAST_SEND "
        "FROM GDB_REPLICAS",
    ]

    try:
        sde = arcpy.ArcSDESQLExecute(sde_path)
    except Exception as exc:
        logger.warning(
            "Could not open ArcSDESQLExecute on the connection (%s); replica "
            "detail columns will be blank.", exc)
        return extra_by_name

    rows = None
    used_q = None
    for q in candidate_queries:
        try:
            rows = sde.execute(q)
            used_q = q
            logger.info("Replica detail query succeeded:\n  %s", q)
            break
        except Exception:
            rows = None
            continue

    if not rows or rows is True:
        logger.warning(
            "No replica detail rows returned from the system table "
            "(tried %d query layouts); extra columns will be blank.",
            len(candidate_queries))
        return extra_by_name

    if not isinstance(rows[0], (list, tuple)):
        rows = [rows]

    for r in rows:
        name = r[0]
        extra_by_name[name] = {
            "guid": r[1],
            "created": r[2],
            "lastReceive": r[3],
            "lastSend": r[4],
        }
    logger.info("Loaded detail rows for %d replica(s) via:\n  %s",
                len(extra_by_name), used_q)
    return extra_by_name


def _code(m, v):
    return m.get(v, v)


def _get_created_date(rep, extra):
    cd = getattr(rep, "creationDate", None)
    if cd:
        return cd
    return extra.get("created", "")


def build_row(rep, extra_by_name, platform):
    name = getattr(rep, "name", "")
    extra = extra_by_name.get(name, {})
    return {
        "platform": platform,
        "name": name,
        "ID": getattr(rep, "ID", ""),
        "type": _code(TYPE_MAP, getattr(rep, "type", "")),
        "role": _code(ROLE_MAP, getattr(rep, "role", "")),
        "service_owner": getattr(rep, "owner", ""),
        "version": getattr(rep, "version", ""),
        "isSender": getattr(rep, "isSender", ""),
        "datasets": ";".join(str(d) for d in getattr(rep, "datasets", []) or []),
        "hasConflicts": getattr(rep, "hasConflicts", ""),
        "replica_id": getattr(rep, "replicaID", ""),
        "service_url": getattr(rep, "serviceName", ""),
        "spatialRelation": _code(REL_MAP, getattr(rep, "spatialRelation", "")),
        "unRegisterFlag": getattr(rep, "unRegisterFlag", ""),
        "useArchiving": getattr(rep, "useArchiving", ""),
        "useGeometry": getattr(rep, "useGeometry", ""),
        "guid": extra.get("guid", ""),
        "date_created": _format_date(_get_created_date(rep, extra)),
        "lastReceive": _format_date(extra.get("lastReceive", "")),
        "lastSend": _format_date(extra.get("lastSend", "")),
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inventory ArcGIS Data Store feature-service replicas "
                    "from the managed SDE via an on-the-fly database "
                    "connection. Portal-only. Live primary auto-detected "
                    "(HA-aware) unless --db-host is given.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_config_arg(parser)
    add_common_portal_args(parser)
    add_datastore_args(parser)
    add_output_dir_arg(parser)
    add_email_args(parser)
    add_test_flag(
        parser,
        help_text="Scan ALL replicas. Omit to run in test mode "
                  f"(first {TEST_LIMIT} replicas).",
    )

    grp_disc = parser.add_argument_group(
        "Data Store primary auto-detection (HA-aware)")
    grp_disc.add_argument(
        "--hosting-url", default=None,
        help="Hosting GIS Server admin URL, used ONLY to discover the live "
             "Data Store topology. If omitted, taken from the --config org "
             "toml.")
    grp_disc.add_argument(
        "--domain-suffix", default=None,
        help="Optional command-line override for the domain suffix appended "
             "to a discovered SHORT Data Store machine name. If omitted, "
             "the value is read from 'domain-suffix' in the runtime --config "
             "TOML; when that key is absent, it is derived from that TOML's "
             "FQDN-valued 'db-host'. No organization-specific suffix is "
             "embedded in this script.")
    grp_disc.add_argument(
        "--tls-mode", choices=list(_TLS_MODE_MAP.keys()),
        default="standard-ca",
        help="Explicit TLS verification strategy for the Portal/hosting-"
             "server discovery calls: 'standard-ca' (public CA, correct "
             "for AGOL/public-CA Portal), 'os-trust-store' (verify against "
             "the OS certificate store; requires the 'truststore' "
             "package), or 'custom-ca-bundle' (requires --ca-bundle-path).")
    grp_disc.add_argument(
        "--ca-bundle-path", default=None,
        help="Path to a PEM CA bundle. Required when --tls-mode is "
             "custom-ca-bundle.")

    grp_mu = parser.add_argument_group(
        "Managed schema owner (required to see replicas)")
    grp_mu.add_argument(
        "--db-user-mu", default=None,
        help="Managed-schema-owner DB user to connect as for the SDE/"
             "ListReplicas connection. If omitted, taken from the --config "
             "org toml ('db-user-mu'). Falls back to the admin/monitor "
             "'db-user' account (with a warning) if unset anywhere.")
    grp_mu.add_argument(
        "--keyring-service-db-mu", default=None,
        help="Keyring service holding the managed-schema-owner's password "
             "(username = --db-user-mu). If omitted, taken from the "
             "--config org toml ('keyring-service-db-mu').")

    args = parser.parse_args()
    args.target = CONFIG_TARGET
    return args


def main():
    args = parse_args()

    finalize_config(
        args,
        required=("portal-url", "username", "keyring-service",
                 "output-dir", "smtp-server", "email-to", "email-from"),
    )

    _config.apply_to_args(
        args, args.target,
        keys=("db-user-mu", "keyring-service-db-mu"),
        path=args.config,
    )

    args.domain_suffix, domain_suffix_source = (
        resolve_runtime_domain_suffix(args))

    ensure_output_dir(args.output_dir)

    timestamp = build_timestamp()
    platform = PLATFORM_LABEL
    org_prefix = args.org_name_safe

    base = f"{org_prefix}_{platform}_DATA_STORE_SDE_REPLICAS_{timestamp}"
    log_path = os.path.join(args.output_dir, base + ".log")
    csv_path = os.path.join(args.output_dir, base + ".csv")

    logger = setup_logging(log_path, logger_name="sde_replicas")
    start_wall, start_perf = start_timer()

    logger.info("=" * 70)
    logger.info("Data Store SDE Replica Inventory - run started")
    logger.info("=" * 70)
    logger.info("Organization       : %s", args.org_name)
    logger.info("Platform           : %s", platform)
    logger.info("Target             : %s (fixed, PORTAL ONLY)", args.target)
    logger.info("Test mode          : %s", args.test)
    logger.info("Output directory   : %s", args.output_dir)
    logger.info("Config path        : %s", args.config)
    logger.info("DB host override   : %s", args.db_host or
                "(none -- will auto-detect live primary)")
    logger.info("Domain suffix      : %s (source: %s)",
                args.domain_suffix or "(none)", domain_suffix_source)
    logger.info("TLS mode           : %s", args.tls_mode)
    logger.info("=" * 70)

    settings = get_datastore_settings(args, args.target, args.config,
                                      logger=logger)
    mu_user = args.db_user_mu
    mu_keyring = args.keyring_service_db_mu
    if mu_user and mu_keyring:
        logger.info(
            "Using managed-schema-owner account '%s' (keyring service "
            "'%s') for the SDE connection. This is separate from the "
            "'%s' admin/monitor account used elsewhere.",
            mu_user, mu_keyring, settings["db-user"])
        settings["db-user"] = mu_user
        settings["keyring-service-db"] = mu_keyring
    else:
        logger.warning(
            "No '%s'/'%s' setting found in the [%s]/[common] section of "
            "%s -- falling back to the admin/monitor account '%s' for the "
            "SDE connection. If replica counts look unexpectedly low, this "
            "is likely why.",
            "db-user-mu", "keyring-service-db-mu", args.target,
            args.config, settings["db-user"])
    missing = missing_datastore_keys(
        settings, required=("dbname", "db-user", "keyring-service-db"))
    if missing:
        logger.error(
            "Missing required Data Store setting(s) with no value on the "
            "command line or in the [%s]/[common] section of %s: %s",
            args.target, args.config, ", ".join("--" + k for k in missing))
        _finish_email(args, log_path, None, platform, start_wall, start_perf,
                      logger, ok=False,
                      extra_note="Run ABORTED: missing Data Store settings.")
        sys.exit(2)

    hosting_url = args.hosting_url or _config.get_config_value(
        "hosting-url", args.target, path=args.config)

    logger.info("STEP 1/4: Detecting live Data Store topology "
               "(primary/HA-aware) ...")
    try:
        db_host, db_host_source = resolve_live_db_host(args, hosting_url,
                                                        logger)
    except StepError as se:
        logger.error("[%s] %s", se.stage, se)
        _finish_email(
            args, log_path, None, platform, start_wall, start_perf, logger,
            ok=False,
            extra_note=(f"Run ABORTED during {se.stage}:\n{se}\n\n"
                       "This script never silently falls back to a "
                       "possibly-wrong host after a failover. Pass "
                       "--db-host explicitly to bypass discovery if you "
                       "need to force a specific machine."))
        sys.exit(1)

    settings["db-host"] = db_host
    logger.info("Resolved DB connection target: %s@%s:%s/%s (source: %s)",
               settings["db-user"], db_host, settings["db-port"],
               settings["dbname"], db_host_source)

    sde_path = None
    tmp_dir = None
    csv_written = None
    replica_count = 0
    describe_seconds = None
    run_ok = False
    error_note = ""

    try:
        password = get_db_password(
            settings["keyring-service-db"], settings["db-user"], logger)

        logger.info("STEP 2/4: Creating on-the-fly SDE connection ...")
        sde_path, tmp_dir, describe_seconds = create_temp_sde(
            settings, password, logger)
        logger.info("Data Store Describe time: %.2f s", describe_seconds)

        logger.info("STEP 3/4: Listing replicas (including feature-service "
                   "sync) ...")
        replicas = arcpy.da.ListReplicas(sde_path, True)
        total_found = len(replicas)
        logger.info("Total replicas found: %d", total_found)

        if args.test:
            replicas = replicas[:TEST_LIMIT]
            logger.info("TEST MODE: limiting to first %d replica(s). "
                        "Use --no-test for a full run.", len(replicas))

        extra_by_name = load_replica_details(sde_path, logger)

        logger.info("STEP 4/4: Writing inventory CSV ...")
        rows = [build_row(rep, extra_by_name, platform) for rep in replicas]
        replica_count = len(rows)

        csv_written = write_dict_csv(
            rows, csv_path, fieldnames=CSV_HEADER, logger=logger,
            label="replica",
        )
        run_ok = True

    except Exception as exc:
        logger.exception("Run FAILED: %s", exc)
        error_note = f"Run FAILED: {exc}"

    finally:
        _safe_cleanup(tmp_dir, logger)

    max_email_mb = get_max_email_mb(args.target, args.config)
    _finish_email(args, log_path, csv_written, platform, start_wall,
                  start_perf, logger, ok=run_ok, replica_count=replica_count,
                  describe_seconds=describe_seconds, extra_note=error_note,
                  db_host=db_host, db_host_source=db_host_source,
                  settings=settings, max_email_mb=max_email_mb)

    sys.exit(0 if run_ok else 1)


def _finish_email(args, log_path, csv_written, platform, start_wall,
                  start_perf, logger, ok, replica_count=0,
                  describe_seconds=None, extra_note="", db_host=None,
                  db_host_source=None, settings=None, max_email_mb=None):
    csv_paths = [csv_written] if csv_written else []
    attachments, size_note = gather_attachments(
        log_path, csv_paths, args.output_dir, logger, label="CSV",
        cap_mb=max_email_mb)

    status = "SUCCESS" if ok else "FAILURE"
    timing = timing_block(start_wall, start_perf)

    describe_str = (f"{describe_seconds:.2f} s"
                    if describe_seconds is not None else "(not measured)")

    body_lines = [
        f"Data Store SDE Replica Inventory: {status}",
        "",
        f"Organization       : {args.org_name}",
        f"Platform           : {platform}",
        f"Test mode          : {getattr(args, 'test', 'n/a')}",
    ]

    if db_host:
        dbname = (settings or {}).get("dbname", "")
        db_user = (settings or {}).get("db-user", "")
        db_port = (settings or {}).get("db-port", "")
        body_lines.extend([
            f"Data Store         : {db_user}@{db_host}:{db_port}/{dbname}",
            f"DB host source     : {db_host_source or '(unknown)'}",
            f"SDE connect as     : {db_user}  (should be the managed-schema owner, NOT the admin/monitor account, or replicas will undercount/read as zero)",
        ])

    body_lines.extend([
        f"Replica rows       : {replica_count}",
        f"Describe time      : {describe_str}",
        f"CSV                : {csv_written or '(not written)'}",
        "",
        timing,
    ])
    if size_note:
        body_lines.append(size_note)
    if extra_note:
        body_lines.append(extra_note)

    subject = subject_line("Data Store SDE Replica Inventory",
                           build_timestamp(), platform, org_name=args.org_name)
    send_email(
        subject=subject,
        body="\n".join(body_lines),
        attachments=attachments,
        smtp_server=args.smtp_server,
        email_to=args.email_to,
        email_from=args.email_from,
        logger=logger,
    )


if __name__ == "__main__":
    main()