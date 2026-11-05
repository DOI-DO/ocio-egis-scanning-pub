# EMPTY_FEATURE_SERVICES.py

**Empty Hosted Feature Service Scan - ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

This tool scans an organization's hosted feature services and reports every service where **all** layers and tables currently contain zero records.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Empty Feature Services](#why-inventory-empty-feature-services)
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

If you received a CSV file from this tool, this section explains what you're looking at - no technical background required.

This report lists **hosted feature services** in ArcGIS Online or your organization's ArcGIS Enterprise Portal that currently have **no data in them at all**. Not "low on data." Not "mostly empty." **Zero records, in every layer and every table.**

Each row shows one empty service, along with who owns it, when it was created, and when it was last changed. The tool doesn't delete anything - it simply points out services worth reviewing.

The script is deliberately conservative: a service is reported only when it has at least one layer/table and every readable layer/table count is exactly zero. A service with no layers or tables is not reported, and a service with a nonzero or unreadable layer/table count is not reported as empty.

## Why Inventory Empty Feature Services

Empty feature services are easy to create and almost impossible to notice afterward. A service can be published for a project that never started, left behind when a workflow is rebuilt, or spun up as a quick test and then forgotten. Because an empty service looks and behaves like any other item in the catalog - it has a title, an owner, a URL, and sharing settings - nothing about day-to-day use draws attention to the fact that it holds no data. Over months and years these empty shells accumulate silently, and no single administrator can recall which ones exist or why. This scan exists to surface that otherwise invisible population so it can be reviewed deliberately rather than stumbled upon.

The reason to inventory them specifically is that emptiness is a strong, cheap signal that something may be worth a closer look, while being far from a guarantee that anything is wrong. The relevant cleanup question is whether an empty service is redundant, abandoned, or intentionally waiting to be used, and that question can only be answered by first knowing which services are empty at all. An empty service may still be related to another item, map, app, workflow, or future project even though it currently has zero records. Reviewing those relationships in Portal or AGOL helps distinguish an intentionally empty service - a template, a data-collection target, a pipeline destination, or a seasonal service - from a true leftover. The inventory does not make that judgment for you; it narrows the entire organization down to the short list of services where that judgment is worth making.

Running this as a periodic scan rather than a one-time cleanup matters because the empty-service population is constantly changing. Services fill with data, get cleared and reloaded, or are newly published between runs, so a current inventory distinguishes a service that has been empty and untouched for years - a strong cleanup candidate - from one created last week that is simply waiting for its first load. A regular report also catches operational problems early: a data-collection layer that should be filling but stays empty, or a scheduled pipeline whose load step silently failed, both show up here as an unexpected empty service. In that sense the scan is as much an early-warning check on automated processes as it is a tidiness exercise, and because it is strictly read-only and downloads only record counts, it is safe to run often against production with minimal load.

## Why It's a Problem

- **Maintenance burden.** Backup jobs, catalog audits, and storage processes all have to account for it, even though it holds no data.
- **Resource usage.** An empty service keeps its own web address (URL), its own settings, and its own search listing - just like a full one.
- **Clutter.** Anyone browsing the organization's content has to sift past empty services to find things actually in use. It may still carry sharing permissions or be referenced by old maps or apps.
- **Signals a deeper issue.** An empty service can point to an abandoned project, an unfinished setup step, or an automated process that isn't working.

Looking at who owns each empty service and comparing creation and last-modified dates often reveals a pattern worth addressing.

## Why Removal Requires Caution

**Being on this list does not mean a service should be deleted.** There are several legitimate reasons a service can be empty:

1. **A genuine leftover** from a one-time task or finished project.
2. **Never used** - set up but data was never loaded.
3. **Intentionally empty and still needed:**
   - **A template** that defines field structure and style so people can copy it for new work.
   - **Set up ahead of time** waiting for an event, inspection, or field season to begin.
   - **A data-collection target** for Field Maps or Survey123 that starts empty and fills as work happens.
   - **A pipeline destination** that gets regularly cleared and reloaded by a scheduled process.
   - **Seasonal or cyclical** - populated only at certain times of year.

