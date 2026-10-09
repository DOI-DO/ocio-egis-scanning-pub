# DEPRECATED_CONTENT.py

**Deprecated Configurable Web App / AppBuilder Extension Scan — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Deprecated Content](#why-inventory-deprecated-content)
- [Why it's a problem](#why-its-a-problem)
- [Why removal requires caution](#why-removal-requires-caution)
- [Recommendations for investigation](#recommendations-for-investigation)
- [Column descriptions](#column-descriptions)

### For Administrators
- [Dependencies on the shared UTILITIES package](#dependencies-on-the-shared-utilities-package)
- [Requirements](#requirements)
- [Security: Certificate (TLS) Validation](#security-certificate-tls-validation)
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

---

## For Report Readers

### Overview

This tool scans an ArcGIS Online organization or ArcGIS Enterprise Portal and creates a simple spreadsheet-style report of web apps that use technology identified in the script as deprecated. It identifies:

- **Configurable Web Apps** — older application templates matched by the configured URL patterns
- **Portal AppBuilder Extensions** — reported as deprecated by type on ArcGIS Enterprise

On ArcGIS Online, the script only reports a web mapping application when its application URL uses one of the deprecated patterns **and** the application's data contains a `webmap` or `group` reference. On Portal, an `AppBuilder Extension` is always included, while matching `/apps` URLs are also included.

The report shows *what* items were found, *who* owns them, what kind of item they are, how they are shared, how many views they have, when they were created or modified, and the application's URL. The tool only looks and reports — it never modifies, deletes, unprotects, or changes anything.

There are two ways to run it:

1. **Test mode (the default).** The tool inspects the first 250 candidate items. This is useful when setting up a new organization configuration or validating a scheduled job.
2. **Full mode.** Run with `--no-test` to inspect the full candidate population up to the script's 2,000,000-item safety limit. Large populations can use the date-range bucketing fallback.

### Why Inventory Deprecated Content

Deprecated application technology is a problem that grows quietly until a fixed deadline turns it into an emergency. Esri retires application frameworks on published schedules, and once a framework reaches its retirement date the applications built on it can stop rendering, lose functionality, or fail to load entirely. The difficulty is that nothing visibly changes in the organization until that date arrives: a Web AppBuilder app or an older configurable template keeps working day after day, so no one has any reason to look at it. An inventory is the deliberate act of looking before the deadline forces the issue, which is the entire reason to run this scan on a schedule rather than in a panic after apps break.

The scan is worth performing because deprecated apps are almost never centrally known. In most organizations these applications accumulate over years, built by staff who may have moved on, scattered across many owners, and shared at every level from private to fully public. No single administrator has a complete mental map of which apps exist, which ones rely on retiring technology, or who depends on them. This script replaces that missing map with a concrete, evidence-based list: it ties each deprecated app to an owner, a sharing level, a view count, and a live URL, so the organization can see the true size and shape of its exposure instead of guessing. Without that list, migration planning is impossible — you cannot migrate, replace, or retire applications you do not know you have.

Running the inventory also lets the organization triage intelligently rather than treating every app as equally urgent. Because AGOL results are sorted by view count, the apps that would affect the most users if they broke rise to the top, letting limited migration effort go where it matters first. A low-view app owned by a departed employee and a high-traffic public map carry very different risk, and only an inventory makes that distinction visible. Finally, this scan is deliberately read-only: it produces the information needed for a decision without taking any action, so it is safe to run early, run often, and run against production. The cost of running it is small; the cost of discovering a broken public-facing application from a user complaint — after the retirement deadline has already passed — is considerably larger.

### Why it's a problem

If an application depends on technology that is being retired and is not migrated in time, users may encounter failures such as:

- A public map application showing a blank page or an error.
- An internal workflow tool used by field staff no longer opening.
- Bookmarked links or embedded applications in websites and emails breaking silently.
- Users continuing to depend on an application even though its underlying technology is no longer supported.

A complete inventory gives the organization time to investigate, migrate, replace, or retire affected applications instead of discovering the problem after users report it.

### Why removal requires caution

Just because an app appears in this report does not mean it is safe to delete immediately. A deprecated application can still be important to a program, field team, website, or public-facing workflow.

The current CSV provides several useful investigation fields:

- **Who owns it** — the `owner` column identifies the account responsible for the item.
- **How widely it is shared** — the `access` value indicates the item's sharing level.
- **How much it is used** — `numViews` provides the recorded view count and, on AGOL, results are sorted with the most-viewed matching items first.
- **What it links to** — `app_url` is the running application URL and `item_url` opens the ArcGIS item details page.
- **When it changed** — `date_modified` and `date_created` help establish whether the item is still active or may be legacy content.

The current Python file does **not** output the `protected`, `numberOfSharedGroups`, or `groups_` columns referenced in older documentation. Those details should not be assumed to be present in the CSV produced by this version.

**This report is read-only.** It never modifies, deletes, or unprotects anything.

### Recommendations for investigation

- **Start with the most-used items.** On ArcGIS Online, candidate rows are sorted by `numViews` from highest to lowest, which can help prioritize applications that may affect more users.
- **Check the owner.** Contact the account listed in `owner` before migrating or removing an application.
- **Review sharing.** Use `access` to understand whether the item is private, organization-visible, shared, or public.
- **Open the item link.** Use `item_url` to review the application's current ArcGIS item details and dependencies.
- **Review the running URL.** Use `app_url` to understand what users currently open.
- **Check dates.** Compare `date_modified` and `date_created` to identify items that may be active, recently maintained, or long-lived legacy content.
- **Use full mode for a complete inventory.** Test mode only inspects the first 250 candidates; run with `--no-test` when the goal is the full configured population.

### Column descriptions

The output CSV contains these columns, in this exact order:

| Column | What it means |
|---|---|
| `platform` | Where the item lives: `PORTAL` for ArcGIS Enterprise or `AGOL` for ArcGIS Online. |
| `item_id` | The unique ArcGIS item ID. |
| `title` | The item's title. |
| `item_type` | The ArcGIS item type, such as `Web Mapping Application` or `AppBuilder Extension`. |
| `access` | The ArcGIS sharing/access value for the item. |
| `app_url` | The web address of the running application, when the item provides one. |
| `item_url` | A direct link to the item's ArcGIS details page. |
| `numViews` | The number of recorded views for the item. On AGOL, matching results are sorted by this value in descending order. |
| `date_modified` | The item modification date in `YYYY/MM/DD` format, UTC. |
| `date_created` | The item creation date in `YYYY/MM/DD` format, UTC. |
| `contentStatus` | The ArcGIS content status value, or blank when none is available. |
| `owner` | The username of the account that owns the item. |

> The current CSV does not contain `protected`, `numberOfSharedGroups`, or `groups_`. Investigation should use the columns listed above and the ArcGIS item page for additional sharing or protection details.

---

## For Administrators

### Dependencies on the shared UTILITIES package

- `gis_connect.py`
- `stack_discovery.py`
- `secure_session.py`
- `auth.py`
- `cli.py`
- `config.py`
- `emailer.py`
- `logging_setup.py`
- `restutils.py`
- `bucketing.py`

### Requirements

- Python with the ArcGIS API for Python (`arcgis`) available.
- Access to the target ArcGIS Online organization or ArcGIS Enterprise Portal.
- The shared `UTILITIES` package.
- Organization-specific TOML configuration supplied with `--config`.
- Matching `[portal]` or `[agol]` configuration selected by `--target`.
- Required configuration values: `org-name`, `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`.
- Account credentials available to `connect_gis()` and the shared authentication helpers.
- Write access to the output directory and access to the configured SMTP relay.
- The `truststore` package when using `--tls-mode os-trust-store`.

### Security: Certificate (TLS) Validation

The script uses TLS certificate validation for its connections, with an explicit verification mode for the startup handshake and the real ArcGIS SDK connection.

- **`standard`** uses the bundled public CA list.
- **`os-trust-store`** uses the operating system trust store for the startup handshake. The current code then uses the standard CA behavior for the real SDK connection unless a custom CA bundle is selected.
- **`custom-ca-bundle`** uses the PEM file supplied with `--ca-bundle-path` for both the startup handshake and the real ArcGIS SDK connection.

A one-time TLS handshake is performed before the scan connection is established. If that check fails, the run is aborted.

**SSRF/network-boundary defenses:** The script does not accept arbitrary target URLs as scan destinations. The target Portal/AGOL URL comes from the resolved configuration and is classified with `get_platform()`. For AGOL application matching, `_same_host()` compares the application URL hostname to the configured organization hostname exactly rather than using a loose substring test, preventing a lookalike hostname from passing the check. Large-search fallback attempts to use an organization-scoped search when an organization ID is available.

**Token handling:** The authenticated GIS connection is created through `connect_gis()`, and the authenticated token is wrapped by the shared `provider_for_gis()` `TokenProvider`. The current script does not store the token itself as a separate credential value in its scan logic, and exception text is passed through `redact_token()` where the script logs it.

**Large/malformed responses:** The current file does not implement an independent HTTP response-size limit. Application data is obtained through the ArcGIS SDK's `item.get_data()` method. Search and item-read failures are caught and counted so the run can report that some items could not be fully processed. The shared REST/SDK layers are responsible for their own response handling.

**Python import-path/package shadowing:** The current file inserts the project parent directory at the front of `sys.path` before importing `UTILITIES`. It does **not** remove equivalent copies of that path first and does **not** verify that the imported `UTILITIES` package actually came from the expected directory. Those stronger controls are therefore not implemented by this version of the script.

**Redirect following:** The current file does not explicitly set or enforce `allow_redirects=False` on HTTP requests because its item discovery and item-data reads are performed through the ArcGIS SDK. Redirect behavior is therefore delegated to the shared/SDK HTTP implementation rather than controlled at an individual request site in this file.

### Configuration

The required `--config` argument points to the organization's TOML file. The required `--target` argument selects the environment section within that config (`portal` or `agol`). Command-line values override configuration values.

Typical required settings are:

- `org-name`
- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

The script also resolves the shared email attachment limit through `get_max_email_mb()`.

**Example:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal
python DEPRECATED_CONTENT.py --config org2.config.toml --target agol
```

### How it works

1. **Configuration and setup** — Parse arguments, finalize organization configuration, build a timestamp, derive the organization name and platform label, prepare the output directory, and create the run log.
2. **TLS verification** — Run the one-time startup handshake check using the selected `--tls-mode`.
3. **Authentication** — Connect to the selected organization through `connect_gis()` and obtain the shared token provider.
4. **Search for candidates** — AGOL searches for `Web Mapping Application` items. Portal searches for `Web Mapping Application` and `AppBuilder Extension` items. In full mode, if flat search reaches the shared result ceiling, the script uses the bucketed date-range fallback.
5. **Filter for deprecated content** — AGOL checks the application URL, exact organization hostname, deprecated application patterns, and application data for a `webmap` or `group` reference. Portal reports `AppBuilder Extension` items and applications whose URLs match the deprecated pattern list.
6. **Build report rows** — Extract the fields defined in `FIELDNAMES` and write one row per deprecated item. AGOL filtering and row extraction use worker threads.
7. **Send completion email** — Email the log and CSV using the shared attachment-size policy.

**AGOL detection:** An item is reported when all of these are true:

- Type is `Web Mapping Application`.
- Its URL contains `/apps`.
- Its URL hostname exactly matches the configured organization hostname.
- Its URL matches one of the AGOL deprecated-application patterns.
- Its item data contains a `webmap` or `group` reference.

**Portal detection:** An item is reported when either:

- Its type is `AppBuilder Extension`; or
- Its URL contains `/apps` and matches one of the Portal deprecated-application patterns.

### Test mode (default)

**Test mode is on by default.** The script inspects only the first **250 candidate items**.

Pass `--no-test` for full mode, which scans up to **2,000,000 items** and can trigger the bucketed-search fallback when the flat search reaches its result ceiling.

Use test mode to validate connection, configuration, filtering, output, and email delivery before committing to a full inventory.

### Usage

**Test run — first 250 candidates:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal
python DEPRECATED_CONTENT.py --config org2.config.toml --target agol
```

**Full run — complete configured inventory:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test
python DEPRECATED_CONTENT.py --config org2.config.toml --target agol --no-test
```

**Full run with output directory override:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target agol --no-test --output-dir "D:\reports"
```

**Full run with more worker threads:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test --workers 16
```

**With custom CA certificate:**

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

**Show help:**

```bash
python DEPRECATED_CONTENT.py --help
```

#### Usage Scenarios

**Scenario: Sanity check after setting up a new organization configuration**

You've just created `org1.config.toml` for a Portal environment and want to verify the connection, credentials, filtering, CSV creation, and email delivery before a full scan.

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal
```

This runs in test mode, inspecting only the first 250 candidates.

**Scenario: Sanity check against AGOL**

Validate an ArcGIS Online configuration before committing to a full-organization inventory.

```bash
python DEPRECATED_CONTENT.py --config org2.config.toml --target agol
```

**Scenario: Full inventory before a migration or retirement project**

Your organization is planning to migrate or retire deprecated application technology and needs the broadest inventory the script can produce.

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test
```

This scans the full matching population, up to the 2,000,000-item limit.

**Scenario: Full AGOL inventory**

Your ArcGIS Online organization needs a complete inventory of matching deprecated web mapping applications.

```bash
python DEPRECATED_CONTENT.py --config org2.config.toml --target agol --no-test
```

Because AGOL results are sorted by view count after filtering, the report can be reviewed starting with the most-viewed items.

**Scenario: Redirect output to a shared reports drive**

Your team keeps scan output on a shared network drive instead of the local path in the configuration.

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target agol --no-test --output-dir "D:\reports"
```

The CSV, log, and completion email use the resolved output directory.

**Scenario: Speed up a large-organization scan**

A large organization's filtering stage is taking longer than desired and the run host has capacity for more concurrency.

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --no-test --workers 16
```

Increasing `--workers` runs more concurrent filter and extraction tasks. Raise the value gradually and watch for throttling or load on the shared environment.

**Scenario: Verify an internal CA before a scheduled run**

Your Portal certificate is issued by an internal CA and you want to validate that the configured trust chain works before running unattended.

```bash
python DEPRECATED_CONTENT.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

This validates the startup handshake against the supplied CA bundle and uses that same bundle for the real SDK connection.

### Command-line options

| Argument | Required | Default | Description |
|---|---|---|---|
| `--config` | Yes | none | Organization-specific TOML config file. |
| `--target` | Yes | none | `portal` or `agol`; selects the environment section in the config. |
| `--no-test` | No | test mode on | Switch to full mode and inspect the full candidate population up to 2,000,000 items. |
| `--portal-url` | No | from config | Override the configured Portal or AGOL URL. |
| `--username` | No | from config | Override the configured login username. |
| `--keyring-service` | No | from config | Override the configured keyring service. |
| `--output-dir` | No | from config | Override the output directory. |
| `--smtp-server` | No | from config | Override the SMTP relay. |
| `--email-to` | No | from config | Override the recipient list; accepts a comma-separated list. |
| `--email-from` | No | from config | Override the sender address. |
| `--workers` | No | `10` | Number of concurrent worker threads. |
| `--tls-mode` | No | `standard` | TLS verification for the startup handshake: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | Requires `--tls-mode custom-ca-bundle` | none | Path to a PEM CA bundle file. |

### Performance

The scan uses a thread pool controlled by `--workers` (default `10`). On AGOL, candidate filtering runs concurrently because item data must be read to verify references. Report-row extraction also uses the worker pool.

Candidate enumeration uses a flat search first. In full mode, if the flat search reaches the shared result ceiling, the script falls back to date-range bucketing. When an organization ID is available, the fallback adds an organization scope to the search.

For organizations below the result ceiling, the flat search avoids the additional bucketing work. For larger organizations, date-range bucketing allows the search to continue beyond the flat search limit.

### Observed Runtime

> Network latency, item complexity, platform throttling, worker count, and matching-item volume affect elapsed time.

Existing project observations are:

| Environment | Typical Duration | Item Volume |
|---|---|---|
| Portal | ~1–5 minutes | ~20,000 matching items |
| AGOL | ~1–5 minutes | ~100,000 matching items |

These are reference observations rather than guarantees. A full run can take longer when many item-data reads are required or the platform throttles requests.

### Output file

Files are written to the resolved output directory using a `YYYYMMDD_HHMM` timestamp. `[ORG]` is the filename-safe form of the configured organization name, and `[PLATFORM]` is `PORTAL` or `AGOL`.

```text
[ORG]_[PLATFORM]_DEPRECATED_CONTENT_YYYYMMDD_HHMM.csv
[ORG]_[PLATFORM]_deprecated_content_YYYYMMDD_HHMM.log
```

The CSV contains one row per deprecated item using the exact `FIELDNAMES` schema described above. The log contains search, filtering, row-processing, and run-summary diagnostics.

**Completion email** includes the organization, platform, run timestamp, mode (`TEST` or `FULL`), TLS mode, username, item counts, CSV path, and timing information. The shared `gather_attachments()` helper applies the resolved email attachment-size policy.

**Aborted runs** send an email with the log attached and exit with status `1`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Successful completion and the results email was sent. Certificate/TLS findings are not an issue for this script; deprecated content findings also do not change the exit code. |
| `1` | A fatal error occurred, such as inability to build the secure session, failed startup TLS handshake, GIS connection failure, or another unrecoverable runtime error. |
| `2` | CLI argument validation failure, including a missing required argument or `custom-ca-bundle` without `--ca-bundle-path`. |

### Notes and caveats

- **Test vs. full mode** — Default test mode inspects 250 candidate items. Use `--no-test` for a broader inventory up to the 2,000,000-item ceiling.
- **Config is always required** — `--config` is mandatory for organization configuration.
- **Platform is URL-derived** — `--target` selects the configuration section, while the platform label comes from the resolved `portal-url`.
- **Search fallback can be unscoped** — If `get_org_id()` cannot determine an organization ID, the bucketed fallback continues without the organization clause after logging a warning. For security and completeness, review such runs carefully.
- **AGOL and Portal detection differ intentionally** — AGOL uses a reduced deprecated-pattern list, exact organization-host matching, and an item-data `webmap`/`group` check. Portal includes `AppBuilder Extension` items.
- **One row per item** — The report contains one row per deprecated application item, not one row per internal reference.
- **Date columns are UTC** — `date_modified` and `date_created` use `YYYY/MM/DD` with day-level precision.
- **AGOL app-data read failures are counted separately** — If application data cannot be read, the item is not automatically classified as deprecated and the run summary reports the number of read failures.
- **Row-build failures are counted separately** — A deprecated item whose report row cannot be constructed is skipped and counted in the run summary.
- **Email attachment limits use the shared configuration** — The current script resolves the limit through `get_max_email_mb()` rather than using a fixed 15 MB value.
- **TLS mode has a split scope** — `standard` and `os-trust-store` govern the startup handshake; the current real SDK connection uses standard CA behavior unless `custom-ca-bundle` is selected, in which case the supplied bundle is used for both.
- **Read-only operation** — The script inventories and reports deprecated content only; it never modifies, unprotects, or deletes items.
- **Point-in-time snapshot** — Results reflect the state encountered during the run; later changes require another scan.