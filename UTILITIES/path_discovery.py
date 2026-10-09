r"""
UTILITIES/path_discovery.py
==============================================================================
Self-discovery of each machine's REAL install/log directories. There is NO
fallback to hard-coded paths -- if discovery fails for a machine, that
machine's log copy for the affected path is SKIPPED with a loud, specific
error explaining exactly what was tried and what failed, rather than silently
guessing at a path that may not exist on that machine.

WHY TWO SEPARATE PATHS PER PRODUCT
-----------------------------------
Every ArcGIS Enterprise component has two independently-configurable
locations that matter for log collection:

  1. The INSTALL directory (what LOG_DUMP.bat called ".../Program Files/...").
     Fixed at install time and recorded in the Windows Registry on that
     machine.
  2. The DATA / CONFIG-STORE directory, where the actual `logs` folder used
     by the running service lives (what LOG_DUMP.bat called
     "arcgisportal\logs" / "arcgisserver\logs" / "Datastore\logs"). For
     Portal and ArcGIS Server this is exposed directly by that product's own
     admin REST API, which is more reliable than reading it out of a config
     file on disk.

DISCOVERY STRATEGY
-------------------------------------------------------------------------
  * ArcGIS Server (hosting AND every other federated site):
        GET <admin_url>/admin/logs/settings   -> {"logDir": "...", ...}
    The install directory is read from the registry (see below).

  * Portal:
        GET <portal_url>/portaladmin/logs/settings -> {"logDir": "...", ...}
    Same registry-based install-dir lookup as Server.

  * Data Store:
        No public REST endpoint exposes its directories. The install
        directory is read from the registry (see below). The DATA directory
        (where its own "logs" subfolder lives) is read directly from its own
        config file on disk, via the SAME D$ admin share already used for
        the log copy itself:
            <InstallDir>\etc\arcgis-data-store-config.properties
        which contains a line like:
            dir.data=D\:/Datastore
        This is Java .properties escaping -- "\:" is a literal colon, and
        forward slashes are used instead of backslashes. Once unescaped this
        becomes "D:/Datastore", i.e. the Data Store's actual data directory,
        and its "logs" subfolder is what LOG_DUMP.bat called "Datastore\logs".
        This file is read AFTER the install directory has already been
        discovered (via registry), since the config file's path is relative
        to that install directory and cannot be found without it.

  * Registry lookup is done REMOTELY over the network via winreg against
    each machine's short hostname -- the same machines already reached over
    the D$ admin share for the copy itself.

    CONFIRMED key layout (as reported directly from a live deployment):
        HKLM\SOFTWARE\esri\Server<version>       e.g. Server11.5, Server12.1
        HKLM\SOFTWARE\esri\Portal for ArcGIS
        HKLM\SOFTWARE\esri\ArcGIS Data Store
    "Server<version>" is NOT a fixed string -- ArcGIS Server's own subkey
    name changes with every version (Server10.9, Server11.0, Server11.5,
    Server12.1, ...), so it cannot be matched with a fixed candidate string
    the way Portal/Data Store can. Instead, discovery ENUMERATES every
    subkey directly under HKLM\SOFTWARE\esri and matches any subkey whose
    name starts with "Server" (case-insensitive) -- this works regardless of
    which version is installed on a given machine, including versions
    released after this code was written.

    Every registry lookup also tries BOTH the 64-bit and 32-bit registry
    views, via winreg's KEY_WOW64_64KEY / KEY_WOW64_32KEY access flags -- a
    key written by a 32-bit installer process is only visible in the
    WOW6432Node view unless that flag is passed explicitly; the default view
    depends on the BITNESS OF THE CALLING PYTHON PROCESS, not the target
    key, so without trying both views a real key can be missed entirely.

NO FALLBACK -- FAILURES ARE EXPLICIT AND PER-MACHINE
-------------------------------------------------------------------------
If discovery fails for a given machine/path, resolve_log_paths() does NOT
substitute a guessed/hard-coded path. It returns that path as None in the
result dict, and the caller (PORTAL_STACK_LOG_DUMP.py) skips exactly that
one robocopy call, logging a specific, actionable error (which REST/registry
calls were attempted, and why each failed) rather than silently copying
from -- or worse, silently NOT copying from -- a path that was never
actually confirmed correct.

Public API
----------
    discover_server_log_dir(session, admin_url, token, logger=None)
        -> (log_dir | None, error_str | None)

    discover_portal_log_dir(session, portal_url, token, logger=None)
        -> (log_dir | None, error_str | None)

    discover_install_dir(short_hostname, product, logger=None)
        -> (install_dir | None, error_str | None)
        product is one of "server", "portal", "datastore".

    discover_datastore_data_dir(short_hostname, install_dir, logger=None)
        -> (data_dir | None, error_str | None)
        Reads <install_dir>\etc\arcgis-data-store-config.properties over the
        D$ admin share and returns the unescaped 'dir.data' value. Requires
        the install directory to already be known (see
        discover_install_dir()) since the config file's location is
        relative to it.

    resolve_log_paths(category, short_hostname, domain_suffix, session=None,
                      admin_url=None, token=None, logger=None)
        -> dict
        {
          "data_log_path": str | None,
          "data_log_error": str | None,
          "install_log_path": str | None,
          "install_log_error": str | None,
        }
        Top-level convenience: attempts full self-discovery for one machine
        in one category ("Portal", "Hosting", "DataStore", or a federated
        site name treated the same as "Hosting"). NEVER falls back to a
        hard-coded path -- a path that could not be discovered comes back as
        None with its error string explaining why, so the caller can skip
        that one copy and report it clearly rather than guessing.
==============================================================================
"""

