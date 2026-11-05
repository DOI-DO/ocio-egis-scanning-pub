r"""
UTILITIES/azcopy_utils.py
==============================================================================
Shared Azure Blob upload helpers for PORTAL_STACK_LOG_DUMP.py's two Blob
use cases, which use DIFFERENT azcopy subcommands:

  1. DATED / SCOPED DUMP (--sync-mode not passed). One-shot extraction of a
     date-filtered window of logs into a fresh, run-timestamped blob
     prefix. Uses `azcopy copy` -- see run_azcopy_upload()/copy_group_to_blob().

  2. STANDING NIGHTLY MIRROR (--sync-mode mirror). A continuously-maintained
     destination kept in sync with the source, without re-uploading
     unchanged files and without ever deleting anything. Uses `azcopy sync
     --delete-destination=false` against a STABLE (non-timestamped) blob
     prefix -- see run_azcopy_sync()/copy_group_to_blob_mirror().

     CAVEAT: sync compares by last-modified-time only. A file appended to
     in place over multiple days (same filename, growing, mtime advancing)
     is RE-UPLOADED IN FULL each night it changes, not diffed. It settles
     once the file stops changing (e.g. it rolls over) -- there is no
     special "wait for close" step, it simply stops being touched once its
     mtime is unchanged. This is an accepted cost, not a bug.

AUTHENTICATION
---------------
*** THIS PROJECT USES SAS-TOKEN AUTHENTICATION, STORED IN WINDOWS
CREDENTIAL MANAGER. *** Managed identity, workload identity, and Azure CLI
modes are also implemented (for sites that may want them later, or for
other scripts built on this module), but SAS is the mode actually in use
here and is treated as a first-class, fully-supported path -- not a
deprecated fallback. Before a SAS is used, this module requires an
HTTPS-only, container-scoped, short-lived token with only the permissions
needed by the requested copy/sync operation. AzCopy requires a SAS to be
part of the destination URL, so SAS mode cannot prevent local process/audit
tools from seeing the URL AzCopy itself constructs -- but this module never
independently prints, logs, or returns that value anywhere. See "SAS
SECRECY GUARANTEES" below for the specific measures taken.

No secrets live in the org config TOML. The SAS token is read from Windows
Credential Manager via keyring.

*** CREDENTIAL MANAGER KEY CHANGE (THIS REVISION): COMPOSITE SERVICE NAME
    -- MULTIPLE CONTAINERS UNDER ONE STORAGE ACCOUNT ***
-------------------------------------------------------------------------
The PREVIOUS convention stored the SAS under:
    Internet or network address (keyring service) = storage account name
    User name (keyring username)                  = container name
This "worked" through keyring's own internal target-name composition, but
it is a bad convention operationally: opening Windows Credential Manager's
own UI (Control Panel -> Credential Manager -> Windows Credentials) shows
MULTIPLE entries all sharing the exact same "Internet or network address"
value (the storage account name), distinguished only by the less-prominent
"User name" column. This is confusing for whoever has to administer these
credentials by hand, and is fragile for any other tool that keys off
address alone.

FIX: the keyring "service" (Internet or network address) is now a
COMPOSITE string that encodes BOTH the storage account AND the container:

    service  = "azblob:<storage_account>:<container>"
    username = "sastoken"                              (fixed, always)
    password = the SAS query string ONLY (no leading '?', no full URL)

This guarantees a UNIQUE, human-legible "Internet or network address" per
(account, container) pair even when the same storage account hosts several
containers (e.g. one for dated dumps, one for the standing mirror) -- there
is never any ambiguity in the raw Credential Manager UI, and this scales
cleanly to additional containers or additional storage accounts without
any further convention changes.

Callers of get_blob_sas_token() are UNAFFECTED by this change -- it still
takes (storage_account, container, logger=None) and builds the composite
key internally. Existing credentials stored under the OLD convention will
no longer be found; they must be RE-STORED under the new composite key
(see the docstring on get_blob_sas_token() for the exact command).

*** COMMON CREDENTIAL PITFALL: the leading '?' ***
The Azure Portal's "Blob SAS URL" field includes a leading '?' before the
query string. If that whole value is stored instead of just the token,
naively appending it to a URL already ending in '?' produces a doubled
'??', which azcopy fails to parse as any credential at all --
"NoAuthenticationInformation" rather than a clear "bad token" error.
build_blob_url() strips a leading '?' defensively; get_blob_sas_token()
warns (without logging the value) if the credential's shape looks wrong.

SAS SECRECY GUARANTEES (READ THIS BEFORE CHANGING ANYTHING BELOW)
-------------------------------------------------------------------------
The SAS token/query string must NEVER be displayed or logged, anywhere,
under any circumstance -- not in a log file, not on the console, not in an
exception message, not in a returned "detail" string, not in debug-level
output. This module enforces that with several independent, overlapping
measures so that no single missed call site can leak it:

  1. NEVER LOGGED DIRECTLY. No function in this module ever passes
     `sas_token` (or the raw query string derived from it) directly to a
     logger call, a print(), an f-string that reaches a return value, or a
     raised exception's message. Every place a URL, command line, or
     caught-exception string might contain it is passed through
     sanitize_azcopy_output()/_redact_sas() FIRST.

  2. get_blob_sas_token() logs ONLY shape/length metadata (e.g. "length=812
     chars") and a boolean-style shape check (has 'sv=', has 'sig=', has a
     leading '?') -- never the value, never a substring of the value.

  3. sanitize_azcopy_output() is applied to EVERY piece of text that could
     have come from AzCopy itself (stdout, stderr, exception text) or been
     built by this module (the destination URL) before that text is
     logged or returned to a caller. It performs THREE independent passes:
       a. An EXACT replacement of the specific SAS query string used for
          THIS invocation (both "?<query>" and a bare "<query>" form),
          which is authoritative because the exact value is known here.
       b. A generic scan for any "https://...&sig=..." URL shape and
          redaction of its query string, in case AzCopy's own output
          embeds the token in a differently-formatted URL (e.g. a retried
          request, or a nested job-plan file path AzCopy prints).
       c. A generic scan for a "bare" SAS-shaped query string (a run of
          key=value pairs including 'sig=' plus at least 2 other SAS
          marker keys) that ISN'T part of a URL, for the same reason.
     This means even a SAS this module did not itself generate (e.g. one
     embedded in an AzCopy internal log line for a reason unrelated to the
     destination URL) still has a good chance of being caught by the
     generic passes, not just the exact-match pass.

  4. _run_azcopy_subprocess() passes a SCRUBBED CHILD ENVIRONMENT to the
     azcopy.exe process: unrelated password/token/secret/key-shaped
     environment variables (by name pattern) are stripped out entirely,
     and only the specific variables required for the SELECTED identity
     mode are added back explicitly. In SAS mode, NO credential
     environment variable is added at all -- the SAS travels only inside
     the destination URL argument, which is AzCopy's own required
     interface for it; this module does not additionally echo it into the
     environment, a temp file, or anywhere else.

  5. Nothing in this module ever writes the SAS to disk (no temp file, no
     cache, no state file). It is read from Credential Manager via
     keyring, held only in local Python variables for the duration of one
     azcopy invocation, and never persisted anywhere by this module.

  6. validate_sas_token()'s returned `detail` string is CONSTRUCTED FRESH
     from parsed, non-secret fields only (scope type, permission letters,
     computed expiry timestamp) -- it is never built by formatting the
     token itself into a string, so there is no accidental-inclusion path
     via that function either. Its failure-path exception text is ALSO
     passed through sanitize_azcopy_output() defensively, even though the
     exceptions raised inside it are constructed from non-secret parsed
     fields, in case a future edit to that function's error paths
     introduces a token-bearing string without the author noticing.

  7. Every one of run_azcopy_upload(), run_azcopy_sync(), and
     test_blob_connection() sanitizes BOTH branches of every try/except
     around subprocess execution and destination-preparation -- success
     and failure -- so there is no failure path that bypasses redaction
     because "it's just an error message."

When modifying this module, treat any new f-string, log call, exception
message, or returned tuple that touches `sas_token`, `url`, `cmd`, `stdout`,
or `stderr` as suspect until it has been run through
sanitize_azcopy_output() (or built exclusively from values already proven
non-secret, like parsed SAS metadata fields).

AZCOPY INVOCATIONS
-------------------
Preferred identity mode (when selected) uses an unsigned destination URL:

    azcopy.exe copy "\\machine\D$\arcgisportal\logs\*" \
        "https://<account>.blob.core.windows.net/<container>/<blobprefix>" \
        --recursive=true

SAS mode (the mode this project uses) appends a validated SAS query to
that URL -- this is AzCopy's own required interface for a SAS credential;
there is no alternate flag to pass it out-of-band.

Both accept UNC paths as an ordinary local source -- no staging copy needed.

WHY THE TRAILING "\*" ON THE SOURCE: azcopy (unlike robocopy) nests the
source directory's own name under the destination unless the source ends
in a wildcard. There is no "--strip-top-dir" flag on `copy` or `sync`; the
wildcard is the only correct mechanism. _append_source_wildcard() applies
it to every source path.

PER-RUN ISOLATION: copy_group_to_blob() (dated dump) prefixes every blob
path with run_timestamp so each dump lands in its own folder.
copy_group_to_blob_mirror() deliberately omits any run_timestamp -- sync
needs a stable destination path across runs to compare against.

KNOWN LIMITATION -- LOCKED FILES: a file open for exclusive write by
another process (e.g. a live Tomcat catalina.0.log) may fail to open for
azcopy with a sharing-violation error. robocopy tolerates this; azcopy
does not have an equivalent permissive-sharing flag. Expect a small number
of per-file failures on actively-written logs; azcopy's own job
summary (Completed vs Failed counts) is the authoritative result, not
just the process exit code.

SUBPROCESS / CONSOLE-HANDLE NOTE
----------------------------------
Every azcopy subprocess call goes through _run_azcopy_subprocess(), which
sets stdin=DEVNULL, close_fds=True, and (on Windows) creationflags=
CREATE_NO_WINDOW. It also supplies a scrubbed child environment: unrelated
password/token/secret variables are removed, then only the variables required
for the selected AzCopy identity mode are added back explicitly (in SAS
mode, none are added -- see SAS SECRECY GUARANTEES point 4 above).

Public API
----------
    get_azcopy_path(target, config_path, logger=None) -> str | None
    get_blob_sas_token(storage_account, container, logger=None) -> (token|None, err|None)
    normalize_azcopy_auth_mode(value) -> str
    validate_azcopy_auth_environment(mode, managed_identity_client_id=None)
        -> (ok, detail)
    validate_sas_token(token, operation, max_remaining_hours) -> (ok, detail)
    build_blob_url(storage_account, container, sas_token=None, blob_prefix=None) -> str
    validate_azcopy_exe(azcopy_path, logger=None) -> (ok, err|None)
    maxage_days_to_include_after(maxage_days) -> str   # dated-dump only
    run_azcopy_upload(...) -> (exit_code, stdout, stderr)          # dated dump (copy)
    copy_group_to_blob(...) -> list[int]                           # dated dump (copy)
    run_azcopy_sync(...) -> (exit_code, stdout, stderr)            # mirror (sync)
    copy_group_to_blob_mirror(...) -> list[int]                    # mirror (sync)
    test_blob_connection(..., auth_mode, operation) -> (ok, detail)
==============================================================================
"""

