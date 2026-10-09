# PORTAL_HOSTING_SVR_DS_INVENTORY.py

Read-only reconciliation of Portal hosted-service table references against the PostgreSQL Data Store, with optional `.msd` service definitions. The Data Store's live PRIMARY machine is auto-detected on every run — HA-aware.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory the Data Store](#why-inventory-the-data-store)
- [Why It's a Problem](#why-its-a-problem)
- [Why Removal Requires Caution](#why-removal-requires-caution)
- [Recommendations for Investigation](#recommendations-for-investigation)
- [Column Descriptions](#column-descriptions)

### For Administrators
- [Dependencies on the Shared UTILITIES Package](#dependencies-on-the-shared-utilities-package)
- [Requirements](#requirements)
- [Security: Certificate (TLS) Validation](#security-certificate-tls-validation)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Test Mode (Default)](#test-mode-default)
- [Usage](#usage)
- [Command-Line Options](#command-line-options)
- [Performance](#performance)
- [Observed Runtime](#observed-runtime)
- [Output File](#output-file)
- [Exit Codes](#exit-codes)
- [Notes and Caveats](#notes-and-caveats)

---

# For Report Readers

## Overview

This report shows how Portal's hosted map and feature services line up with the actual PostgreSQL Data Store underneath them. For every table or view the inventory finds, the report tells you whether the database relation exists, whether a live Portal service references it, whether the reference also appears in an older `.msd` service definition, and whether a database relation may be unreferenced.

Think of it as a **cross-check between three lists**: what Portal's hosted FeatureServer definitions say they need, what older `.msd` service definitions on disk say they need, and what actually exists in the PostgreSQL Data Store.

The report never changes anything — it only reads the Portal, hosting-server, `.msd`, and database information and writes a CSV showing the current state.

## Why Inventory the Data Store

The Data Store is the one layer of the stack that almost nobody looks at directly, and that is exactly why it needs a dedicated inventory. Administrators work in Portal, where they see items, services, and maps; the PostgreSQL tables and views sitting underneath are not visible from that interface, so the database quietly becomes the place where the organization's blind spots collect. This is a reconciliation tool for finding duplicate or repeated **database references** and for identifying database relations that do not line up cleanly with the service references it can see. The only way to know whether what Portal believes it has still matches what the database actually holds is to compare the two directly, which is what this scan does.

The reason this particular scan is necessary is that Portal and the Data Store are maintained through different paths and inevitably drift apart. Over time, the map and feature services in Portal can drift apart from the actual database: a service is republished or deleted but the database table stays behind; someone makes changes directly in the database outside of Portal; old MapServer-style service definitions reference tables that a newer approach no longer uses; or a publish operation is interrupted before completing. Each of these leaves the two sides inconsistent in a way that no single system reports, because Portal only knows about its own references and the database only knows about its own relations — neither can see the mismatch alone. The result is that the same physical table can end up referenced by multiple services, or by nothing at all — creating "orphaned" tables that use space and add maintenance work, or "missing" tables that break services. A three-way reconciliation across live service definitions, `.msd` files, and the database catalog is the only thing that surfaces both failure directions at once.

This scan is worth running on a schedule because both kinds of drift are cumulative and silent until they become expensive. Orphaned tables never appear in Portal, so they accumulate storage and backup/restore cost that grows unnoticed until someone audits database size; missing relations, by contrast, are latent service failures waiting to be discovered by an end user. Running the reconciliation periodically catches both early and, because each row is tagged with a status, a source, an owner, and explanatory notes, it supports root-cause work rather than one-off cleanup: when the same naming pattern, publisher, or service family shows up repeatedly in the unused or missing categories, that recurring signal points to a broken process worth fixing at the source. Just as importantly, the scan is built to be honest about its own coverage — when a service definition or `.msd` path cannot be read, affected rows are marked `INDETERMINATE` rather than reported as confirmed orphans — so the inventory can be trusted as evidence. Because it is strictly read-only and never deletes anything (deletion is a separate, deliberate tool used only after review), it is safe to run often as a standing consistency check on the layer of the stack that otherwise goes unexamined.

## Why It's a Problem

**Leftover tables build up silently.** They don't appear in Portal's interface, so nobody notices they're there. Over time, they accumulate storage cost and backup/restore burden.

**A table can be used by something you didn't know about.** A service might still depend on a table, even if Portal's catalog doesn't clearly show the connection.

**Missing tables break services.** If a service says it needs a table that no longer exists in the database, that's the opposite problem — the service won't work for end users.

**Portal item references can become stale.** A service might reference a Portal item ID that no longer exists, which can show up as broken links or inaccessible content.

**Patterns point to root causes.** If the same naming pattern, publisher, or service family shows up repeatedly in unused or missing categories, that often indicates a process that needs fixing.

## Why Removal Requires Caution

**This report is evidence for investigation, not a delete list.** A table marked `ORPHAN_DB_RELATION` or otherwise appearing unreferenced in this scan might actually be needed by something this report didn't check — a service in a different folder, a service definition that could not be read, an `.msd` location that was not supplied, or a workflow outside Portal.

A table can be missed by this scan for legitimate reasons:

- The service using it lives in a different admin folder than the one checked.
- The service is an older MapServer type, and the `.msd` folder wasn't included in this run.
- A service definition failed to load this time, so its references were invisible.
- The table is used in a way the script's naming patterns don't recognize.

Some tables that look "unused" by name are actually essential internal support tables, attachments, versioned editing views, or other system-managed relations that ArcGIS creates and manages automatically. **Never delete a table without independent verification.**

There is a separate, dedicated cleanup tool for use only after careful review. This inventory report and any deletion decision are always two separate actions.

## Recommendations for Investigation

Before anyone changes or removes anything:

1. Check the log for services that failed to load, Portal item checks that failed, or `.msd` paths/files that couldn't be reached — those gaps mean the real answer is hidden.
2. Confirm the report scanned the right service folder and the right `.msd` file locations.
3. Search Portal itself and any scripts your organization uses for references to the table or service name.
4. Confirm who owns the table in the database and what type of object it is (plain table vs. system-managed view).
5. For anything marked `MISSING_DB_RELATION`, check whether the service still works. If it does, the reference may be stale or pointing elsewhere.
6. For anything marked `COMPANION_DB_RELATION`, treat the suggested parent table as a lead, not proof. The script infers this relationship from naming patterns rather than database metadata.
7. For anything marked `ORPHAN_DB_RELATION`, double-check that no service outside this run's scope uses it.
8. For anything marked `INDETERMINATE_DB_RELATION` or `INDETERMINATE_SERVICE_REFERENCE`, fix the missing coverage and re-run before treating the result as confirmed.
9. Test any proposed change in a non-production copy first.
10. Keep the CSV and log that supported your decision.

## Column Descriptions

Each row represents one table, view, or service reference discovered by the reconciliation. The CSV columns are emitted in this exact order:

| Column | Meaning |
|---|---|
| `status` | Overall reconciliation result. Current values include `MAPPED_OK`, `MISSING_DB_RELATION`, `ORPHAN_DB_RELATION`, `COMPANION_DB_RELATION`, `INDETERMINATE_DB_RELATION`, and `INDETERMINATE_SERVICE_REFERENCE`. `MAPPED_OK` means the referenced relation exists; the other values explain a missing, inferred, unconfirmed, or orphaned relationship. |
| `table_name` | Normalized lowercase `schema.table` name used for reconciliation. |
| `raw_table_name` | Exact table/reference name as it appeared in the service definition before normalization. |
| `db_exists` | `yes` or `no` when the database snapshot was available for the relation; blank when not populated by that row type. |
| `db_relkind` | Database relation type: `table`, `partitioned table`, `view`, `materialized view`, or `foreign table`. |
| `db_owner` | PostgreSQL role that owns the relation. |
| `db_size_pretty` | Database relation size rendered in megabytes with two decimal places. |
| `db_size_bytes` | Exact PostgreSQL relation size in bytes. |
| `service_is_view` | Indicates whether the service/layer is identified as a view. |
| `service_name` | FeatureServer, MapServer, or other recognized service name associated with the reference. Multiple services may be separated by semicolons. |
| `service_type` | Service type associated with the reference, such as `FeatureServer` or `MapServer`. |
| `service_admin_url` | Direct hosting-server administration URL for the service when available. |
| `reference_source` | Where the reference was found: `admin_api` for the live hosting-server service definition or `msd` for a scanned `.msd` service definition. |
| `source_file` | For `.msd` references, the source `.msd` file path. It may also include `(+N more)` when multiple `.msd` files referenced the same service/table combination. |
| `layer_id` | Numeric layer/table ID from the service definition when available. |
| `layer_name` | Display name of the layer/table when available. |
| `kind` | Reference role such as `layer`, `table`, `attachment`, `versioned view`, or `track table`. |
| `portal_item_ids` | Portal item ID(s) declared by the service definition, separated by semicolons. |
| `portal_items_exist` | Portal item verification result: `yes`, `no`, `partial`, `unknown`, or `n/a`. |
| `portal_item_titles` | Titles of Portal items that were confirmed to exist. |
| `portal_item_missing` | Portal item IDs that were checked and did not exist. |
| `possible_owning_service` | Reserved output column; currently blank in generated rows. |
| `notes` | Plain-language details explaining the finding, such as missing relations, inferred companion tables, cached references, or incomplete coverage. |

The inventory can include rows that come from live service definitions, `.msd` files, the database catalog, or inferred companion/managed relations. An `INDETERMINATE_*` status means the script found enough information to report a relationship but did not have complete live coverage to treat the conclusion as final.

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

The script imports helpers from these UTILITIES modules:

- `cli.py`
- `config.py`
- `gis_connect.py`
- `auth.py`
- `secure_session.py`
- `logging_setup.py`
- `restutils.py`
- `emailer.py`
- `stack_discovery.py`

## Requirements

- Python 3.9+ with ArcGIS API for Python.
- `requests`, `urllib3`, and `keyring`.
- `psycopg2` or `psycopg2-binary` for PostgreSQL access.
- The shared `UTILITIES` package in the expected project layout.
- An organization-specific TOML file via `--config`.
- A Portal account able to authenticate, list services, and read service definitions.
- A PostgreSQL account able to connect to the Data Store and read the required system catalog information.
- Network access to Portal, the hosting-server admin API, the Data Store, and SMTP.
- Write access to the configured output directory.

## Security: Certificate (TLS) Validation

This script uses TLS verification on the Portal/hosting-server HTTP connections and requires TLS for the PostgreSQL connection.

### Certificate validation

1. **The ArcGIS SDK connection** (via `connect_gis()`) always verifies TLS. There is no option to disable it.
2. **This script's direct HTTP sessions** use the selected `--tls-mode`:
   - `os-trust-store` (default) — verifies against the operating system certificate store. This is the default for this Portal-only script and is useful when an organizational CA is trusted by Windows. It requires the `truststore` package.
   - `standard` — verifies against the public CA bundle.
   - `custom-ca-bundle` — verifies against the PEM file supplied by `--ca-bundle-path`.

Before scanning begins, the script performs a one-time TLS handshake check against the Portal URL using the selected verification strategy. If that check fails, the run stops immediately.

The PostgreSQL connection also requires TLS via `sslmode` (`require`, `verify-ca`, or `verify-full`). With `verify-ca` or `verify-full`, `--db-sslrootcert` can supply the CA file; `verify-full` additionally performs hostname validation.

### SSRF / network-boundary defenses

- Token-bearing REST requests must use HTTPS.
- Portal and hosting-server origins are normalized and explicitly trusted before direct REST requests are sent.
- Request URLs reject user-info credentials and fragments and are restricted to the expected service/API origins.
- The configured hosting URL must match an exact registered hosting-server entry in the authenticated Portal federation registry. The script refuses to send the Portal token to an unregistered hosting server.
- The Data Store host is constrained to a validated hostname or IP address. Auto-discovered short names may require the configured `domain-suffix`; a discovered host outside the configured suffix is rejected.

### Token handling

Tokens are held through the shared `provider_for_gis()`/`TokenProvider` mechanism and exposed at the request-building boundary. Callers cannot replace the authentication token through ordinary request parameters. Authentication failures trigger one synchronized refresh-and-retry.

Token-bearing REST traffic is limited to the authenticated Portal and the hosting server after federation validation. Exception/error text is passed through `redact_token()` before being written to logs or email.

The PostgreSQL password is also held only for the connection setup and is cleared after the database connection is established or fails.

### Large and malformed responses

- Direct ArcGIS REST JSON responses are streamed and limited to 64 MB.
- `.msd` archives are bounded by member count, individual member size, aggregate uncompressed size, aggregate bytes read, file count, and compression-ratio checks.
- The incremental cache is limited to 128 MB.
- JSON/object shapes and REST error responses are validated before the data is used.
- Service and `.msd` reference failures are contained and surfaced through `INCOMPLETE` status rather than silently treated as complete coverage.

### Redirect handling

Automatic HTTP redirect following is explicitly disabled with `allow_redirects=False`. A redirect response is refused rather than followed, preventing a token-bearing request from silently moving to another origin.

### Python import-path/package shadowing

The script removes no equivalent project-root copies from `sys.path`. It checks whether the expected project root is already present, inserts it at the front when it is absent, imports `UTILITIES`, and then verifies that the loaded package actually came from the expected `UTILITIES` directory.

## Configuration

Every run requires an organization-specific TOML file:

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml
```

### Example Configuration

```toml
[common]
org-name = "ORG"
output-dir = "C:\temp\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "sender@example.com"

[portal]
portal-url = "https://portal.example.com/portal"
username = "portal_service_account"
keyring-service = "PORTAL"

hosting-url = "https://portal.example.com/server"
dbname = "db_example"
db-user = "datastore_monitor"
keyring-service-db = "DATASTORE"
arcgisinput = "\\fileserver\d$\arcgissystem\arcgisinput"
```

### Configuration Precedence

1. Command-line value
2. `[portal]` section
3. `[common]` section
4. Script default (where applicable)

### Required Settings

- `org-name` (config only)
- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`
- `hosting-url`
- `dbname`
- `db-user`
- `keyring-service-db`

The following settings are optional and have defaults or alternate discovery paths:

- `folder` defaults to `Hosted`.
- `db-port` defaults to `9876`.
- `db-sslmode` defaults to `require`.
- `tls-mode` defaults to `os-trust-store`.
- `vview-suffix` defaults to `_evw`.
- `arcgisinput` can be omitted, in which case no `.msd` files are scanned.
- `db-prefix` defaults to the configured database name.
- `domain-suffix` can be supplied explicitly, read from configuration, or derived from a configured `db-host` FQDN.

Store passwords in the OS keyring, not in TOML:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('DATASTORE', 'datastore_monitor', '<password>')"
```

## How It Works

The script executes five major steps:

**STEP 1:** Connect to Portal via the ArcGIS SDK and acquire a token. The target is Portal-only; AGOL is rejected.

**STEP 2:** Validate the configured hosting-server URL against the authenticated Portal federation registry, then auto-detect the live Data Store PRIMARY machine via the hosting server's admin API (HA-aware), unless `--db-host` is given.

**STEP 3:** List all FeatureServers from the selected admin folder and read each service's definition concurrently. Extract table names, declared companion tables, and Portal item IDs. Check each Portal item.

**STEP 4:** Recursively scan `.msd` files below the resolved `arcgisinput` roots for table references using regex. The scan is bounded by file/member/size limits.

**STEP 5:** Query PostgreSQL for all relations in the discovered schemas, then reconcile three sources (live service definitions, `.msd` files, and database catalog) into CSV rows.

The script loads and updates a stable JSON cache to reuse parsed service definitions and scanned `.msd` files across runs, unless `--force` is given. A cross-process lock prevents simultaneous use of that cache.

### Live Data Store primary discovery

Unless `--db-host` is provided, the script calls the hosting server admin API for `/admin/data/findItems` with `ancestorPath=/enterpriseDatabases`. It evaluates registered machines and refuses to guess when the PRIMARY role is ambiguous or multiple candidate hosts resolve differently.

A discovered short machine name can be expanded with `--domain-suffix`. A full hostname or IP address is used as-is, subject to validation.

### Service and `.msd` reconciliation

Live hosting-server definitions are the primary source. For each FeatureServer, the script extracts normalized table references, Portal item IDs, layer/table metadata, and companion table references. It can also scan `.msd` files for references that are not present in the live service JSON.

If a live service JSON read fails and a usable cache entry exists, the cached reference is retained and marked as stale coverage. That can cause downstream rows to become `INDETERMINATE_SERVICE_REFERENCE` or `INDETERMINATE_DB_RELATION` instead of being presented as confirmed orphaned data.

### Database snapshot

The script derives the schemas to inspect from the service and `.msd` references it found. PostgreSQL is queried for relation kinds `r`, `p`, `v`, `m`, and `f`, and internal relation names matching `^i\d+$` are ignored.

The database session is established with TLS settings from `--db-sslmode` and `--db-sslrootcert`, is set to read-only, uses a PostgreSQL statement timeout, and is closed after the snapshot is collected.

### Status logic

| Status | Meaning |
|---|---|
| `MAPPED_OK` | The service/.msd reference maps to a relation found in the database snapshot. |
| `MISSING_DB_RELATION` | A service or `.msd` reference was found, but the expected database relation was not found. |
| `ORPHAN_DB_RELATION` | A database relation in the scanned schemas has no matching live service or scanned `.msd` reference. |
| `COMPANION_DB_RELATION` | A database relation looks like a companion of a mapped table based on its name, but the relationship is inferred rather than declared in service metadata. |
| `INDETERMINATE_DB_RELATION` | The database relation appears potentially orphaned/managed, but missing service-reference coverage prevents a definitive classification. |
| `INDETERMINATE_SERVICE_REFERENCE` | The relation is backed by cached service information because live service JSON could not be read, so the mapping needs revalidation. |

## Test Mode (Default)

This script has **no test mode.** Every run scans all FeatureServers in the selected folder and all `.msd` files in the configured paths. There is no service limit or single-service mode. For limited validation, use a non-production organization or configure a small folder.

## Usage

### Scenario 1 — Routine Monthly Health Check

Your team runs a regular reconciliation check using the main `Hosted` folder. The script auto-detects the live Data Store primary and reuses valid cached service definitions and `.msd` scans when lifecycle probes confirm they are unchanged.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml
```

**Expect:** A full Portal inventory, a timestamped CSV and log, and an email summary. With a warm cache, many unchanged service definitions can be reused; the Data Store primary is still checked during the run unless `--db-host` is supplied.

### Scenario 2 — Post-Migration Full Refresh

After a database restore, republish, or service migration, you want every service definition and `.msd` file re-read from scratch, ignoring the incremental cache.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --force
```

**Expect:** Service JSON and `.msd` parsing is forced to refresh. The cache is replaced with the newly collected references at the end of successful phases.

### Scenario 3 — Large Organization, Higher Concurrency

You have a large number of FeatureServers and want more concurrent service/item work while also skipping the lifecycle probe because you want fresh reads rather than lifecycle-based cache reuse.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --workers 32 --no-lifecycle-probe
```

**Expect:** Up to 32 worker threads are used for the concurrent phases, and service definitions are fetched rather than reused based on lifecycle timestamps. A failed fresh read can still fall back to an existing cache entry and mark that coverage stale.

### Scenario 4 — Validating QA Before Production

Test the configuration against a smaller QA folder first to confirm everything works before running against `Hosted`.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --folder QA
```

**Expect:** Only FeatureServers under the `QA` admin folder are enumerated for that run. The Data Store and Portal checks remain otherwise unchanged.

### Scenario 5 — MapServer Definitions on Multiple Shares

Older `.msd` files are split across two file shares. Scan both in one run.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml \
  --arcgisinput "D:\arcgissystem\arcgisinput" \
  --arcgisinput "\\other-host\d$\arcgissystem\arcgisinput"
```

**Expect:** Both roots are scanned recursively for `.msd` files. Duplicate paths are de-duplicated, and unreadable roots/files are logged and contribute to incomplete coverage rather than silently disappearing.

### Scenario 6 — Forcing a Specific Database Machine

You need to test or temporarily use a known Data Store host instead of live PRIMARY discovery.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --db-host datastore-primary.example.com
```

**Expect:** Live Data Store primary discovery is bypassed for that run. The supplied host is validated and used for the PostgreSQL connection.

### Scenario 7 — Custom Domain Suffix for Machine Names

The hosting server reports a Data Store machine name without a domain, and your configuration does not already contain a usable FQDN.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --domain-suffix example.com
```

**Expect:** A discovered short machine name such as `datastore01` is expanded to `datastore01.example.com`, subject to hostname validation. A full hostname or IP is not rewritten.

### Scenario 8 — Non-Standard Versioned View Suffix

Your organization uses `_vw` as a versioned-view suffix in addition to the default `_evw`. Include both in the reconciliation.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml \
  --vview-suffix _evw --vview-suffix _vw
```

**Expect:** Both suffixes are recognized when matching database relations to already-referenced base tables. The match is naming-based and should be independently verified.

### Scenario 9 — Internal Portal with Organizational CA

Your Portal uses an internal CA already trusted by the operating system.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --tls-mode os-trust-store
```

**Expect:** The HTTP session and ArcGIS SDK connection use OS trust-store validation. The startup handshake confirms the Portal certificate chain before the inventory starts.

### Scenario 10 — PostgreSQL Hostname Verification

Your security baseline requires PostgreSQL certificate and hostname verification with an organizational CA bundle.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --db-sslmode verify-full --db-sslrootcert C:\certs\datastore-ca.pem
```

**Expect:** PostgreSQL uses `verify-full`, so the Data Store certificate must chain to the supplied CA and the certificate hostname must match the database host.

### Scenario 11 — Scan With Debug Detail

You need detailed per-service and per-`.msd` diagnostics while troubleshooting an unexpected reconciliation result.

```bash
python PORTAL_HOSTING_SVR_DS_INVENTORY.py --config Org.config.toml --debug
```

**Expect:** The log contains additional per-service and per-`.msd` information. Report contents and scope remain the same.

## Command-Line Options

| Option | Required | Purpose |
|---|---:|---|
| `--config TOML_PATH` | Yes | Organization TOML path. |
| `--portal-url URL` | No | Portal URL override. |
| `--username USERNAME` | No | Portal username override. |
| `--keyring-service NAME` | No | Portal-password keyring service. |
| `--output-dir PATH` | No | Report and log directory. |
| `--smtp-server HOST` | No | SMTP relay override. |
| `--email-to ADDRESS_LIST` | No | Email recipient(s) override. |
| `--email-from ADDRESS` | No | Email sender override. |
| `--hosting-url URL` | No | Hosting GIS Server URL for admin API and Data Store discovery. |
| `--folder NAME` | No | Admin services folder to scan (default: `Hosted`). Use `/` for the root folder. |
| `--arcgisinput PATH` | No | Path to `arcgisinput` (repeatable). |
| `--db-prefix NAME` | No | Database name used to anchor `db.schema.table` matches in `.msd` files. Defaults to the configured database name. |
| `--vview-suffix SUFFIX` | No | Versioned/editor-view suffix (repeatable; default: `_evw`). |
| `--db-host HOST` | No | Explicit override: force this Data Store host and bypass live PRIMARY auto-detection. |
| `--domain-suffix SUFFIX` | No | Domain appended to discovered short Data Store machine names. May be derived from a configured `db-host` FQDN when not supplied. |
| `--db-port PORT` | No | PostgreSQL port (default: `9876`). |
| `--db-sslmode MODE` | No | PostgreSQL TLS mode: `require`, `verify-ca`, or `verify-full` (default: `require`). |
| `--db-sslrootcert PATH` | No | PostgreSQL CA file for `verify-ca`/`verify-full`. Also accepted for `require`. |
| `--dbname NAME` | No | Database name override. |
| `--db-user USER` | No | PostgreSQL username override. |
| `--keyring-service-db NAME` | No | Database-password keyring service. |
| `--force` | No | Ignore the incremental cache and re-fetch every service JSON and `.msd`. |
| `--workers N` | No | Concurrent worker threads. Default: `24`; maximum: `64`. |
| `--no-lifecycle-probe` | No | Skip per-service `lifecycleinfos` probes and force fresh service reads instead of lifecycle-based cache reuse. |
| `--debug` | No | Enable verbose per-service / per-`.msd` debug logging. |
| `--tls-mode MODE` | No | TLS verification strategy: `os-trust-store`, `standard`, or `custom-ca-bundle`. Default: `os-trust-store`. |
| `--ca-bundle-path PATH` | Only with `custom-ca-bundle` | PEM CA bundle file path passed to the direct HTTP session and ArcGIS SDK connection. |
| `-h`, `--help` | No | Display help and exit. |

## Performance

Service definition reads and Portal item checks are concurrent. `.msd` files are scanned concurrently. The HTTP session uses retries and connection pooling. All other phases (service listing, Data Store discovery, database query, row reconciliation, CSV write) are single-threaded.

Lower `--workers` when the endpoint throttles or the client has limited resources. The default 24 workers work well for most organizations.

The script bounds queued concurrent work to about two times the configured worker count. HTTP responses are streamed, and `.msd` scans are bounded by explicit archive and file-size limits.

## Observed Runtime

> Runtime depends on service count, layer/table count, `.msd` file count, REST latency, cache hit rate, PostgreSQL latency, and whether lifecycle probes allow cache reuse.

| Run type | Observed duration | Feature services |
|---|---|---:|
| Portal (medium org, warm cache) | ~5 minutes | ~2,500 |
| Portal (large org, cold cache, with `.msd`) | ~20 minutes | ~5,500 |
| Portal (very large org, high workers) | ~35 minutes | ~12,000 |

## Output File

A successful run writes or attempts the following local files:

| File | Contents |
|---|---|
| `<ORG>_<PLATFORM>_DATASTORE_INVENTORY_<YYYYMMDD_HHMM>.csv` | Status-tagged reconciliation report. |
| `<ORG>_<PLATFORM>_inventory_cache.json` | Stable incremental cache containing service and `.msd` reference data for the current inventory context; not emailed. |
| `<ORG>_<PLATFORM>_ds_inventory_<YYYYMMDD_HHMM>.log` | Run log. |
| `<ORG>_<PLATFORM>_inventory_cache.json.lock` | Cross-process cache lock file used to prevent simultaneous cache writers. |

Example: `ORG_PORTAL_DATASTORE_INVENTORY_20260830_0015.csv`

The CSV is UTF-8 with a header row. A clean run produces a header-only CSV rather than no file. The log is placed in the email attachment list. The CSV is attached only when its size is within the shared attachment cap.

The cache file is intentionally stable across runs for incremental reuse; it is not timestamped. `--force` replaces the in-memory cache context for the run and repopulates it with fresh data.

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | The inventory workflow completed and the CSV was written successfully. The run may still be labeled `INCOMPLETE` in the report/email when individual coverage gaps or indeterminate rows were encountered. |
| `1` | Fatal error during output preparation, dependency validation, TLS/Portal setup, hosting/Data Store discovery, service enumeration, `.msd` processing, PostgreSQL setup/query, or another unrecovered processing failure. |
| `2` | Argument validation failure, such as a missing required `--ca-bundle-path` for `custom-ca-bundle`. |

## Notes and Caveats

- **No test mode.** Every run performs a full scan of the selected folder and `.msd` paths.
- **Portal-only.** AGOL is rejected.
- **Data Store host is auto-detected.** Live primary detection runs every time unless `--db-host` is given.
- **Hosting-server trust is verified.** The configured hosting URL must match an exact server entry in the authenticated Portal federation registry before the Portal token can be sent there.
- **Orphan coverage is schema-derived.** Only schemas found from service and `.msd` references are queried.
- **`.msd` scan is optional and best-effort.** Missing paths, unreadable archives, unrecognized files, and archive safety-limit failures are surfaced in the run and can make the inventory incomplete.
- **Service-definition failures can reuse cache.** Failed live reads reuse a usable prior cached record when available and mark that coverage stale.
- **The cache is stable, not timestamped.** It is keyed by the current Portal/hosting/folder/database/`.msd` context and protected by a cross-process lock.
- **`--no-lifecycle-probe` disables lifecycle-based cache reuse.** Service JSON is refetched instead of validated for reuse by `lifecycleinfos`.
- **Naming inference is a prefix test.** Longer matches win. It doesn't inspect foreign keys or database metadata.
- **Versioned/track recognition requires a referenced base.** A suffix match without a referenced base is treated through companion/orphan logic rather than assumed to belong to something.
- **Relation coverage is selective.** Only PostgreSQL relation kinds `r`, `p`, `v`, `m`, and `f` are included.
- **Platform labeling is URL-based.** This script requires an Enterprise Portal URL and rejects a non-Portal platform.
- **Email cap comes from the shared `MAX_ATTACHMENTS_MB` setting.** The CSV is omitted from email when it exceeds that cap; the CSV remains on disk.
- **Email delivery is nonfatal.** A run can exit 0 while SMTP delivery failed, provided the inventory workflow itself completed.
- **Cache writing is nonfatal.** A run can still complete when the cache cannot be written.
- **A successful scan can still be INCOMPLETE.** Coverage gaps and indeterminate reconciliation rows are retained, and missing references must not be treated as proof of an orphan relation.
- **PostgreSQL is read-only.** The connection is configured with `default_transaction_read_only=on`, a statement timeout, and the connection is closed after the snapshot.
- **Database TLS is separate from Portal TLS.** `--db-sslmode` controls PostgreSQL certificate behavior; `--tls-mode` controls the Portal/hosting HTTP and SDK connections.
- **`verify-full` needs a matching hostname.** When PostgreSQL hostname verification is enabled, the Data Store certificate must match the resolved database host.
- **The direct REST client refuses redirects.** It does not follow HTTP redirects for token-bearing requests.
- **Large input is bounded.** REST responses, cache files, `.msd` member count, member size, total uncompressed size, bytes read, and file count have explicit limits.
- **The import path is checked, not de-duplicated.** The script inserts the expected project root only when absent and verifies that the imported `UTILITIES` package came from the expected directory. It does not remove equivalent project-root entries already present in `sys.path`.