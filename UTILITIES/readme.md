# UTILITIES

Shared helper package for the `PORTAL_*` item-management scripts. Every `PORTAL_*` script imports from this package (located at `.\UTILITIES`) so that connection, logging, email, timestamping, platform labeling, bucketed search, run scaffolding, configuration, checkpointing, TLS verification, and deployment/topology discovery are defined **once** and reused everywhere. Any new helper file added to the project also lives in `.\UTILITIES` and is referenced from there.

The package is importable as a whole (`from UTILITIES import ...`) for most helpers — see the [`__init__.py`](#__init__py) section at the bottom for the full re-exported public surface, and the **not re-exported** note directly below it for the modules you must import directly. 

---

## Table of contents

- [Config Files (Organization-Specific)](#config-files-organization-specific)
- [Running multiple organizations from Scheduled Tasks](#running-multiple-organizations-from-scheduled-tasks)
- [config.py](#configpy) — multi-org loader / resolver for an org config toml
- [cli.py](#clipy) — shared argparse building blocks + multi-org config merge
- [gis_connect.py](#gis_connectpy) — AGOL / Portal connection (with hardened TLS)
- [secure_session.py](#secure_sessionpy) — explicit TLS/SSL verification for a script's own `requests.Session`
- [auth.py](#authpy) — token wrapping (`TokenProvider`) + text-level `redact_sensitive_data()` safety net
- [logging_setup.py](#logging_setuppy) — console + file logging, with automatic token redaction
- [restutils.py](#restutilspy) — REST / HTTP helpers, platform label, timestamp, run timing
- [bucketing.py](#bucketingpy) — date-range bucketed search (>10,000 items)
- [arcgis_rest.py](#arcgis_restpy) — raw-REST attachment/layer scan + batched item metadata / owner resolution
- [emailer.py](#emailerpy) — SMTP email sender + output dir / CSV / attachment-gating scaffolding
- [checkpoint.py](#checkpointpy) — per-service checkpoint/resume for long scans
- [exclusions.py](#exclusionspy) — shared "exclude this group's items" filtering
- [path_discovery.py](#path_discoverypy) — self-discovery of per-machine install/log directories
- [stack_discovery.py](#stack_discoverypy) — Portal/Server/Data Store deployment topology discovery
- [robocopy_utils.py](#robocopy_utilspy) — date-filter selection + robocopy invocation (local file log dumps)
- [azcopy_utils.py](#azcopy_utilspy) — Azure Blob upload (dated dump + standing mirror sync)
- [datastore_config.py](#datastore_configpy) — Data Store / hosting-server key resolution
- [\_\_init\_\_.py](#__init__py) — package exports

---

## Config Files (Organization-Specific)

Each organization has its **own** TOML configuration file. Examples provided in this project:

- **ORG.config.toml** — National Park Service configuration (primary example)
- **ORGTRAIN.config.toml** — ORG training/test environment configuration
- **OTHER_ORGLDT.config.toml** — OTHER_ORG LDT (Limited Deployment Testing) configuration

Each org config uses the same `[common]` / `[portal]` / `[agol]` layout but with organization-specific values for URLs, usernames, credentials, and output directories. See [ORG.config.toml](#ORGconfigtoml-org-config) section below for the complete reference.

---

## ORG.config.toml (org config)

**MULTI-ORG.** There is no single fixed `config.toml` anymore. Each organization has its **own** toml file (e.g. `ORG.config.toml`, `OTHER_ORG.config.toml`, `org2.config.toml`, ...), and every script is told which one to use via the **required** `--config <path>` argument (see `cli.add_config_arg()`). `config.py` has no default/implicit path — `load_config(None)` raises `ValueError`, and a nonexistent path raises `FileNotFoundError`.

Each org's toml uses the same `[common]` / `[portal]` / `[agol]` layout.

### How target selection works

There is no "active" key in the file. The **target is chosen at run time** with the required `--target` argument:

```bash
python SCRIPT.py --config ORG.config.toml --target portal   # reads [portal] + [common]
python SCRIPT.py --config ORG.config.toml --target agol      # reads [agol]   + [common]
```

### Precedence (highest wins)

1. **Command-line argument** (e.g. `--portal-url ...`)
2. The matching value in the **chosen `--target` section** (`[portal]` or `[agol]`)
3. The value in **`[common]`**

### The `[common]` section

| Setting | Example value | What it does |
|---|---|---|
| `org-name` | `"ORG"` | **Mandatory.** Short label for this organization. `finalize_config()` resolves this into `args.org_name` (raw, human-readable — used in email subjects/bodies) and `args.org_name_safe` (filename-safe, via `config.sanitize_org_name()` — used to prefix output files and checkpoint file names). Missing/blank `org-name` is a hard `SystemExit(2)`. |
| `output-dir` | `"C:\\temp\\reports"` | Directory where each script writes its timestamped output CSV (and checkpoint files). Overridable with `--output-dir`. |
| `smtp-server` | `"smtp.server.com"` | SMTP relay hostname for the completion email. Combined with the fixed port `25` in `emailer.py`. Overridable with `--smtp-server`. |
| `email-to` | `"some@mail.com, person@mail.com"` | Recipient(s). Single address or comma/semicolon-separated list; every address is delivered to. Overridable with `--email-to`. Parsed by `emailer.normalize_recipients()`. |
| `email-from` | `"some@mail.com"` | Sender address (single). Overridable with `--email-from`. |
| `max-email-mb` | `20` | Mail-server attachment size ceiling (MB), resolved via `cli.get_max_email_mb()` and passed into `emailer.gather_attachments()`. Falls back to `emailer.MAX_ATTACHMENTS_MB` (20) if unset. |
| `content-items-dir` | `""` | On-disk `content\items` directory, used only by the content-directory orphan-scan scripts. AGOL has none, so left blank here and set under `[portal]`. |
| `azcopy-path` | `"D:\\User\\azcopy.exe"` | Per-machine install path of `azcopy.exe` (not a secret). Used only by `PORTAL_STACK_LOG_DUMP.py --destination blob`. Resolved via `azcopy_utils.get_azcopy_path()`. |
| `logdump-blob-container` / `logdump-blob-account` | `"logdump"` / `"AZStorageAccountName"` | Storage account/container for the one-shot dated Blob dump (`azcopy copy`). |
| `logmirror-blob-container` / `logmirror-blob-account` | `"logmirror"` / `"AZStorageAccountName"` | Storage account/container for the standing nightly Blob mirror (`azcopy sync`). |

### The `[portal]` section — ArcGIS Enterprise Portal target (`--target portal`)

| Setting | Example value | What it does |
|---|---|---|
| `portal-url` | `"https://enterprise.com/portal"` | Enterprise Portal base URL. Overridable with `--portal-url`. |
| `username` | `"scheduledtask"` | Portal login username (keyring password looked up under this). Overridable with `--username`. |
| `keyring-service` | `"PORTAL"` | OS-keyring service name for the Portal password. Overridable with `--keyring-service`. |
| `content-items-dir` | `"\\\\big_servergis-f5e6\\gis01\\PRODPTL\\content\\items"` | Portal's on-disk `content\items` directory (orphan-scan scripts). **Always a UNC path, never a mapped drive letter** — a scheduled task runs under a service account that will not have the interactively-mapped drive letters a logged-in user has. |

#### Hosting server / Data Store settings (Portal only)

| Setting | Example value | What it does |
|---|---|---|
| `hosting-url` | `"https://enterprise.com/server"` | Hosting GIS Server admin URL. Also used as the seed for **live Data Store topology discovery** (see below). |
| `db-host` | `"datatore_fqdn"` | **A manual override only — not a fallback the scripts fall back to automatically.** See [How `db-host` actually gets used](#how-db-host-actually-gets-used) below. |
| `db-port` | `9876` | Data Store port. Default `9876` (numeric, unquoted). Always needed for Portal-only scans connecting directly to the database. |
| `dbname` | `"hosting_datastore_db_name"` | Data Store database name. |
| `db-user` | `"hosting_datastore_db_admin"` | DB admin to **connect as** (superuser/`pg_monitor`, not `sde`). |
| `keyring-service-db` | `"DATASTORE"` | Keyring service holding `db-user`'s password. |
| `arcgisinput` | `"\\\\big_servergis-f5e6\\gis01\\PRODHOST\\arcgissystem\\arcgisinput"` | Hosting server's `arcgisinput` folder. **Always a UNC path, never a drive letter**. Blank to skip. |
| `db-prefix` | *(commented out)* | Optional; defaults to `dbname`. |

##### How `db-host` actually gets used

Every script that needs a live Data Store connection resolves the host via `stack_discovery.discover_data_store()` against the hosting server's live admin API **on every run**, to find whichever machine is *currently* the primary. This is what makes the scans safe to run unattended after a failover. The config file's `db-host` value is **never read or consulted** in this path. If discovery fails, the run **hard-aborts** with an explanatory message rather than quietly falling back to the config value.

The **only** situation where a host name other than the live-discovered primary is ever used is when an operator explicitly types `--db-host SOME_MACHINE` on the command line to **bypass** discovery entirely (with a warning logged that HA-awareness has been bypassed).

**Practical takeaway:** keep `db-host` in the config up to date as a *documented reference value* for troubleshooting, but don't rely on it being read silently — it isn't. `db-port`, `dbname`, `db-user`, and `keyring-service-db` **are** read automatically every time.

#### Managed schema owner settings (Portal only)

| Setting | Example value | What it does |
|---|---|---|
| `db-user-mu` | `"hosting_datastore_schema_owner"` | Schema-owner DB user to connect as — required because `ListReplicas()` needs schema-owner visibility. |
| `keyring-service-db-mu` | `"DATASTORE_MU"` | Keyring service holding that user's password. |

### The `[agol]` section — ArcGIS Online target (`--target agol`)

| Setting | Example value | What it does |
|---|---|---|
| `portal-url` | `"https://org.maps.arcgis.com/home"` | AGOL organization URL. |
| `username` | `"ORG_Scheduled_Task"` | AGOL login username. |
| `keyring-service` | `"AGOL"` | OS-keyring service name for the AGOL password. |

---

## Running multiple organizations from Scheduled Tasks

This section walks through, concretely, how the `--config` argument, multiple `.toml` files, and Windows Task Scheduler fit together.

### The core idea: one config file per organization, one scheduled task per (script, config, target) combination

**Which organization a run targets is decided entirely by which `.toml` file path is passed on the command line** via `--config`. This means:

- To scan **ORG's** Portal, run any script with `--config ORG.config.toml --target portal`.
- To scan a **second agency's** Portal with the exact same script, unmodified, run it with `--config OTHER_ORG.config.toml --target portal` instead.
- To scan the **same organization's** AGOL org instead of its Portal, keep the same `--config` and change only `--target agol`.

No script code changes, no environment variables to flip, and no "which org am I pointed at right now?" ambiguity.

### Where the config files should live

Keep each organization's `.toml` file wherever makes sense for your deployment (commonly right alongside `UTILITIES\`, as in this project's own `ORG.config.toml`), and give each one a name that makes it obvious which org it belongs to — `ORG.config.toml`, `OTHER_ORG.config.toml`, `BLM.config.toml`, and so on.

### Setting up one scheduled task per organization/target combination

Because the organization and target are just command-line arguments, **each combination you want to run automatically needs its own separate Scheduled Task**, each pointed at a different `--config` (and/or `--target`) value. Example pattern:

```powershell
$TaskPath   = '\PTERODACTYL\'
$TaskName   = 'ORG_PORTAL_ScanAttachmentsSize'
$UserId     = 'ORG\IORG_gSMA_ACCOUNT'
$PythonExe  = 'D:\User\TomPy\python.exe'
$WorkingDir = 'D:\User\PTERODACTYL_ORG'
$ScriptPath = 'D:\User\PTERODACTYL_ORG\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py'
$ConfigPath = 'D:\User\PTERODACTYL_ORG\UTILITIES\ORG.config.toml'  # ← THIS ORG

$PyArgs = @(
  "`"$ScriptPath`""
  '--config', "`"$ConfigPath`""
  '--target', 'portal'
) -join ' '
```

**To add a second organization**, copy this script and change exactly two things:

1. `$TaskName` — give it a name identifying the new org (e.g. `OTHER_ORG_PORTAL_ScanAttachmentsSize`).
2. `$ConfigPath` — point it at that org's own `.toml` file (e.g. `OTHER_ORG.config.toml`).

---

## config.py

Multi-org loader/resolver for **one org's** toml file, keyed by the path passed via `--config`.

- **No default file.** `load_config(path)` raises `ValueError` if `path` is empty/`None`, and `FileNotFoundError` if the path doesn't exist on disk.
- **Caching** is keyed by the resolved absolute path, so multiple orgs' configs never collide within one process.
- **`CONFIG_KEYS`**: `org-name`, `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`, `max-email-mb`.

| Function | What it does |
|---|---|
| `load_config(path)` | Parse and return the raw TOML dict for the given org toml (cached by absolute path). `path` is **required**. |
| `normalize_target(value)` | Validate/normalize a target label to lowercase `"portal"`/`"agol"`; raises `ValueError` otherwise. |
| `get_config_value(key, target, cfg=None, path=None)` | Resolve one key for a target: target-section → `[common]` → `None`. |
| `resolve(args, key, target, cfg=None, path=None)` | Merge a parsed-args value with config for one key; CLI value always wins. |
| `apply_to_args(args, target, keys=CONFIG_KEYS, path=None)` | Fill each `args` attribute from config when the CLI left it unset. |
| `require(args, keys, logger=None)` | Return the list of keys still missing a value (empty = all present). |
| `sanitize_org_name(raw)` | Raw `org-name` → filename-safe token (letters/digits/dot/dash/underscore only). |

---

## cli.py

Shared `argparse` building blocks and the multi-org config-merge entry point.

| Function | What it does |
|---|---|
| `add_config_arg(parser)` | Adds the **required** `--config ORG_TOML_PATH` argument. Call first. |
| `add_target_arg(parser)` | Adds the **required** `--target {portal,agol}` argument. |
| `add_common_portal_args(parser)` | Adds optional `--portal-url` / `--username` / `--keyring-service`. |
| `add_email_args(parser)` | Adds optional `--smtp-server` / `--email-to` / `--email-from`. |
| `add_output_dir_arg(parser)` | Adds optional `--output-dir`. |
| `add_test_flag(parser, help_text)` | Adds `--no-test` (stores `test=False`); test mode is the default. |
| `subject_line(prefix, timestamp, platform=None, org_name=None)` | Builds the email subject with optional org/platform tags. |
| `finalize_config(args, required=(), logger=None)` | **Call once, right after `parse_args()`.** Validates `--config`, normalizes `--target`, loads that org's toml, fills every unset config-backed attribute, resolves+validates `org-name`, then verifies every `required` key has a value. |
| `get_max_email_mb(target, config_path, default=None)` | Resolves `max-email-mb` from the chosen org's toml. |
| `normalize_target(value)` | Pass-through to `config.normalize_target`. |

---

## gis_connect.py

Shared AGOL / Portal connection using a keyring-stored password, with **TLS verification always enabled end-to-end**.

### `connect_gis(portal_url, username, keyring_service, logger, verify_mode=VerifyMode.STANDARD_CA, ca_bundle_path=None)`

`connect_gis()` retrieves the password from keyring (service = `keyring_service`, user = `username`), falling back to the `PORTAL_PASSWORD` environment variable if keyring has no entry, and raises `RuntimeError` if no password is found anywhere. It then connects via `GIS(url, user, pass, verify_cert=...)` and returns the authenticated `GIS` object.

**There is no `verify_cert=False` escape hatch anywhere in this function.** Every connection made through `connect_gis()` is verified.

### Three layers of TLS verification

A single `verify_cert=True/False/<path>` passed to the `GIS()` constructor is **not enough** on its own to guarantee every request the SDK makes afterward is verified. `connect_gis()` addresses this with:

1. **The initial connection.** `verify_cert` (derived from `verify_mode`) is passed directly into `GIS(...)`.
2. **Every SDK call made *after* login.** `gis.content.search()` and similar calls consult the module-level `arcgis.env.verify_cert` flag independently. `connect_gis()` sets this **before** constructing `GIS(...)`.
3. **Connection pool pre-warming (BUGFIX #3b).** The arcgis SDK has a race condition in its own `EsriTrustStoreAdapter` where the first few underlying `HTTPSConnection` objects can be created before the pool has "settled" on verified settings when concurrent requests fire immediately. `connect_gis()` issues **several cheap, serial, already-authenticated requests** immediately after `GIS()` returns, forcing the pool's first connections to be created and fully verified while nothing else is racing.

### `verify_mode` — the three TLS strategies

| `VerifyMode` member | When to use it | What `connect_gis()` does |
|---|---|---|
| `STANDARD_CA` (default) | AGOL, or any Enterprise Portal with a publicly-trusted cert. | `verify_cert=True`. Verifies against certifi's bundled public CA list. |
| `OS_TRUST_STORE` | Internal Enterprise Portal with a certificate signed by an organizational/internal CA already trusted at the OS level. | Calls `truststore.inject_into_ssl()` **itself** before constructing `GIS(...)`. **Requires the `truststore` package**; raises `ImportError` with an explicit message if not installed. |
| `CUSTOM_CA_BUNDLE` | Internal Portal signed by a CA not in the OS trust store, but with a PEM bundle on disk. | `verify_cert=<ca_bundle_path>`. **Requires `ca_bundle_path`** to be supplied. |

**Parameter validation** (enforced with clear exceptions):
- Passing `ca_bundle_path` while `verify_mode` is not `CUSTOM_CA_BUNDLE` raises `ValueError`.
- Passing `verify_mode=CUSTOM_CA_BUNDLE` without `ca_bundle_path` raises `ValueError`.

---

## secure_session.py

Explicit, caller-decided TLS/SSL verification for a script's **own** direct `requests.Session` (separate from the arcgis SDK's connection, which `connect_gis()` always secures). **There is no default, implicit "just build me a session" call.** `build_secure_session(mode, ...)` requires `mode` — omitting it raises `TypeError` immediately.

### `VerifyMode` (shared enum)

| Member | Meaning |
|---|---|
| `STANDARD_CA` | Verify against certifi's bundled public CA list. |
| `OS_TRUST_STORE` | Verify against the **operating system's** native certificate store (via the `truststore` package). |
| `CUSTOM_CA_BUNDLE` | Verify against an explicit PEM CA bundle file. |

| Function | What it does |
|---|---|
| `build_secure_session(mode, *, ca_bundle_path=None, ...)` | Builds a pooled + retrying `requests.Session` with verification set according to `mode`. Raises `TypeError` if `mode` isn't a `VerifyMode` member. |
| `verify_tls_handshake(url, session, logger=None, timeout=10)` | **One-time startup check:** opens one real TLS connection and logs protocol/cipher/certificate detail plus a PASS/FAIL verdict. Returns `(ok, detail)`. |
| `allow_insecure_verify_false(reason, logger=None, ...)` | **Debug-only escape hatch.** Requires a non-blank `reason` and **always logs a loud warning**. Never suppresses `InsecureRequestWarning`. |

---

## logging_setup.py

Shared logging configuration (console + file), **with automatic token redaction on every record**.

`setup_logging(log_file_path, logger_name)` returns a logger that writes every message to **both** the console (stdout, `DEBUG`) and, when `log_file_path` is given, a log file (`mode="w"`, UTF-8, also `DEBUG`). 

**Token redaction.** Every log record passes through a `_TokenRedactionFilter` before formatting: it rewrites `record.msg` via `auth.redact_sensitive_data()`, so credentials embedded in query-string parameters or exception tracebacks can never reach the console or the log file — even if a particular `logger.*()` call site forgot to redact it explicitly.

This is a **defense-in-depth backstop**, not the primary control. Call sites should still explicitly call `auth.redact_sensitive_data()` for email bodies, CSV cells, and other paths that do not go through logging.

---

## auth.py

Token acquisition/holding, **plus** a text-level redaction helper (`redact_sensitive_data()`, aliased as `redact_token()` for backward compatibility) used as a last-resort safety net anywhere a token could otherwise reach a log line, email body, or CSV cell.

### `redact_sensitive_data(text)` / `redact_token(text)`

Redacts recognized credentials from arbitrary text:
- ArcGIS/OAuth tokens, access/refresh tokens, and related parameters
- Azure SAS signatures (`sig=`), SAS values, account keys, connection strings
- Authorization / Proxy-Authorization header values (Bearer, Basic)
- `password`/`passwd`/`pwd`, client secrets, API keys, secret access keys
- Credentials embedded in URLs (PostgreSQL, SQL Server, MySQL, MongoDB, HTTP basic-auth)

Operation is case-insensitive, idempotent, and safe to call on text with no credential. Non-string values are returned unchanged.

### TokenProvider classes

| Class | What it does |
|---|---|
| `TokenProvider` (abstract) | Holds a token privately. `.get()` is the ONLY way to retrieve the raw value — call it immediately before building the request. `.repr()`/`__str__()`/`__format__()` NEVER expose the value, even if logged by accident. |
| `PortalGISTokenProvider(gis)` | Wraps an authenticated Enterprise Portal `GIS`. `refresh()` re-reads `gis._con.token`. |
| `AgolGISTokenProvider(gis)` | Same shape as Portal provider, kept as a distinct subclass for platform-specific behavior later. |
| `StaticTokenProvider(token_str, label)` | Wraps a bare token string. `refresh()` is a no-op — a static token cannot renew itself. |
| `provider_for_gis(gis, platform_label)` | Factory: returns `AgolGISTokenProvider(gis)` if `platform_label` is `"AGOL"`, else `PortalGISTokenProvider(gis)`. |

---

## restutils.py

Shared REST / HTTP helpers, **plus** platform-label, timestamp, and run-timing helpers.

Shared defaults: `DEFAULT_TIMEOUT=30`, `DEFAULT_RETRY_TOTAL=3`, `DEFAULT_PAGE_SIZE=100`, `DEFAULT_BACKOFF_FACTOR=0.3`.

**Platform label** — `get_platform(portal_url)` returns `"PORTAL"`, `"AGOL"`, or `"UNKNOWN"` (no hard-coded URLs; classifies generically from hostname suffix).

**Timestamp** — `build_timestamp()` returns shared `YYYYMMDD_HHMM` string for filenames/subjects.

**Run timing** — `start_timer()`, `format_elapsed()`, `elapsed_since()`, `timing_block()` build email timing blocks.

**Sessions** — `tune_gis_session()`, `build_pooled_session()` build pooled + retrying sessions.

**Search** — `rest_search_page()`, `bucketed_hosted_fs()`, `get_org_id()`, `resolve_items()`.

**Low-level REST** — `get_json()`, `count_query()`.

**Dates** — `epoch_to_str()` (epoch **milliseconds** only), `epoch_to_date()` (superset: also handles `datetime`/`date` objects, epoch **seconds**, numeric strings).

---

## bucketing.py

Date-range **bucketing** search, used to enumerate **more than 10,000 items** past the AGOL/Portal search cap.

**Approach:** Partition on a date field (default `created`, epoch milliseconds) in contiguous, gap-free **monthly** windows. If a window hits the cap, recursively split it in half by time down to 1 ms. Results are merged and **de-duplicated by item id**.

**Decoupled from the search API** via a caller-supplied `search_fn(query, cap) -> list` callback and `id_fn(result) -> id`.

| Name | What it does |
|---|---|
| `DEFAULT_CAP` | `10000` — the hard search cap. |
| `bucketed_search(search_fn, ...)` | Enumerates all matching items by date bucketing. Default span: 2000-01-01 UTC through *now + 365 days*. Returns `{id: result}`. |

---

## arcgis_rest.py

Fast, raw-REST helpers for attachment/layer scanning and **batched item metadata / owner resolution**. Avoids the heavy arcgis SDK object graph for hot paths.

**Attachment / layer scanning**

| Function | What it does |
|---|---|
| `get_service_definition(session, service_url, token, timeout)` | Fetches a Feature/Map service's definition JSON (service root, not sub-layer). |
| `iter_service_sublayers(defn)` | Flattens service-definition dict to list of sub-layer/table descriptors. |
| `list_object_ids(session, layer_url, token, page_size, timeout)` | Enumerates all object ids for a layer. Includes fixes for undercount bugs. |
| `query_attachments(session, layer_url, token, is_agol, ...)` | Enumerates a layer's attachments with size. Fixes for Portal empty-result edge cases. |
| `fill_missing_sizes(session, layer_url, token, rows, workers=8, timeout)` | For rows with `size is None`, fetches per-object attachments in parallel and patches them. |

**Batched item metadata / owner resolution**

| Function | What it does |
|---|---|
| `resolve_items_metadata(session, portal_url, token, item_ids, batch_size=100, ...)` | Resolves `title`/`owner`/`created`/`modified` for many item ids using batched `/sharing/rest/search`. Returns `{item_id: {metadata}}`. |
| `resolve_owners_batched(session, portal_url, token, item_ids, ...)` | Owner-only wrapper over `resolve_items_metadata()` — no extra HTTP round-trips. Returns `{item_id: owner}`; every input id is present. |

---

## emailer.py

Shared SMTP email sender with attachments (multi-recipient aware), **plus** run-scaffolding helpers.

| Function | What it does |
|---|---|
| `normalize_recipients(email_to)` | Single address, comma-separated list, or pre-split list → clean list. |
| `ensure_output_dir(output_dir)` | Creates directory (and parents). On failure, writes to stderr and exits(1). |
| `write_dict_csv(rows, path, fieldnames=None, ...)` | Writes dict rows to CSV. Returns path if written, else None. |
| `gather_attachments(log_file_path, csv_paths, output_dir, logger, cap_mb=None, ...)` | **Three-tier policy:** originals fit → attach as-is. Zipped fit → attach zip + log. Neither fit → attach only log, explain on-disk paths. |
| `send_email(subject, body, attachments, smtp_server, email_to, email_from, logger)` | Sends completion email with attachments. Failures logged, never raised. |

---

## checkpoint.py

Shared per-**service** checkpointing for long-running scan scripts.

**Granularity:** Per-service, not per-layer/record. A service mid-scan when the process dies is simply re-scanned in full on resume.

**Mode safety:** A checkpoint records which mode (TEST vs FULL) produced it, and for TEST mode, the exact `--test-count`. Resuming under a different mode/test-count is refused unless `--fresh-start` is passed.

| Function | What it does |
|---|---|
| `checkpoint_path(output_dir, org_prefix, script_tag, target, mode)` | Builds the checkpoint file path using the shared convention. |
| `load_or_init_checkpoint(path, mode, test_count, logger=None)` | Loads an existing checkpoint, or initializes a fresh dict. Raises `CheckpointModeMismatch` on mode/test-count mismatch. |
| `save_checkpoint(path, state, logger=None)` | Atomically writes the checkpoint state to disk. |
| `mark_service_complete(state, item_id)` | Adds `item_id` to the completed set (mutates state). |
| `is_service_complete(state, item_id)` | True if already recorded as completed. |
| `clear_checkpoint(path, logger=None)` | Deletes the checkpoint file (clean completion). |
| `CheckpointModeMismatch(Exception)` | Raised when an existing checkpoint's mode/test-count doesn't match the current run. |

---

## exclusions.py

Shared "exclude this group's items from the scan" feature.

`--exclude-group-id` accepts a **comma-separated list** of group ids; every named group's membership is unioned into one excluded-id set.

| Function | What it does |
|---|---|
| `add_exclude_group_arg(parser)` | Adds the optional `--exclude-group-id GROUP_ID[,GROUP_ID,...]` flag. |
| `resolve_excluded_item_ids(gis, exclude_group_id, logger=None)` | Resolves group id(s) into the union of every member item id. Returns `set()` if no exclusion. |
| `filter_excluded(records, excluded_ids, id_getter, logger=None, label="item")` | Returns subset of `records` whose id is **not** excluded, preserving order. `id_getter` makes this work for SDK Items, REST dicts, or custom rows. |

---

## path_discovery.py

Self-discovery of each machine's **real** install and log directories. **No fallback to hard-coded paths** — a discovery failure comes back as `None` with a specific error string.

**Two paths per product**, discovered differently:
1. **Data/config-store directory** (the live `logs` folder) — via each product's admin REST API for Portal/Server; via Data Store's own properties config file for Data Store.
2. **Install directory** — read from Windows Registry **remotely** via `winreg` against each machine's short hostname, trying both 64-bit and 32-bit views.

| Function | What it does |
|---|---|
| `discover_server_log_dir(session, admin_url, token, logger=None)` | `GET <admin_url>/admin/logs/settings`. Returns `(log_dir, error_str)`. |
| `discover_portal_log_dir(session, portal_url, token, logger=None)` | `GET <portal_url>/portaladmin/logs/settings`. Returns `(log_dir, error_str)`. |
| `discover_install_dir(short_hostname, product, logger=None)` | Remote registry `InstallDir` lookup. `product` is `"server"` / `"portal"` / `"datastore"`. Returns `(install_dir, error_str)`. |
| `discover_datastore_data_dir(short_hostname, install_dir, logger=None)` | Reads `<install_dir>\etc\arcgis-data-store-config.properties` over D$ share, unescapes the `dir.data=` line. Returns `(data_dir, error_str)`. |
| `resolve_log_paths(category, short_hostname, domain_suffix, ...)` | Top-level convenience: full self-discovery for one machine/category. Returns dict with `data_log_path`, `data_log_error`, `install_log_path`, `install_log_error`. |

---

## stack_discovery.py

Enumerates every machine in an ArcGIS Enterprise Portal deployment (Portal, hosting server, Data Store, federated servers) and their FQDNs, **plus live Data Store primary/HA resolution**.

`discover_data_store()` is standalone — callers that only need "which machine is the live PRIMARY Data Store right now" (e.g. scripts connecting directly to the database) can call it directly with just session/hosting-admin-url/token, without a `gis` object or full federation walk.

| Function | What it does |
|---|---|
| `discover_stack(gis, session, logger)` | Full deployment walk: Portal machines/primary-standby, federated servers (with site_name and FQDN fallback chains), Data Store. Returns dict with `portal`, `hosting_server`, `data_store`, `federated_servers`. Each federated server additionally carries `server_id` (the raw federation GUID). |
| `discover_data_store(session, hosting_admin_url, token, logger=None)` | Standalone Data Store discovery. Returns `{"host", "fqdns", "items", "ambiguous", "error"}`. |
| `hostname_from_url(url)` | Extract just the hostname from a URL. |
| `looks_like_fqdn(value)` | True if value looks like a dotted hostname or IPv4. |
| `short_hostname(fqdn)` | Strip an FQDN down to just the leading label (e.g. "big_serverGISAP33.enterprise.com" → "big_serverGISAP33"). |

---

## robocopy_utils.py

Shared log-collection helpers (date-filter prompting + robocopy invocation mechanics) factored out of the retired `LOG_DUMP.bat`.

**Date filter modes (unchanged):** Previous Day, Date Range, Previous N Days.

**Robocopy option set (unchanged):** `/E /Z /MT:32 /R:1 /W:1 /NFL /NDL /NP /XJ <date-filter-args>`.

**Environment sanitization:** Robocopy receives a copy of the parent environment with credential-shaped variables removed (password, token, secret, SAS, API keys, etc.).

| Name | What it does |
|---|---|
| `ROBOCOPY_BASE_OPTS` | The fixed option list (excluding date-filter flags). |
| `ROBOCOPY_SUCCESS_MAX` | `7` — robocopy exit codes 0–7 are informational success; 8+ is failure. |
| `prompt_date_filter()` | Interactive prompt replicating LOG_DUMP.bat's menu exactly. Returns dict with mode/maxage/minage/range_start/range_end. |
| `date_filter_from_args(mode, previous_n_days=None, range_start=None, range_end=None)` | Non-interactive equivalent for CLI-flag-driven runs. |
| `build_date_filter_opts(filter_info)` | Turns the dict from above into robocopy `/MAXAGE[/MINAGE]` argument list. |
| `run_robocopy(source, dest, extra_opts, logger=None)` | Runs one robocopy call. Returns exit code, or `-1` if robocopy couldn't be launched. |
| `copy_group(category, server_short_name, domain_suffix, path_pairs, dest_root, date_filter_opts, ...)` | Replicates LOG_DUMP.bat's `:CopyGroup` for one machine: `\\<fqdn>\D$\<relpath>` → `<dest_root>\<category>\<fqdn>\<relpath>`. Returns list of exit codes. |

---

## azcopy_utils.py

Shared Azure Blob upload helpers for `PORTAL_STACK_LOG_DUMP.py`'s two Blob use cases:

1. **Dated/scoped dump** (`--destination blob`, no `--sync-mode`) — one-shot extraction into a fresh, run-timestamped blob prefix via `azcopy copy`.
2. **Standing nightly mirror** (`--sync-mode mirror`) — continuously-maintained destination kept in sync via `azcopy sync --delete-destination=false` against a **stable** (non-timestamped) blob prefix.

**Authentication:** SAS-token only (no secrets in the org toml). `azcopy-path` (per-machine install path) is read from config; the SAS token comes from Windows Credential Manager via keyring using a **composite service name** pattern:
- Service (Internet or network address) = `azblob:<storage_account>:<container>`
- Username = `sastoken` (fixed)
- Password = the SAS query string only (no leading `?`)

**SAS secrecy guarantees:** The SAS token value is NEVER logged directly, printed, or returned in any string. Every piece of text that could have come from AzCopy itself (stdout, stderr, exception text) or been built here (the destination URL) is sanitized via `sanitize_azcopy_output()` before being logged or returned.

| Function | What it does |
|---|---|
| `get_azcopy_path(target, config_path, logger=None)` | Resolves `azcopy-path` from the org toml. |
| `validate_azcopy_exe(azcopy_path, logger=None)` | Confirms the path points at an existing file. Returns `(ok, err)`. |
| `get_blob_sas_token(storage_account, container, logger=None)` | Keyring lookup for the SAS token. Returns `(token, error_str)`. |
| `build_blob_url(storage_account, container, sas_token=None, blob_prefix=None)` | Builds HTTPS Blob URL; appends SAS only when explicitly supplied. |
| `run_azcopy_upload(azcopy_path, source_path, storage_account, container, sas_token, ...)` | Runs one `azcopy copy` call. Returns `(exit_code, stdout, stderr)`. |
| `copy_group_to_blob(category, server_short_name, domain_suffix, path_pairs, ...)` | **Dated dump:** uploads each relative path to `<run_timestamp>/<category>/<fqdn>/<relpath>`. |
| `run_azcopy_sync(azcopy_path, source_path, storage_account, container, ...)` | Runs one additive-only `azcopy sync` call. Returns `(exit_code, stdout, stderr)`. |
| `copy_group_to_blob_mirror(category, server_short_name, domain_suffix, path_pairs, ...)` | **Standing mirror:** syncs each relative path to `<category>/<fqdn>/<relpath>` — **no run_timestamp segment**. |
| `test_blob_connection(azcopy_path, storage_account, container, ...)` | Read-only `azcopy list` connectivity check. Returns `(ok, detail)`. |
| `sanitize_azcopy_output(text, sas_token=None)` | Redacts complete SAS queries and other credentials from text. **Choke point for every AzCopy-adjacent text.** |

**Authentication modes** (SAS is the project default):
- `AZCOPY_AUTH_SAS` — SAS query string appended to destination URL (this project's mode).
- `AZCOPY_AUTH_MANAGED_IDENTITY` — Entra/MSI mode (available for other deployments).
- `AZCOPY_AUTH_WORKLOAD_IDENTITY` — Federated credential mode (available for other deployments).
- `AZCOPY_AUTH_AZURE_CLI` — Azure CLI authentication (available for other deployments).

---

## datastore_config.py

Resolves the Data Store / hosting-server keys that live in `[portal]` but are **not** part of the shared `CONFIG_KEYS` (so `cli.finalize_config()` doesn't touch them): `hosting-url`, `db-host`, `db-port`, `dbname`, `db-user`, `keyring-service-db`, `arcgisinput`, `db-prefix`.

**Signature note (breaking, intentional):** `get_datastore_settings(args, target, config_path, *, logger=None)` now takes a **required** `config_path` positional argument (the org toml path — normally `args.config`), matching the multi-org pattern. `logger` is **keyword-only** so a stale 3-argument call fails loudly instead of silently misbinding arguments.

Resolution precedence per key (highest wins): **CLI flag → `--target` section → `[common]` → built-in default** (`db-port` → `9876`; others → `None`).

| Function | What it does |
|---|---|
| `add_datastore_args(parser)` | Adds optional `--db-host` / `--db-port` / `--dbname` / `--db-user` / `--keyring-service-db` overrides. |
| `get_datastore_settings(args, target, config_path, *, logger=None)` | Resolves all Data Store keys. Returns a dict; missing values are `None`. Normalizes `db-port` to `int`. |
| `missing_datastore_keys(settings, required=(...))` | Returns the required keys still missing a value. |

---

## \_\_init\_\_.py

Package entry point. Re-exports the public surface so scripts can do `from UTILITIES import connect_gis, setup_logging, ...` rather than reaching into individual modules.

**Re-exported modules and names** (see the file itself for the complete `__all__` list):
- **restutils.py:** platform, timestamp, timing, sessions, search, low-level REST, dates
- **logging_setup.py:** `setup_logging`
- **gis_connect.py:** `connect_gis`
- **emailer.py:** email sending + run scaffolding helpers
- **bucketing.py:** `bucketed_search`
- **cli.py:** argparse helpers, config merger, subject-line builder
- **config.py:** toml loader, key resolver, org-name sanitizer
- **exclusions.py:** group exclusion filtering
- **stack_discovery.py:** topology discovery, hostname helpers
- **robocopy_utils.py:** log-collection date filtering and robocopy invocation
- **azcopy_utils.py:** Azure Blob upload (dated and mirror modes)
- **datastore_config.py:** Data Store key resolution
- **auth.py:** TokenProvider classes, `redact_sensitive_data()`
- **secure_session.py:** VerifyMode, explicit TLS session building

**NOT currently re-exported through `__init__.py`** — import these directly:
- `from UTILITIES.checkpoint import checkpoint_path, load_or_init_checkpoint, save_checkpoint, mark_service_complete, is_service_complete, clear_checkpoint, CheckpointModeMismatch`
- `from UTILITIES.arcgis_rest import get_service_definition, iter_service_sublayers, list_object_ids, query_attachments, fill_missing_sizes, resolve_owners_batched, resolve_items_metadata`
- `from UTILITIES.path_discovery import discover_server_log_dir, discover_portal_log_dir, discover_install_dir, discover_datastore_data_dir, resolve_log_paths`