import datetime
import os
import posixpath
import re
import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

try:
    import keyring
    _KEYRING_AVAILABLE = True
except ImportError:  # pragma: no cover - keyring should always be present
    keyring = None
    _KEYRING_AVAILABLE = False

from . import config as _config

# --------------------------------------------------------------------------
# Redaction import, with graceful fallback.
#
# This module MUST be able to sanitize free-text (AzCopy stdout/stderr,
# exception strings, URLs) so a SAS value never reaches a log line. The
# preferred implementation lives in UTILITIES.auth. Different revisions of
# auth.py have exposed this under different names as the project's
# redaction helper evolved, so this import tries each in turn and only
# falls back to a strictly local implementation if auth.py has neither --
# this keeps azcopy_utils.py import-safe regardless of which auth.py
# revision is currently in place, without silently downgrading redaction
# quality when a real implementation IS available.
# --------------------------------------------------------------------------
try:
    from .auth import redact_sensitive_data as _external_redact
except ImportError:
    try:
        from .auth import redact_token as _external_redact
    except ImportError:
        _external_redact = None


def _fallback_redact_sensitive_data(text):
    """
    Local last-resort fallback used ONLY if UTILITIES.auth exposes neither
    redact_sensitive_data() nor redact_token(). Handles the same
    'token=<value>' / common secret-parameter shapes as auth.redact_token()
    so this module is never left with zero generic-text redaction.
    """
    if not isinstance(text, str) or not text:
        return text
    pattern = re.compile(
        r"(?i)\b(token|sig|sas|password|passwd|secret|key)=[^&\s'\"]+")
    return pattern.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)


def redact_sensitive_data(text):
    """
    Module-local wrapper so every call site here has one stable name to
    call, regardless of which redaction implementation UTILITIES.auth
    actually provides in the running revision of this project.
    """
    if _external_redact is not None:
        try:
            return _external_redact(text)
        except Exception:
            # Redaction must never itself be the reason an exception is
            # raised or a log line is lost -- fall back rather than throw.
            pass
    return _fallback_redact_sensitive_data(text)


