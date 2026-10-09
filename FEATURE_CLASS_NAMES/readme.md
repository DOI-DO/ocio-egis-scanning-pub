# FEATURE_CLASS_NAME_SCAN.py

Read-only scan of ArcGIS Enterprise hosted feature-service table NAMES, checked against the **actual underlying Data Store (SDE) table name** — not the published service's layer/alias name — for compliance with Esri's feature class/table naming rules. The Data Store's live PRIMARY machine is auto-detected on every run — HA-aware, so it works correctly whether the Data Store is a single machine or a two-machine primary/standby pair.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Feature Class Names](#why-inventory-feature-class-names)
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

Every map or data layer published to your Portal is backed by a real table stored in a database (the "Data Store"). The friendly name you see in Portal (like "Trail Boundaries 2024") is separate from the actual database table name behind it — and that's perfectly fine for daily work, since most people never need to know the internal name.

This tool checks those **internal, behind-the-scenes database table names** against naming rules that Esri (the maker of ArcGIS) requires. It flags any table whose real name breaks these rules, but **only** when that table is still connected to something people are actively using — a live map or service with a Portal item associated with it.

Think of this like checking the "engine serial number" on every car rather than just looking at the "make/model" sticker on the dashboard. Everything can look fine on the dashboard while the engine's own paperwork has a problem.

**The scan makes no changes.** It doesn't rename, delete, or alter any table, service, or Portal item — it only reads and reports what it finds.

## Why Inventory Feature Class Names

The core reason to run this scan is that the thing it checks is invisible from every surface people normally look at. The useful distinction for a non-technical reader is that two Portal layers can have similar or completely different display names while their underlying Data Store tables are separate database objects. This scan is concerned only with the underlying database name and whether that name follows the configured compliance rules. Because the display name and the internal table name are independent, a layer can present a perfectly clean, friendly title in Portal while its real table name silently breaks the rules — and nobody browsing the catalog, editing data, or viewing a map would ever see it. An inventory is the only practical way to look behind the dashboard at the engine's own paperwork across the whole organization at once.

This particular check is worth running because non-compliant internal names are latent defects: they cost nothing on an ordinary day and everything on a bad one. A rule-breaking table name typically works fine for routine viewing and editing, so there is no natural moment when it announces itself. The problems it causes are deferred to exactly the moments when an organization can least afford them — an upgrade, a data migration, a backup/restore, or integration with a system that assumes standard naming. Inventorying the names ahead of time converts a future surprise during a stressful operation into a routine item reviewed on a calm day. It also catches names that drift into non-compliance long after publication, because manual edits, outside imports, enabling version tracking, or republishing can introduce a naming problem years after the original layer was created, where a one-time check at publication would have missed it entirely.

Two design choices make this scan especially valuable rather than just noisy. First, it deliberately correlates each non-compliant table with whether a live Portal item still depends on it, so the report separates real operational risk from harmless background noise: a badly-named table nothing uses is not listed in the default report, while one that a live, actively used map or service depends on is. That turns a raw list of naming defects into a prioritized list of names that actually matter to current users and workflows. Second, the same pass surfaces a related but distinct integrity problem — `MISSING_DB_TABLE` rows, where a service points at a database table that no longer exists — so running the inventory doubles as a check that the service-to-database links underneath your Portal are still intact. Finally, because the scan is strictly read-only and uses a single targeted catalog query rather than renaming anything or sweeping the whole database, it is safe to run regularly as a standing compliance and integrity check, giving administrators current evidence about the health of the names their production services quietly depend on.

## Why It's a Problem

**Problems from bad table names often hide until something stressful happens.** A table with a rule-breaking name usually works fine for everyday editing and viewing — sometimes for months or years. The issue typically surfaces later: during a system upgrade, data migration, backup/restore, or when connecting to another system that expects standard naming. By then, it's harder to diagnose because everything looks fine in Portal.

**Reserved-prefix names confuse automated tools.** Names starting with `gdb_`, `sde_`, or `delta_` risk being mistaken for the database software's own housekeeping tables. Automated maintenance tools might skip them, and manual troubleshooting can accidentally affect the wrong tables.