Think of this report as the **start of a conversation**, not a deletion checklist.

The scan is read-only. It does not delete, unshare, modify, or repopulate services.

## Recommendations for Investigation

Before acting on any item in this report:

- **Talk to the owner first.** The `owner` column tells you who to ask.
- **Check the dates.** A service created years ago and never modified since is a stronger cleanup candidate than one created last week.
- **Open the item** in Portal or ArcGIS Online and check its description, sharing, and whether any maps or apps reference it.
- **Ask if it fits one of the "legitimate reasons"** - a template, a waiting collection target, or a seasonal service.
- **Check the complete service definition.** Confirm that the service really has layers/tables and that every layer/table is still empty before taking action.
- **Review the scan status.** If the run was incomplete because of item or TLS errors, do not interpret missing rows as evidence that the affected services are non-empty.
- **Add exceptions to an exclusion list.** If a service is confirmed as intentionally empty (approved template, for example), ask the administrator to exclude it from future reports.

## Column Descriptions

| Column | Meaning |
|---|---|
| `platform` | Where the service lives: `PORTAL` (your organization's ArcGIS Enterprise Portal) or `AGOL` (ArcGIS Online). |
| `title` | The service's name in the catalog. |
| `item_id` | The service's unique identifier - use this to look it up directly. |
| `date_created` | When the service was created, formatted as `YYYY/MM/DD`. |
| `date_modified` | When the service was last modified, formatted as `YYYY/MM/DD`. |
| `owner` | The username of the person or account that owns the service. |

The report intentionally does **not** include service URLs, layer/table counts, sharing details, or which maps/apps reference the service - check those directly in Portal or AGOL before taking action.

One row is written for each confirmed fully-empty service. The CSV always includes its header when writing succeeds, even if no empty services are found.

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

- `auth.py`
- `bucketing.py`
- `cli.py`
- `config.py`
- `emailer.py`
- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `secure_session.py`

## Requirements

- Python 3 with the ArcGIS API for Python (`arcgis`) installed.
- Access to the target ArcGIS Online organization or ArcGIS Enterprise Portal with permission to search hosted services, inspect service definitions, and query layer/table counts.
- The shared `UTILITIES` package as a sibling directory (`.\UTILITIES`).
- An organization-specific TOML configuration file (`--config`).
- Required settings: `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, and `email-from`.
- Account password available to the shared authentication helper through the configured keyring.
- Write access to the output directory and access to the configured SMTP relay.
- Optional: the `truststore` package if `--tls-mode os-trust-store` is used.

## Security: Certificate (TLS) Validation

The script's direct REST calls use HTTPS with TLS certificate verification. There is no option to disable certificate validation. The ArcGIS SDK connection established through `connect_gis()` also verifies TLS.

**TLS modes:**

- `standard` (default) - verifies against the standard public CA set.
- `os-trust-store` - verifies against the operating system's native certificate store. The `truststore` package is required.
- `custom-ca-bundle` - verifies against an explicit PEM CA bundle supplied with `--ca-bundle-path`.

**Startup certificate check:** Before real scan traffic begins, the script performs a one-time TLS handshake check against the target URL. A failed check aborts the run.

**SSRF / network-boundary defenses:** The scan requires an authenticated organization ID before hosted-feature-service enumeration and refuses to run an unscoped catalog search. Candidate service URLs are parsed and restricted to HTTPS destinations inside the trusted Portal/federation boundary before the organization token is sent. In AGOL mode, the trusted boundary includes the Esri `arcgis.com` host suffix; in Portal mode, trusted origins are built from the Portal URL and federation metadata, with optional exact HTTPS origins accepted through `--trusted-service-origin` for unusual reverse-proxy topologies. The additional trusted-origin option is restricted to an exact HTTPS origin with no path, query string, fragment, or user-info.

**Token handling:** Authentication is provided through the shared `TokenProvider`. Direct REST requests send the current token only after destination validation. Recognized stale-token conditions such as HTTP 401/498/499, invalid-token messages, and expired-token messages trigger one controlled refresh/retry. Error text is passed through `redact_token()` before logging or email.

**Large / malformed responses:** The script does not define a maximum HTTP response-body size of its own. It does validate response shapes before processing: search results must be lists of objects; service definitions must be JSON objects with valid `layers` and `tables` lists; count responses must contain a valid nonnegative integer `count`; and temporary candidate records must deserialize to dictionaries. An invalid or unexpected response is treated as a check/run error rather than as evidence that a service is empty.

**Python import-path / package shadowing:** The script inserts the expected project root into `sys.path` before importing `UTILITIES` and verifies that the loaded package came from the expected `UTILITIES` directory. This prevents an unexpected package location from being silently accepted.

The current script does **not** remove equivalent copies of the expected project root from `sys.path` before insertion. That stronger cleanup step is not implemented here.

**Redirect following:** Direct REST requests explicitly use `allow_redirects=False`. Redirect destinations are resolved manually with `urljoin()` and validated again before the next request. Redirect processing is limited to `MAX_REDIRECTS` (3). This prevents an authenticated session from automatically following an untrusted redirect.

**Read-only:** The script only searches content, reads service definitions, queries counts, writes local files, and sends email. It does not modify ArcGIS items or services.

## Configuration

The required `--config` argument selects an organization-specific TOML file. The required `--target` argument selects the `portal` or `agol` environment within that file.

The required settings are:

```text
portal-url
username
keyring-service
output-dir
smtp-server
email-to
email-from
```

Command-line values override TOML values. The password is not stored in the script or TOML; the shared authentication helper retrieves it from the configured keyring.

The optional `--exclude-group-id` accepts one or more comma-separated group IDs. Group membership is resolved before the candidate service inventory is inspected. A requested exclusion group that cannot be resolved or read causes the run to abort rather than silently scanning excluded items.

The optional `--trusted-service-origin` may be specified more than once. Each value must be an exact HTTPS origin with no path, query, fragment, or user-info. This is intended for unusual reverse-proxy topologies where legitimate service endpoints are not discoverable from normal federation metadata.

The optional `max-email-mb` configuration value controls the email attachment limit used by the shared attachment helper.

## How It Works

1. **Resolve configuration and platform.** Parse the command line, load the target configuration, and classify the configured URL as `PORTAL` or `AGOL`.
2. **Build and verify TLS.** Create the direct REST session using the selected TLS mode and perform the startup handshake check.
3. **Connect to GIS.** Authenticate through `connect_gis()` and obtain the shared token provider.
4. **Build trusted service origins.** Use the Portal URL, configured URL, GIS URL, and, for Portal, federation server metadata. AGOL also allows the Esri `.arcgis.com` host boundary.
5. **Require organization scope.** Resolve the authenticated organization ID. If it cannot be resolved, the scan aborts instead of performing an unscoped search.
6. **Resolve exclusions.** Resolve requested group memberships and remove matching item IDs from the candidate set.
7. **Enumerate hosted feature services.** Search using the hosted-feature-service clause and strict date-range bucketing, with an internal limit of 2,000,000 items.
8. **Use a disk-backed candidate store.** Candidate metadata is held in a temporary SQLite database so very large organizations do not require the full search result set to remain in memory.
9. **Check each service concurrently.** For each candidate, resolve its FeatureServer root when necessary, read the service definition, inspect `layers` and `tables`, and query `returnCountOnly=true` for each sublayer/table.
10. **Classify emptiness.** Report the service only when it has at least one layer/table and every layer/table count is exactly zero.
11. **Write the CSV.** The report is written even when it contains zero rows; a successful write produces the CSV header.
12. **Send the completion email.** The email includes the run status, service counts, error counts, TLS failures, output location, timing, and available attachments.

### Search Completeness

The hosted-feature-service search is organization-scoped and uses strict date-range bucketing. In test mode the later scan stages stop at the 250-service limit. In full mode the limit is 2,000,000 services, and reaching that cap aborts the run because a complete inventory cannot be guaranteed.

### Empty-Service Classification

A service is reported only when it has at least one valid layer/table ID and **every** layer/table count is exactly `0`. If any layer/table returns a nonzero count, the service is not reported. If any required count request fails or returns an invalid count, the service is not reported as empty and the item is recorded as an error.

### Token Renewal

If a direct REST request returns a recognized authentication failure, the shared token is refreshed once under a lock and the request is retried. This prevents concurrent workers from repeatedly refreshing the same stale token.

### Candidate Storage

The scan uses a temporary, disk-backed SQLite candidate store with a uniqueness constraint on `item_id`. Duplicate search results retain first-insertion order while the latest returned metadata replaces the previous payload. The temporary database and associated journal/WAL files are removed after processing.

## Test Mode (Default)

**Test mode is on by default.** It limits the later scan stages to the first **250 hosted feature services**. Discovery still enumerates the organization into the temporary candidate store.

Pass `--no-test` for full mode. Full mode raises the internal limit to **2,000,000 hosted feature services** and treats reaching that cap as a run-level failure because completeness cannot be guaranteed.

Use test mode to validate credentials, connectivity, configuration, output creation, exclusions, and email delivery before a full run - not to produce a complete organization inventory.

## Usage

### Scenario 1: First-time validation run against a Portal organization

You've set up a new organization config and want to confirm credentials, connectivity, exclusions, output creation, and email delivery before a long full scan.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal
```

### Scenario 2: First-time validation run against ArcGIS Online

Same as above, but for an AGOL environment.

```bash
python EMPTY_FEATURE_SERVICES.py --config org2.config.toml --target agol
```

### Scenario 3: Full inventory of a Portal organization

The validation run looked good, and now you need a complete list for a quarterly cleanup review. `--no-test` removes the 250-service limit.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal --no-test
```

### Scenario 4: Full inventory with increased concurrency

The AGOL organization has 20,000+ hosted services. Raising `--workers` from the default of 32 speeds up the scan, at the cost of higher concurrent load.

```bash
python EMPTY_FEATURE_SERVICES.py --config org2.config.toml --target agol --no-test --workers 48
```

### Scenario 5: Test run excluding a known template group

The organization maintains an approved set of intentionally-empty templates that show up in every scan. Excluding that group keeps results focused on genuinely unreviewed candidates.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal \
    --exclude-group-id abc123def456
```

### Scenario 6: Full run excluding multiple reviewed groups

Exclude both an approved-templates group and a known staging group - both have been reviewed and confirmed as intentionally empty.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target agol --no-test \
    --exclude-group-id "abc123def456,789ghi012jkl"
```

### Scenario 7: Full run with custom output location and reduced concurrency

Reports need to go to a network drive. Concurrency is dialed back because the organization showed throttling at the default worker count.

```bash
python EMPTY_FEATURE_SERVICES.py --config org2.config.toml --target agol --no-test \
    --output-dir "D:\reports" --workers 16
```

### Scenario 8: Running against an internally-signed Enterprise Portal

The Portal's certificate is signed by your organization's internal Certificate Authority, which is already trusted at the OS level. Using `os-trust-store` lets verification succeed without relying on the public CA list.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal --tls-mode os-trust-store
```

### Scenario 9: Running with an explicit CA bundle file

Your internal CA is not in the OS trust store, but a PEM file is available. Point directly at that file for verification.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal \
    --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

### Scenario 10: Use an additional trusted service origin for a reverse proxy

A legitimate FeatureServer is reached through a reverse proxy whose exact HTTPS origin is not present in the normal federation metadata. Add the exact origin explicitly so authenticated item-supplied service requests can be sent there.

```bash
python EMPTY_FEATURE_SERVICES.py --config org1.config.toml --target portal \
    --trusted-service-origin https://services.example.com
```

Only use this option after independently confirming that the origin is an authorized service destination for the organization being scanned.

### Scenario 11: Check available options

Review every flag and its default before wiring this into a scheduled task.

```bash
python EMPTY_FEATURE_SERVICES.py --help
```

On Windows Command Prompt, replace backslash line continuations with `^` or place each command on one line.

## Command-Line Options

| Argument | Required | Default | Description |
|---|---:|---|---|
| `--config` | Yes | none | Path to the organization-specific TOML configuration file. |
| `--target` | Yes | none | `portal` or `agol`; selects the environment within the config. |
| `--no-test` | No | test mode on | Enables full mode; raises the later scan-stage limit from 250 to 2,000,000 services. |
| `--exclude-group-id` | No | none | One group ID or comma-separated group IDs whose member services are omitted from inspection. |
| `--portal-url` | No | from config | Overrides the configured Portal or AGOL URL. |
| `--username` | No | from config | Overrides the configured login username. |
| `--keyring-service` | No | from config | Overrides the configured keyring service. |
| `--output-dir` | No | from config | Overrides the configured output directory. |
| `--smtp-server` | No | from config | Overrides the configured SMTP relay. |
| `--email-to` | No | from config | Overrides the recipient(s). |
| `--email-from` | No | from config | Overrides the sender address. |
| `--workers` | No | `32` | Number of concurrent worker threads for emptiness checks; maximum `64`. |
| `--trusted-service-origin` | No | none | Additional exact HTTPS origin trusted to receive the Portal token; may be specified more than once. |
| `--tls-mode` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | Conditional | none | Path to a PEM CA bundle file. Required when `--tls-mode` is `custom-ca-bundle`. |

## Performance

Emptiness checks run concurrently with `--workers` threads (default: 32; maximum: 64). Each worker receives its own `requests.Session` while retaining the same TLS policy.

The direct REST connection pool is configured for concurrent connections and two retries. Each request has a 30-second timeout.

For each layer and table, the script sends a `where=1=1&returnCountOnly=true` query - no feature geometries or attribute rows are downloaded. A service is rejected as soon as one layer/table returns a nonzero count, so later sublayers are not queried.

Large organizations use a temporary SQLite candidate store so the full candidate inventory is not kept in memory. The candidate store de-duplicates item IDs and preserves the first insertion order while updating retained metadata from later search results.

Increasing `--workers` reduces elapsed time but increases concurrent load. Reduce the value if the organization shows throttling, timeouts, or request pressure.

### Observed Runtime

> Observed runtimes are approximations from full (`--no-test`) production runs at the default worker count. Actual duration depends on latency, service structure, throttling, exclusions, worker count, and the number of layers/tables per service.

| Run Type | Duration | Services |
|---|---|---|
| Portal | ~1–5 minutes | ~5,500 |
| AGOL | ~5–20 minutes | ~20,000 |

## Output File

All files are written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp. `[ORG]` is the filename-safe form of `org-name`, and `[PLATFORM]` is `PORTAL` or `AGOL`.

```text
[ORG]_[PLATFORM]_EMPTY_FEATURE_SERVICES_YYYYMMDD_HHMM.csv
[ORG]_[PLATFORM]_empty_feature_services_YYYYMMDD_HHMM.log
```

The CSV always includes its header when the write succeeds, even if no empty services are found. One row is written for each confirmed fully-empty service.

**Completion email** includes:

- Organization, platform, and run timestamp.
- Mode (`TEST` or `FULL`) and TLS verification mode.
- Services inspected and empty services found.
- Item-level errors, with a separate count of TLS/certificate failures when present.
- Output CSV path or `(NOT WRITTEN)`.
- Start, end, and elapsed timing.

Connection failures, TLS session-build failures, failed handshake checks, search/completeness aborts, or other controlled aborts trigger an abbreviated abort email with the error and log attachment.

**Attachment handling:** The script passes the log and CSV to `gather_attachments()` using the configured `max-email-mb` cap. If the files cannot fit under the cap after the helper's handling, the email includes a note directing the operator to the local output location.

A temporary SQLite candidate spool is also created during the scan for large inventories. It is removed when processing completes or aborts, including associated journal/WAL/shared-memory files when cleanup is possible.

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | The scan and completion email finished successfully, and the output CSV was written. The email may still mark the report `INCOMPLETE` when individual services could not be classified. |
| `1` | A run-level failure occurred, such as TLS/session setup failure, startup handshake failure, GIS connection failure, failure to guarantee search integrity, candidate-processing integrity failure, or failure to write the output CSV. An abort-email attempt is made. |
| `2` | Command-line validation failure, such as missing `--config` or omitting `--ca-bundle-path` when required. |

The script intentionally separates **report completeness** from **process success**. An item-level error can leave the report marked `INCOMPLETE` while the process still returns `0` when all intended candidates were processed and the CSV was successfully written. Run-level failures return `1`.

## Notes and Caveats

- **Read-only.** The script does not delete, unshare, modify, or repopulate services.
- **A result is not a deletion recommendation.** Classify each service as a leftover, never-used service, or operationally needed empty service before taking action.
- **Organization scoping is mandatory.** The script refuses unscoped enumeration and never falls back to an unscoped public catalog search.
- **Search completeness is treated explicitly.** Full mode uses a 2,000,000-item safety cap; reaching that cap aborts the run because a complete inventory cannot be guaranteed.
- **Conservative emptiness rule.** Every layer and table must return exactly zero. Any nonzero or indeterminate count excludes the service from the empty-service report.
- **Services with no layers or tables are not reported.** This is deliberate and preserves the classification rule.
- **A failed count is not treated as empty.** Ordinary item-level request failures exclude the service and increment the error count. TLS failures are counted separately in the run summary.
- **Test limiting occurs before group exclusions.** The search is capped at 250 services in test mode before excluded group members are removed, so fewer than 250 services may be inspected.
- **Group exclusions are strict.** If a requested exclusion group cannot be resolved or read, the run aborts rather than silently ignoring the exclusion.
- **Only record counts are queried.** No feature geometries or attribute rows are downloaded.
- **Worker sessions are isolated.** Each worker gets its own direct HTTP session, reducing cross-thread session-state sharing while retaining one TLS policy.
- **Candidate storage is disk-backed.** Large inventories are spooled to a temporary SQLite database rather than kept entirely in memory.
- **Result order follows future completion.** Rows are appended as worker results complete, so do not depend on CSV row order for business logic.
- **Dates have no time-of-day detail.** `date_created` and `date_modified` are formatted as `YYYY/MM/DD`.
- **TLS verification is always on.** `--tls-mode` selects the trusted-certificate source, not whether verification occurs.
- **A TLS failure invalidates completeness.** If TLS/certificate failures occur partway through a run, review the affected services and treat the empty-service list as incomplete until they can be checked successfully.
- **The report does not identify dependencies.** Review maps, apps, integrations, sharing, and business ownership outside this CSV before retirement.
- **Point-in-time results.** A service can gain or lose records after the scan. Run the inventory again before acting on an older report.
- **Custom trusted-service origins are security-sensitive.** Only add an exact HTTPS origin that you have independently confirmed is a legitimate service destination for the organization being scanned.
- **Search errors are run-level failures.** A failed hosted-feature-service search aborts the run and does not produce a successful header-only result.