# Supported Blob authentication modes. This project's selected mode is SAS;
# the identity modes remain available for other deployments/scripts built
# on this module.
AZCOPY_AUTH_MANAGED_IDENTITY = "managed-identity"
AZCOPY_AUTH_WORKLOAD_IDENTITY = "workload-identity"
AZCOPY_AUTH_AZURE_CLI = "azure-cli"
AZCOPY_AUTH_SAS = "sas"
AZCOPY_AUTH_MODES = (
    AZCOPY_AUTH_MANAGED_IDENTITY,
    AZCOPY_AUTH_WORKLOAD_IDENTITY,
    AZCOPY_AUTH_AZURE_CLI,
    AZCOPY_AUTH_SAS,
)
# *** THIS PROJECT'S DEFAULT IS SAS. *** A caller that does not specify
# auth_mode, and DOES supply a sas_token, is resolved to SAS mode by
# _effective_auth_mode() below regardless of this module-level default;
# this constant only matters when NEITHER auth_mode NOR a token is given
# anywhere in the call chain (e.g. normalize_azcopy_auth_mode(None)).
DEFAULT_AZCOPY_AUTH_MODE = AZCOPY_AUTH_SAS
DEFAULT_SAS_MAX_HOURS = 24.0
MAX_SAS_MAX_HOURS = 168.0
_MIN_SAS_REMAINING_MINUTES = 5.0

_AUTH_MODE_ALIASES = {
    "entra": AZCOPY_AUTH_MANAGED_IDENTITY,
    "identity": AZCOPY_AUTH_MANAGED_IDENTITY,
    "managed": AZCOPY_AUTH_MANAGED_IDENTITY,
    "managed-identity": AZCOPY_AUTH_MANAGED_IDENTITY,
    "msi": AZCOPY_AUTH_MANAGED_IDENTITY,
    "workload": AZCOPY_AUTH_WORKLOAD_IDENTITY,
    "workload-identity": AZCOPY_AUTH_WORKLOAD_IDENTITY,
    "federated": AZCOPY_AUTH_WORKLOAD_IDENTITY,
    "azure-cli": AZCOPY_AUTH_AZURE_CLI,
    "azcli": AZCOPY_AUTH_AZURE_CLI,
    "sas": AZCOPY_AUTH_SAS,
}

# --------------------------------------------------------------------------
# Windows Credential Manager key convention for the stored SAS.
#
# COMPOSITE SERVICE NAME (see module docstring "CREDENTIAL MANAGER KEY
# CHANGE" section for the full rationale): this guarantees a distinct,
# human-legible "Internet or network address" entry per (storage account,
# container) pair, so multiple containers under the SAME storage account
# never collide or look ambiguous in Credential Manager's own UI.
# --------------------------------------------------------------------------
_SAS_SERVICE_PREFIX = "azblob"
_SAS_CREDENTIAL_USERNAME = "sastoken"


def _sas_credential_service_name(storage_account, container):
    """
    Build the composite Credential Manager "Internet or network address"
    (keyring service) string for one (storage_account, container) pair:

        azblob:<storage_account>:<container>

    Using one fixed, distinct string per pair -- rather than the OLD
    convention of service=storage_account, username=container -- means
    Credential Manager's own UI shows a unique, unambiguous entry per
    container even when several containers share one storage account.
    """
    return f"{_SAS_SERVICE_PREFIX}:{storage_account}:{container}"


# Remove unrelated credentials before starting AzCopy. Required identity
# variables are added back explicitly by _build_azcopy_child_env(). In SAS
# mode specifically, NOTHING credential-shaped is added back (see SAS
# SECRECY GUARANTEES point 4 in the module docstring).
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
_AZCOPY_AUTH_CONTROL_ENV_NAMES = {
    "AZCOPY_AUTO_LOGIN_TYPE",
    "AZCOPY_MSI_CLIENT_ID",
    "AZCOPY_MSI_OBJECT_ID",
    "AZCOPY_MSI_RESOURCE_STRING",
    "AZCOPY_SPA_APPLICATION_ID",
    "AZCOPY_SPA_CLIENT_SECRET",
    "AZCOPY_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_TENANT_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_CLIENT_SECRET",
    "AZURE_AUTHORITY_HOST",
    "AZURE_CONFIG_DIR",
}

# On Windows, prevent any subprocess (azcopy) -- and anything it spawns --
# from inheriting this process's console handles.
_SUBPROCESS_CREATIONFLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


def normalize_azcopy_auth_mode(value):
    """Normalize a configured/CLI authentication mode or raise ValueError."""
    if value is None or not str(value).strip():
        return DEFAULT_AZCOPY_AUTH_MODE
    key = str(value).strip().lower().replace("_", "-")
    mode = _AUTH_MODE_ALIASES.get(key)
    if mode is None:
        raise ValueError(
            f"Unsupported AzCopy auth mode {value!r}; choose one of: "
            + ", ".join(AZCOPY_AUTH_MODES)
        )
    return mode


def _is_sensitive_env_name(name):
    upper = str(name).upper()
    return (
        upper in _EXPLICIT_SECRET_ENV_NAMES
        or upper in _AZCOPY_AUTH_CONTROL_ENV_NAMES
        or bool(_SENSITIVE_ENV_NAME_RE.search(upper))
    )


def _sanitized_base_child_env():
    """Copy the process environment while dropping credential-like values."""
    clean = {}
    for name, value in os.environ.items():
        if _is_sensitive_env_name(name):
            continue
        # Proxy URLs can themselves contain user:password credentials. Keep
        # ordinary proxy settings, but do not propagate credential-bearing
        # proxy URLs to a child process.
        try:
            rendered_value = str(value)
            if "://" in rendered_value:
                parsed = urlsplit(rendered_value)
                if parsed.username is not None or parsed.password is not None:
                    continue
            lowered_value = rendered_value.lower()
            if ("sig=" in lowered_value and
                    ("sv=" in lowered_value or "se=" in lowered_value)):
                continue
        except Exception:
            continue
        clean[name] = value
    return clean


def _clean_nonsecret_identifier(value, label):
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"{label} contains an invalid control character.")
    return cleaned


