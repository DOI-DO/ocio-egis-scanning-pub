# GET_RELATED_ITEMS.py

**Related Items Inventory — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Related Items](#why-inventory-related-items)
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
- [Observed runtime](#observed-runtime)
- [Output file](#output-file)
- [Exit codes](#exit-codes)
- [Notes and caveats](#notes-and-caveats)

---

# For Report Readers

## Overview

This tool looks at certain types of raw data files stored in your ArcGIS organization — CSV files, KML files, Excel spreadsheets, File Geodatabases, and Shapefiles — and figures out what (if anything) is actually using each one.

Think of it like checking which files in a shared drive are actively linked to something useful versus which ones are just sitting there taking up space. The tool produces a spreadsheet (CSV file) that lists each data file and shows what it's connected to.

The report is labeled with your organization's name and whether it came from Portal (your organization's own ArcGIS Enterprise) or AGOL (ArcGIS Online), so results from different organizations are never mixed up.

## Why Inventory Related Items

Data files like CSV, KML, Excel, File Geodatabase, and Shapefile are the most common "raw" formats that accumulate in an ArcGIS organization. They're usually created when someone publishes a map service, runs a survey, creates a report, or exports data for backup. Because they are created as a byproduct of other work rather than as destinations in their own right, they pile up quietly — and nothing in the ArcGIS interface tells you, when you look at one of these files, whether anything still depends on it. This scan exists to answer that specific, hard-to-answer question across the whole organization at once.

Before anyone decides whether a file can be safely archived or deleted, it's essential to know what (if anything) is actually using it. A file that looks abandoned might be feeding a live survey, powering a report, or supporting a published service. This is the core reason the inventory is worth running: the dependency that makes a file dangerous to delete is invisible from the file itself. Opening the file, checking its size, or looking at its age tells you nothing about whether a Survey123 form, a published service, a web map, or a report is quietly consuming it. The relationship lives on the *other* side of the connection, so the only way to judge a file safely is to trace its relationships outward — exactly what this scan does, probing each file's connections to services, apps, maps, scenes, surveys, and reports in both directions.

This particular scan is necessary because the alternative — reasoning about a file from its own metadata — systematically produces the most expensive kind of mistake. A storage cleanup guided by "this file is old, large, and looks unused" will eventually delete a file that is silently feeding a live survey or a scheduled report, breaking a workflow with no warning and often no obvious cause. The inventory replaces that guesswork with evidence: for every data file it lists the items that depend on it, the relationship type (service, survey, report, map), the direction the link was found, and the owners on both ends, so a reviewer can tell at a glance whether a file is genuinely orphaned or load-bearing. A file with a `Survey` or `Report` relationship is still in use even though it would look identical to junk from its metadata alone — and surfacing that distinction is the whole point.

Running this as an inventory rather than checking files one at a time matters because dependency information is scattered and tedious to gather by hand. Confirming a single file's safety manually means hunting through surveys, apps, and services looking for anything that references it — impractical across thousands of accumulated files, and error-prone enough that in practice nobody does it, which is why "export just in case" files sit untouched for years. A whole-organization scan turns that unbounded manual effort into one reviewable spreadsheet, and because it is strictly read-only and reports rather than deletes, it is safe to run before any cleanup and safe to re-run to confirm a file is still safe to remove right before acting. It also makes the honest distinction between a complete and an incomplete answer: a default test run samples each type as a spot-check, while a full run is the basis for actual decisions, so a reviewer is never misled into treating a sample as proof of abandonment.

## Why it's a problem

Organizations often export data "just in case" and leave it on the server indefinitely. Over time, these duplicate and backup copies pile up and consume significant storage. But here's the tricky part: **something might still be using that file**, even if it looks unused.

This is especially true for Excel and CSV files, which often power Survey123 forms and result layers. A file that appears to have no connections might actually be feeding a live survey or report. Deleting it without checking could break that survey or report.

## Why removal requires caution

**Never assume a file is unused just because it shows no direct "service" connection.** Always check all the relationship columns in the report, not just the "service" ones. A file with a `Survey` relationship is still actively in use, even if it has no service connection.

Before removing or archiving any file, verify what owns it and what depends on it. Contact the file's owner (listed in the `owner` column) and ask before taking action.

## Recommendations for investigation

If you're reviewing one of these reports:

1. **Check for a TEST label in the email subject.** A report run in default TEST mode only samples up to 20 items per file type — read it as a spot-check to confirm the tool is working, not as a complete inventory.

2. **Ask for (or wait for) a FULL run report** before making any decisions about deleting files. A full report scans every matching file in the organization.

3. **Check all relationship columns, not just "service."** Before treating a file as orphaned, specifically look for any `Survey`, `SurveyAddIn`, or `Report` relationships. If any exist, the file is still in use.

4. **If a file shows no relationships, contact the owner first.** The owner (listed in the report) can confirm whether the file is genuinely abandoned or just not showing up in this particular scan.

5. **Look at the file's size and modification date.** A small file that was modified recently is less likely to be abandoned than a large file that hasn't been touched in years.

## Column descriptions

Each row in the report shows one relationship between a data file and something using it. If a file has no relationships, it still gets one row with the relationship columns left blank.

| Column | What it tells you |
|---|---|
| `platform` | Where this file lives: `PORTAL` (your organization's Enterprise system) or `AGOL` (ArcGIS Online). |
| `item_id` | The unique ID ArcGIS assigned to this file — useful for looking it up directly if needed. |
| `title` | The file's display name/title. |
| `item_type` | What kind of file it is (e.g., `File Geodatabase`, `Shapefile`, `CSV`, `Microsoft Excel`, `KML`). |
| `owner` | The username of the person who owns this file. Contact them before deleting anything. |
| `item_size_mb` | The file's size in megabytes. Larger files consume more storage. |
| `date_created` | The date the file was originally created (`YYYY/MM/DD`). |
| `date_modified` | The date the file was last modified (`YYYY/MM/DD`). |
| `related_item_id` | The ID of the item using this file. Blank if nothing was found to be using it. |
| `related_title` | The name of that related item (e.g., a map, a survey, a report). |
| `related_type` | What kind of item is using it (e.g., `Feature Service`, `Web Map`, `Survey123 Form`, `Web Mapping Application`, `Report`). |
| `related_owner` | The username of the person who owns the related item. |
| `relationship_type` | The specific kind of connection (e.g., `Data2Service` = this file feeds a service; `Data2Survey` = this file feeds a survey; `Data2Report` = this file feeds a report; `Data2Map` = this file is used in a web map). |
| `direction` | Whether the connection was found by looking "forward" (from the file to what it feeds) or "reverse" (from something back to the file). |

---

# For Administrators

## Dependencies on the shared UTILITIES package

This script imports from the following UTILITIES modules:
- `restutils.py`
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `bucketing.py`
- `cli.py`
- `secure_session.py`
- `auth.py`
- `exclusions.py`

## Requirements

- ArcGIS API for Python (`arcgis`) and `requests`.
- A per-organization `--config <org_toml_path>` TOML file with `[portal]`, `[agol]`, and `[common]` sections.
- The account password stored in OS keyring (service = `--keyring-service`, user = `--username`), or exported as `PORTAL_PASSWORD` environment variable.
- Both `--config` and `--target` arguments are always required.

## Security: Certificate (TLS) Validation

This script **always verifies TLS certificates**. There is no option to disable certificate verification.

Two separate connections are involved, and both are always verified:

1. **The ArcGIS SDK's connection** (used for authentication and some item enumeration) — opened via `connect_gis()`, which **always** verifies certificates.

2. **This script's own direct REST calls** (item metadata, relationship probes) — uses a separate `requests.Session` whose certificate strategy is chosen explicitly via `--tls-mode`:
   - `standard` (default) — checks against the standard public list of trusted certificate authorities. Correct for AGOL or any Portal with a publicly issued certificate.
   - `os-trust-store` — checks against the operating system's own certificate store. Use this for an internal Enterprise Portal signed by your organization's certificate authority.
   - `custom-ca-bundle` — checks against a specific certificate file you provide with `--ca-bundle-path`.

Before any item scanning begins, the script performs **one test connection** to confirm the certificate check succeeds, and logs a clear PASS/FAIL result. If that test fails, the run stops immediately.

The authentication token is never written to the log, email, or CSV — it is held in a wrapper object that only reveals its value at the exact moment needed.

If a certificate becomes untrusted during the run (for example, an expired or rotated certificate), any resulting errors are specifically flagged in the log and email as **TLS/CERTIFICATE VERIFICATION FAILURES** — distinct from ordinary network errors — so results from a run with such failures are never mistaken for a complete, trustworthy scan.

### SSRF / network-boundary defenses

Direct Portal REST URLs are constrained to the authenticated Portal origin and `/sharing/rest/` boundary. Portal URLs must use HTTPS and unsafe URL forms such as embedded credentials, query strings, fragments, encoded separators, control characters, and traversal components are rejected.

The inventory scope is also restricted to the authenticated organization ID (`orgid:<id>`), and search results are checked so cross-organization items are not silently accepted.

### Token handling

Authentication tokens are supplied through the shared `TokenProvider` and inserted only when a request is constructed. Caller-supplied token parameters are removed before the controlled token is added. Authentication failures can trigger one synchronized refresh/retry, and sensitive error text is redacted before logging or emailing.

### Large/malformed responses

Direct REST responses are streamed and limited to 64 MB. JSON responses must decode successfully and have the expected object/list structure. Search totals, pagination progress, item IDs, duplicate page results, and organization IDs are validated so malformed or partial catalog responses do not appear to be complete.

### Redirect following

Automatic HTTP redirect following is explicitly disabled with `allow_redirects=False`. Redirects can only continue through the script's validation path and remain subject to the same authenticated-origin and `/sharing/rest/` boundary checks, with a maximum of three redirects.

### Python import-path/package shadowing

The script determines the expected project root, inserts it at the front of `sys.path` when absent, locates the `UTILITIES` package, and verifies that the loaded package came from the expected `UTILITIES` directory before importing it. Equivalent duplicate project-root entries are **not** removed from `sys.path` by the current implementation.

## Configuration

Connection, email, and output settings are read from the **org-specific config TOML file** named by the required `--config` argument (for example `org1.config.toml` or `org2.config.toml`). Within that file, the required `--target portal` or `--target agol` argument chooses the matching section (`[portal]` or `[agol]`). Any config value can be overridden on the command line, and a command-line flag always wins.

Required settings sourced from config: `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`.

This script also resolves `max-email-mb` from the org config and passes it to the email attachment handler — the same project-wide setting used by every other script. If unset, the built-in default of 20 MB applies.

**Customizable lists.** Two lists near the top of the Python file are safe to edit directly without changing anything else:

- **`ITEM_TYPES`** — the source item types to enumerate. Each entry must match the ArcGIS item type string exactly (including capitalization and spacing, e.g., `Microsoft Excel` not `Excel`). Default: `CSV`, `CSV Collection`, `KML`, `KML Collection`, `File Geodatabase`, `Shapefile`, `Microsoft Excel`.

- **`RELATIONSHIP_PROBES`** — list of `(relationship_type, direction)` tuples to query for each item. Fewer probes = faster scans. Default includes connections to services, apps, maps, scenes, surveys, and reports.

**Group exclusion.** The optional `--exclude-group-id <group_id>` argument accepts one or more comma-separated group IDs. Items in any named group are resolved to item IDs once, right after connecting, and filtered from all subsequent processing. This reduces both runtime and noise.

## How it works

1. **Resolve org scope.** The script calls `get_org_id()` and requires a successful result. If the org id cannot be determined, the run aborts to avoid an unscoped AGOL public-catalog enumeration.

2. **Establish TLS session and verify certificate.** The script builds its own `requests.Session` per `--tls-mode`, runs a one-time TLS handshake check against the portal URL, and only then connects the ArcGIS SDK.

3. **Enumerate items per type.** For each source item type:
   - **Test mode**: fetches up to 20 items directly (one small search page per type, no bucketing — fast)
   - **Full mode**: enumerates all items via date-range bucketed search (complete population, slower)

4. **Apply group exclusions.** If any `--exclude-group-id` is specified, matching items are filtered out before any probing.

5. **Probe relationships.** For each enumerated item, the script fetches metadata (size, dates) and queries the `/relatedItems` endpoint once per relationship-type/direction pair. Each discovered relationship yields one row.

6. **Write and email results.** The output CSV is written and emailed with the log file, sized according to the `max-email-mb` attachment policy.

## Test mode (default)

⚠️ **Test mode is ON by default.** Test mode:

1. Samples up to 20 items per source type via direct search (no bucketing)
2. Completes in seconds, not hours
3. Tags the email subject `[TEST]`

Test mode is meant for quick validation — to verify the tool is working and the relationship probing pipeline runs end-to-end for every source type. After you confirm test mode looks good, run with `--no-test` for a complete inventory.

Pass `--no-test` to scan the complete population per type via bucketing.

## Usage

**Scenario: "I want to quickly confirm the tool is working and see a sample of relationships."**

Run the default TEST mode against your org. It samples 20 items per type and completes in seconds:

```bash
python GET_RELATED_ITEMS.py --config org1.config.toml --target portal
python GET_RELATED_ITEMS.py --config org2.config.toml --target agol
```

**Scenario: "I need a complete inventory of every data file and what's using each one for a storage cleanup review."**

Run a full scan with `--no-test`. This enumerates all matching files across all item types:

```bash
python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --no-test
python GET_RELATED_ITEMS.py --config org2.config.toml --target agol --no-test
```

**Scenario: "A full run is taking too long because we have many data files. I want to speed it up."**

Increase the worker thread count to add more concurrency:

```bash
python GET_RELATED_ITEMS.py --config org2.config.toml --target agol --no-test --workers 24
```

**Scenario: "Our organization keeps a group of intentionally duplicated demo/staging data that we've already reviewed and approved. I don't want that cluttering the inventory."**

Pass the group ID(s) to `--exclude-group-id`:

```bash
python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --no-test --exclude-group-id abc123xyz
```

**Scenario: "Our internal Enterprise Portal uses an organizational certificate authority that Windows already trusts, but the standard TLS check is failing."**

Use `os-trust-store` mode instead:

```bash
python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --no-test --tls-mode os-trust-store
```

**Scenario: "I have a specific certificate file I need to validate against."**

Use `custom-ca-bundle` mode with the path to the PEM file:

```bash
python GET_RELATED_ITEMS.py --config org1.config.toml --target portal --no-test --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
```

**Scenario: "I just need to see all available options."**

```bash
python GET_RELATED_ITEMS.py --help
```

## Command-line options

| Option | Required | Default | Meaning |
|---|---|---|---|
| `--config` | Yes | — | Path to the org-specific TOML config file. |
| `--target` | Yes | — | `portal` or `agol`; selects the matching config section. |
| `--no-test` | No | off (test ON) | Scan all items via bucketing. Omit to run fast 20-per-type test mode. |
| `--portal-url` | No | config | Overrides the config value. |
| `--username` | No | config | Overrides the config value. |
| `--keyring-service` | No | config | Overrides the config value. |
| `--output-dir` | No | config | Overrides the config value. |
| `--smtp-server` | No | config | Overrides the config value. |
| `--email-to` | No | config | Single address or comma-separated list. |
| `--email-from` | No | config | Overrides the config value. |
| `--exclude-group-id` | No | — | One or more comma-separated group IDs to exclude. |
| `--workers` | No | `16` | Concurrent worker threads. |
| `--tls-mode` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | No | — | Path to a PEM CA bundle. Required when `--tls-mode custom-ca-bundle`. |

## Performance

Runtime scales with the number of items to process and the number of relationship probes per item.

**Test mode** completes in **seconds** because it issues one small search page per type instead of enumerating the whole organization.

**Full mode** (`--no-test`) uses date-range bucketing to enumerate all items, which takes longer but is complete and reproducible on large organizations.

Increasing `--workers` adds more concurrency and speeds up the relationship probing phase.

## Observed runtime

| Run type | Observed duration | Number of items scanned |
|----------|-------------------|--------------------------|
| Portal   | ~1-5 minutes (full)      | ~20,000         |
| AGOL     | ~5-20 minutes (full)     | ~100,000        |

Test mode (default) completes in **seconds** on both platforms.

## Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp, prefixed with the org name and platform label:

```
[ORG]_[PLATFORM]_RELATED_ITEMS_<ts>.csv
```

A matching log file, `[org]_[platform]_related_items_<ts>.log`, is written alongside it. Both are emailed on completion.

One row per discovered relationship, or a single row per scanned item with no relationships (relationship columns left blank). The CSV always has a header row, even on a run that finds zero items.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — run completed and output CSV was written. |
| `1` | Failure — run aborted (TLS handshake failed, connection failed, org id unavailable, or CSV write failed). A report email with the log is still sent. |
| `2` | Argument error — `--tls-mode custom-ca-bundle` without `--ca-bundle-path`, or a required config value is missing. |

## Notes and caveats

- **Test vs. full mode.** Test mode (default) is a fast 20-per-type sample for validation. To get a complete inventory, run with `--no-test`.
- **All searches are org-scoped.** Both modes restrict every search to org-owned content via `orgid:<id>`. If the org id can't be determined, the script refuses to run to avoid an unscoped public-catalog scan on AGOL.
- **Multi-org by design.** `--config` selects org-specific settings. Output file names and email subjects are always prefixed with that org's name, so results from different organizations are never ambiguous.
- **Group exclusion is applied early.** `--exclude-group-id` resolves to item IDs once, right after connecting, and is applied to each item type's list before any probing — excluded items never cost scan time.
- **Rows for unrelated items.** Items with no discovered relationships still produce one row (with blank related-item columns), so the output reflects every scanned item.
- **Completeness via bucketing.** Full mode uses date-range bucketing rather than a single unpaged search, so the inventory is complete and reproducible on large organizations.
- **Customization is expected.** `ITEM_TYPES` and `RELATIONSHIP_PROBES` are meant to be edited for local needs. After any edit, run in test mode first and check the per-type confirmation block before committing to a full `--no-test` run.
- **File size from search results.** The script prefers size, created, and modified values from the search payload for speed. If those values are missing or unreliable (`-1` or `0` for size), the script re-fetches from the item JSON endpoint.
- **Email attachment cap is configurable, project-wide.** This script resolves `max-email-mb` from the org config TOML. If the CSV exceeds that cap, it is first zipped and re-checked; only if the zip is still too large is it left on disk (with a note in the email). If `max-email-mb` is unset, the built-in default of 20 MB applies. The log is always attached regardless of size.
- **TLS verification is never optional.** Both the ArcGIS SDK connection and this script's own REST session always verify certificates; the only choice is which trust source to verify against (`--tls-mode`).
- **TLS failures are distinguished from ordinary errors.** If a certificate becomes untrusted mid-run, affected item errors are tagged as TLS/CERTIFICATE VERIFICATION FAILURES in the log and email — treat a run with such failures as incomplete and investigate the certificate before re-running.