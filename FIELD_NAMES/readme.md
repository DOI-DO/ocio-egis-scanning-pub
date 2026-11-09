# FIELD_NAME_SCAN.py

Scan feature-service field names across an ArcGIS Enterprise Portal or ArcGIS Online (AGOL) organization for names that collide with database/vendor reserved keywords, violate ESRI field-name rules, expose an `SDE.` or `DBO.` schema prefix, or indicate duplicate/imported system fields. The script writes a detail CSV and a word-count CSV, then emails both reports. It never modifies anything — it only reads and reports.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Field Names](#why-inventory-field-names)
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

### Reference
- [Reserved-Keyword Sets](#reserved-keyword-sets)

---

# For Report Readers

## Overview

Every field in a map or feature layer (think "spreadsheet column header" like OWNER_NAME or PARCEL_ID) has a name. This tool checks every field name in your organization's published services and flags names that could cause problems. It doesn't fix anything — it only reads and reports.

Most databases and software products have a list of special words they treat differently (like `SELECT`, `TABLE`, or `ORDER`). If a field happens to be named one of these special words, or if the name breaks a basic naming rule, it can cause quiet failures — problems that don't show up as a clear error message, but rather as a search that returns no results, an edit that fails to save, or a form that works in one app but not another.

This tool gives you a shortlist of "field names worth checking" before these hidden problems turn into a support ticket or a failed migration.

## Why Inventory Field Names

A field name is a small thing that can cause big, confusing problems, and the reason to inventory names deliberately is that nothing about normal use ever puts them in front of you. People interact with layers through maps, forms, and apps, where a field is usually seen through its friendly alias rather than its real name — so a field whose actual name is a reserved word or breaks a naming rule can sit in a service for years, working in the one app it was built for, while quietly carrying a defect that only surfaces somewhere else. Taking an inventory of every field name in your organization lets you catch that trouble early, in one place, instead of discovering it one broken map or failed sync at a time. This scanner does not look for duplicate layers as separate ArcGIS items; it identifies duplicate/imported system fields within individual layers, along with other field-name problems. Field names are expected to follow a few simple rules:

1. **Start with a letter** — and contain only letters, numbers, and underscores after that. No spaces, punctuation, or special characters.
2. **Don't use reserved words** — different databases and software reserve certain words for their own purposes. Using one as a field name can break queries, edits, exports, or mobile forms.
3. **Don't expose system schema prefixes** — a field name beginning with `DBO.` or `SDE.` reveals database structure and is a security concern.
4. **Avoid duplicate system fields** — occasionally data imported into ArcGIS already has a column named `GlobalID` or `ObjectID`. ArcGIS renames the original and creates its own, which can confuse tools and break relationships.

The reason this particular scan is worth running is that a field name is portable in a way its problems are not: the same name that is harmless in one database or application becomes a hard failure in another. A name is only truly "legal" relative to wherever the data happens to live, and ArcGIS content moves — from AGOL to an enterprise database, from one DBMS to another, from a web map into a mobile form or an export. Each of those destinations enforces a different set of reserved words and rules, which is exactly why this scanner checks names against eight separate vendor keyword sets rather than one. Inventorying names ahead of a move turns a migration-breaking surprise into a reviewable list you can clear before the move, and it is far cheaper to rename a field on a copy beforehand than to diagnose why a sync or export silently failed afterward. The word-count report sharpens this further: a bad name repeated across dozens of services is usually a systemic habit — a template, a standard import step, a shared workflow — worth fixing at the source, and the count makes that pattern visible in a way that scanning services one at a time never would.

Two of the four rule families are worth inventorying for reasons beyond simple breakage. Schema-exposure prefixes (`DBO.`/`SDE.`) are a standing security concern even when the layer works perfectly today, because they leak internal database structure into a published, often publicly reachable, service — and the only way to know whether any exist across the whole organization is to look at every field name. Duplicate/imported system fields such as `GlobalID_1` or `OBJECTID_2` are quiet integrity problems: they usually mean the real system field was displaced during import, which can confuse tools that expect the canonical field and break relationships in ways that are very hard to trace back to their cause. A field name that looks fine in one context (for example, in a web map) can fail in another (for example, in a mobile form, an export, or after an upgrade). This report helps staff spot potential issues before those contexts arise. Because the scan is strictly read-only and produces only a shortlist to review — never a change — it is safe to run regularly as a standing governance check and especially before any audit, upgrade, or migration.

## Why It's a Problem

These naming problems usually don't show a clear error message. Instead, they show up as everyday things quietly not working:

- A query that mysteriously returns no results.
- An edit that silently fails to save.
- A map that works in one browser app but crashes in another.
- A mobile form whose sync fails with an unclear message.
- An export or report that won't build.

Because the symptoms are indirect, it's easy to spend hours troubleshooting the wrong thing. This report flags likely field names before deeper debugging begins.

The problems this report looks for include:

- **Reserved word conflicts** — The field name matches a word that has special meaning to a database or software product (Microsoft SQL Server, Oracle, PostgreSQL, SQLite, Databricks, Esri's file geodatabase, or ArcGIS Pro).
- **Illegal characters or length** — Esri requires field names to start with a letter and use only letters, numbers, and underscores, with a 128-character limit.
- **Exposed database schema prefixes** — A field name that begins with `DBO.` or `SDE.` reveals internal database structure.
- **Duplicate or "imported" system fields** — Names like `GlobalID_1` or `OBJECTID_2` usually mean the original data already had a column with that name, causing ArcGIS to create a duplicate. This can confuse tools that expect the "real" field.

## Why Removal Requires Caution

**Do not rename or delete a flagged field just because it appears in this report.** Some of what looks "wrong" may be structural or intentional:

- ArcGIS sometimes renames fields automatically during import — the field in this report may not be a mistake.
- Fields like `ObjectID` and `GlobalID` are structural. Deleting the wrong one of a duplicate pair can break the entire layer.
- A field that looks redundant might actually be the one ArcGIS is using, while the "normal-looking" one is just leftover data.

Always ask: Is this field actively used? Does any map, form, relationship, sync process, or external script depend on it? If a change is made, has it been tested on a copy first?

## Recommendations for Investigation

1. **Start with real symptoms.** Try to reproduce the actual issue (a failed edit, an empty query, etc.) on the affected layer before changing anything.
2. **Understand what's actually registered.** For duplicate GlobalID/ObjectID/editor-tracking fields, confirm which one ArcGIS is actually using internally before removing the other.
3. **Check the word-count report.** If the same offending field name shows up across dozens of services, that's usually higher-value to fix than a name appearing only once.
4. **Evaluate the specific finding.** Schema-exposure findings (`DBO.`/`SDE.` prefixes) are security concerns even if the layer works fine today. Reserved-word conflicts are more likely to cause actual failures than length violations.
5. **Test changes on a copy first.** Confirm that every dependent map, form, relationship, script, and integration still works after any rename.
6. **Review with stakeholders.** Confirm with the people who manage the layer whether it's still in active use and whether any change would be disruptive.

The `is_survey123` column indicates whether the service came from a Survey123 form, which can help distinguish an expected Survey123 naming quirk from a more general problem.

## Column Descriptions

### Detail Report (one row per offending field)

| Column | What it means |
|---|---|
| `platform` | `PORTAL` (your organization's Enterprise Portal) or `AGOL` (ArcGIS Online). |
| `title` | The name of the map/feature service containing this field. |
| `service_url` | The REST web address of the service, for direct lookup. |
| `owner` | The ArcGIS username that owns this service. Blank when a single service was scanned directly. |
| `layer_id` | The numeric ID of the specific layer or table within the service. |
| `layer_name` | The display name of that layer or table. |
| `field_name` | The actual field name that triggered the finding. |
| `field_alias` | The human-friendly "display name" for the field, if configured. |
| `is_survey123` | `Y` if the service came from Survey123, `N` otherwise. Survey123 has a known naming quirk (leading underscore) that this scan accounts for separately. |
| `problem` | Plain-language explanation(s) of what was flagged, separated by semicolons if multiple issues apply. |

### Word-Count Report (one row per distinct field name)

| Column | What it means |
|---|---|
| `field_name` | A field name that appeared at least once in the detail report. |
| `count` | How many times that exact field name appeared across all scanned services. A high count usually means the same naming habit is repeated widely. |

If a scan finds zero problems, neither CSV is created. The completion email will state that no findings were produced — this is a good outcome.

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

The script imports helpers from these files in the `UTILITIES` folder:

- `cli.py`
- `config.py`
- `gis_connect.py`
- `auth.py`
- `secure_session.py`
- `logging_setup.py`
- `restutils.py`
- `emailer.py`
- `arcgis_rest.py`
- `bucketing.py`
- `exclusions.py`

## Requirements

- Python 3.10 or later.
- `arcgis`, `requests`, `urllib3`, `keyring`.
- `truststore` (only for `--tls-mode os-trust-store`).
- An organization TOML passed via `--config`.
- A keyring credential (or `PORTAL_PASSWORD`/`AGOL_PASSWORD` env var).
- An account that can enumerate and read service definitions; Portal admin access for full federated discovery.
- Network access to Portal/AGOL REST and SMTP, plus write access to the output directory.

## Security: Certificate (TLS) Validation

This script never disables certificate verification. TLS is always on; `--tls-mode` selects *which* trust source is used, not *whether* verification occurs. Two separate connections are verified on every run:

1. **The ArcGIS SDK connection** (via `connect_gis()`), which verifies TLS using your chosen `--tls-mode`.
2. **This script's own direct HTTP session**, used for every service-definition, field, and related-items REST call, also verified per `--tls-mode`:
   - `standard` (default) — Verifies against the public CA bundle in `certifi`. Correct for AGOL and any Portal with a publicly trusted certificate.
   - `os-trust-store` — Verifies against the operating system's certificate store. Use for internal Portal certificates already trusted at the OS level (requires `truststore`).
   - `custom-ca-bundle` — Verifies against a specific PEM file via `--ca-bundle-path`.

Before any API traffic is sent, the script runs a one-time TLS handshake check against your Portal URL using these same settings. If that check fails, the run aborts immediately.

**Tokens are never handled as bare strings.** They are wrapped in a `TokenProvider` immediately after connecting, the token is exposed only at the request boundary, and any text that could embed a token — including exception messages — is redacted before logging or emailing. The token is also never sent to a service origin outside the authenticated Portal/federation boundary unless you explicitly authorize that origin.

**Network-boundary / SSRF defenses.** Portal REST URLs are restricted to the authenticated Portal origin and the expected `/sharing` or `/portaladmin` path boundaries. Service URLs must use HTTPS, must be under an ArcGIS `/rest/services/` path, and must match a FeatureServer/MapServer root or numeric sub-layer. Automatically discovered cross-origin services are not contacted unless the origin is recognized as hosted/federated or explicitly authorized. Anonymous service access is also restricted to exact explicitly configured origins.

**Large and malformed responses.** Direct JSON responses are streamed and limited to 64 MB. The script checks `Content-Length` when supplied, enforces the same byte limit while reading, requires valid UTF-8 JSON, requires a JSON object at the REST response root, and validates expected list/object shapes before using them. Search pagination and totals are also validated so malformed or inconsistent catalog responses cannot silently appear complete.

**Redirect handling.** Automatic HTTP redirect following is explicitly disabled. Redirect destinations, when the server returns one, are processed by the script and revalidated against the same HTTPS, origin, path, and token-destination policy before another request is attempted, with a maximum of 3 redirects.

**Python import-path/package shadowing.** The script inserts the expected project root at the front of `sys.path` before importing `UTILITIES`, then verifies that the loaded package actually resides in the expected `UTILITIES` directory. The current file does not remove other equivalent project-root entries before insertion.

## Configuration

Every run requires an organization-specific TOML file:

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal
```

### Example Configuration

```toml
[common]
org-name = "ORG1"
output-dir = "C:\\temp\\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "sender@example.com"
max-email-mb = 20

[portal]
portal-url = "https://portal.example.com/portal"
username = "portal_service_account"
keyring-service = "PORTAL"

[agol]
portal-url = "https://example.maps.arcgis.com/home"
username = "agol_service_account"
keyring-service = "AGOL"
```

### Configuration Precedence

1. Command-line override
2. Selected target section: `[portal]` or `[agol]`
3. `[common]`

### Required Settings

- `org-name` (config only)
- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

`max-email-mb` is optional. When absent or invalid, the shared default of 20 MB is used.

Store credentials in the OS keyring, not in TOML:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('AGOL', 'agol_service_account', '<password>')"
```

## How It Works

### Coverage

The scan excludes content owned by these built-in ESRI accounts: `esri`, `esri_nav`, `esri_livingatlas`, `esri_boundaries`, `esri_demographics`, `esri_apps`, `esri_en`.

**Portal target.** Searches for Feature Service and Map Service items. Hosted services are kept; non-hosted services are kept only when their URL matches a federated server root. Services that are neither hosted nor federated are dropped. Federation metadata is required for federated discovery; if it cannot be read safely, the run aborts rather than silently treating the deployment as hosted-only.

**AGOL target.** Searches for Feature Service items, scoped to your authenticated organization. If the organization ID cannot be resolved, no services are returned.

### Run Sequence

1. Parse arguments and load the organization TOML.
2. Validate required settings and create the output directory.
3. Initialize logging and build this script's TLS session.
4. Run a one-time TLS handshake check against the target URL.
5. Authenticate to Portal/AGOL via `connect_gis()`.
6. Wrap the token in a `TokenProvider`.
7. Enumerate services or read a single service.
8. Filter excluded services (if any) and apply test-mode limit.
9. Scan services concurrently, collecting fields with findings.
10. Build the field-name word-count summary.
11. Write the report CSVs (only if rows exist).
12. Apply the report attachment policy and send the completion email.

### Field-Name Evaluation

The script applies four rule families in deterministic order. Findings are joined with semicolons.

**Rule set 1: Reserved keywords by vendor.** Each name is compared (case-insensitive) against keyword sets for ESRI File Geodatabase, ESRI ArcGIS Pro, Microsoft SQL Server, PostgreSQL, Oracle, SQLite, ESRI fully qualified names, and Databricks. Portal applies all eight sets; AGOL applies five (ESRI File Geodatabase, ESRI ArcGIS Pro, PostgreSQL, SQLite, Databricks). The full keyword sets are listed under [Reserved-Keyword Sets](#reserved-keyword-sets).

When a name appears in every applicable vendor set, the finding collapses to: `Field name contains a Reserved Keyword listed by all software and database vendors and is definitely causing a problem`

Otherwise, one message is appended per matching set in the order listed above.

**Rule set 2: ESRI character rules.** A field name passes only when both are true:
- Matches `^[A-Za-z][A-Za-z0-9_]*$`
- Length ≤ 128 characters

A violation produces: `Field name violates ESRI field name rules`

Six computed-geometry expressions are exceptions: `Shape.STLength()`, `Shape.STArea()`, `DirtyArea.STArea()`, `DirtyArea.STLength()`, `st_area(shape)`, `st_length(shape)`.

For Survey123-origin services, a field beginning with `_` receives a BUG-000183727 advisory instead of outright rejection.

**Rule set 3: Schema-exposure prefix.** Any field name beginning with `DBO.` or `SDE.` (case-insensitive) produces: `Exposes SDE schema: security violation`

**Rule set 4: Duplicate/imported system fields.** A field matching `GLOBALID_<digits>` or `OBJECTID_<digits>` is flagged when the unsuffixed base name exists or the field has the corresponding system type. Editor-tracking suffixed fields (`created_by_1`, etc.) are flagged only when their unsuffixed base is also present.

### Survey123 Detection

Survey123 status is resolved once per service:
1. If item `typeKeywords` contain `survey123` or `survey2service`, the service is flagged as Survey123.
2. If keywords are unavailable, the script queries `relatedItems` for a `Survey2Service` relationship.
3. Missing IDs, REST errors, or empty results yield `False` (no extra request required for that service).

## Test Mode (Default)

**Test mode is enabled by default** and limits deployment-wide scans to the first 250 discovered services. Use it to validate credentials, visibility, and output before a full run.

Pass `--no-test` to scan every discovered service:

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --no-test
```

`--service-url` scans exactly one service root or sub-layer and ignores test mode, enumeration, and group exclusions.

## Usage

Each scenario below describes a real situation, the command to run, and what to expect.

### Scenario 1 — Quick Credential and Visibility Check on Portal

**Situation:** You've just created a new TOML file and want to confirm the script can authenticate and enumerate services before committing to a long run. Because test mode is the default, this scans only the first 250 discovered services — enough to prove connectivity and see sample output quickly.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal
```

**Expect:** A short run, a completion email, and (if any of the first 250 services have flagged fields) two CSVs. If no problems appear in that sample, the email will say so.

### Scenario 2 — Quick Credential and Visibility Check on AGOL

**Situation:** Same first-run confidence check, but against your ArcGIS Online organization instead of Portal. Useful to confirm the AGOL service account and org scoping resolve correctly.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target agol
```

**Expect:** The scan is scoped to your authenticated org. If the org ID can't be resolved, no services are returned (fail-closed) and the log will explain why.

### Scenario 3 — Full Governance Sweep of Portal Before an Audit

**Situation:** Your GIS team needs a complete inventory of every feature/map service field name ahead of a compliance review. You disable test mode so nothing is skipped.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --no-test
```

**Expect:** A longer run covering all discovered hosted and federated services, with full detail and word-count CSVs suitable for handing to auditors.

### Scenario 4 — Full Inventory Before an AGOL-to-Enterprise Migration

**Situation:** You're moving hosted AGOL content into an enterprise database (SQL Server, Oracle, PostgreSQL, etc.) and need to know which field names will collide with the destination DBMS *before* the migration breaks something.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target agol --no-test
```

**Expect:** A full AGOL inventory. Note that the AGOL rule set excludes Microsoft SQL, Oracle, and ESRI fully qualified keywords; if your destination is one of those databases, also review the [Reserved-Keyword Sets](#reserved-keyword-sets) for that vendor manually.

### Scenario 5 — Excluding One Approved Legacy Group

**Situation:** A specific group of older services has already been reviewed and approved with nonstandard field names. You want them out of the report so real findings stand out. The group ID must be the full 32-character hexadecimal ArcGIS item/group ID.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --exclude-group-id 0123456789abcdef0123456789abcdef
```

**Expect:** Every item belonging to that group is dropped before scanning. The log records how many items the exclusion contributed.

### Scenario 6 — Excluding Multiple Approved Groups

**Situation:** Approved exceptions span more than one group (for example, one per division). You pass a comma-separated list of full 32-character hexadecimal group IDs.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --exclude-group-id 0123456789abcdef0123456789abcdef,abcdef0123456789abcdef0123456789,1234567890abcdef1234567890abcdef
```

**Expect:** Items from all listed groups are excluded. An invalid or unreadable group ID aborts the run; review the error and correct the configuration before retrying.

### Scenario 7 — Investigating a Single Problem Service

**Situation:** A user reported a Survey123 form failing to sync, and you suspect a field-naming conflict. Rather than sweep the whole org, you scan just that one service.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --service-url https://host.example.com/server/rest/services/Foo/FeatureServer
```

**Expect:** Only that service is read; test mode, enumeration, and group exclusions are ignored. The `owner` column will be blank because single-service metadata is limited.

### Scenario 8 — Investigating a Single Sub-Layer

**Situation:** You've narrowed the problem to one specific sub-layer within a multi-layer service and don't want noise from the other layers.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --service-url https://host.example.com/server/rest/services/Foo/FeatureServer/3
```

**Expect:** Only layer 3 is evaluated. This is the fastest way to confirm or rule out a naming issue on a single layer.

### Scenario 9 — Throttled Endpoint

**Situation:** Your organization's endpoint throttles aggressively under high concurrency, causing errors or slowdowns at the default 64 workers. You reduce the worker count to ease pressure.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target agol --no-test --workers 24
```

**Expect:** Fewer simultaneous requests, a gentler load on the endpoint, and typically fewer transient errors at the cost of a longer total runtime.

### Scenario 10 — Internal Portal with Organizational CA

**Situation:** Your Portal's certificate is signed by an internal CA that is already trusted at the operating-system level, so you don't need a custom PEM file — you just point the script at the OS trust store.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --tls-mode os-trust-store
```

**Expect:** TLS verification uses the OS certificate store (requires the `truststore` package). The one-time handshake check confirms trust before any traffic is sent.

### Scenario 11 — Internal Portal with Custom CA Bundle

**Situation:** Your organization distributes a specific PEM file for internal services rather than installing the CA system-wide. You point the script directly at that file.

```bash
python FIELD_NAME_SCAN.py --config ORG.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
```

**Expect:** TLS is verified against that PEM bundle only. If `--ca-bundle-path` is omitted with this mode, the run exits with code 2.

## Command-Line Options

| Option | Required | Purpose |
|---|---:|---|
| `--config TOML_PATH` | Yes | Path to the organization-specific TOML file. |
| `--target {portal,agol}` | Yes | Selects discovery behavior and keyword rule set. |
| `--portal-url URL` | No | Connection URL override. |
| `--username USERNAME` | No | Login username override. |
| `--keyring-service NAME` | No | Keyring service-name override. |
| `--smtp-server HOST` | No | SMTP relay override. |
| `--email-to ADDRESS_LIST` | No | Recipient override (comma/semicolon-separated accepted). |
| `--email-from ADDRESS` | No | Sender override. |
| `--output-dir PATH` | No | Reports and log directory. |
| `--exclude-group-id GROUP_ID[,...]` | No | Exclude services whose item IDs belong to supplied group(s). |
| `--service-url URL` | No | Scan one FeatureServer/MapServer root or sub-layer only. |
| `--trusted-service-origin HTTPS_ORIGIN` | No | Additional exact HTTPS origin authorized to receive the Portal token (repeatable). Hosted/federated origins are learned automatically. |
| `--anonymous-service-origin HTTPS_ORIGIN` | No | Exact HTTPS origin that discovered items may be queried anonymously (repeatable). |
| `--no-test` | No | Disable the default 250-service cap. |
| `--workers N` | No | Concurrent service-definition workers. Default: `64`; maximum `64`. |
| `--tls-mode MODE` | No | TLS verification: `standard`, `os-trust-store`, or `custom-ca-bundle`. Default: `standard`. |
| `--ca-bundle-path PATH` | No | PEM CA bundle path (required only with `custom-ca-bundle` mode). |
| `-h`, `--help` | No | Display help and exit. |

## Performance

- Service discovery uses date-range bucketing to handle result sets exceeding the 10,000-item search cap.
- Service definitions are read concurrently with 64 workers by default; `--workers` may be set from 1 through 64.
- HTTP connection pool is sized from `--workers` and retries 429/500/502/503/504 responses.
- Survey123 status normally comes from `typeKeywords`, avoiding an extra request.
- Field-name evaluation uses a precomputed union set and fast legality checks before walking vendor sets.
- Group exclusions are applied before test-mode slicing.

Lower `--workers` when an endpoint throttles or when the client host has limited resources.

## Observed Runtime

> These are operational observations, not guarantees. Runtime depends on matching services, layers/tables and fields, Portal/AGOL latency, worker count, and whether per-layer fallbacks are required.

| Run type | Observed duration | Feature services |
|---|---|---|
| Portal | ~15-30 minutes | ~5,500 |
| AGOL | ~5-15 minutes | ~20,000 |

## Output File

A run creates three local files. The two CSVs are created only when at least one field problem exists.

**Detail report:**
```text
<ORG>_<PLATFORM>_FIELD_NAME_SCAN_<TARGET>_<YYYYMMDD_HHMM>.csv
```

**Field-name count report:**
```text
<ORG>_<PLATFORM>_FIELD_NAME_SCAN_<TARGET>_<YYYYMMDD_HHMM>_WORD_COUNTS.csv
```

**Run log:**
```text
<ORG>_field_name_scan_<target>_<YYYYMMDD_HHMM>.log
```

Example:
```text
ORG_PORTAL_FIELD_NAME_SCAN_PORTAL_20260829_1756.csv
ORG_PORTAL_FIELD_NAME_SCAN_PORTAL_20260829_1756_WORD_COUNTS.csv
ORG_field_name_scan_portal_20260829_1756.log
```

- `<ORG>` is `org-name` after filename sanitization.
- `<PLATFORM>` is the uppercase result from `get_platform()` (may be `UNKNOWN`).
- `<TARGET>` is `PORTAL` or `AGOL` from `--target`.
- The log uses lowercase target and does not include the platform label.
- The timestamp is local time in `YYYYMMDD_HHMM` form.

The detail report contains one row per offending field. The word-count report contains one row per distinct `field_name`, sorted by count descending and then by field name ascending (case-insensitive).

When no problems are found, no CSVs are written, and the completion email states that no findings were produced.

## Exit Codes

| Exit code | Meaning |
|---:|---|
| `0` | Scan completed COMPLETE. All discovered services/layers were read successfully. |
| `1` | Run aborted (config/connection/TLS failure), OR the scan finished INCOMPLETE because one or more services/layers could not be fully read. Check the log and email. |
| `2` | Argument or configuration validation failed (missing/invalid `--config`, missing `org-name`, invalid `--target`, or missing `--ca-bundle-path` with custom-ca-bundle mode). |

## Notes and Caveats

- **The report is a lead, not a diagnosis.** A keyword hit does not prove the field is causing an active failure.
- **Keyword sets are baked into the source.** They are not read from disk at run time.
- **ODBC keyword concerns are outside scope.** Microsoft future-reserved words and ODBC-specific restrictions are not evaluated.
- **Computed-geometry exceptions are exact and fixed.** Only the six listed expressions bypass the character rule.
- **Schema-prefix detection is textual.** Any name beginning with `DBO.` or `SDE.` is flagged.
- **Duplicate detection is layer-scoped.** It compares fields only within one layer.
- **Editor-tracking duplicates need the base field.** An isolated `created_by_1` without `created_by` is not flagged.
- **Survey123 fallback is best-effort.** Missing item ID, errors, or empty results become `N`; the scan continues.
- **Portal federation discovery requires sufficient privileges.** Without access to `/portaladmin/federation/servers`, coverage is limited to hosted services.
- **AGOL org scoping is fail-closed.** If the organization ID cannot be resolved, no services are returned.
- **Group-exclusion failures are fatal.** An invalid or unreadable group ID aborts the run; correct the ID or access problem before retrying.
- **Test mode is based on discovery order.** It takes the first 250 services after filtering, not a random sample.
- **Service-root definitions may omit fields.** The script makes one numbered-layer fallback request per layer; a failed fallback skips that layer.
- **Per-service failures are contained.** Worker exceptions are logged; that service contributes no rows, and the remaining scan continues.
- **Missing Map Services are a clean skip.** A 404 from a MapServer root is treated as out of scope for a field-name scan; FeatureServer 404s and all other errors count as scan errors.
- **Incomplete runs fail closed.** If any service/layer can't be read, the run reports INCOMPLETE and exits `1`; confirmed findings are kept, but missing rows are not proof of compliance.
- **Discovered cross-origin services are not contacted by default.** Only hosted, federated, or explicitly authorized origins receive the token; others require `--anonymous-service-origin` or `--trusted-service-origin`.
- **Platform labeling is hostname-based.** `UTILITIES.restutils.get_platform()` classifies `*.arcgis.com` as `AGOL` and any other hostname as `PORTAL`.
- **Single-service metadata is limited.** The owner is blank when a service is scanned directly.
- **The email cap measures report files, not the log.** The log remains in attachments even when reports exceed the cap.
- **No CSV means no findings.** The absence of a CSV is not a write failure; check the completion email.
- **Email failure is not a scan failure.** `send_email()` logs the exception and returns; reports remain on disk.
- **TLS verification is always on.** `--tls-mode` selects *which* trust source is used, not whether verification occurs.

---

# Reference

## Reserved-Keyword Sets

These are the exact keyword sets provided by the software vendor(s) compiled into the script.

### ESRI File Geodatabase

| | | | | |
|---|---|---|---|---|
| ALTER | AND | BETWEEN | BY | COLUMN |
| CREATE | DELETE | DROP | EXISTS | FOR |
| FROM | GROUP | IN | INSERT | INTO |
| IS | LIKE | NOT | NULL | OR |
| ORDER | SELECT | SET | TABLE | UPDATE |
| VALUES | WHERE | | | |

### ESRI ArcGIS Pro

| | | | |
|---|---|---|---|
| DAY | MONTH | TABLE | TEXT |
| USER | WHEN | WHERE | YEAR |

### Microsoft SQL Server

| | | | | |
|---|---|---|---|---|
| ALL | ALTER | AND | ANY | AS |
| ASC | AUTHORIZATION | BACKUP | BEGIN | BETWEEN |
| BREAK | BROWSE | BULK | BY | CASCADE |
| CASE | CHECK | CHECKPOINT | CLOSE | CLUSTERED |
| COALESCE | COLLATE | COLUMN | COMMIT | COMPUTE |
| CONSTRAINT | CONTAINS | CONTAINSTABLE | CONTINUE | CONVERT |
| CREATE | CROSS | CURRENT | CURRENT_DATE | CURRENT_TIME |
| CURRENT_TIMESTAMP | CURRENT_USER | CURSOR | DATABASE | DBCC |
| DEALLOCATE | DECLARE | DEFAULT | DELETE | DENY |
| DESC | DISK | DISTINCT | DISTRIBUTED | DOUBLE |
| DROP | DUMP | ELSE | END | ERRLVL |
| ESCAPE | EXCEPT | EXEC | EXECUTE | EXISTS |
| EXIT | EXTERNAL | FETCH | FILE | FILLFACTOR |
| FOR | FOREIGN | FREETEXT | FREETEXTTABLE | FROM |
| FULL | FUNCTION | GOTO | GRANT | GROUP |
| HAVING | HOLDLOCK | IDENTITY | IDENTITYCOL | IDENTITY_INSERT |
| IF | IN | INDEX | INDEXED | INITIALLY |
| INNER | INSERT | INSTEAD | INTERSECT | INTO |
| IS | JOIN | KEY | KILL | LEFT |
| LIKE | LINENO | LOAD | MERGE | NATIONAL |
| NOCHECK | NONCLUSTERED | NOT | NULL | NULLIF |
| OF | OFF | OFFSETS | ON | OPEN |
| OPENDATASOURCE | OPENQUERY | OPENROWSET | OPENXML | OPTION |
| OR | ORDER | OUTER | OVER | PERCENT |
| PIVOT | PLAN | PRECISION | PRIMARY | PRINT |
| PROC | PROCEDURE | PUBLIC | RAISERROR | READ |
| READTEXT | RECONFIGURE | REFERENCES | REPLICATION | RESTORE |
| RESTRICT | RETURN | REVERT | REVOKE | RIGHT |
| ROLLBACK | ROWCOUNT | ROWGUIDCOL | RULE | SAVE |
| SCHEMA | SECURITYAUDIT | SELECT | SEMANTICKEYPHRASETABLE | SEMANTICSIMILARITYDETAILSTABLE |
| SEMANTICSIMILARITYTABLE | SESSION_USER | SET | SETUSER | SHUTDOWN |
| SOME | STATISTICS | SYSTEM_USER | TABLE | TABLESAMPLE |
| TEXTSIZE | THEN | TO | TOP | TRAN |
| TRANSACTION | TRIGGER | TRUNCATE | TRY_CONVERT | TSEQUAL |
| UNION | UNIQUE | UNPIVOT | UPDATE | UPDATETEXT |
| USE | USER | VALUES | VARYING | VIEW |
| WAITFOR | WHEN | WHERE | WHILE | WITH |
| WITHIN GROUP | WRITETEXT | | | |

### Oracle

| | | | | |
|---|---|---|---|---|
| ADD | ALL | ALTER | AND | ANY |
| AS | ASC | AUDIT | BETWEEN | BY |
| CHAR | CHECK | CLUSTER | COLUMN | COLUMN_VALUE |
| COMMENT | COMPRESS | CONNECT | CREATE | CURRENT |
| DATE | DECIMAL | DEFAULT | DELETE | DESC |
| DISTINCT | DROP | ELSE | EXCLUSIVE | EXISTS |
| FILE | FLOAT | FOR | FROM | GRANT |
| GROUP | HAVING | IDENTIFIED | IMMEDIATE | IN |
| INCREMENT | INDEX | INITIAL | INSERT | INTEGER |
| INTERSECT | INTO | IS | LEVEL | LIKE |
| LOCK | LONG | MAXEXTENTS | MINUS | MLSLABEL |
| MODE | MODIFY | NESTED_TABLE_ID | NOAUDIT | NOCOMPRESS |
| NOT | NOWAIT | NULL | NUMBER | OF |
| OFFLINE | ON | ONLINE | OPTION | OR |
| ORDER | PCTFREE | PRIOR | PUBLIC | RAW |
| RENAME | RESOURCE | REVOKE | ROW | ROWID |
| ROWNUM | ROWS | SELECT | SESSION | SET |
| SHARE | SIZE | SMALLINT | START | SUCCESSFUL |
| SYNONYM | SYSDATE | TABLE | THEN | TO |
| TRIGGER | UID | UNION | UNIQUE | UPDATE |
| USER | VALIDATE | VALUES | VARCHAR | VARCHAR2 |
| VIEW | WHENEVER | WHERE | WITH | |

### PostgreSQL

| | | | | |
|---|---|---|---|---|
| ALL | ANALYSE | ANALYZE | AND | ANY |
| ARRAY | AS | ASC | ASYMMETRIC | BOTH |
| CASE | CAST | CHECK | COLLATE | COLUMN |
| CONSTRAINT | CREATE | CURRENT_CATALOG | CURRENT_DATE | CURRENT_ROLE |
| CURRENT_TIME | CURRENT_TIMESTAMP | CURRENT_USER | DEFAULT | DEFERRABLE |
| DESC | DISTINCT | DO | ELSE | END |
| EXCEPT | FALSE | FETCH | FOR | FOREIGN |
| FROM | GRANT | GROUP | HAVING | IN |
| INITIALLY | INTERSECT | INTO | LATERAL | LEADING |
| LIMIT | LOCALTIME | LOCALTIMESTAMP | NOT | NULL |
| OFFSET | ON | ONLY | OR | ORDER |
| PLACING | PRIMARY | REFERENCES | RETURNING | SELECT |
| SESSION_USER | SOME | SYMMETRIC | TABLE | THEN |
| TO | TRAILING | TRUE | UNION | UNIQUE |
| USER | USING | VARIADIC | WHEN | WHERE |
| WINDOW | WITH | | | |

### SQLite

| | | | | |
|---|---|---|---|---|
| ACTION | ADD | AFTER | ALL | ALTER |
| ALWAYS | ANALYZE | AND | AS | ASC |
| ATTACH | AUTOINCREMENT | BEFORE | BEGIN | BETWEEN |
| BY | CASCADE | CASE | CAST | CHECK |
| COLLATE | COLUMN | COMMIT | CONFLICT | CONSTRAINT |
| CREATE | CROSS | CURRENT | CURRENT_DATE | CURRENT_TIME |
| CURRENT_TIMESTAMP | DATABASE | DEFAULT | DEFERRABLE | DEFERRED |
| DELETE | DESC | DETACH | DISTINCT | DO |
| DROP | EACH | ELSE | END | ESCAPE |
| EXCEPT | EXCLUDE | EXCLUSIVE | EXISTS | EXPLAIN |
| FAIL | FILTER | FIRST | FOLLOWING | FOR |
| FOREIGN | FROM | FULL | GENERATED | GLOB |
| GROUP | GROUPS | HAVING | IF | IGNORE |
| IMMEDIATE | IN | INDEX | INDEXED | INITIALLY |
| INNER | INSERT | INSTEAD | INTERSECT | INTO |
| IS | ISNULL | JOIN | KEY | LAST |
| LEFT | LIKE | LIMIT | MATCH | MATERIALIZED |
| NATURAL | NO | NOT | NOTHING | NOTNULL |
| NULL | NULLS | OF | OFFSET | ON |
| OR | ORDER | OTHERS | OUTER | OVER |
| PARTITION | PLAN | PRAGMA | PRECEDING | PRIMARY |
| QUERY | RAISE | RANGE | RECURSIVE | REFERENCES |
| REGEXP | REINDEX | RELEASE | RENAME | REPLACE |
| RESTRICT | RETURNING | RIGHT | ROLLBACK | ROW |
| ROWS | SAVEPOINT | SELECT | SET | TABLE |
| TEMP | TEMPORARY | THEN | TIES | TO |
| TRANSACTION | TRIGGER | UNBOUNDED | UNION | UNIQUE |

### ESRI Fully Qualified

| | | | |
|---|---|---|---|
| AREA | EMAXX | EMAXY | EMAXZ |
| EMINX | EMINY | EMINZ | ENTITY |
| LEN | MAX_MEASURE | MIN_MEASURE | NUMOFPTS |
| POINTS | | | |

### Databricks

| | | | |
|---|---|---|---|
| CROSS | CURRENT_ | DEFAULT | EXCEPT |
| FALSE | FULL | INNER | INTERSECT |
| JOIN | LATERAL | LEFT | MASK |
| MINUS | NATURAL | NULL | ON |
| RIGHT | SEMI | TRUE | UNION |
| USING | | | |