def _build_azcopy_child_env(auth_mode, managed_identity_client_id=None):
    """
    Build a scrubbed child environment for exactly one AzCopy auth mode.

    SAS mode adds NO credential-shaped environment variable -- the SAS
    travels only inside the destination URL argument passed to AzCopy
    (its own required interface for a SAS credential). This module does
    not additionally place it in the environment, a temp file, or
    anywhere else. See "SAS SECRECY GUARANTEES" in the module docstring.
    """
    mode = normalize_azcopy_auth_mode(auth_mode)
    env = _sanitized_base_child_env()

    if mode == AZCOPY_AUTH_MANAGED_IDENTITY:
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "MSI"
        client_id = _clean_nonsecret_identifier(
            managed_identity_client_id, "managed identity client id"
        )
        if client_id:
            env["AZCOPY_MSI_CLIENT_ID"] = client_id

    elif mode == AZCOPY_AUTH_WORKLOAD_IDENTITY:
        required = (
            "AZURE_CLIENT_ID",
            "AZURE_TENANT_ID",
            "AZURE_FEDERATED_TOKEN_FILE",
        )
        values = {name: os.environ.get(name) for name in required}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(
                "workload-identity mode requires these environment variables: "
                + ", ".join(missing)
            )
        token_file = values["AZURE_FEDERATED_TOKEN_FILE"]
        if not os.path.isfile(token_file):
            raise ValueError(
                "AZURE_FEDERATED_TOKEN_FILE does not name an existing file."
            )
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "WORKLOAD"
        env.update(values)
        authority = os.environ.get("AZURE_AUTHORITY_HOST")
        if authority:
            env["AZURE_AUTHORITY_HOST"] = authority

    elif mode == AZCOPY_AUTH_AZURE_CLI:
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
        azure_config_dir = os.environ.get("AZURE_CONFIG_DIR")
        if azure_config_dir:
            env["AZURE_CONFIG_DIR"] = azure_config_dir

    # SAS mode (mode == AZCOPY_AUTH_SAS): deliberately no credential
    # environment variable is added. Nothing further to do here.
    return env


def validate_azcopy_auth_environment(auth_mode,
                                      managed_identity_client_id=None):
    """Validate that the selected identity mode can build a safe child env."""
    try:
        mode = normalize_azcopy_auth_mode(auth_mode)
        _build_azcopy_child_env(mode, managed_identity_client_id)
    except Exception as exc:
        return False, redact_sensitive_data(str(exc))
    return True, f"AzCopy authentication environment is ready ({mode})."


def _run_azcopy_subprocess(cmd, timeout=None, auth_mode=None,
                           managed_identity_client_id=None):
    """Run AzCopy with closed handles and a credential-scrubbed environment."""
    mode = normalize_azcopy_auth_mode(auth_mode)
    child_env = _build_azcopy_child_env(
        mode, managed_identity_client_id=managed_identity_client_id
    )
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=_SUBPROCESS_CREATIONFLAGS,
        close_fds=True,
        timeout=timeout,
        env=child_env,
    )


# ==========================================================================
# azcopy.exe path resolution (config.toml, NOT a secret)
# ==========================================================================
def get_azcopy_path(target, config_path, logger=None):
    """Resolve 'azcopy-path' from the chosen org's config toml (target
    section, then [common]). Returns None if unset everywhere."""
    target = _config.normalize_target(target)
    path = _config.get_config_value("azcopy-path", target, path=config_path)
    if logger is not None:
        if path:
            logger.info("Resolved azcopy-path from config (%s): %s",
                       config_path, path)
        else:
            logger.error(
                "No 'azcopy-path' setting found in the [%s] or [common] "
                "section of %s. Add e.g.:\n"
                "    [common]\n"
                '    azcopy-path = "C:\\\\Program Files\\\\azcopy\\\\azcopy.exe"',
                target, config_path)
    return path


def validate_azcopy_exe(azcopy_path, logger=None):
    """Confirm azcopy_path points at an existing file. Never raises."""
    if not azcopy_path:
        msg = "azcopy-path is not configured (see config.toml)."
        if logger is not None:
            logger.error("  [azcopy] %s", msg)
        return False, msg
    if not os.path.isfile(azcopy_path):
        msg = f"azcopy.exe not found at configured path: {azcopy_path}"
        if logger is not None:
            logger.error("  [azcopy] %s", msg)
        return False, msg
    if logger is not None:
        logger.info("  [azcopy] confirmed azcopy.exe exists at: %s",
                   azcopy_path)
    return True, None


# ==========================================================================
# Credential lookup (Windows Credential Manager via keyring)
# ==========================================================================
def get_blob_sas_token(storage_account, container, logger=None):
    """
    Look up the SAS token for (storage_account, container) in Windows
    Credential Manager via keyring, under the COMPOSITE service name:

        Internet or network address (keyring service) =
            "azblob:<storage_account>:<container>"
        User name (keyring username) = "sastoken"   (fixed)
        Password                     = the SAS query string only

    This composite-key convention (see the module docstring's "CREDENTIAL
    MANAGER KEY CHANGE" section) guarantees a distinct Credential Manager
    entry per (account, container) pair, so MULTIPLE CONTAINERS UNDER THE
    SAME STORAGE ACCOUNT never collide or look ambiguous in Credential
    Manager's own UI -- each shows its own unique "Internet or network
    address" value.

    To store a credential under this convention:

        python -c "import keyring; keyring.set_password(
            'azblob:<storage_account>:<container>', 'sastoken',
            '<sas_query_string_no_leading_question_mark>')"

    Example:

        python -c "import keyring; keyring.set_password(
            'azblob:AZStorageAccountName:logdump', 'sastoken',
            'sv=2024-11-04&ss=b&srt=co&sp=rwl&spr=https&se=2026-09-08T18:00:00Z&sig=...')"

    Returns (token, error_str). Never raises. *** THE TOKEN VALUE ITSELF IS
    NEVER LOGGED HERE, NOT EVEN PARTIALLY. *** Only non-secret metadata is
    logged: length, and a boolean-style shape check (does it look like it
    has 'sv=' / 'sig=' / a leading '?'). See "SAS SECRECY GUARANTEES" in
    the module docstring for the full set of protections this module
    applies everywhere a SAS could otherwise leak.
    """
    if not _KEYRING_AVAILABLE:
        msg = "the 'keyring' package is not available in this environment"
        if logger is not None:
            logger.error("  [azcopy-auth] %s; cannot retrieve the Blob SAS "
                        "token.", msg)
        return None, msg

    if not storage_account:
        msg = "no storage account name was supplied"
        if logger is not None:
            logger.error("  [azcopy-auth] %s.", msg)
        return None, msg
    if not container:
        msg = "no container name was supplied"
        if logger is not None:
            logger.error("  [azcopy-auth] %s.", msg)
        return None, msg

    service_name = _sas_credential_service_name(storage_account, container)

    if logger is not None:
        logger.info(
            "  [azcopy-auth] looking up SAS token in Windows Credential "
            "Manager: Internet or network address (service)=%s, User "
            "Name (username)=%s ...", service_name, _SAS_CREDENTIAL_USERNAME)

    try:
        token = keyring.get_password(service_name, _SAS_CREDENTIAL_USERNAME)
    except Exception as exc:
        # exc's own string form is not expected to contain the credential
        # value (this is a lookup failure, not a value being echoed back),
        # but it is redacted defensively before logging on the general
        # principle that no exception text in this module goes unredacted.
        msg = (f"keyring lookup failed for service={service_name!r} "
              f"username={_SAS_CREDENTIAL_USERNAME!r}: "
              f"{redact_sensitive_data(str(exc))}")
        if logger is not None:
            logger.error("  [azcopy-auth] %s", msg)
        return None, msg

    if not token or not token.strip():
        msg = (f"no credential found in Windows Credential Manager for "
              f"Internet or network address={service_name!r} / "
              f"User Name={_SAS_CREDENTIAL_USERNAME!r}. Store it with, "
              f"e.g.:\n"
              f"    python -c \"import keyring; "
              f"keyring.set_password({service_name!r}, "
              f"{_SAS_CREDENTIAL_USERNAME!r}, '<sas_query_string>')\"")
        if logger is not None:
            logger.error("  [azcopy-auth] %s", msg)
        return None, msg

    token = token.strip()

    has_sv = "sv=" in token
    has_sig = "sig=" in token
    has_leading_q = token.startswith("?")
    if logger is not None:
        logger.info(
            "  [azcopy-auth] SAS token retrieved successfully for "
            "service=%s (length=%d chars; value not logged).",
            service_name, len(token))
        if has_leading_q:
            logger.warning(
                "  [azcopy-auth] The retrieved credential STARTS WITH '?' "
                "-- likely the full 'Blob SAS URL' was copied instead of "
                "just the token. The leading '?' is stripped automatically "
                "before use, so this should still work; consider "
                "re-storing just the part after '?' under service=%r.",
                service_name)
        if not (has_sv and has_sig):
            missing = []
            if not has_sv:
                missing.append("'sv=' (storage service version)")
            if not has_sig:
                missing.append("'sig=' (signature)")
            logger.warning(
                "  [azcopy-auth] The retrieved credential does NOT look "
                "like a well-formed Blob SAS query string -- missing: %s. "
                "This will likely produce 'NoAuthenticationInformation' / "
                "'please authenticate using Microsoft Entra ID' rather "
                "than a signature/expiry error. Re-check what was stored "
                "under service=%r -- it should be the full SAS query "
                "string, e.g. 'sv=2024-11-04&ss=b&srt=co&sp=rcwl&spr=https"
                "&se=<within-24-hours>&sig=...'.",
                ", ".join(missing), service_name)
    return token, None