**A table can "go bad" long after it was published.** Manual edits, imports from outside systems, enabling version tracking, or republishing can introduce a naming problem years after the original layer was created.

**But urgency depends on whether anyone is using it.** A badly-named table that nothing is using is just background noise. A badly-named table that a live, actively used map or service depends on is a real risk to current users and workflows. This report makes that distinction: **it only lists tables that are both improperly named and tied to a Portal item.**

There is also an important distinction between a naming violation and a broken service reference. The current report can contain `MISSING_DB_TABLE` rows when the service refers to a database table that no longer exists. That is primarily a service-to-database linkage problem, even when the name is also non-compliant.

## Why Removal Requires Caution

Finding a table on this report is a signal to **review it**, not a directive to change or delete it immediately. Renaming a table that a live service depends on is a disruptive operation. Users' bookmarks, saved app configurations, and other systems may depend on that exact internal name continuing to exist — even though it's technically non-compliant.

Treat every row on this report as something to **investigate and discuss with stakeholders**, not as a to-do item for automatic action:

- First, confirm the service and the Portal item are genuinely in active use.
- Contact the item's owner to understand the impact of any change.
- Any rename should go through your organization's normal republishing or migration process — never rename a live production table directly just because it appeared on this report.
- A row marked `MISSING_DB_TABLE` (see [Column Descriptions](#column-descriptions)) means the map/service is pointing at a table that no longer exists — that's a broken link problem to investigate and fix first, not a naming issue.

## Recommendations for Investigation

1. **Find the service** — Use the service name and admin URL columns to locate the actual service in your hosting server.
2. **Confirm active use** — Use the Portal item ID and title columns to identify the owner and verify with them (or check your organization's records) whether it's still being used.
3. **Read the specific problem** — The `name_problem` column explains what's wrong:
   - A reserved-prefix problem (`gdb_`/`sde_`/`delta_`) is more urgent because it risks confusing the database software itself.
   - A character or length problem is less urgent but still needs review.
4. **Handle broken links first** — If a row shows `MISSING_DB_TABLE`, treat it as a broken service-to-table reference first. Find out why the service still points to a nonexistent table before worrying about its name.
5. **Follow your standard process** — Any rename must go through your organization's documented republish or data migration procedures.
6. **Keep records** — Document which rows you reviewed, what you found, and what decisions were made.

## Column Descriptions

The current Python file writes **20 columns** to both report types, although the default violations report contains only rows with a naming violation and an associated Portal item ID. The optional `--include-all-rows` CSV contains every evaluated service-referenced table row.

| Column | Meaning |
|---|---|
| `platform` | Environment scanned. This script is Portal-only, so a normal run reports `PORTAL`. |
| `status` | Database mapping result. `MAPPED_OK` means the referenced table was found. `MISSING_DB_TABLE` means the service references a table not found in the live Data Store. `DB_LOOKUP_UNKNOWN` means the table reference could not be parsed well enough for a database lookup. `DB_LOOKUP_AMBIGUOUS` means an unqualified table name matched multiple schemas. |
| `table_name` | Normalized internal database table name used for matching and display. The parser keeps the final two identifier segments and uses lower case for the normal display representation. |
| `raw_table_name` | Internal table name as it was reported by the service definition before normalization. |
| `db_exists` | `yes` when the table was found in the Data Store, `no` when it was not, or `unknown` when existence could not be determined. |
| `db_relkind` | Database object type reported by PostgreSQL: `table`, `partitioned table`, `view`, `materialized view`, or `foreign table`. Blank when no object was resolved. |
| `db_owner` | PostgreSQL role that owns the database object. Blank when no object was resolved. |
| `service_name` | FeatureServer service name using the table. |
| `service_type` | Service type. For this scanner, the collected services are `FeatureServer`. |
| `service_admin_url` | Hosting GIS Server administrative URL for the service. |
| `layer_id` | Layer/table ID from the FeatureServer definition when provided. |
| `layer_name` | Published display name of the layer/table when provided. This is not the underlying database table name used for compliance. |
| `kind` | `layer` for a map layer or `table` for a standalone table entry. |
| `portal_item_ids` | Portal item IDs associated with the service, joined with semicolons when multiple IDs are present. |
| `portal_items_exist` | Portal-item existence result: `yes`, `no`, `partial`, `unknown`, or `n/a`. |
| `portal_item_titles` | Titles of Portal items confirmed to exist, joined with semicolons. |
| `portal_item_missing` | Portal item IDs confirmed to be missing, joined with semicolons. |
| `name_checked` | The exact unqualified Data Store table name evaluated against the naming rules. |
| `name_problem` | Plain-language description of naming violations. A blank value means the name passed the configured naming rules. Multiple violations are separated by semicolons. |
| `notes` | Additional context. The current implementation uses this primarily for `DB_LOOKUP_UNKNOWN`, `DB_LOOKUP_AMBIGUOUS`, and `MISSING_DB_TABLE` explanations. |

### Naming rules used by the report

The script evaluates the **actual internal Data Store table name**, not the Portal layer alias or display name.

1. **Character rule:** the final table name must match `^[A-Za-z_][A-Za-z0-9_]*$` and be no longer than 128 characters.
2. **Forbidden prefixes:** names beginning with `gdb_`, `sde_`, or `delta_` are reported as non-compliant, case-insensitively.

A row is included in the default violations report only when **both** conditions are true:

- `name_problem` is not blank; and
- `portal_item_ids` contains at least one Portal item ID.

A `MISSING_DB_TABLE` row can still appear in the violations report if its table name is also non-compliant and the service has a Portal item.

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

- `auth.py`
- `cli.py`
- `config.py`
- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `secure_session.py`
- `stack_discovery.py`
- `emailer.py`

## Requirements

- Python 3 with `requests`, `keyring`, and `psycopg2` or `psycopg2-binary`.
- ArcGIS Enterprise Portal and hosting-server admin access required by the scan.
- PostgreSQL/Data Store account with read access to `pg_class`, `pg_namespace`, and `pg_roles`.
- Shared `UTILITIES` package in the expected project layout.
- Organization TOML configuration file.
- Writable output directory and SMTP access.
- `truststore` is required only when using `--tls-mode os-trust-store`.

## Security: Certificate (TLS) Validation

The script uses **two separately secured TLS paths**.

**Portal / ArcGIS SDK connection:** `connect_gis()` always uses TLS verification. There is no insecure mode for the SDK connection. When `custom-ca-bundle` is selected, the configured PEM CA bundle is passed to `connect_gis()`.

**Direct REST session:** the hosting-server admin API and Portal item-existence REST calls use this script's own explicit TLS strategy:

- `standard` — verifies against the standard public CA bundle.
- `os-trust-store` — verifies against the operating-system trust store and requires `truststore`.
- `custom-ca-bundle` — verifies against the PEM bundle supplied through `--ca-bundle-path`.

Before the scan proceeds, the script performs a real TLS handshake against both the Portal and hosting-server HTTPS bases. A failed handshake aborts the run.

**SSRF / network-boundary defenses:** The script does not allow arbitrary token-bearing REST destinations. Direct REST requests are permitted only to trusted HTTPS origins established from the configured Portal, the configured hosting server, and federation metadata. Additional exact HTTPS origins may be supplied with `--trusted-service-origin` for an unusual reverse-proxy topology. The value must be an exact HTTPS origin with no path, query, fragment, or user-info. This prevents a service URL from redirecting the scan's organization token to an unrelated destination.

**Token handling:** Portal authentication is centralized through the shared `provider_for_gis()` `TokenProvider`. Direct REST requests obtain the current token at request time. Authentication failures such as HTTP 401, 498, 499, invalid-token, or expired-token conditions trigger one controlled token refresh/retry under a lock. Errors are passed through `redact_token()` before logging or email.

**Large / malformed responses:** The script validates the expected shape of search, Data Store discovery, service definition, Portal item, and count responses before processing them. Malformed responses become controlled errors rather than being interpreted as successful results. This file does not define an independent maximum HTTP response-body size; response-size handling beyond the explicit structural validation is provided by the shared session/REST helpers.

**Python import-path / package shadowing:** The script inserts the expected project root into `sys.path`, imports `UTILITIES`, and verifies that the imported package came from the expected `UTILITIES` directory. The stronger cleanup step of removing equivalent path spellings before insertion is **not** implemented in this file.

**Redirect following:** Direct REST calls explicitly use `allow_redirects=False`. Redirects are not followed automatically, so an unexpected redirect cannot transparently carry the authenticated request somewhere else.

**Database security:** The PostgreSQL connection is encrypted according to `--db-sslmode`. The default is `require`, which encrypts the connection but does not verify the server certificate. `verify-ca` and `verify-full` require `--db-sslrootcert`; `verify-full` also verifies the server identity. The database transaction is explicitly read-only and uses a 300-second statement timeout.

**Read-only behavior:** The scanner reads hosting-server metadata, Portal item metadata, and PostgreSQL catalog information. It does not rename tables, modify services, alter Portal items, or change database content.

## Configuration

Every run requires an organization-specific TOML configuration. This is a **Portal-only** scanner; there is no AGOL mode and no `--target` command-line option.

### Example configuration

```toml
[portal]
org-name = "ORG"
portal-url = "https://portal.example.com/portal"
username = "portal_service_account"
keyring-service = "PORTAL"
output-dir = "C:\\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com, administrator@example.com"
email-from = "sender@example.com"
hosting-url = "https://hosting.example.com/server"
db-host = "datastore.example.com"
domain-suffix = "example.com"
db-port = 9876
dbname = "egdb"
db-user = "datastore_monitor"
keyring-service-db = "DATASTORE"
db-sslmode = "require"
```

### Required settings

The shared configuration requires:

- `org-name`
- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

This script additionally requires:

- `hosting-url` — Hosting GIS Server admin base URL.
- `dbname` — PostgreSQL/Data Store database name.
- `db-user` — PostgreSQL account used for the Data Store catalog query.
- `keyring-service-db` — Keyring service used for the Data Store password.

### Optional settings

- `db-host` — Normally auto-detected as the live Data Store PRIMARY; `--db-host` explicitly overrides discovery.
- `domain-suffix` — Used when auto-discovered Data Store machine names are short names. If omitted, the script attempts to infer a suffix from the configured `db-host` FQDN.
- `db-port` — Defaults to `9876`.
- `db-sslmode` — Defaults to `require`; choices are `require`, `verify-ca`, and `verify-full`.
- `db-sslrootcert` — Required when `db-sslmode` is `verify-ca` or `verify-full`.
- `folder` — Defaults to `Hosted`.
- `tls-mode` — Defaults to `standard`.
- `max-email-mb` — Shared email attachment-size policy.

### Credentials

Passwords are not stored in the script or TOML.

**Portal password lookup:**

1. Keyring service from `keyring-service` and username from `username`.
2. The `PORTAL_PASSWORD` environment variable if the keyring does not provide a password.

**Database password lookup:**

1. Keyring service from `keyring-service-db` and username from `db-user`.
2. The `PGPASSWORD` environment variable if the keyring does not provide a password.

Example keyring setup:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('DATASTORE', 'datastore_monitor', '<password>')"
```

### Configuration precedence

For settings with a command-line option:

1. Command-line value
2. Target configuration section
3. `[common]` configuration value, where supported
4. Script default, where one exists

## How It Works

### Run sequence

1. Parse arguments and load the organization TOML.
2. Validate required settings, dependencies, and command-line combinations.
3. Build the output directory and reserve unique report/log filenames.
4. Build the direct TLS-verified `requests.Session` and verify TLS against the Portal and hosting-server bases.
5. Connect to Portal through the ArcGIS SDK and acquire a token provider.
6. Verify that the configured hosting URL is registered in Portal federation metadata.
7. Detect the live Data Store PRIMARY machine unless `--db-host` is explicitly supplied.
8. List `FeatureServer` services in the selected hosting-server admin folder.
9. Read each service's admin JSON concurrently, extract service-referenced table names, and collect associated Portal item IDs.
10. Check each unique Portal item ID once and cache the result.
11. Connect to PostgreSQL with the selected encrypted/read-only database configuration.
12. Run one targeted catalog query for all referenced schema/table pairs.
13. Build one output row per service-referenced table.
14. Evaluate the actual unqualified Data Store table name against the naming rules.
15. Write the default violations CSV and, when requested, the unfiltered all-rows CSV.
16. Send the completion email and return the documented process status.

### Data Store primary discovery

By default, the script asks the hosting server's `admin/data/findItems` endpoint for the `enterpriseDatabases` registration and determines the live `PRIMARY` machine. It is HA-aware: when there is one primary and a standby, the primary is selected. Ambiguous or incomplete topology information causes the run to abort rather than guessing.

Use `--db-host` only when you intentionally want to bypass live primary discovery and force a specific Data Store host.

### Extracting real table names

The script searches for the real table name in these locations within FeatureServer JSON:

- `jsonProperties.layers`
- `jsonProperties.tables`
- `layers`
- `tables`

It prefers `adminLayerInfo.tableName` but recursively searches for `tableName` as a fallback. PostgreSQL identifiers with doubled quotes are parsed, the relevant final identifier segments are retained, and the normalized display representation is lowercased.

### Extracting Portal Item IDs

The script reads `portalProperties.portalItems` from each service definition. Each unique Portal item ID is checked once through:

```text
<portal-url>/sharing/rest/content/items/<item-id>
```

The report summarizes the item checks as:

- `yes` — All referenced items exist.
- `no` — All referenced items are absent.
- `partial` — Some exist and some are absent.
- `unknown` — At least one check failed before a definitive result.
- `n/a` — No Portal item IDs were associated with the service.

### Confirming table existence

A single targeted PostgreSQL query checks only the schema/table pairs referenced by the services:

```sql
SELECT n.nspname AS schema, c.relname AS relname, c.relkind AS relkind,
       r.rolname AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.oid = c.relowner
JOIN unnest(%(schemas)s::text[], %(tables)s::text[])
    AS wanted(schema, relname)
    ON n.nspname = wanted.schema AND c.relname = wanted.relname
WHERE c.relkind IN ('r','p','v','m','f');
```

This is **not** a full Data Store catalog scan — it only asks about the specific tables referenced by the services.

### Naming compliance

The script checks only the final, unqualified table name against:

1. **Character rule:** Must match `^[A-Za-z_][A-Za-z0-9_]*$` and be ≤128 characters.
2. **Forbidden prefixes:** Case-insensitive check against `gdb_`, `sde_`, `delta_`.

If both checks fail, both violations appear in `name_problem` separated by a semicolon.

### Final report

Only rows with **both** a naming violation **and** a Portal item ID are included in the default violations CSV. A `MISSING_DB_TABLE` row can still appear if its name is also non-compliant and the service has a Portal item.

When `--include-all-rows` is used, the second CSV contains every evaluated service-referenced table row, including compliant rows and `MISSING_DB_TABLE` rows.

## Test Mode (Default)

This script has **no separate test/dry-run switch**. Every invocation performs a read-only scan of all `FeatureServer` entries in the selected folder.

For limited validation, use a non-production organization, or point `--folder` at a smaller admin services folder such as `QA`.

The script is read-only, so test validation does not modify Portal, services, or the database. The operational scope can still be large, so starting with a smaller folder is the safest way to validate configuration.

## Usage

### Scenario 1 — Routine organization-wide scan

A GIS administrator runs a regular check of the main `Hosted` folder to identify services whose actual Data Store table names violate the naming rules and are associated with Portal items.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml
```

### Scenario 2 — Validate against a QA folder first

Before scanning the production `Hosted` folder, an administrator wants to test the naming checks on a smaller QA services folder.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --folder QA
```

### Scenario 3 — Faster scan on a large deployment

An organization has thousands of FeatureServer services and wants to increase the number of concurrent service and Portal item checks.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --workers 32
```

The maximum supported value is 64.

### Scenario 4 — Override to a specific Data Store machine

An administrator needs to use a known Data Store host for troubleshooting or to bypass live primary auto-detection.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --db-host datastore.example.com
```

This completely bypasses automatic primary discovery for the run.

### Scenario 5 — Write all evaluated rows

An administrator wants a comprehensive file containing every service-referenced table, not just naming violations, to spot-check coverage or investigate orphaned references.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --include-all-rows
```

The default violations CSV is still written.

### Scenario 6 — Custom domain suffix for an auto-discovered Data Store host

The hosting server reports the Data Store machine by short name, and the organization's DNS suffix is not directly available from the current configuration.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --domain-suffix example.com
```

The suffix is used only when a discovered Data Store machine needs to be converted into a connectable FQDN.

### Scenario 7 — Use the OS trust store for Portal and hosting TLS

The Enterprise deployment uses an internal CA that is already trusted by the operating system.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --tls-mode os-trust-store
```

The `truststore` package is required.

### Scenario 8 — Use a custom CA bundle

The Enterprise deployment uses an internal CA that is not available in the normal public CA bundle.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

The same CA bundle is supplied to the ArcGIS SDK connection.

### Scenario 9 — Use a custom PostgreSQL certificate-validation mode

The Data Store administrator wants PostgreSQL server certificate validation in addition to encryption.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --db-sslmode verify-full --db-sslrootcert "C:\certs\datastore-root-ca.pem"
```

`verify-full` validates both the certificate chain and the server identity.

### Scenario 10 — Add an exact trusted service origin

The hosting topology uses a reverse proxy whose HTTPS origin is not discovered from Portal federation metadata. The administrator has independently verified that the origin is a legitimate service endpoint.

```bash
python FEATURE_CLASS_NAME_SCAN.py --config ORG.config.toml --trusted-service-origin https://services.example.com
```

The value must be an exact HTTPS origin with no path, query string, fragment, or user-info.

### Scenario 11 — Show help

Review the current command-line interface before wiring the scanner into a scheduled task.

```bash
python FEATURE_CLASS_NAME_SCAN.py --help
```

## Command-Line Options

| Option | Required | Default | Description |
|---|---:|---|---|
| `--config TOML_PATH` | Yes | none | Organization TOML configuration file. |
| `--portal-url URL` | No | from config | Portal URL override. The script requires a Portal URL. |
| `--username USERNAME` | No | from config | Portal username override. |
| `--keyring-service NAME` | No | from config | Portal password keyring service override. |
| `--output-dir PATH` | No | from config | Report and log directory override. |
| `--smtp-server HOST` | No | from config | SMTP relay override. |
| `--email-to ADDRESS_LIST` | No | from config | Email recipient(s) override. Commas and semicolons are accepted by the shared email helper. |
| `--email-from ADDRESS` | No | from config | Email sender override. |
| `--hosting-url URL` | No | config `hosting-url` | Hosting GIS Server admin base URL. |
| `--folder NAME` | No | `Hosted` | One ArcGIS Server services-folder name. Use `/` for the root folder. |
| `--db-host HOST` | No | auto-detected | Explicit Data Store host; bypasses live PRIMARY auto-detection. |
| `--domain-suffix SUFFIX` | No | inferred when possible | Suffix appended to an auto-discovered short Data Store machine name. |
| `--db-port PORT` | No | `9876` | PostgreSQL/Data Store port. |
| `--dbname NAME` | No | from config | Data Store database name. Required unless supplied through config. |
| `--db-user USER` | No | from config | PostgreSQL/Data Store user. Required unless supplied through config. |
| `--keyring-service-db NAME` | No | from config | Keyring service for the database password. Required unless supplied through config. |
| `--db-sslmode {require,verify-ca,verify-full}` | No | `require` | PostgreSQL TLS mode. `verify-ca` and `verify-full` require `--db-sslrootcert`. |
| `--db-sslrootcert PATH` | No | from config | PEM CA file used for PostgreSQL certificate validation. |
| `--workers N` | No | `24` | Concurrent worker threads; maximum `64`. |
| `--debug` | No | off | Enable verbose per-service debug logging. |
| `--include-all-rows` | No | off | Also write the unfiltered all-rows CSV. |
| `--trusted-service-origin HTTPS_ORIGIN` | No | none | Additional exact HTTPS origin trusted to receive the Portal token. May be specified more than once. |
| `--tls-mode {standard,os-trust-store,custom-ca-bundle}` | No | `standard` | TLS verification strategy for direct REST calls and the ArcGIS SDK connection. |
| `--ca-bundle-path PATH` | Conditional | none | PEM CA bundle; required when `--tls-mode custom-ca-bundle`. |
| `-h, --help` | No | — | Display help and exit. |

There is **no `--target` option** because this script is Portal-only.

There is **no `--no-auto-install` option** in the current Python file. If `psycopg2` is missing, the script reports the installation command and aborts instead of installing packages automatically.

## Performance

Network operations are concurrent:

- FeatureServer admin JSON reads use `--workers` threads (default 24).
- Portal item ID checks use the same worker pool.
- Each worker uses its own `requests.Session` through the session pool.
- The direct HTTP session uses three retries for recoverable errors and a connection pool scaled to the worker count.

Single-threaded phases include service listing, Data Store discovery, the targeted PostgreSQL table-existence query, row assembly, CSV writing, and email.

The table-existence check is **one targeted query** built with `unnest()` over the referenced schema/table pairs — not a per-table round-trip, and not a full catalog sweep. Runtime scales primarily with the number of FeatureServer services, service JSON size, number of service-referenced tables, and number of unique Portal item IDs checked; it does not scale with the total number of unrelated tables in the Data Store.

The temporary candidate collections are kept in Python objects for the service list being processed, while each worker receives isolated HTTP session state. The scan does not download feature records or geometries.

## Observed Runtime

Against a Portal with approximately 5,300 hosted feature services and 12,000 layer/table references, a full run including discovery, admin JSON collection, Portal item checks, and the targeted table-existence query completed in approximately **2.5 minutes**.

| Deployment Scale | Duration | Feature Services |
|---|---|---:|
| Medium to large | ~2.5 minutes | ~5,300 |

These are reference observations. Actual runtime depends on network latency, Portal performance, service complexity, the number of item IDs, worker count, and database response time.

## Output File

A normal successful run writes these files to `output-dir`:

| File | Contents |
|---|---|
| `<ORG>_<PLATFORM>_FC_NAME_VIOLATIONS_<YYYYMMDD_HHMM>.csv` | Filtered report containing naming violations that also have a Portal item ID. |
| `<ORG>_<PLATFORM>_fc_name_scan_<YYYYMMDD_HHMM>.log` | Execution log containing discovery, Data Store, Portal item-check, TLS, reporting, and timing information. |

When `--include-all-rows` is used, the script additionally writes:

| File | Contents |
|---|---|
| `<ORG>_<PLATFORM>_FC_NAME_ALL_ROWS_<YYYYMMDD_HHMM>.csv` | Unfiltered report containing every evaluated service-referenced table row. |

Example:

```text
ORG_PORTAL_FC_NAME_VIOLATIONS_20260902_1725.csv
ORG_PORTAL_FC_NAME_ALL_ROWS_20260902_1725.csv
ORG_PORTAL_fc_name_scan_20260902_1725.log
```

The CSVs are UTF-8 with headers. A run with zero naming violations still writes a header-only violations CSV when the required output write succeeds.

The `platform` column is present in both CSVs. The organization is represented in the filename, log, and email subject.

The completion email includes the organization, platform, database/hosting context, TLS settings, counts, output paths, and timing. The shared `gather_attachments()` helper applies the configured email attachment-size limit.

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | The main workflow completed, the required violations CSV was written, and the completion workflow finished. Email delivery failures are logged by the shared email helper and do not change this code. |
| `1` | A fatal run-level failure occurred, such as dependency failure, missing required setting, TLS setup/handshake failure, Portal connection failure, Data Store discovery failure, hosting-service enumeration failure, PostgreSQL failure, or required CSV-write failure. An abort email is attempted when available. |
| `2` | Argument or configuration validation failure, including invalid required values or `--tls-mode custom-ca-bundle` without `--ca-bundle-path`. A Portal-only target mismatch is also rejected with status 2 before the scan begins. |
| Other | An unhandled exception outside the explicit failure paths. |

`--help` exits normally.

## Notes and Caveats

- **Read-only does not mean small scope.** The script reads the entire selected FeatureServer folder by default.
- **Portal-only.** No AGOL mode and no `--target` option are supported by the current script.
- **One folder per run.** The default is `Hosted`; other folders require separate runs.
- **No `.msd` scanning.** Coverage is limited to what the FeatureServer admin JSON reports.
- **Data Store host is not assumed from static configuration.** Live primary detection runs every time unless `--db-host` is given; discovery failure or ambiguity is a hard abort.
- **Existence check is targeted, not exhaustive.** Only tables referenced by services are checked; unrelated/orphan database tables are outside the scan scope.
- **`MISSING_DB_TABLE` is a service-configuration problem first.** Investigate the service-to-table reference before treating the name violation as the main problem.
- **Portal-item checks are best-effort.** A failed item check is represented in the service's Portal-item status and contributes to the incomplete result state; a parseable item response with an unexpected ID is treated as an error rather than silently accepted.
- **Admin-JSON read failures are per-service, not automatically fatal.** Failed services contribute no parsed table rows and cause the final report to be marked `INCOMPLETE`.
- **The database query is read-only.** The PostgreSQL connection is explicitly configured read-only and uses a statement timeout of 300 seconds.
- **Email delivery is nonfatal after the main workflow completes.** Check the log if an expected email is not received.
- **Worker count is range-validated.** Use a value from 1 through 64.
- **TLS verification is always on.** `--tls-mode` chooses the trust source; it does not disable certificate verification.
- **Portal and hosting certificate checks happen before the scan.** A failed startup handshake aborts the run.
- **PostgreSQL TLS is separate from HTTPS TLS.** The default database mode `require` encrypts the connection but does not validate the PostgreSQL server certificate. Use `verify-ca` or `verify-full` with `--db-sslrootcert` when certificate validation is required.
- **Platform labeling is Portal-only.** The script rejects a configured URL classified as anything other than `PORTAL`.
- **FeatureServer endpoints are version-specific.** Validate behavior after major Enterprise upgrades.
- **Data Store discovery depends on correct `enterpriseDatabases` registration.** A discovery failure may indicate a hosting-server/Data Store registration issue before it indicates a database problem.
- **The default violations CSV is intentionally filtered.** A compliant service-referenced table is absent from that file unless `--include-all-rows` is used.
- **The optional all-rows CSV is broader than the violations report.** It includes compliant rows and orphan `MISSING_DB_TABLE` references for coverage review.
- **Naming checks use the unqualified internal table identifier.** Published layer aliases, display names, and service titles are not used to decide compliance.
- **A database object can exist without being a physical table.** `db_relkind` may report a view, materialized view, partitioned table, or foreign table if the referenced name resolves to one of those PostgreSQL relation types.
- **Ambiguous unqualified names are not guessed.** `DB_LOOKUP_AMBIGUOUS` indicates that the same relation name exists in multiple schemas and cannot be mapped safely without a schema qualifier.
- **The script verifies package origin but does not perform equivalent-path cleanup.** It inserts the expected project root and checks the imported `UTILITIES` location; it does not remove duplicate equivalent `sys.path` entries first.
- **Redirect following is disabled for direct REST calls.** Redirects are not automatically followed by the script's own HTTP client.
- **Point-in-time results.** A service or database mapping can change after the scan. Re-run before taking action on an older report.