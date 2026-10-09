# PORTAL_STACK_LOG_DUMP.py

## Table of contents

**For Report Readers**
- [Overview](#overview)
- [Why we collect logs from the whole stack](#why-we-collect-logs-from-the-whole-stack)
- [Why missing logs are a problem](#why-missing-logs-are-a-problem)
- [Why this tool never deletes anything](#why-this-tool-never-deletes-anything)
- [Recommendations for investigation](#recommendations-for-investigation)
- [Column descriptions](#column-descriptions)

**For Administrators**
- [Requirements](#requirements)
- [Dependencies on the shared UTILITIES package](#dependencies-on-the-shared-utilities-package)
- [Security: TLS certificate verification](#security-tls-certificate-verification)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Test mode (default)](#test-mode-default)
- [Usage](#usage)
- [Command-line options](#command-line-options)
- [Performance](#performance)
- [Observed Runtime](#observed-runtime)
- [Output file](#output-file)
- [Exit codes](#exit-codes)
- [Notes and caveats](#notes-and-caveats)
- [Setting up Azure Blob login (SAS token) — plain language guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)

---

# For Report Readers

## Overview

`PORTAL_STACK_LOG_DUMP.py` is a log collection tool for an Esri ArcGIS
Portal deployment. Rather than producing a spreadsheet of findings, this
tool's "output" is a copy of the deployment's own log files, gathered
automatically from every machine currently participating in the stack:

- **Portal** (primary/standby)
- **Hosting Server** (the ArcGIS Server site federated as the hosting server)
- **Data Store** (the relational database used by hosted services,
  primary/standby)
- **Federated Servers** (any other ArcGIS Server site connected to Portal,
  grouped by site)

The tool automatically figures out where each machine actually stores its
logs, then copies those logs to a safe, central location — either a local
network folder or an Azure Blob Storage container — so they're available
later if someone needs to investigate a problem.

## Why we collect logs from the whole stack

A single "Portal problem" reported by a user can actually originate in any
one of several places: Portal itself, the hosting server, the database
behind hosted data, or a federated server elsewhere in the deployment.
Collecting logs from **all** of these machines in one run — rather than
guessing which one to check first — means an investigator has everything
they need in one place from the start, without having to remote into each
machine individually.

## Why missing logs are a problem

Log files on these machines don't live forever. Most are rolled off
(deleted or overwritten) automatically after a set retention window —
often 30 days or less. If an issue is reported after that window has
passed, the evidence needed to diagnose it may simply be gone.

Running this tool regularly (especially as a nightly "mirror" job — see
below) means:

- Investigators are not racing a retention clock when a problem is
  reported days or weeks after it happened.
- Logs from every machine in the deployment can be searched together
  instead of one machine at a time.
- A copy of the logs survives even if the original machine is later
  rebuilt, decommissioned, or otherwise made inaccessible.

## Why this tool never deletes anything

This tool is intentionally **additive only** — in every mode, and to
either destination, it only ever adds files. It never deletes a file it
previously copied, and it never deletes anything at the source. This means
it is always safe to run, and safe to schedule to run repeatedly, without
any risk of losing previously collected evidence.

## Recommendations for investigation

- **Start with the nightly mirror output** if one is already running — it
  will have the most complete, up-to-date set of logs across the entire
  deployment, without needing to remember which portion to ask for.
- **Ask for a shorter, recent window** (a "dated dump" covering the last
  few days) when you already know roughly when an issue started — this
  produces a smaller, easier-to-search set of files than the full mirror.
- **Narrow to one part of the deployment** (Portal, Hosting, Data Store, or
  the other federated servers) when the problem is clearly isolated to one
  tier — this keeps the collected files focused and the investigation
  faster.
- **Prefer a local/network folder copy when you need the very latest
  activity** and have direct network access to the affected machines — it
  can capture files that are still actively being written to, which a
  cloud copy cannot (see below).
- **Prefer a cloud (Blob) copy when you don't have direct network access**
  to the machines, need to share the results with a remote team, or want a
  long-term, durable archive outside of local disk space.
- **If a specific recent log seems to be missing from a cloud copy, check
  whether it's still open/actively being written to** on the source
  machine before concluding the evidence doesn't exist — see the caveats
  below.
- **Check the run's debug summary** for any entries marked `SKIPPED` — a
  log path that could not be automatically located was *not* copied, and
  that gap should be ruled out before concluding a log doesn't contain
  relevant information.
- **Use the printed inventory report** at the start of every run (which
  labels each machine by its role, e.g. "PORTAL-PRIMARY",
  "DATASTORE-STANDBY") to map a folder of collected logs back to the
  machine's actual function in the deployment.

## Column descriptions

This tool does not produce a row-and-column report. Instead, at the start
of every run it prints (and logs) a **server inventory** and a **copy
plan**. The meaningful fields in that output are:

| Field | What it means |
|---|---|
| Machine FQDN | The full network name of a machine participating in the deployment. |
| Role | The machine's function, e.g. `PORTAL-PRIMARY`, `PORTAL-STANDBY`, `SITE-HOSTING`, `DATASTORE-PRIMARY`, `DATASTORE-STANDBY`, or `SITE-<name>` for another federated server. |
| Category | Which part of the deployment a machine belongs to for copy purposes: `Portal`, `Hosting`, `DataStore`, or a named federated site. |
| data-log / install-log | The two kinds of log location checked per machine — the product's own configured log folder, and its install/framework (Tomcat) log folder. |
| source | Where a given log folder was found on the source machine (only shown outside test mode). |
| dest / blob prefix | Where that log folder's contents will be copied to — a local/network folder path, or a redacted Azure Blob Storage location. |
| SKIPPED | The tool could not automatically determine this log location for this machine, so nothing was copied for it — this is reported clearly rather than guessed. |

---

# For Administrators

## Requirements

- Python 3, using only the following directly in this script:
  - `argparse`, `logging`, `os`, `sys`, `threading` (standard library)
  - `psutil` (optional — enables the child-process check at exit; the
    script degrades gracefully and logs a notice if it isn't installed)
- `robocopy` (Windows, File destination) and/or `azcopy.exe` (Blob
  destination), the latter resolved via
  `UTILITIES.azcopy_utils.get_azcopy_path`.
- Network/UNC access (`\\<fqdn>\D$\<relpath>`) from the machine running the
  script to every discovered machine, for whichever categories are in
  scope.
- A valid Portal admin/owner account credential retrievable via the
  configured `keyring-service` (see Configuration).
- For Blob destinations: an Azure Storage account and container already
  provisioned, with a SAS token retrievable via
  `UTILITIES.azcopy_utils.get_blob_sas_token`. See [Setting up Azure Blob
  login (SAS token) — plain language
  guide](#setting-up-azure-blob-login-sas-token--plain-language-guide) at
  the bottom of this document for a complete, step-by-step walkthrough.

## Dependencies on the shared UTILITIES package

This script contains no standalone logic for configuration, authentication,
discovery, TLS verification, or copying — all of that is delegated to the
shared `UTILITIES` package, which must be importable at runtime. The script
bootstraps `sys.path` itself (`_bootstrap_utilities_path`) by walking up to
4 parent directories from its own location looking for a `UTILITIES\`
folder, so it can live in a subfolder without `UTILITIES` needing to be at
the same level.

Per the project convention, **all code in this project — including this
script and any newly created helper files — must reference the shared
`.\UTILITIES` folder** rather than duplicating logic locally. The specific
`UTILITIES` modules and symbols this script depends on:

| Module | Symbols used |
|---|---|
| `UTILITIES` (`__init__.py`) | `add_config_arg`, `add_common_portal_args`, `add_output_dir_arg`, `finalize_config`, `setup_logging`, `connect_gis`, `build_timestamp`, `start_timer`, `timing_block`, `provider_for_gis`, `VerifyMode`, `build_secure_session`, `verify_tls_handshake` |
| `UTILITIES.config` | `get_config_value` |
| `UTILITIES.stack_discovery` | `discover_stack`, `short_hostname`, `looks_like_fqdn` |
| `UTILITIES.path_discovery` | `resolve_log_paths` |
| `UTILITIES.robocopy_utils` | `prompt_date_filter`, `date_filter_from_args`, `build_date_filter_opts`, `copy_group`, `ROBOCOPY_SUCCESS_MAX` |
| `UTILITIES.azcopy_utils` | `get_azcopy_path`, `get_blob_sas_token`, `normalize_azcopy_auth_mode`, `validate_azcopy_auth_environment`, `validate_sas_token`, `validate_azcopy_exe`, `maxage_days_to_include_after`, `copy_group_to_blob`, `copy_group_to_blob_mirror`, `test_blob_connection` |

Other files in the project's `UTILITIES` folder — `bucketing.py`, `cli.py`,
`config.py`, `emailer.py`, `gis_connect.py`, `logging_setup.py`,
`restutils.py` (which folds in the former `platform_detect.py`,
`timestamps.py`, and `runtime.py`), `secure_session.py`, `auth.py`, and the
per-org `*.config.toml` files — support the package as a whole (CLI
argument helpers, GIS connection logic, logging setup, timestamp
formatting, platform detection, config parsing, TLS verification, token
handling, and email notifications). Any new helper module added to this
project should likewise be placed under `.\UTILITIES` and imported the
same way this script imports its dependencies, so every script in the
project continues to share one authoritative implementation of
cross-cutting concerns.

## Security: TLS certificate verification

This script never disables certificate verification. Two independent
connections are involved, and both are always verified:

- **This script's own direct discovery requests** (used to query stack
  topology and per-machine log paths) go through
  `UTILITIES.secure_session.build_secure_session()`, using an explicit,
  caller-chosen strategy selected with `--tls-mode`:
  - `standard` (default) — verifies against certifi's bundled public CA
    list. Correct for any Portal with a certificate from a public CA.
  - `os-trust-store` — verifies against the operating system's own
    certificate store (requires the `truststore` package). Use this for an
    internal Enterprise Portal signed by an organizational CA that is
    already trusted at the OS level.
  - `custom-ca-bundle` — verifies against an explicit PEM file supplied via
    `--ca-bundle-path`.
  A one-time TLS handshake check (`verify_tls_handshake`) runs at startup,
  before any real API traffic, and the run aborts immediately if that
  check fails.
- **The arcgis SDK's own connection** (established via
  `UTILITIES.gis_connect.connect_gis()`) always verifies TLS as well,
  independent of `--tls-mode`, and is passed the same CA bundle path when
  `--tls-mode custom-ca-bundle` is used, so both connections trust the
  same certificate authority.

There is no flag or configuration setting anywhere in this script that
disables certificate checking.

## Configuration

Configuration is loaded from a TOML file (passed via `--config`) and
resolved per-target through `UTILITIES.config.get_config_value`, which
checks the `[portal]` section (this script always targets `"portal"`) and
falls back to a `[common]` section.

**Required settings** (validated by `finalize_config` against
`REQUIRED_SETTINGS`):

- `portal-url`
- `username`
- `keyring-service`
- `output-dir`

**Blob-destination settings** (only required when `--destination blob` is
used; keys differ by `--sync-mode`):

| Sync mode | Container key | Account key |
|---|---|---|
| Dated dump (no `--sync-mode`) | `logdump-blob-container` | `logdump-blob-account` |
| Mirror (`--sync-mode mirror`) | `logmirror-blob-container` | `logmirror-blob-account` |

Using separate keys for dump vs. mirror allows the two run types to target
different containers (e.g. a short-lived "dump" container for ad hoc
investigations vs. a long-lived "mirror" container for the standing nightly
job) — or the same container/account, if the config simply repeats the same
values under both key pairs.

**Blob authentication settings:**

| Setting | CLI override | What it does |
|---|---|---|
| `azcopy-auth-mode` | `--blob-auth-mode` | One of `managed-identity`, `workload-identity`, `azure-cli`, or `sas`. **This project's deployments use `sas`.** Defaults to `managed-identity` if left unset anywhere, so `--blob-auth-mode sas` (or an `azcopy-auth-mode = "sas"` line in the toml) must be set explicitly for SAS-based runs. |
| `azcopy-managed-identity-client-id` | `--managed-identity-client-id` | Only used in `managed-identity` mode with a user-assigned identity; not relevant to SAS. |
| `azcopy-sas-max-hours` | `--sas-max-hours` | Only used in `sas` mode. The longest remaining lifetime a SAS token is allowed to have when this script uses it. Defaults to 24 hours; the hard maximum enforced by the code is 168 hours (7 days) — a longer-lived token is always rejected. See the [plain-language SAS guide](#setting-up-azure-blob-login-sas-token--plain-language-guide) below for the full walkthrough, including what each validation error means. |

**Other configuration-driven values:**

- `--domain-suffix` defaults to `enterprise.com` and is appended to each
  discovered short machine name to reconstruct the FQDN used in UNC source
  paths and Blob prefixes. Override with `--domain-suffix` if the
  deployment uses a different domain.
- `--dest-root` defaults to the config's `output-dir` if omitted
  (`args.dest_root_defaulted` tracks this for logging purposes).

## How it works

1. **Argument parsing & validation** (`parse_args`) — determines Dated Dump
   vs. Mirror, enforces the mutual-exclusivity rules described below, and
   defaults `--scope` to `all` for Mirror runs.
2. **Config resolution** (`finalize_config`) — loads and validates the
   required settings from the TOML config for the `portal` target.
3. **Blob authentication resolution (if `--destination blob`)** — the
   auth mode (`sas`, `managed-identity`, `workload-identity`, or
   `azure-cli`) is resolved from `--blob-auth-mode` or the config, and the
   corresponding environment/credential prerequisites are validated before
   anything else runs.
4. **Destination root setup** — creates a timestamped run folder under
   `--dest-root` (or the config's `output-dir` if `--dest-root` was
   omitted); this timestamp is used for the File destination folder name
   and, for a Dated Dump's Blob destination, as the blob prefix. Mirror's
   Blob destination instead uses a stable, non-timestamped folder in the
   container.
5. **Logging setup** — console-only logging is established first, then
   re-initialized to also write to `_debug_summary.txt` inside the run
   folder once the destination path is known.
6. **Blob pre-check (if `--destination blob`)** — container/account keys
   are resolved from config immediately (before discovery even begins) so
   a missing/misconfigured Blob setting fails fast.
7. **Date filter resolution** (`_resolve_effective_filter_info`) — `None`
   for Mirror; for a Dated Dump, either interactively prompted or parsed
   from `--filter-mode`/`--range-start`/`--range-end`. For Blob
   destinations, the filter is further translated into an
   `--include-after` cutoff (`_include_after_for_blob`); a RANGE filter
   under Blob is approximated using only its start date, since azcopy has
   no direct "before" equivalent to pair with it.
8. **Secure session + handshake check** — this script's own discovery
   session is built with the explicit `--tls-mode` strategy, and a one-time
   TLS handshake to the Portal URL is verified before any other network
   call is made (see [Security](#security-tls-certificate-verification)).
9. **GIS connection** (`connect_gis`) — authenticates to the Portal using
   the configured `keyring-service` credential, always with TLS
   verification enabled, producing a session and admin token used for
   subsequent REST calls. **This is a completely separate credential from
   the Blob SAS token** — see the note in [Notes and
   caveats](#notes-and-caveats).
10. **Stack discovery** (`discover_stack`) — queries Portal's federation
    info to enumerate Portal machines (primary/standby), the Hosting Server
    site, registered Data Store items (primary/standby), and every other
    federated ArcGIS Server site. The full topology is printed to the
    console as a human-readable inventory report (`print_stack_report`) and
    logged.
11. **Machine list extraction** — short hostnames are derived per category
    (`_category_machines`) and per federated site
    (`_federated_site_machines`), de-duplicated, with the Hosting site
    explicitly excluded from the "federated" grouping (it's already
    covered under `Hosting`).
12. **Per-machine log path discovery** (`resolve_log_paths`) — for each
    machine, both a "data log" path and an "install log" path are
    self-discovered (no hard-coded fallback); a path that can't be
    discovered is recorded with an error and skipped later, not silently
    dropped.
13. **Scope filtering** (`_filter_plan_by_scope`) — the full plan (every
    category) is filtered down to just the requested `--scope` (or left
    untouched for `all`/Mirror), with the number of excluded entries logged.
14. **Copy plan report** — every machine/category/path combination that
    will (or, due to discovery failure, will not) be copied is logged,
    along with source/destination paths or blob prefixes (SAS tokens are
    always redacted in logs).
15. **Test-mode short-circuit** — if `--test-mode` was passed, the run
    stops here (after an optional Blob connectivity test, including a real
    SAS validation check when `--blob-auth-mode sas` is in effect),
    reporting what *would* happen without transferring data.
16. **Blob connection setup (real run only)** — for `--destination blob`,
    `azcopy.exe` is located/validated and, for SAS mode, a SAS token is
    retrieved from Credential Manager and validated (permissions,
    HTTPS-only, expiry window) before any transfers begin; failure here
    aborts the whole run before any copying starts.
17. **Copy/sync execution** — for each plan entry with at least one usable
    path, the appropriate copy function is called: `copy_group` (File),
    `copy_group_to_blob` (Blob, Dated Dump), or `copy_group_to_blob_mirror`
    (Blob, Mirror). Exit codes from every call are collected.
18. **Result summary** — exit codes are evaluated against
    `ROBOCOPY_SUCCESS_MAX` (File) or checked for non-zero (Blob) to
    determine overall success/failure, logged along with any paths that
    were skipped due to failed discovery.
19. **Clean exit** (`_clean_exit`) — before exiting, the script logs any
    non-main threads or lingering child processes (e.g. an azcopy helper
    that didn't fully terminate) as a diagnostic aid, flushes logging, and
    calls `os._exit()` to force immediate process termination regardless of
    any non-daemon threads still running.

## Test mode (default)

Test mode is not the default execution mode, but it is **strongly
recommended before every new configuration, scheduled task, or unfamiliar
deployment** is run for real. Pass `--test-mode` to:

- Run full stack discovery and print the complete server inventory report.
- Resolve and print the full COPY PLAN (which machines, which paths).
- Resolve the date filter (Dated Dump only) and report what would be
  applied.
- For `--destination blob`: additionally validate the `azcopy.exe` path,
  resolve the container/account from config, retrieve and validate the
  Blob credential for the selected auth mode (for `sas`, this includes the
  full permission/HTTPS/expiry check described in the [plain-language SAS
  guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)),
  and run `test_blob_connection` to confirm connectivity — all without
  transferring any data.

**No robocopy or azcopy call is made in test mode.** Per-path source/
destination lines are suppressed in the console/log output (to keep the
output readable), but the plan summary line still reports how many paths
resolved successfully vs. were skipped due to failed discovery.

Test mode works identically for both run types and both destinations:

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob --blob-auth-mode sas --test-mode
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogDumps --scope portal --filter-mode previous-day --test-mode
```

Use test mode any time you: change `--domain-suffix`, point at a new
Portal/config, add a new federated site, change Blob container/account
settings, change `--blob-auth-mode`, generate a new SAS token, change
`--tls-mode`, or simply want to confirm what *would* happen before it runs
unattended on a schedule.

## Usage

Below are the usage examples from the script's own header, each followed by
the scenario in which it would actually be used.

### 1. Dated dump, File destination, prompted date filter, everything

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root C:\Temp --scope all
```

**Scenario:** An administrator is troubleshooting an issue interactively at
the console and isn't sure yet how far back they need to look. Omitting
`--filter-mode` triggers an interactive prompt for the date range/lookback
at runtime. `--scope all` pulls Portal, Hosting, Data Store, and every
federated site in one pass — appropriate when the root cause hasn't been
isolated to one tier yet. Output lands in a timestamped subfolder under
`C:\Temp`, so repeated ad hoc runs don't overwrite each other.

### 2. Dated dump, File destination, previous 7 days, Portal only

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogDumps --scope portal --filter-mode previous-n-days 7
```

**Scenario:** A Portal-specific symptom has been reported (e.g. login
failures, a licensing warning, a Portal admin API error) and the
administrator already suspects the Portal tier specifically, within
roughly the last week. `--scope portal` skips Hosting/Data Store/federated
discovery entirely for the copy plan, keeping the dump small and fast.
`--filter-mode previous-n-days 7` avoids the interactive prompt, making
this suitable for a documented runbook step.

### 3. Dated dump, File destination, explicit date range, Hosting only

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogDumps --scope hosting --filter-mode --range-start 20260801 --range-end 20260828
```

**Scenario:** A known incident window has already been identified — for
example, a hosting server outage or a specific failed publish job that
occurred sometime between August 1 and August 28. Passing `--filter-mode`
with no value activates RANGE mode, which requires `--range-start` and
`--range-end`. `--scope hosting` narrows to just the Hosting Server
machines, appropriate once the investigation has ruled in that tier
specifically.

### 4. Dated dump, Blob destination, previous-day (7-day lookback), federated sites, SAS auth

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --scope federated --filter-mode previous-day --destination blob --blob-auth-mode sas
```

**Scenario:** One of the federated ArcGIS Server sites (not the hosting
server itself) had a problem reported "yesterday" or within the last
couple of days, and the team wants the logs centralized in Azure Blob
Storage rather than a local disk — for example, because the investigator
doesn't have direct file-share access but does have access to the blob
container, or because the result needs to be shared with a remote team.
`--blob-auth-mode sas` is required explicitly here since SAS is not the
default authentication mode (see
[Configuration](#configuration)). Note that `previous-day` here still
resolves to a **7-day lookback** (`PREVIOUS_DAY_LOOKBACK_DAYS = 7`),
intentionally wider than "1 day," so that a missed nightly run doesn't
silently lose a day's logs before 30-day source retention rolls them off.
`--scope federated` matches every non-Portal, non-Hosting, non-DataStore
site — useful when the issue is known to be in a federated site but not
necessarily which one, since all federated sites are included together.
`--dest-root` is omitted here because it's irrelevant for Blob
destinations.

### 5. Mirror, File destination, nightly

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root D:\LogMirror --sync-mode mirror
```

**Scenario:** This is the "set it and forget it" case — a Windows
Scheduled Task configured to run every night against a local or UNC
destination folder, so that logs from the entire stack (Portal, Hosting,
Data Store, and all federated sites — `--scope` is forced to `all`) are
continuously kept in sync on disk before source retention can roll them
off. No date filter is applied or accepted; `robocopy`'s own default
unchanged-file skip behavior keeps re-transfers minimal on subsequent
nightly runs. This is the recommended baseline job for any deployment
where ongoing historical log retention (beyond the source's own retention
window) is a goal, independent of any specific incident.

### 6. Mirror, Blob destination, nightly, SAS auth

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob --blob-auth-mode sas
```

**Scenario:** The same standing nightly job as example 5, but the
organization wants the long-term log archive in Azure Blob Storage instead
of (or in addition to) a local file share — for example, to centralize
logs from multiple sites/deployments into one cloud location, to take
advantage of Blob lifecycle/retention policies, or because the destination
needs to be reachable from outside the local network. Because a nightly
job runs unattended and SAS tokens expire, this scenario requires either
rotating the stored SAS token before it expires (see the [plain-language
SAS guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)
for how), or requesting a longer allowed lifetime with `--sas-max-hours`
(up to the 168-hour/7-day hard maximum). Internally this uses
`azcopy sync --delete-destination=false`, which compares source vs.
destination itself (rather than relying on a local skip like robocopy) and
never deletes at the destination. A file that is appended to across
multiple days will be re-uploaded in full each time it changes until it
stops changing, at which point the blob copy "settles."

### 7. Test mode (either run type, either destination) — plan + connectivity only

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --sync-mode mirror --destination blob --blob-auth-mode sas --test-mode
```

**Scenario:** Before enabling any new scheduled task, or after changing
config values (a new container, a new domain suffix, a newly federated
site, a freshly rotated SAS token), run the exact same command line you
intend to schedule, with `--test-mode` appended. This confirms discovery
finds the expected machines, the copy plan looks right, and — for Blob —
that the `azcopy.exe` path, container/account config, SAS token (or other
selected auth mode), and network connectivity are all valid, all without
moving a single byte of log data. This example specifically pairs
`--test-mode` with a Mirror/Blob/SAS run, but the same flag works
identically for a Dated Dump, a File destination, or another
`--blob-auth-mode`.

### 8. Non-default TLS verification strategy

```
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --dest-root C:\Temp --scope all --tls-mode os-trust-store
```

**Scenario:** The Portal deployment uses a certificate issued by an
internal/organizational Certificate Authority rather than a public CA.
`--tls-mode os-trust-store` verifies this script's own discovery requests
against the operating system's certificate store (which already trusts
the internal CA), instead of the default certifi bundle, which would
otherwise reject the certificate as untrusted. Requires the `truststore`
package to be installed.

## Command-line options

| Option | Required? | Applies to | Description |
|---|---|---|---|
| `--config` | Yes | Both | Path to the TOML config file (e.g. `ORG.config.toml`). |
| `--dest-root` | No | Both | Root directory for this run's timestamped subfolder. Defaults to the config's `output-dir`. |
| `--destination {file,blob}` | No (default `file`) | Both | `file`: robocopy to `--dest-root`. `blob`: azcopy to Azure Blob Storage. |
| `--storage-account` | No | Blob only | Overrides the `logdump-blob-account`/`logmirror-blob-account` config value. |
| `--container` | No | Blob only | Overrides the `logdump-blob-container`/`logmirror-blob-container` config value. |
| `--blob-auth-mode {managed-identity,workload-identity,azure-cli,sas}` | No (default `managed-identity`) | Blob only | Which Blob authentication method to use. **This project's deployments require `--blob-auth-mode sas` to be passed explicitly** (or set via `azcopy-auth-mode` in the toml), since SAS is not the module's own default. See the [plain-language SAS guide](#setting-up-azure-blob-login-sas-token--plain-language-guide). |
| `--managed-identity-client-id` | No | Blob, `managed-identity` mode only | Client ID for a user-assigned managed identity. Not used in `sas` mode. |
| `--sas-max-hours` | No (default 24) | Blob, `sas` mode only | Longest remaining SAS lifetime this script will accept. Hard maximum: 168 hours (7 days) — a token with a longer expiry is always rejected, regardless of this setting. |
| `--scope {portal,hosting,datastore,federated,all}` | **Required for Dated Dump**; not accepted for Mirror | Dated Dump | Which machine category to copy logs from. |
| `--sync-mode {mirror}` | No | Both | Omit for a Dated Dump. Pass `mirror` for a standing nightly mirror (implies `--scope all`, no date filter). |
| `--filter-mode [MODE [N]]` | No | Dated Dump only | `previous-day`; `previous-n-days N`; or no value (used with `--range-start`/`--range-end`). Omit to be interactively prompted. Not accepted with `--sync-mode mirror`. |
| `--range-start` | Conditional | Dated Dump only | Start date, `YYYYMMDD`. Required if `--filter-mode` is passed with no value. |
| `--range-end` | Conditional | Dated Dump only | End date, `YYYYMMDD`. Required if `--filter-mode` is passed with no value. |
| `--domain-suffix` | No (default `enterprise.com`) | Both | Domain suffix appended to short machine names to build FQDNs. |
| `--test-mode` | No | Both | Print the plan (and run the Blob connection check) without copying/syncing anything. |
| `--tls-mode {standard,os-trust-store,custom-ca-bundle}` | No (default `standard`) | Both | TLS verification strategy for this script's own discovery requests (see [Security](#security-tls-certificate-verification)). The arcgis SDK connection always verifies TLS regardless of this setting. |
| `--ca-bundle-path` | Conditional | Both | Path to a PEM CA bundle file. Required when `--tls-mode custom-ca-bundle` is used; ignored otherwise. |

Also inherited from `UTILITIES` (`add_config_arg`, `add_common_portal_args`,
`add_output_dir_arg`): standard config, Portal connection, and output
directory arguments — see those modules for their individual flags.

**Argument validation notes:**

- If `--sync-mode mirror` is passed together with `--scope` set to anything
  other than `all`, the script errors out immediately (`parser.error`) —
  Mirror runs are always full-stack.
- If `--sync-mode mirror` is passed together with `--filter-mode`,
  `--range-start`, or `--range-end`, the script errors out — Mirror never
  takes a date filter.
- If `--sync-mode` is omitted (Dated Dump) and `--scope` is not given, the
  script errors out — scope is mandatory for a Dated Dump.
- If `--tls-mode custom-ca-bundle` is passed without `--ca-bundle-path`,
  the script errors out.
- If `--blob-auth-mode sas` is selected and the stored SAS token fails
  validation (wrong permissions, not HTTPS-only, or expires outside the
  allowed window), the run aborts before any azcopy call is made — see the
  [plain-language SAS guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)
  for what each validation message means and how to fix it.

## Performance

Runtime is driven primarily by:

- The number of machines discovered in the stack (Portal, Hosting, Data
  Store, and each federated site add machines linearly).
- The size and count of log files under each discovered path.
- Network throughput between the machine running the script and each
  remote machine's UNC share (File) or to Azure (Blob).
- Whether a date filter narrows the file set (Dated Dump) or not (Mirror,
  which always considers the full tree, relying on robocopy's/azcopy's own
  unchanged-file skip logic to avoid re-transferring everything).

The script does not parallelize copy calls across machines — each
`category`/`machine` entry in the COPY PLAN is processed in turn, invoking
`copy_group`, `copy_group_to_blob`, or `copy_group_to_blob_mirror`
sequentially. Total runtime scales roughly with the number of plan entries
times the copy time per entry.

## Observed Runtime

> Actual timing depends heavily on deployment size, log volume, and
> network path.

| Run type    | Destination | Scope     | Machines | Observed duration |
|-------------|-------------|-----------|----------|--------------------|
| Dated Dump  | File        | all       |          |                    |
| Dated Dump  | File        | portal    |          |                    |
| Dated Dump  | Blob        | federated |          |                    |
| Mirror      | File        | all       |          |                    |
| Mirror      | Blob        | all       |          |                    |

The `timing_block()` helper (from `UTILITIES`) logs a wall-clock and
perf-counter timing summary at the end of every run — use this as the
authoritative source for populating the table above rather than
stopwatching it externally.

## Output file

This script does not write a CSV or other row-based report. Its outputs
are:

- **A copy of collected log files**, located either under the run's
  timestamped folder on disk (File destination) or under a blob prefix in
  Azure Storage (Blob destination) — see [Column
  descriptions](#column-descriptions) for how paths and prefixes are
  labeled in the console/log output.
- **A debug summary log file** (`_debug_summary.txt`), written inside the
  run's destination folder, containing the full console output for the
  run — the server inventory report, the copy plan (including any
  `SKIPPED` entries), and the final result summary. This file is printed
  to the console again at the end of every run.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. Either the run completed with all copy/sync calls succeeding, test mode completed cleanly (including a passed Blob connection test, if applicable), or the copy plan was empty and there was nothing to copy. |
| `1` | Failure. Causes include: a config/settings resolution error caught during filter resolution; a failed TLS handshake verification to the Portal URL; a failed Blob authentication-environment/container/account/SAS-token resolution or validation (either pre-check or the real pre-copy check); a failed Blob connection test in test mode; or one or more robocopy/azcopy calls reporting a failure exit code during the real run. |
| `2` | Argument-parsing error raised directly via `SystemExit(2)` — e.g. `--filter-mode previous-n-days` given without a day count, an invalid integer day count, an unrecognized `--filter-mode` value, `--filter-mode` with no value but missing `--range-start`/`--range-end`, an invalid `--blob-auth-mode`/`--sas-max-hours` value, or `--tls-mode custom-ca-bundle` without `--ca-bundle-path`. Distinct from `argparse`'s own usage errors (invalid choice, missing required arg), which `argparse` itself reports with its own standard exit behavior via `parser.error()`. |

A non-zero robocopy exit code that is **still ≤ `ROBOCOPY_SUCCESS_MAX`** is
not treated as a failure — robocopy uses low positive exit codes to
indicate benign conditions (e.g. files copied, extra files present), not
just 0-for-success. Similarly, an azcopy job reporting
`CompletedWithErrors` due to a small number of locked/actively-written
files is logged as a warning, not necessarily treated as reason to believe
the whole run failed — the per-job output above the summary line should be
checked for the specific cause.

## Notes and caveats

- **This script never deletes anything at the destination**, in either run
  type or either destination. Mirror's Blob mode explicitly uses
  `azcopy sync --delete-destination=false`.
- **No hard-coded log path fallback.** If `path_discovery.py` cannot
  self-discover a machine's data-log or install-log path, that path is
  skipped and reported as an error — it is not silently guessed at using a
  default path.
- **Mirror always covers every category.** Passing `--scope` with Mirror is
  only accepted if the value is `all` (or omitted); any other value is a
  hard argument error.
- **`previous-day` is a misnomer for a 7-day lookback**, by design
  (`PREVIOUS_DAY_LOOKBACK_DAYS = 7`), specifically to tolerate a missed
  scheduled run without losing a day of logs to source-side retention
  rollover. Don't assume it means literally "yesterday only."
- **A Date Range filter under `--destination blob` is only approximated.**
  Because azcopy's `--include-after` has no paired "before" flag, a RANGE
  filter's end date is effectively ignored for Blob — only the start date
  is applied as the cutoff. This is logged as an explicit warning at
  runtime; File destinations do not have this limitation since robocopy
  date filtering supports both bounds.
- **azcopy does not copy files that are open on the source machine.** An
  actively-written log (one still held open by the Portal/Server/Data Store
  process) is skipped on that pass rather than partially copied. For logs
  with a size- or time-based rollover policy, this means the Blob copy of
  that file may not appear until the file is rolled over and closed, which
  can be several days after the log entries were actually written. This is
  distinct from — and in addition to — the "settles once it stops changing"
  behavior described below: a file can't even begin settling in Blob until
  it's closed in the first place. `robocopy` (File destination) has no such
  restriction and can copy a file while it's still open, so File will
  reflect very recent, in-progress log activity that Blob may not show yet.
- **A file that changes daily is fully re-uploaded each time under Blob
  Mirror** (no partial/binary diff support in azcopy sync) — it only
  "settles" (stops being re-uploaded) once it stops changing. This can
  matter for very large, frequently-appended log files under Blob Mirror.
- **The COPY PLAN can legitimately be empty** — for example, requesting
  `--scope datastore` against a deployment with no registered relational
  data stores, or `--scope federated` against a deployment with no
  federated sites beyond the hosting server. An empty plan is not treated
  as an error; the script logs a warning and exits `0` with nothing copied.
- **Credentials come from the OS keyring**, not the config file directly —
  `keyring-service` in the config identifies *which* keyring entry to use,
  via `connect_gis`. The config file itself does not store the password.
- **The Portal login and the Blob SAS token are two completely separate
  credentials**, stored under two completely separate Credential Manager
  entries, and used by two completely different parts of the run. Logging
  into Portal (`connect_gis`, using `keyring-service`/`username` from the
  config's `[portal]` section) happens first and is required for every
  run regardless of destination. The Blob SAS token
  (`get_blob_sas_token`, using the composite
  `azblob:<account>:<container>` key described in the [plain-language SAS
  guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)) is
  only looked up afterward, and only when `--destination blob` with
  `--blob-auth-mode sas` is used. A missing/incorrect Portal credential and
  a missing/incorrect Blob SAS token produce two different error messages
  at two different points in the run — see the cheat sheet at the bottom
  of the SAS guide before assuming the two are related.
- **SAS tokens are always redacted in logs** — blob destination lines are
  logged as `.../<prefix>?<SAS-REDACTED>`, never with the actual token.
  This applies to every log line, every console line, and every error
  message produced anywhere in this script or in
  `UTILITIES.azcopy_utils` — the raw token value is never printed,
  written to a file, or included in an exception message, under any
  circumstance.
- **A SAS token's maximum allowed lifetime is 168 hours (7 days), enforced
  in code, with no override.** A token signed to expire further in the
  future than that — including, notably, a token deliberately generated
  to last for a year — is always rejected before any azcopy call is made.
  This is a deliberate security control, not a bug or an oversight; see
  the [plain-language SAS guide](#setting-up-azure-blob-login-sas-token--plain-language-guide)
  for the reasoning. Organizations that want to avoid periodic SAS
  rotation entirely should use `--blob-auth-mode managed-identity` (or
  `workload-identity`/`azure-cli`) instead, which involve no expiring
  token at all.
- **`os._exit()` is used instead of `sys.exit()`** at every exit point,
  specifically to guarantee immediate process termination even if a
  non-daemon thread or lingering child process (e.g., an azcopy subprocess
  that didn't fully release console handles) would otherwise keep the
  process alive. The diagnostic logging in `_log_lingering_threads` exists
  specifically because this problem was previously observed and traced to
  a child *process*, not a Python thread.
- **`psutil` is optional.** If it isn't installed, the child-process check
  at exit is skipped with an informational log line — this does not affect
  the success/failure of the run itself.

---

# Setting up Azure Blob login (SAS token) — plain language guide
⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️
This script is set up to test with Azure Blob SAS Tokens which are limited by this project to not work after seven days. Production deployment of this script using Blob as the storage destination requires the use of Managed Identity authentication to the storage account, instructions of which are beyond the scope of this project. This code will accept Managed Identity as a parameter. 
⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️⚠️
Unlike Robocopy, AZCopy will not copy currently-open log files. Use of Blob to comply with offline log storage requirements may require the use of Shadow Copy on the destination servers to first create a snapshop then substantial modification of this code to use the Shadow Copy as the source. 

This section explains, in the simplest words possible, how to set up the
secret code (called a **SAS token**) that lets this tool send log files to
Azure Blob Storage. Follow these steps in order. Don't skip any.

## What is a SAS token, really?

Think of a SAS token like a **hotel key card**.

- It lets you into **one specific room** (one storage container), not the
  whole hotel.
- It only lets you do **certain things** — maybe it lets you drop off a
  package and pick up your mail, but it does NOT let you take furniture out
  of the room. (In computer terms: it can let you **write** and **list**
  files, but not **delete** them.)
- It **stops working after a set time** — like a key card that turns off
  after your checkout date. After that, the card is useless, even if
  someone steals it.

This program needs one of these "key cards" to be allowed to copy log files
into your Azure Storage container. You make the key card once, save it in a
safe place on your computer (called **Windows Credential Manager**), and
the program looks it up every time it runs.

## Why the token can't last forever

You might think: "Why not make a key card that never expires? Then I never
have to make a new one."

Here's why that's a bad idea: if that key card is ever lost or copied by
someone who shouldn't have it, they can use it **forever** — for as long as
you let it last. A key card that expires after a week means that even if
someone bad gets a copy of it, it stops working soon. A key card that lasts
one year means someone bad could use it for a whole year without you
knowing.

Because of this, the program will only accept a SAS token that:

- Lasts **24 hours or less** (this is the normal, safe setting), **or**
- Lasts up to **7 days (168 hours)** if you tell the program to allow that
  with a special option (`--sas-max-hours 168`)

**A token good for 1 year, or any time longer than 7 days, will always be
rejected.** The program will not run with a token like that, on purpose,
to keep your logs and your Azure account safe. This is not a bug — it is a
safety rule that was built in on purpose.

If typing in a new token every week sounds like too much work, there is a
different way to log in (called "managed identity") that never needs a
token at all. Ask your IT administrator about it if you want to explore
that option instead.

## Step 1: Make the SAS token in Azure

1. Log in to the **Azure Portal** (portal.azure.com).
2. Find your **Storage Account** (for example: `AZStorageAccountName`).
3. Click into the **Container** you want to use (for example: `logdump`).
4. On the left-hand menu, find and click **Shared access tokens**.
5. Under **Permissions**, check ONLY the boxes you actually need:

   | If you are OTHER_ORGng a... | Check these boxes | Leave these UNCHECKED |
   |---|---|---|
   | One-time or occasional log pickup ("dated dump") | **Write**, **List**, **Create** | Read, Add, Delete, everything else |
   | Nightly automatic backup ("mirror") | **Read**, **Write**, **List**, **Create** | Add, Delete, everything else |

   **This step matters a lot.** If you check extra boxes you don't need
   (like "Add" or "Delete"), the program will refuse to use the token and
   show an error like:

   ```
   SAS validation failed: SAS grants permissions outside the least-privilege policy: a
   ```

   That error means: "this key card can do more than it's supposed to —
   please make a new one with fewer permissions." If you see this error,
   go back to Azure and make a new token with only the boxes from the
   table above checked.

6. Under **Allowed protocols**, choose **HTTPS only**. (Never choose HTTP.)
7. Under **Expiry**, pick a date and time **no more than 24 hours from
   right now** (or up to 7 days if your administrator has set the program
   to allow that). Do NOT pick a date months or years in the future — the
   program will reject it. If you see this error:

   ```
   SAS validation failed: SAS expiry is more than the allowed 24 hours away
   ```

   it means your expiry date was too far away. Go back and make a new
   token with a closer expiry date.

8. Click **Generate SAS token and URL**.
9. Azure will show you TWO different boxes of text. **This is the part
   people get wrong most often — read carefully:**

   | Box name | What's in it | Do you want this? |
   |---|---|---|
   | **Blob SAS token** | Starts with `sv=` and has lots of `&` symbols | ✅ **YES — this is the one you want** |
   | **Blob SAS URL** | Starts with `https://` and has a `?` right before the `sv=` part | ❌ **NO — do not use this one** |

   Copy the **Blob SAS token** box only. It will look something like this
   (this is a fake example, not a real token):

   ```
   sv=2024-11-04&ss=b&srt=co&sp=rwl&spr=https&se=2026-09-08T18:00:00Z&sig=AbCdEf1234567890%2FXyZ%3D
   ```

## Step 2: Save the token on your computer using Windows Credential Manager

Windows has a built-in, locked "safe" for passwords and secret codes called
**Credential Manager**. This program reads your SAS token from that safe
instead of from the settings file, so the secret code is never sitting in
plain text where someone could accidentally see it.

You can put the token into that safe **one of two ways** — pick whichever
is easier for you. Both do the exact same thing.

### Way #1: Using the Windows Credential Manager window (point and click)

1. Click the Windows **Start** button.
2. Type `Credential Manager` and press Enter.
3. Click **Windows Credentials**.
4. Click **Add a generic credential**.
5. A little form pops up with three boxes. Fill them in **exactly** like
   this:

   | Box on the screen | What to type into it |
   |---|---|
   | **Internet or network address** | `azblob:AZStorageAccountName:logdump` |
   | **User name** | `sastoken` |
   | **Password** | Paste your SAS token here (the `sv=...` text from Step 1 — nothing else, no `?`, no `https://`) |

6. Click **OK**. You're done!

**Important — swap in your own storage account and container name.** The
example above uses `AZStorageAccountName` (the account) and `logdump` (the
container). If yours are different, change those two parts. The pattern
is always:

```
azblob:YOUR_STORAGE_ACCOUNT_NAME:YOUR_CONTAINER_NAME
```

**Why does it look like that instead of just the account name?** Because
you might use more than one container with the same storage account (for
example, one called `logdump` for occasional pickups, and another called
`logmirror` for the nightly backup job). Each one needs its OWN entry in
the safe, with its OWN name, so they don't get mixed up. If you only ever
typed the storage account name by itself, Windows wouldn't let you make a
second entry with that same name — it would think you were trying to
replace the first one. Writing it as
`azblob:account:container` keeps each one separate and clearly labeled.

### Way #2: Using a typed command (faster once you're used to it)

If you'd rather type one line into a black command window than click
through several screens, you can do the exact same thing this way.

1. Open **PowerShell** (click Start, type `PowerShell`, press Enter).
2. Copy this line, then change the account name, container name, and SAS
   token to your own real values:

   ```powershell
   python -c "import keyring; keyring.set_password('azblob:AZStorageAccountName:logdump', 'sastoken', 'sv=2024-11-04&ss=b&srt=co&sp=rwl&spr=https&se=2026-09-08T18:00:00Z&sig=AbCdEf1234567890%2FXyZ%3D')"
   ```

3. Paste it into PowerShell and press Enter.

**Very important:** keep the SAS token wrapped in single quotes `' '` like
in the example. The `&` symbol shows up a lot in a SAS token, and to
PowerShell, `&` normally means "stop, run a new command here" — the single
quotes tell PowerShell "no, this whole thing is just text, don't break it
apart." If you forget the quotes, only part of your token will get saved,
and the program won't be able to log in.

### Setting up a SECOND container (like a nightly mirror)

If you also use a nightly "mirror" job that saves to a different container
(for example, `logmirror` instead of `logdump`), repeat Step 2 again with
the new container name. You will end up with two separate, clearly labeled
entries in your safe:

| Internet or network address | User name | Password |
|---|---|---|
| `azblob:AZStorageAccountName:logdump` | `sastoken` | (the SAS token for `logdump`) |
| `azblob:AZStorageAccountName:logmirror` | `sastoken` | (the SAS token for `logmirror`) |

Each one has its own token, and each token should only have the
permissions that job actually needs (see the table back in Step 1).

## Step 3: Check your work with Test Mode before OTHER_ORGng a real run

Before you trust this for real, always do a **practice run** first. Add
`--test-mode` to the end of your command. This checks everything —
including looking up your SAS token and making sure Azure accepts it —
WITHOUT actually copying any files. It's completely safe to run as many
times as you want.

Example — a practice run for the Hosting server logs, going to Blob
storage, using the token you just saved:

```powershell
python PORTAL_STACK_LOG_DUMP.py --config ORG.config.toml --scope hosting --filter-mode previous-day --destination blob --blob-auth-mode sas --test-mode
```

If everything is set up right, near the bottom of the output you'll see
something like:

```
Blob connection test PASSED: connection OK (sas)
```

That means your token is saved correctly, has the right permissions, and
hasn't expired. You're ready to run it for real — just remove
`--test-mode` from the end of the command.

## What each error message actually means (cheat sheet)

| If you see this message... | What it really means | What to do |
|---|---|---|
| `No password found in keyring for service 'PORTAL' and username 'scheduledtask'` | This is about logging into **Portal itself**, not Blob. The Portal password hasn't been saved in Credential Manager on this computer yet. | Save the Portal password using the `PORTAL` / `scheduledtask` service+username pair (see your Portal setup instructions — this is separate from the Blob token). |
| `no credential found in Windows Credential Manager for Internet or network address='azblob:...'` | The program looked in the safe for your Blob token and didn't find it. | Go back to Step 2 above and save it, making sure the account/container name matches exactly. |
| `SAS grants permissions outside the least-privilege policy: a` | Your token has a permission checked that it shouldn't (in this example, "Add"). | Go back to Azure, make a new token, and only check the boxes listed in Step 1's table. |
| `SAS expiry is more than the allowed 24 hours away` | Your token's expiration date is too far in the future. | Make a new token with an expiration date within the next 24 hours (or ask about the 7-day option). |
| `SAS must be HTTPS-only` | Your token allows insecure (HTTP) connections. | Make a new token and choose **HTTPS only** under Allowed protocols. |
| `SAS expires too soon` | Your token is about to expire in the next few minutes — there's not enough time left to safely use it. | Make a fresh token with more time left on it. |
| The retrieved credential "does NOT look like a well-formed Blob SAS query string" | You may have pasted the wrong box from Azure (the full **URL** instead of just the **token**), or pasted it with a typo. | Go back to Azure, copy only the **Blob SAS token** box (not the **Blob SAS URL** box), and save it again. |

## Quick summary — the whole process in five sentences

1. In Azure, make a SAS token for your container, checking only the
   permission boxes you need, HTTPS only, expiring within 24 hours (or 7
   days if allowed).
2. Copy only the **token** text (starts with `sv=`), not the full URL.
3. Save it in Windows Credential Manager under the address
   `azblob:YOUR_ACCOUNT:YOUR_CONTAINER`, username `sastoken`, and the
   token as the password.
4. Run the program once with `--test-mode` to make sure it works.
5. When you see it pass, run the real command (without `--test-mode`) to
   copy your logs.