# ==========================================================================
# URL building / redaction
# ==========================================================================
def _normalize_sas_query(sas_token):
    if sas_token is None:
        return ""
    token = str(sas_token).strip()
    if token.startswith("?"):
        token = token[1:]
    return token


def build_blob_url(storage_account, container, sas_token=None,
                   blob_prefix=None):
    """Build an HTTPS Blob URL; append a SAS only when explicitly supplied."""
    base = f"https://{storage_account}.blob.core.windows.net/{container}"
    if blob_prefix:
        normalized = blob_prefix.replace("\\", "/").strip("/")
        if normalized:
            base = base + "/" + normalized
    sas_query = _normalize_sas_query(sas_token)
    return f"{base}?{sas_query}" if sas_query else base


_SAS_URL_RE = re.compile(r"(?i)https?://[^\s\"'<>]+")
_BARE_SAS_QUERY_RE = re.compile(
    r"(?ix)(?P<prefix>^|[\s\"'(])"
    r"(?P<query>\??(?:[a-z][a-z0-9_-]*=[^&\s\"'<>]+&)*"
    r"sig=[^&\s\"'<>]+(?:&[a-z][a-z0-9_-]*=[^&\s\"'<>]+)*)"
)
_SAS_MARKER_KEYS = {"sig", "sv", "se", "sp", "spr", "sr", "ss", "srt"}


def _url_has_sas_query(url):
    try:
        query = urlsplit(url).query
        keys = {key.lower() for key, _value in parse_qsl(
            query, keep_blank_values=True
        )}
        return "sig" in keys and len(keys & _SAS_MARKER_KEYS) >= 3
    except Exception:
        return False


def _redact_sas_url_match(match):
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in "),.;]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    if not _url_has_sas_query(raw):
        return raw + trailing
    base = raw.split("?", 1)[0]
    return base + "?<SAS-REDACTED>" + trailing


def _redact_bare_sas_match(match):
    query = match.group("query")
    keys = {part.split("=", 1)[0].lstrip("?").lower()
            for part in query.split("&") if "=" in part}
    if "sig" not in keys or len(keys & _SAS_MARKER_KEYS) < 3:
        return match.group(0)
    return match.group("prefix") + "<SAS-REDACTED>"


def sanitize_azcopy_output(text, sas_token=None):
    """
    Redact complete SAS queries and other known credentials from text.

    *** THIS IS THE SINGLE CHOKE POINT every piece of AzCopy-adjacent text
    in this module passes through before it is logged or returned. ***
    Three independent passes are applied, in order:

      1. EXACT replacement of the specific SAS query used for this
         invocation (both "?<query>" and bare "<query>" forms) -- this is
         authoritative because sas_token is the actual value used to build
         THIS call's destination URL.
      2. A generic "https://...&sig=..." URL-shape scan, in case AzCopy's
         own stdout/stderr embeds the token in a differently-formatted URL
         than the one this module built (e.g. inside a retry log line).
      3. A generic "bare SAS query string" scan (a run of key=value pairs
         including 'sig=' plus 2+ other SAS marker keys) for a token that
         appears outside of any URL shape.

    After those three SAS-specific passes, the general-purpose
    redact_sensitive_data() (backed by UTILITIES.auth) is run as well, to
    catch any other credential-shaped text (e.g. an incidentally-embedded
    'token=' parameter unrelated to Blob).

    Never raises; a None input returns "" (this module only ever calls it
    with text intended for display/logging, where an empty string is the
    safe empty-case result).
    """
    if text is None:
        return ""
    rendered = str(text)

    # Exact replacement guarantees the SAS used for this invocation is gone,
    # even if AzCopy emits it in an unusual format that is not a normal URL.
    query = _normalize_sas_query(sas_token)
    if query:
        rendered = rendered.replace("?" + query, "?<SAS-REDACTED>")
        rendered = rendered.replace(query, "<SAS-REDACTED>")

    rendered = _SAS_URL_RE.sub(_redact_sas_url_match, rendered)
    rendered = _BARE_SAS_QUERY_RE.sub(_redact_bare_sas_match, rendered)
    return redact_sensitive_data(rendered)


def _redact_sas(text, sas_token=None):
    """Backward-compatible private helper used by existing logging code."""
    return sanitize_azcopy_output(text, sas_token=sas_token)