import ntpath

from .restutils import get_json, DEFAULT_TIMEOUT

try:
    import winreg
    _WINREG_AVAILABLE = True
except ImportError:  # pragma: no cover - non-Windows dev/test environment
    winreg = None
    _WINREG_AVAILABLE = False


# Confirmed registry layout (from a live deployment):
#     HKLM\SOFTWARE\esri\Server<version>     e.g. Server11.5, Server12.1
#     HKLM\SOFTWARE\esri\Portal for ArcGIS
#     HKLM\SOFTWARE\esri\ArcGIS Data Store
# Portal and Data Store use a fixed subkey name regardless of version.
# ArcGIS Server's subkey name CHANGES with every version, so it cannot be
# matched with a fixed string -- see _find_server_subkey() below, which
# enumerates HKLM\SOFTWARE\esri and matches anything starting with "Server".
_ESRI_ROOT = r"SOFTWARE\esri"
_SERVER_SUBKEY_PREFIX = "server"  # matched case-insensitively

_FIXED_SUBKEYS = {
    "portal": _ESRI_ROOT + r"\Portal for ArcGIS",
    "datastore": _ESRI_ROOT + r"\ArcGIS Data Store",
}

# Value name holding each product's install directory, once its subkey has
# been located.
_INSTALL_DIR_VALUE = "InstallDir"

if _WINREG_AVAILABLE:
    _REGISTRY_VIEWS = [
        ("64-bit", winreg.KEY_WOW64_64KEY),
        ("32-bit", winreg.KEY_WOW64_32KEY),
    ]
else:  # pragma: no cover
    _REGISTRY_VIEWS = []