def _parse_sas_timestamp(value):
    raw = unquote(str(value or "")).strip()
    if not raw:
        raise ValueError("timestamp is blank")
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def validate_sas_token(sas_token, operation="copy",
                       max_remaining_hours=DEFAULT_SAS_MAX_HOURS, now=None):
    """
    Validate a short-lived, HTTPS-only, least-privilege Blob SAS.

    Returns (ok, detail). `detail` is built ONLY from parsed, non-secret
    fields (scope type, permission letters, computed ISO expiry timestamp)
    -- the raw token/query string is never interpolated into it. The
    failure-path message is passed through sanitize_azcopy_output()
    defensively as well, even though today's failure messages are already
    built from non-secret parsed fields, so a future edit to an error
    branch here can't silently introduce a leak.
    """
    try:
        query = _normalize_sas_query(sas_token)
        if not query:
            raise ValueError("SAS token is blank")
        if "://" in query:
            raise ValueError("store only the SAS query string, not a full URL")
        if any(ch.isspace() or ch in "\r\n\x00" for ch in query):
            raise ValueError("SAS query contains whitespace/control characters")
        segments = query.split("&")
        if any("=" not in segment for segment in segments):
            raise ValueError("SAS query contains a malformed parameter")

        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        params = {}
        duplicates = set()
        for key, value in pairs:
            normalized_key = key.lower()
            if normalized_key in params:
                duplicates.add(normalized_key)
            params[normalized_key] = value
        if duplicates:
            raise ValueError(
                "SAS query contains duplicate parameter(s): "
                + ", ".join(sorted(duplicates))
            )

        for required_key in ("sv", "sig", "se", "sp", "spr"):
            if not params.get(required_key):
                raise ValueError(f"SAS query is missing required '{required_key}'")

        if params["spr"].strip().lower() != "https":
            raise ValueError("SAS must be HTTPS-only (spr=https)")

        op = str(operation or "copy").strip().lower()
        requirements = {
            "copy": (set("cwl"), set("rcwl")),
            "sync": (set("rcwl"), set("rcwl")),
            "list": (set("l"), set("rl")),
        }
        if op not in requirements:
            raise ValueError("operation must be copy, sync, or list")
        required_permissions, allowed_permissions = requirements[op]
        permissions = set(params["sp"].lower())
        missing_permissions = required_permissions - permissions
        excessive_permissions = permissions - allowed_permissions
        if missing_permissions:
            raise ValueError(
                f"SAS lacks permission(s) required for {op}: "
                + "".join(sorted(missing_permissions))
            )
        if excessive_permissions:
            raise ValueError(
                "SAS grants permissions outside the least-privilege policy: "
                + "".join(sorted(excessive_permissions))
            )

        # Accept either a container service SAS (sr=c), or a Blob-only
        # account SAS scoped exactly to container/object resource types.
        signed_resource = params.get("sr", "").lower()
        if signed_resource:
            if signed_resource != "c":
                raise ValueError("service SAS must be container-scoped (sr=c)")
        else:
            services = set(params.get("ss", "").lower())
            resource_types = set(params.get("srt", "").lower())
            if services != {"b"}:
                raise ValueError("account SAS must be Blob-only (ss=b)")
            if resource_types != set("co"):
                raise ValueError(
                    "account SAS must be scoped to container/object only "
                    "(srt=co)"
                )

        max_hours = float(max_remaining_hours)
        if max_hours <= 0 or max_hours > MAX_SAS_MAX_HOURS:
            raise ValueError(
                f"SAS maximum lifetime must be >0 and <= "
                f"{MAX_SAS_MAX_HOURS:g} hours"
            )

        current = now or datetime.datetime.now(datetime.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=datetime.timezone.utc)
        current = current.astimezone(datetime.timezone.utc)
        expiry = _parse_sas_timestamp(params["se"])
        remaining = expiry - current
        if remaining <= datetime.timedelta(minutes=_MIN_SAS_REMAINING_MINUTES):
            raise ValueError(
                f"SAS expires too soon; at least "
                f"{_MIN_SAS_REMAINING_MINUTES:g} minutes must remain"
            )
        if remaining > datetime.timedelta(hours=max_hours):
            raise ValueError(
                f"SAS expiry is more than the allowed {max_hours:g} hours away"
            )

        if params.get("st"):
            start = _parse_sas_timestamp(params["st"])
            if start > current + datetime.timedelta(minutes=15):
                raise ValueError("SAS start time is more than 15 minutes in the future")
            if expiry - start > datetime.timedelta(hours=max_hours):
                raise ValueError(
                    f"SAS signed lifetime exceeds {max_hours:g} hours"
                )

        scope = "container service SAS" if signed_resource else "Blob account SAS"
        detail = (
            f"SAS validation passed ({scope}; HTTPS-only; "
            f"permissions={''.join(sorted(permissions))}; "
            f"expires={expiry.isoformat()})."
        )
        return True, detail
    except Exception as exc:
        return False, "SAS validation failed: " + sanitize_azcopy_output(str(exc))


def _effective_auth_mode(auth_mode, sas_token):
    if auth_mode is None:
        return AZCOPY_AUTH_SAS if sas_token else DEFAULT_AZCOPY_AUTH_MODE
    return normalize_azcopy_auth_mode(auth_mode)


def _prepare_blob_destination(storage_account, container, sas_token,
                              blob_prefix, auth_mode, operation,
                              sas_max_hours):
    mode = _effective_auth_mode(auth_mode, sas_token)
    if mode == AZCOPY_AUTH_SAS:
        ok, detail = validate_sas_token(
            sas_token, operation=operation,
            max_remaining_hours=sas_max_hours,
        )
        if not ok:
            raise ValueError(detail)
        return mode, build_blob_url(
            storage_account, container, sas_token, blob_prefix=blob_prefix
        )
    if sas_token:
        raise ValueError(
            "A SAS token was supplied while an Entra authentication mode was "
            "selected; refusing to place it in the AzCopy command line."
        )
    return mode, build_blob_url(
        storage_account, container, None, blob_prefix=blob_prefix
    )


def _append_source_wildcard(source_path):
    r"""
    Append a trailing "\*" (or "/*") to a directory source path so azcopy
    copies/syncs the CONTENTS of that directory rather than nesting the
    directory itself under the destination. Idempotent -- a path already
    ending in "*" is returned unchanged.
    """
    if not source_path:
        return source_path
    if source_path.endswith("*"):
        return source_path
    sep = "/" if "/" in source_path and "\\" not in source_path else "\\"
    trimmed = source_path.rstrip("\\/")
    return trimmed + sep + "*"


# ==========================================================================
# Date filter translation -- DATED-DUMP PATH ONLY
# ==========================================================================
def maxage_days_to_include_after(maxage_days):
    """
    Convert a robocopy-style /MAXAGE day count into the UTC ISO8601
    timestamp azcopy's --include-after expects. Not used by the mirror
    (sync) path, which has no equivalent flag.
    """
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=int(maxage_days)))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


# ==========================================================================
# azcopy invocation -- DATED DUMP (copy), single path
# ==========================================================================
def run_azcopy_upload(azcopy_path, source_path, storage_account, container,
                      sas_token=None, blob_prefix=None, include_after=None,
                      logger=None, extra_args=None, auth_mode=None,
                      managed_identity_client_id=None,
                      sas_max_hours=DEFAULT_SAS_MAX_HOURS):
    r"""
    Run one AzCopy copy operation.

    *** SAS SECRECY: `url`, `cmd`, `stdout`, `stderr`, and every exception
    string in this function are sanitized via sanitize_azcopy_output()
    (aliased here as _redact_sas) before being logged or returned. The raw
    `sas_token` value itself is passed ONLY to build_blob_url() (to
    construct the one URL argument AzCopy requires it in) and to
    sanitize_azcopy_output() (so it knows the exact value to strip out) --
    never to a logger, print(), or return value directly. ***
    """
    wildcard_source = _append_source_wildcard(source_path)
    try:
        mode, url = _prepare_blob_destination(
            storage_account, container, sas_token, blob_prefix, auth_mode,
            "copy", sas_max_hours,
        )
    except Exception as exc:
        detail = sanitize_azcopy_output(str(exc), sas_token=sas_token)
        if logger is not None:
            logger.error("  [azcopy copy] destination/auth validation failed: %s",
                         detail)
        return -1, "", detail

    safe_url = _redact_sas(url, sas_token=sas_token)
    cmd = [azcopy_path, "copy", wildcard_source, url, "--recursive=true"]
    if include_after:
        cmd.append(f"--include-after={include_after}")
    if extra_args:
        cmd.extend(extra_args)
    safe_cmd = [sanitize_azcopy_output(arg, sas_token=sas_token)
                for arg in cmd]

    if logger is not None:
        logger.info("  [azcopy copy] uploading: %s", wildcard_source)
        logger.info("  [azcopy copy]        to: %s", safe_url)
        logger.info("  [azcopy copy] auth mode: %s", mode)
        if include_after:
            logger.info("  [azcopy copy]  filter  : --include-after=%s",
                        include_after)
        logger.debug("  [azcopy copy] command: %s", " ".join(safe_cmd))

    try:
        result = _run_azcopy_subprocess(
            cmd,
            auth_mode=mode,
            managed_identity_client_id=managed_identity_client_id,
        )
        code = result.returncode
        stdout = sanitize_azcopy_output(result.stdout or "", sas_token)
        stderr = sanitize_azcopy_output(result.stderr or "", sas_token)
    except Exception as exc:
        detail = sanitize_azcopy_output(str(exc), sas_token)
        if logger is not None:
            logger.error("  [azcopy copy] could not launch azcopy for %s "
                         "-> %s: %s", wildcard_source, safe_url, detail)
        return -1, "", detail

    if logger is not None:
        if code != 0:
            logger.warning("  [azcopy copy] exit code %d (failure) for "
                           "%s -> %s", code, wildcard_source, safe_url)
            if stderr.strip():
                logger.warning("  [azcopy copy] stderr: %s", stderr.strip())
            if stdout.strip():
                logger.debug("  [azcopy copy] stdout: %s", stdout.strip())
        else:
            logger.info("  [azcopy copy] exit code %d (success) for %s "
                        "-> %s", code, wildcard_source, safe_url)
    return code, stdout, stderr


# ==========================================================================
# "copy group" -- DATED DUMP, the Blob-mode sibling of
# robocopy_utils.copy_group()
# ==========================================================================
def copy_group_to_blob(category, server_short_name, domain_suffix,
                       path_pairs, storage_account, container, sas_token,
                       azcopy_path, run_timestamp, include_after=None,
                       logger=None, auth_mode=None,
                       managed_identity_client_id=None,
                       sas_max_hours=DEFAULT_SAS_MAX_HOURS):
    r"""
    For ONE machine, uploads each relative path in `path_pairs` from its
    UNC source (\\<server>.<domain>\D$\<relpath>) to
    <run_timestamp>/<category>/<fqdn>/<relpath> in the Blob container, via
    one `azcopy copy` call per path. run_timestamp isolates each dump into
    its own folder. Returns one exit code per path, in order.
    """
    fqdn = f"{server_short_name}.{domain_suffix}"

    if logger is not None:
        logger.info("-------------------------------------------------------")
        logger.info("%s - %s  (Blob dated-dump upload)", category,
                   server_short_name)
        logger.info("-------------------------------------------------------")

    codes = []
    for relpath in path_pairs:
        source = f"\\\\{fqdn}\\D$\\{relpath}"
        blob_prefix = posixpath.join(run_timestamp, category, fqdn,
                                     relpath.replace("\\", "/"))
        code, _stdout, _stderr = run_azcopy_upload(
            azcopy_path, source, storage_account, container, sas_token,
            blob_prefix=blob_prefix, include_after=include_after,
            logger=logger, auth_mode=auth_mode,
            managed_identity_client_id=managed_identity_client_id,
            sas_max_hours=sas_max_hours)
        codes.append(code)
    return codes


# ==========================================================================
# azcopy invocation -- STANDING MIRROR (sync), single path
# ==========================================================================
def run_azcopy_sync(azcopy_path, source_path, storage_account, container,
                    sas_token=None, blob_prefix=None, logger=None,
                    extra_args=None, auth_mode=None,
                    managed_identity_client_id=None,
                    sas_max_hours=DEFAULT_SAS_MAX_HOURS):
    r"""
    Run one additive-only AzCopy sync.

    *** SAS SECRECY: same guarantees as run_azcopy_upload() above -- every
    logged/returned string is passed through sanitize_azcopy_output()
    first; the raw sas_token is used only to build the URL and to drive
    that sanitization, never logged/returned directly. ***
    """
    wildcard_source = _append_source_wildcard(source_path)
    try:
        mode, url = _prepare_blob_destination(
            storage_account, container, sas_token, blob_prefix, auth_mode,
            "sync", sas_max_hours,
        )
    except Exception as exc:
        detail = sanitize_azcopy_output(str(exc), sas_token=sas_token)
        if logger is not None:
            logger.error("  [azcopy sync] destination/auth validation failed: %s",
                         detail)
        return -1, "", detail

    safe_url = _redact_sas(url, sas_token=sas_token)
    cmd = [azcopy_path, "sync", wildcard_source, url, "--recursive=true",
           "--delete-destination=false"]
    if extra_args:
        cmd.extend(extra_args)
    safe_cmd = [sanitize_azcopy_output(arg, sas_token=sas_token)
                for arg in cmd]

    if logger is not None:
        logger.info("  [azcopy sync] syncing : %s", wildcard_source)
        logger.info("  [azcopy sync]      to : %s", safe_url)
        logger.info("  [azcopy sync] auth mode: %s", mode)
        logger.debug("  [azcopy sync] command: %s", " ".join(safe_cmd))

    try:
        result = _run_azcopy_subprocess(
            cmd,
            auth_mode=mode,
            managed_identity_client_id=managed_identity_client_id,
        )
        code = result.returncode
        stdout = sanitize_azcopy_output(result.stdout or "", sas_token)
        stderr = sanitize_azcopy_output(result.stderr or "", sas_token)
    except Exception as exc:
        detail = sanitize_azcopy_output(str(exc), sas_token)
        if logger is not None:
            logger.error("  [azcopy sync] could not launch azcopy for %s "
                         "-> %s: %s", wildcard_source, safe_url, detail)
        return -1, "", detail

    if logger is not None:
        if code != 0:
            logger.warning("  [azcopy sync] exit code %d (failure) for "
                           "%s -> %s", code, wildcard_source, safe_url)
            if stderr.strip():
                logger.warning("  [azcopy sync] stderr: %s", stderr.strip())
            if stdout.strip():
                logger.debug("  [azcopy sync] stdout: %s", stdout.strip())
        else:
            logger.info("  [azcopy sync] exit code %d (success) for %s "
                        "-> %s", code, wildcard_source, safe_url)
    return code, stdout, stderr