# ==========================================================================
# REST-based discovery (Portal / Server "data" log directory)
# ==========================================================================
def discover_server_log_dir(session, admin_url, token, logger=None):
    """
    Discover an ArcGIS Server site's (hosting or federated) configured log
    directory via its own admin REST API:
        GET <admin_url>/admin/logs/settings

    CONFIRMED response shape (from a live deployment): the value is nested
    one level under a "settings" object, e.g.
        {"settings": {"logDir": "D:\\arcgisserver\\logs\\", ...}}
    NOT at the top level the way Portal's equivalent endpoint returns it.
    Both shapes are checked (settings.logDir first, then a top-level logDir
    as a defensive fallback in case a different Server version/build returns
    it unwrapped), so this keeps working across variations without treating
    a merely-differently-shaped success as a failure.

    Returns (log_dir, error_str) -- exactly one is meaningful. Never raises.
    Every attempt (success or failure) is logged so a missing result is
    always traceable to a specific cause.
    """
    if not session:
        msg = "no HTTP session was available for this REST call"
        if logger is not None:
            logger.error("  [path-discovery] server log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg
    if not admin_url:
        msg = "no admin URL was available for this site"
        if logger is not None:
            logger.error("  [path-discovery] server log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg
    if not token:
        msg = "no auth token was available for this REST call"
        if logger is not None:
            logger.error("  [path-discovery] server log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg

    url = admin_url.rstrip("/") + "/admin/logs/settings"
    if logger is not None:
        logger.debug("  [path-discovery] GET %s ...", url)
    data, err = get_json(session, url, token, timeout=DEFAULT_TIMEOUT)
    if err or not isinstance(data, dict):
        msg = f"GET {url} failed: {err}"
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    settings = data.get("settings")
    log_dir = None
    if isinstance(settings, dict):
        log_dir = settings.get("logDir")
    if not log_dir:
        log_dir = data.get("logDir")  # defensive fallback, unwrapped shape

    if not log_dir:
        msg = f"GET {url} succeeded but no 'logDir' found (checked settings.logDir and logDir): {data!r}"
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    if logger is not None:
        logger.debug("  [path-discovery] %s reports logDir=%s", admin_url,
                    log_dir)
    return log_dir, None


def discover_portal_log_dir(session, portal_url, token, logger=None):
    """
    Discover Portal's configured log directory via its own admin REST API:
        GET <portal_url>/portaladmin/logs/settings

    Checks both a top-level 'logDir' field (the shape confirmed against a
    live deployment) and a nested 'settings.logDir' field (defensive, in
    case a different Portal version wraps it the way ArcGIS Server's
    equivalent endpoint does -- see discover_server_log_dir()).

    Returns (log_dir, error_str) -- exactly one is meaningful. Never raises.
    """
    if not session:
        msg = "no HTTP session was available for this REST call"
        if logger is not None:
            logger.error("  [path-discovery] portal log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg
    if not portal_url:
        msg = "no Portal URL was available"
        if logger is not None:
            logger.error("  [path-discovery] portal log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg
    if not token:
        msg = "no auth token was available for this REST call"
        if logger is not None:
            logger.error("  [path-discovery] portal log-dir discovery "
                        "skipped: %s.", msg)
        return None, msg

    url = portal_url.rstrip("/") + "/portaladmin/logs/settings"
    if logger is not None:
        logger.debug("  [path-discovery] GET %s ...", url)
    data, err = get_json(session, url, token, timeout=DEFAULT_TIMEOUT)
    if err or not isinstance(data, dict):
        msg = f"GET {url} failed: {err}"
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    log_dir = data.get("logDir")
    if not log_dir:
        settings = data.get("settings")
        if isinstance(settings, dict):
            log_dir = settings.get("logDir")

    if not log_dir:
        msg = f"GET {url} succeeded but no 'logDir' found (checked logDir and settings.logDir): {data!r}"
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    if logger is not None:
        logger.debug("  [path-discovery] %s reports logDir=%s", portal_url,
                    log_dir)
    return log_dir, None


# ==========================================================================
# Registry-based discovery (install directory; Data Store data directory)
# ==========================================================================
def _connect_remote_hklm(short_hostname, logger=None):
    """
    Connect to short_hostname's remote HKEY_LOCAL_MACHINE. Returns the
    connection handle, or None if the connection fails (with the failure
    logged at ERROR level -- callers treat None as a hard discovery
    failure for that machine, not something to guess around).
    """
    if not _WINREG_AVAILABLE:
        if logger is not None:
            logger.error("  [path-discovery] winreg is not available on "
                        "this platform (not running on Windows?); registry "
                        "discovery cannot run.")
        return None
    try:
        return winreg.ConnectRegistry(short_hostname, winreg.HKEY_LOCAL_MACHINE)
    except Exception as exc:
        if logger is not None:
            logger.error(
                "  [path-discovery] could not connect to the remote "
                "registry on %s: %s (is the Remote Registry service "
                "running and reachable through the firewall on that "
                "machine?)", short_hostname, exc)
        return None


def _find_server_subkey(hklm, short_hostname, logger=None):
    """
    Enumerate HKLM\\SOFTWARE\\esri on the remote machine and return the full
    subkey path of the ArcGIS Server entry (e.g.
    "SOFTWARE\\esri\\Server11.5"), trying both the 64-bit and 32-bit
    registry views. ArcGIS Server's subkey name changes with every product
    version (Server10.9, Server11.5, Server12.1, ...), so this cannot be a
    fixed candidate string -- it is found by matching any subkey directly
    under esri whose name starts with "server" (case-insensitive).

    Returns (subkey_path, None) on success, or (None, error_str) if no
    matching subkey is found under either view, or esri itself can't be
    enumerated.
    """
    attempts = []
    for view_label, view_flag in _REGISTRY_VIEWS:
        try:
            with winreg.OpenKey(hklm, _ESRI_ROOT, 0,
                                winreg.KEY_READ | view_flag) as esri_key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(esri_key, i)
                    except OSError:
                        break  # no more subkeys
                    i += 1
                    if name.lower().startswith(_SERVER_SUBKEY_PREFIX):
                        subkey_path = _ESRI_ROOT + "\\" + name
                        if logger is not None:
                            logger.debug(
                                "  [path-discovery] found ArcGIS Server "
                                "registry subkey on %s (%s view): %s",
                                short_hostname, view_label, subkey_path)
                        return subkey_path, None
                attempts.append(f"{_ESRI_ROOT} ({view_label} view): no "
                               f"subkey starting with 'Server' found")
        except FileNotFoundError:
            attempts.append(f"{_ESRI_ROOT} ({view_label} view): key not found")
        except Exception as exc:
            attempts.append(f"{_ESRI_ROOT} ({view_label} view): {exc}")

    msg = (f"could not find an ArcGIS Server subkey under "
          f"HKLM\\{_ESRI_ROOT} on {short_hostname} after trying "
          f"{len(attempts)} location/view combination(s): "
          + "; ".join(attempts))
    if logger is not None:
        logger.error("  [path-discovery] %s", msg)
    return None, msg


def _read_value_both_views(hklm, short_hostname, subkey, value_name,
                           logger=None):
    """
    Read one value from `subkey`, trying both the 64-bit and 32-bit
    registry views. Returns (value, None) on the first hit, or
    (None, error_str) summarizing both attempts if neither succeeds.
    """
    attempts = []
    for view_label, view_flag in _REGISTRY_VIEWS:
        try:
            with winreg.OpenKey(hklm, subkey, 0,
                                winreg.KEY_READ | view_flag) as key:
                value, _type = winreg.QueryValueEx(key, value_name)
                if logger is not None:
                    logger.debug(
                        "  [path-discovery] %s\\%s\\%s (%s view) = %s",
                        short_hostname, subkey, value_name, view_label,
                        value)
                return value, None
        except FileNotFoundError:
            attempts.append(f"{subkey}\\{value_name} ({view_label} view): not found")
        except Exception as exc:
            attempts.append(f"{subkey}\\{value_name} ({view_label} view): {exc}")

    msg = (f"value not found on {short_hostname} after trying "
          f"{len(attempts)} view(s): " + "; ".join(attempts))
    if logger is not None:
        logger.error("  [path-discovery] %s", msg)
    return None, msg


def discover_install_dir(short_hostname, product, logger=None):
    """
    Remote registry lookup of a product's InstallDir on `short_hostname`.

    `product` is one of "server", "portal", "datastore" (case-insensitive).
    For "server", the version-specific subkey (e.g. "Server11.5") is first
    located by enumerating HKLM\\SOFTWARE\\esri (see _find_server_subkey());
    for "portal"/"datastore" the subkey name is fixed and known.

    Returns (install_dir, error_str) -- exactly one is meaningful.
    """
    key = str(product).strip().lower()
    if key not in ("server", "portal", "datastore"):
        msg = f"unknown product {product!r} for install-dir discovery"
        if logger is not None:
            logger.error("  [path-discovery] %s.", msg)
        return None, msg

    hklm = _connect_remote_hklm(short_hostname, logger=logger)
    if hklm is None:
        return None, f"could not connect to the remote registry on {short_hostname}"

    try:
        if key == "server":
            subkey, err = _find_server_subkey(hklm, short_hostname,
                                              logger=logger)
            if not subkey:
                return None, err
        else:
            subkey = _FIXED_SUBKEYS[key]

        return _read_value_both_views(hklm, short_hostname, subkey,
                                      _INSTALL_DIR_VALUE, logger=logger)
    finally:
        try:
            winreg.CloseKey(hklm)
        except Exception:
            pass


def discover_datastore_data_dir(short_hostname, install_dir, logger=None):
    """
    Discover the Data Store's DATA directory (distinct from its install
    directory) by reading its own config file over the D$ admin share:
        <install_dir>\\etc\\arcgis-data-store-config.properties
    which contains a line of the form:
        dir.data=D\\:/Datastore
    This is Java .properties escaping: "\\:" is a literal colon, and forward
    slashes are used in place of backslashes. Once unescaped this becomes
    "D:/Datastore" -- the actual data directory. This function returns that
    unescaped, Windows-style path (e.g. "D:\\Datastore").

    Parameters
    ----------
    short_hostname : str
        The machine's short (non-FQDN) name, used to build the UNC path to
        its D$ share.
    install_dir : str
        The Data Store's install directory as already discovered via
        discover_install_dir(short_hostname, "datastore", ...), e.g.
        "D:\\Program Files\\DataStore\\". Required -- the config file's
        location is relative to this and cannot be found without it.
    logger : logging.Logger, optional

    Returns
    -------
    (data_dir, error_str)
        Exactly one is meaningful. Never raises.
    """
    if not install_dir:
        msg = ("cannot locate arcgis-data-store-config.properties without "
              "a known install directory (install-dir discovery must "
              "succeed first)")
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    # Build the UNC path to the config file via the SAME D$ share already
    # used for the log copy itself, re-expressing install_dir (a LOCAL path
    # as reported by that machine, e.g. "D:\Program Files\DataStore\")
    # relative to D$.
    drive, tail = ntpath.splitdrive(install_dir)
    if drive.rstrip(":\\").upper() != "D":
        msg = (f"install directory {install_dir!r} is not on the D: "
              f"drive; cannot reach its config file via the D$ admin "
              f"share")
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    install_rel = tail.strip("\\/")
    config_unc = (f"\\\\{short_hostname}\\D$\\{install_rel}"
                 f"\\etc\\arcgis-data-store-config.properties")

    if logger is not None:
        logger.debug("  [path-discovery] reading %s ...", config_unc)

    try:
        with open(config_unc, "r", encoding="utf-8", errors="replace") as fh:
            contents = fh.read()
    except Exception as exc:
        msg = f"could not read {config_unc}: {exc}"
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    raw_value = None
    for line in contents.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue  # comment line (e.g. the timestamp header) or blank
        if stripped.startswith("dir.data="):
            raw_value = stripped[len("dir.data="):].strip()
            break

    if raw_value is None:
        msg = (f"{config_unc} was read successfully but contained no "
              f"'dir.data=' line")
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg

    # Unescape Java .properties syntax: "\:" -> ":" (a literal colon that
    # Java escapes because ':' is also a valid key/value separator in this
    # file format), then normalize forward slashes to backslashes for a
    # normal Windows path.
    unescaped = raw_value.replace("\\:", ":").replace("/", "\\")

    if logger is not None:
        logger.debug("  [path-discovery] %s dir.data=%s (raw) -> %s "
                    "(unescaped)", config_unc, raw_value, unescaped)

    return unescaped, None


# ==========================================================================
# Local-path -> D$-relative path conversion
# ==========================================================================
def _to_share_relative(local_path, logger=None):
    """
    Convert a local Windows path as reported BY THAT MACHINE (e.g.
    "D:\\arcgisserver\\logs") into a path relative to its own D$ admin share
    (e.g. "arcgisserver\\logs").

    Returns (relative_path, None) on success, or (None, error_str) if the
    path is not on the D: drive (the only drive reachable via the D$ share
    this project's UNC copying depends on) -- there is no way to reach a
    path on a different drive letter through D$, so this is reported as a
    hard discovery failure rather than silently guessed around.
    """
    if not local_path:
        return None, "discovered path was empty"
    drive, tail = ntpath.splitdrive(local_path)
    if drive.rstrip(":\\").upper() != "D":
        msg = (f"discovered path {local_path!r} is not on the D: drive; "
              f"this project's log copy is built around the D$ admin "
              f"share, so this path cannot be reached over the network")
        if logger is not None:
            logger.error("  [path-discovery] %s", msg)
        return None, msg
    return tail.lstrip("\\/"), None


# ==========================================================================
# Top-level convenience
# ==========================================================================
def resolve_log_paths(category, short_hostname, domain_suffix, session=None,
                      admin_url=None, token=None, logger=None):
    """
    Attempt full self-discovery of the two relative log paths for ONE
    machine in ONE category. There is NO fallback to a hard-coded default --
    a path that cannot be discovered comes back as None with an explanatory
    error string, so the caller can skip exactly that one copy and report
    precisely why.

    Parameters
    ----------
    category : str
        "Portal", "Hosting", "DataStore", or a federated site name (treated
        identically to "Hosting" -- federated sites are plain ArcGIS Server
        installs).
    short_hostname : str
        The machine's short (non-FQDN) name, used for the registry lookup.
    domain_suffix : str
        Appended to short_hostname to log the FQDN for readability only.
    session : requests.Session, optional
        Required (along with admin_url/token) for the REST-based "data log
        directory" discovery on Portal/Server categories.
    admin_url : str, optional
        The site's admin REST base URL (Portal URL for "Portal"; the
        federated server's admin URL for "Hosting"/site categories).
    token : str, optional
        Auth token for the REST call.
    logger : logging.Logger, optional
        Every attempt (success or failure) is logged; failures at ERROR
        level with the specific cause, successes at DEBUG/INFO.

    Returns
    -------
    dict
        {
          "data_log_path": str | None,
          "data_log_error": str | None,
          "install_log_path": str | None,
          "install_log_error": str | None,
        }
        A None path always has a corresponding non-None error string
        explaining exactly what was tried and why it failed.
    """
    fqdn = f"{short_hostname}.{domain_suffix}"
    cat_lower = category.strip().lower()
    is_portal = cat_lower == "portal"
    is_datastore = cat_lower == "datastore"
    # Everything else (Hosting, or any federated site name) is a plain
    # ArcGIS Server install and is discovered exactly like Hosting.
    is_server_like = not is_portal and not is_datastore

    result = {
        "data_log_path": None, "data_log_error": None,
        "install_log_path": None, "install_log_error": None,
    }

    # --- Data Store is handled as a special case: BOTH pieces depend on the
    # install directory (the data-log piece needs it to locate the config
    # file; the install-log piece needs it directly), so it is discovered
    # once up front and reused for both, rather than each piece discovering
    # it independently. ---
    if is_datastore:
        install_dir, install_err = discover_install_dir(
            short_hostname, "datastore", logger=logger)

        if not install_dir:
            # Neither piece can proceed without the install directory.
            result["data_log_error"] = install_err
            result["install_log_error"] = install_err
            if logger is not None:
                logger.error("  [%s / %s] data-log path discovery FAILED: %s",
                            category, fqdn, install_err)
                logger.error("  [%s / %s] install-log path discovery "
                            "FAILED: %s", category, fqdn, install_err)
            return result

        install_rel, install_rel_err = _to_share_relative(install_dir,
                                                          logger=logger)
        if install_rel:
            tomcat_tail = r"framework\runtime\tomcat\logs"
            result["install_log_path"] = ntpath.join(install_rel, tomcat_tail)
            if logger is not None:
                logger.info("  [%s / %s] install-log path self-discovered: "
                          "%s", category, fqdn, result["install_log_path"])
        else:
            result["install_log_error"] = install_rel_err

        data_dir, data_dir_err = discover_datastore_data_dir(
            short_hostname, install_dir, logger=logger)
        if data_dir:
            data_log_dir = ntpath.join(data_dir, "logs")
            rel, rel_err = _to_share_relative(data_log_dir, logger=logger)
            if rel:
                result["data_log_path"] = rel
                if logger is not None:
                    logger.info("  [%s / %s] data-log path "
                              "self-discovered: %s", category, fqdn, rel)
            else:
                result["data_log_error"] = rel_err
        else:
            result["data_log_error"] = data_dir_err
            if logger is not None:
                logger.error("  [%s / %s] data-log path discovery FAILED: "
                            "%s", category, fqdn, data_dir_err)

        return result

    # --- Portal / Server (Hosting or any federated site) -------------------
    # --- Piece 1: the "data" log directory (arcgisportal\logs, etc.) ------
    if is_portal:
        data_log_dir, data_err = discover_portal_log_dir(
            session, admin_url, token, logger=logger)
    else:  # is_server_like
        data_log_dir, data_err = discover_server_log_dir(
            session, admin_url, token, logger=logger)

    if data_log_dir:
        rel, rel_err = _to_share_relative(data_log_dir, logger=logger)
        if rel:
            result["data_log_path"] = rel
            if logger is not None:
                logger.info("  [%s / %s] data-log path self-discovered: %s",
                          category, fqdn, rel)
        else:
            result["data_log_error"] = rel_err
    else:
        result["data_log_error"] = data_err
        if logger is not None:
            logger.error("  [%s / %s] data-log path discovery FAILED: %s",
                        category, fqdn, data_err)

    # --- Piece 2: the install/tomcat log directory -------------------------
    product = "portal" if is_portal else "server"
    install_dir, install_err = discover_install_dir(short_hostname, product,
                                                    logger=logger)
    if install_dir:
        install_rel, install_rel_err = _to_share_relative(install_dir,
                                                          logger=logger)
        if install_rel:
            tomcat_tail = r"framework\runtime\tomcat\logs"
            result["install_log_path"] = ntpath.join(install_rel, tomcat_tail)
            if logger is not None:
                logger.info("  [%s / %s] install-log path self-discovered: "
                          "%s", category, fqdn, result["install_log_path"])
        else:
            result["install_log_error"] = install_rel_err
    else:
        result["install_log_error"] = install_err
        if logger is not None:
            logger.error("  [%s / %s] install-log path discovery FAILED: %s",
                        category, fqdn, install_err)

    return result