# ==========================================================================
# "copy group" -- STANDING MIRROR, the Blob-mode sibling of
# robocopy_utils.copy_group() for mirror runs
# ==========================================================================
def copy_group_to_blob_mirror(category, server_short_name, domain_suffix,
                              path_pairs, storage_account, container,
                              sas_token, azcopy_path, logger=None,
                              auth_mode=None,
                              managed_identity_client_id=None,
                              sas_max_hours=DEFAULT_SAS_MAX_HOURS):
    r"""
    For ONE machine, syncs each relative path in `path_pairs` from its UNC
    source (\\<server>.<domain>\D$\<relpath>) to
    <category>/<fqdn>/<relpath> in the Blob container, via one
    `azcopy sync` call per path. DELIBERATELY NO run_timestamp segment --
    the prefix must stay stable across nightly runs for sync's comparison
    to skip unchanged files; adding a per-run segment would make every
    run's destination fresh/empty and defeat the point of using sync at
    all. Returns one exit code per path, in order.
    """
    fqdn = f"{server_short_name}.{domain_suffix}"

    if logger is not None:
        logger.info("-------------------------------------------------------")
        logger.info("%s - %s  (Blob mirror sync)", category, server_short_name)
        logger.info("-------------------------------------------------------")

    codes = []
    for relpath in path_pairs:
        source = f"\\\\{fqdn}\\D$\\{relpath}"
        blob_prefix = posixpath.join(category, fqdn, relpath.replace("\\", "/"))
        code, _stdout, _stderr = run_azcopy_sync(
            azcopy_path, source, storage_account, container, sas_token,
            blob_prefix=blob_prefix, logger=logger, auth_mode=auth_mode,
            managed_identity_client_id=managed_identity_client_id,
            sas_max_hours=sas_max_hours)
        codes.append(code)
    return codes


# ==========================================================================
# Connection test (Blob --test-mode) -- shared by both paths
# ==========================================================================
def test_blob_connection(azcopy_path, storage_account, container,
                         sas_token=None, logger=None, auth_mode=None,
                         managed_identity_client_id=None,
                         sas_max_hours=DEFAULT_SAS_MAX_HOURS,
                         operation="copy"):
    """
    Run a read-only AzCopy list check using the selected auth mode.

    *** SAS SECRECY: identical guarantees to run_azcopy_upload()/
    run_azcopy_sync() -- every logged/returned string is sanitized via
    sanitize_azcopy_output() first; sas_token is used only to build the
    URL and to drive that sanitization. ***
    """
    ok, err = validate_azcopy_exe(azcopy_path, logger=logger)
    if not ok:
        return False, err

    try:
        mode, url = _prepare_blob_destination(
            storage_account, container, sas_token, None, auth_mode,
            operation, sas_max_hours,
        )
        env_ok, env_detail = validate_azcopy_auth_environment(
            mode, managed_identity_client_id=managed_identity_client_id
        )
        if not env_ok:
            raise ValueError(env_detail)
    except Exception as exc:
        detail = sanitize_azcopy_output(str(exc), sas_token)
        if logger is not None:
            logger.error("  [azcopy-test] authentication validation failed: %s",
                         detail)
        return False, detail

    safe_url = _redact_sas(url, sas_token=sas_token)
    cmd = [azcopy_path, "list", url, "--machine-readable"]
    safe_cmd = [sanitize_azcopy_output(arg, sas_token=sas_token)
                for arg in cmd]

    if logger is not None:
        logger.info("  [azcopy-test] verifying Blob connection with a "
                    "read-only 'azcopy list' call ...")
        logger.info("  [azcopy-test] account=%s container=%s",
                    storage_account, container)
        logger.info("  [azcopy-test] auth mode: %s", mode)
        logger.info("  [azcopy-test] target: %s", safe_url)
        logger.debug("  [azcopy-test] command: %s", " ".join(safe_cmd))

    try:
        result = _run_azcopy_subprocess(
            cmd,
            timeout=60,
            auth_mode=mode,
            managed_identity_client_id=managed_identity_client_id,
        )
        code = result.returncode
        stdout = sanitize_azcopy_output(result.stdout or "", sas_token).strip()
        stderr = sanitize_azcopy_output(result.stderr or "", sas_token).strip()
    except subprocess.TimeoutExpired:
        msg = "azcopy list timed out after 60s"
        if logger is not None:
            logger.error("  [azcopy-test] %s", msg)
        return False, msg
    except Exception as exc:
        msg = "could not launch azcopy for connection test: " + \
            sanitize_azcopy_output(str(exc), sas_token)
        if logger is not None:
            logger.error("  [azcopy-test] %s", msg)
        return False, msg

    if code != 0:
        detail = (f"azcopy list exited {code}"
                  + (f" -- stderr: {stderr}" if stderr else ""))
        detail = sanitize_azcopy_output(detail, sas_token)
        if logger is not None:
            logger.error("  [azcopy-test] connection test FAILED: %s", detail)
            if stdout:
                logger.warning("  [azcopy-test] stdout (truncated): %s",
                               stdout[:500])
        return False, detail

    line_count = stdout.count("\n") + (1 if stdout else 0)
    if logger is not None:
        logger.info("  [azcopy-test] connection test SUCCEEDED (exit code "
                    "0). Account/container resolved and authentication mode "
                    "%s was authorized. Container currently holds "
                    "approximately %d blob(s) (listing not printed).",
                    mode, line_count)
    return True, f"connection OK ({mode})"