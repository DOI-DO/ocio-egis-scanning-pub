# PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py

**Portal Catalog vs. Disk Content Directory Reconciliation — ArcGIS Enterprise Portal (Portal only)**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory the Portal Content Directory](#why-inventory-the-portal-content-directory)
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

This tool compares two things that should always match perfectly: Portal's internal catalog of items, and the actual folders of files sitting on the server's hard drive.

Every item in Portal (a map, a layer, a document, etc.) exists in two places at once:

1. **In Portal's catalog** — the list you see when you browse the Portal website
2. **On disk** — a folder containing the real files that make up that item

Normally these stay perfectly synchronized. But upgrades, backups, migrations, or network issues can occasionally cause them to drift apart. When that happens, you end up with either:

- **A catalog entry with no files behind it** — Portal thinks something exists, but the files are missing
- **Files on disk with no catalog entry** — folders sitting on the server that Portal has lost track of

This tool finds exactly where that mismatch has occurred and produces a spreadsheet showing the status of every item and folder. It's purely a reporting tool — it never deletes, moves, or changes anything.

## Why Inventory the Portal Content Directory

The reconciliation this section describes compares Portal catalog item IDs with the item-ID folders on disk. It is not a duplicate-layer content comparison; it finds duplicate or mismatched representations of Portal items between the catalog and the server content directory. The fundamental reason this check is needed is that Portal keeps the same truth in two independent places — the catalog database and the on-disk content directory — and nothing automatically proves to an administrator that those two copies still agree. Each is maintained by different machinery, and the only way to confirm they remain in lockstep is to compare them item by item, which is exactly what this scan does.

In a library, you have two separate records: the card catalog (what customers see) and the shelves of books (the actual content). They're only useful if they stay in sync — a card pointing to a book that isn't on the shelf is useless, and a book on the shelf with no card in the catalog is invisible to users. Portal works the same way. If the catalog and the disk get out of sync, users experience broken items that won't open or download, or IT has storage being consumed by files nobody knows about. Periodic reconciliation catches these problems early, before users run into them.

The reason this particular scan is worth running — rather than waiting for a symptom — is that neither kind of drift announces itself from inside Portal. A catalog entry whose files have vanished looks completely normal in the Portal website until someone actually tries to open or download it, so the failure surfaces as a user complaint rather than an administrator's early warning; and a folder on disk that Portal has lost track of is invisible from the catalog entirely, so it can consume storage, backups, and disaster-recovery capacity indefinitely with nothing in Portal pointing to it. The only vantage point from which both failure directions are visible is outside both systems, comparing the two lists directly. This scan is that vantage point, and it also quantifies the problem — measuring the disk space each orphaned folder consumes — so a mismatch becomes a prioritizable, sortable finding rather than a vague worry.

Running this as a periodic reconciliation rather than a one-time check matters because drift is produced by exactly the events an organization performs on purpose: upgrades, backups, restores, and migrations are precisely when the catalog and the content directory are most likely to fall out of step, and a half-completed or interrupted operation can leave inconsistencies that are silent until much later. A single scan tells you the current state; repeated scans tell you the trajectory — whether orphans and missing-file items are a stable background level or a growing number that points to a deeper platform-consistency problem worth addressing at its source. Because the tool is strictly read-only and classifies rather than acts — and because it refuses to label a folder orphaned when a verifying Portal lookup fails for an operational reason rather than a genuine absence — it is safe and trustworthy to run regularly as a standing integrity check on the one place where Portal's two copies of the truth can quietly disagree.

## Why it's a problem

A mismatch is rarely "just cosmetic" — it usually signals something that needs attention:

- **Missing files behind a catalog entry** means a user is going to try to use that item and get an error. It also means storage, backups, and disaster-recovery systems are protecting an entry that points to nothing real.

- **Files on disk with no catalog entry** usually means storage is being wasted, or a past migration/backup/upgrade didn't complete cleanly. A handful of orphan folders is worth investigating; a growing number over time suggests a deeper platform consistency problem.

- **Not every mismatch requires immediate action**, but seeing them periodically — and seeing whether they're growing or shrinking over time — is essential for platform health.

- **The size of the problem is measurable.** The tool calculates how much disk space each orphaned folder is using, so you can prioritize which mismatches are worth attention first.

## Why removal requires caution

This tool only reports; it never deletes anything automatically. A folder flagged as orphaned or an item flagged as missing is a **lead to investigate**, not an automatic instruction to delete.

Network hiccups, temporary outages, or lookups that haven't finished can make something look orphaned when it isn't really. Always verify with a second run, or by checking backup records, before removing anything.

## Recommendations for investigation

Use this report as a checklist for investigation, not as a deletion list.

- **Run the scan more than once.** If something is flagged as orphaned in one run but shows up as matched in the next, it was probably a temporary glitch. If it's consistently orphaned across multiple runs, it's more likely a real problem worth investigating.

- **Look at the biggest orphans first.** Sorting by folder size shows which orphaned folders are consuming the most disk space — tackle those first for the best return on effort.

- **Check your recent backups.** The most reliable way to know whether a folder is truly abandoned is to check whether it exists in your backup records. If something was removed cleanly, it should be absent from recent backups.

- **Verify "PORTAL_ONLY" items in Portal itself.** These are items Portal's catalog claims exist but whose files may be missing. Try to open or preview them in the Portal website to confirm they're broken before taking action.

## Column descriptions

The report is a spreadsheet (CSV file) that can be opened directly in Excel. Each row represents either a Portal item, a folder on disk, or both. The columns are:

| Column | What it tells you |
|---|---|
| `item_id` | The unique ID shared by the Portal item and/or its matching on-disk folder. |
| `status` | One of `MATCHED`, `PORTAL_ONLY`, or `ORPHAN`. `SYSTEM` rows (Esri's built-in content) are counted but excluded from the CSV. See the table below. |
| `in_portal` | `True` if this row corresponds to an organization-owned item that Portal recognizes; `False` if it exists only as a folder on disk. |
| `on_disk` | `True` if a folder for this item exists on the server's hard drive; `False` if Portal has a catalog entry but no folder was found. |
| `title` | The item's name/title from Portal, when it could be identified. Blank if it could not. |
| `owner` | The Portal username that owns the item, when it could be identified. Blank if it could not. |
| `item_type` | The kind of item (e.g., "Feature Service" or "Web Map"), when it could be identified. Blank if it could not. |
| `folder_size_bytes` | The total size of the on-disk folder, in bytes. Blank for `PORTAL_ONLY` rows (no folder to measure). |
| `folder_size_mb` | The same size in megabytes, rounded to two decimal places, for easier reading. Blank for `PORTAL_ONLY` rows. |
| `folder_path` | The full location of the folder on disk. Blank for `PORTAL_ONLY` rows. |

**Status meanings:**

| Status | Meaning |
|---|---|
| `MATCHED` | The item exists in Portal **and** its folder exists on disk. Everything is healthy for this item. |
| `PORTAL_ONLY` | Portal has a catalog entry, but **no folder was found on disk.** This item is likely broken and worth investigating. |
| `ORPHAN` | A folder exists on disk, but **no matching item could be found in Portal.** This is disk space that may be reclaimable, once confirmed safe to remove. |

**Important:** `SYSTEM` is counted in the run summary but excluded from the CSV. A disk folder is only classified as `ORPHAN` after a verified direct Portal lookup fails to find the item. If that lookup fails for an operational reason, the run aborts rather than falsely calling the folder orphaned.

---

# For Administrators

## Dependencies on the shared UTILITIES package

This script imports from the following UTILITIES modules:
- `bucketing.py`
- `restutils.py`
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `cli.py`
- `secure_session.py`
- `auth.py`

## Requirements

- Python 3.9+ with ArcGIS API for Python (`arcgis`) and `requests`.
- The shared `UTILITIES` package in a parent directory.
- A per-organization `--config <org_toml_path>` TOML file with `[portal]` and `[common]` sections.
- The account password stored in OS keyring (service = `--keyring-service`, user = `--username`), or exported as `PORTAL_PASSWORD` environment variable.
- Read access to the Portal `content\items` directory (UNC or local path) from the machine running the script.

**Portal only.** This tool always targets Portal; there is no `--target` flag.

## Security: Certificate (TLS) Validation

This script **always verifies TLS certificates**. There is no option to disable certificate verification.

Two separate connections are involved:

1. **The ArcGIS SDK's connection** (used for initial Portal login) — opened via `connect_gis()`, which **always** verifies certificates.

2. **This script's own direct REST calls** (item enumeration, user lookups, directory listings) — uses a separate HTTP session whose certificate strategy is chosen explicitly via `--tls-mode`:
   - `standard` (default) — checks against the standard public list of trusted certificate authorities. Correct for any Portal with a publicly issued certificate.
   - `os-trust-store` — checks against the operating system's own certificate store. Use this for an internal Enterprise Portal signed by your organization's certificate authority.
   - `custom-ca-bundle` — checks against a specific certificate file you provide with `--ca-bundle-path`.

Before any real data is requested, the script performs **one test connection** to confirm the certificate check succeeds, and logs the result. If that test fails, the run stops immediately.

The authentication token is never written to the log, email, or CSV — it is held in a wrapper object that only reveals its value at the exact moment needed.

### SSRF / network-boundary defenses

Direct Portal REST URLs are required to use HTTPS and stay under the authenticated Portal `/sharing/rest/` path. URLs containing embedded credentials, query strings, fragments, encoded slashes/backslashes, unsafe control characters, or path traversal are rejected. Organization user enumeration is scoped to the authenticated organization ID, and returned users are checked against that same organization.

### Token handling

Tokens are supplied through the shared `TokenProvider` and are inserted only when a request is constructed. Caller-supplied `token` parameters are removed before the controlled token is added. Authentication failures can trigger one synchronized token refresh/retry. Error text is redacted through `redact_token()` before it is logged or included in notifications.

### Large / malformed responses

Direct REST response bodies are streamed and limited to 64 MB. Declared content length is checked, the byte limit is enforced while reading, JSON decoding is validated, and user-list structure, totals, pagination progress, duplicate usernames, and organization IDs are checked before results are accepted.

### Redirect following

Automatic redirect following is explicitly disabled with `allow_redirects=False`. Redirects may only continue through the script's validation code, which requires the same HTTPS origin and authenticated `/sharing/rest/` path. Cross-origin redirects are refused, with a maximum of three redirects.

### Python import-path / package shadowing

The script determines the expected project root and expected `UTILITIES` directory, inserts the expected root at the front of `sys.path` when it is absent, verifies the `UTILITIES` import origin with `importlib.util.find_spec()`, and refuses to import the package from an unexpected location. **Equivalent duplicate project-root entries are not removed from `sys.path`; origin verification is the protection used here.**

## Configuration

The organization is always Portal; settings come from the `[portal]` and `[common]` sections of the org's `--config <org_toml_path>` TOML file. The `--config` argument also supplies the org name for file/subject prefixing.

Required settings sourced from config: `portal-url`, `username`, `keyring-service`, `smtp-server`, `email-to`, `email-from`.

Two settings are resolved separately after `finalize_config()` returns:
- `output-dir` — overridable by positional argument; if neither is provided, the run exits with an error
- `content-items-dir` — overridable by `--content-items-dir`; if neither is provided, the run exits with an error

Command-line values always override config values.

## How it works

1. **Establish a verified TLS session and connect to Portal.** Both an explicit `requests.Session` and the ArcGIS SDK connection use the same certificate trust strategy (`--tls-mode`). If connection fails, an abort email (with log attached) is sent and the run exits.

2. **Retrieve all Portal items and org usernames.** Items are enumerated using date-range bucketed search (to handle organizations with more than 10,000 items). Org users are fetched to distinguish organization-owned content from system/Esri-owned content.

3. **Scan the disk content directory (in parallel).** Root-level item folders are enumerated (excluding known non-item folders like `groupinfo`, `userinfo`, `kml`, `accountresources`, `portal`, `userappresources`, `portal-ha`). Each folder's total size is measured recursively with live progress and an ETA in the log.

4. **Compare and classify every ID.** Items are filtered to those owned by real org users (system/Esri owners are excluded). Unmatched disk folders are resolved via direct Portal API calls (in parallel) to separate `SYSTEM` from `ORPHAN`, then every ID is classified and written to the CSV.

Classification logic:
- **In Portal AND on disk** → `MATCHED`
- **In Portal but NOT on disk** → `PORTAL_ONLY`
- **On disk but NOT in Portal** → fallback to direct API lookup; if found and org-owned, `MATCHED`; if found but system-owned, `SYSTEM` (excluded from CSV); if not found, `ORPHAN`

5. **Write the CSV and email the summary** with both the report and log attached.

## Test mode (default)

**This script does not have a test mode.** There is no `--test` or `--no-test` flag. Every run always scans the full Portal item roster and the full content directory. Because the tool is read-only and makes no changes regardless of run size, the only way to limit scope on a first run against an unfamiliar Portal is to point `--content-items-dir` at a smaller content directory.

## Usage

The script is Portal-only and has no `--target` option. Each scenario below describes when to use the command and what the result means.

**Scenario: "I want to run the standard reconciliation using the output and content-directory paths already configured for this org."**

Both paths come from the org's config TOML. This is the normal scheduled or administrative run.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml
```

**Expect:** The script inventories the Portal catalog, organization users, and the configured `content\items` directory, compares IDs, writes the CSV, and emails the report plus log.

**Scenario: "I'm scanning a secondary or DR content directory that isn't configured in the org's TOML, and I want the report written to a specific location."**

Override both the output directory and the content directory on the command line. This is useful when validating a DR copy, restored content tree, or alternate file share.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org2.config.toml "D:\reports" ^
    --content-items-dir "\\server\gis\content\items"
```

**Expect:** Portal is still the comparison source, but disk-side folder presence and sizes come from the supplied content directory.

**Scenario: "I need the output to land at an exact file path so a downstream process can find it by name."**

Pass a full `.csv` path as the positional argument instead of a directory. The companion `.log` uses the same filename stem.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml "D:\reports\scan.csv"
```

**Expect:** The exact CSV path is used. This is useful for scheduled pipelines that expect a fixed path. If that explicit CSV already exists, the script does not silently overwrite the reserved run output.

**Scenario: "The content directory is on a slower file server, and I want to reduce concurrent I/O load."**

Lower `--workers` to reduce parallel folder sizing and unmatched-folder lookup activity; raise it if the storage and Portal API have headroom.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --workers 8
```

**Expect:** The scan still covers the full scope, but folder measurements and fallback Portal item lookups run with fewer concurrent workers.

**Scenario: "Our internal certificate authority isn't in the public trust store and the default TLS check is failing."**

Switch to the OS certificate store when the internal CA is already trusted by the operating system.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --tls-mode os-trust-store
```

**Expect:** Both the startup certificate check and subsequent direct REST traffic use the OS trust store. Certificate validation remains enabled.

**Scenario: "I have a specific certificate file to validate against."**

Use a PEM CA bundle when the required internal CA is supplied as a file.

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config org1.config.toml --tls-mode custom-ca-bundle ^
    --ca-bundle-path C:\certs\internal-ca.pem
```

**Expect:** The supplied PEM bundle is used for certificate validation. `--ca-bundle-path` is required with `custom-ca-bundle`; omitting it causes argument/configuration validation to fail.

**Scenario: "I just need to see the available options."**

```bash
python PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --help
```

**Expect:** The current command-line help is displayed and the comparison is not run.

## Command-line options

| Option | Required | Default | Meaning |
|---|---|---|---|
| `--config` | Yes | — | Path to the org-specific TOML config file. |
| `output_path` | No (positional) | config `output-dir` | Output directory or explicit `.csv` path. |
| `--content-items-dir` | No | config `content-items-dir` | Path to the Portal `content\items` directory. |
| `--portal-url` | No | config | Overrides the config value. |
| `--username` | No | config | Overrides the config value. |
| `--keyring-service` | No | config | Overrides the config value. |
| `--smtp-server` | No | config | Overrides the config value. |
| `--email-to` | No | config | Single address or comma-separated list. |
| `--email-from` | No | config | Overrides the config value. |
| `--workers` | No | `16` | Concurrent threads for folder sizing and lookups. |
| `--tls-mode` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | No | — | Path to a PEM certificate bundle. Required when `--tls-mode custom-ca-bundle`. |

**Note:** There is no `--target` option — this script does not use it.

## Performance

Two phases run with concurrency controlled by `--workers` (default **16**):

- **Folder sizing.** Root-level item folders are enumerated and each folder's total size (recursing into subfolders) is measured in parallel, with live progress, cumulative footprint, elapsed time, and ETA logged.
- **Unmatched-folder lookups.** Disk folders not already matched to an org item are resolved via Portal API calls in parallel to separate `SYSTEM` from `ORPHAN`, with progress logged.

Portal item enumeration uses date-range bucketed search to handle organizations with more than 10,000 items. The 2,000,000-item and 2,000,000-folder values in the script are safety ceilings, not normal truncation limits.

## Observed runtime

| Run type | Observed duration | Number of items |
|----------|-------------------|-----------------|
| Portal   | ~5-10 minutes     | ~20,000         |

Actual runtime scales with the number of item folders on disk (folder sizing walks every file recursively) and how many disk folders require individual lookups. Increasing `--workers` helps on storage with concurrent I/O headroom, but very high worker counts can trigger diminishing returns or throttling.

## Output file

If a **directory** is given (or taken from config), the CSV is named with the org and platform prefix and a timestamp:

```
[ORG]_PORTAL_PORTAL_ITEM_ID_ANALYSIS_<ts>.csv
```

If an explicit `.csv` path is passed, that exact path is used. A `.log` file with the same stem is written next to the CSV, and both are emailed on completion. The CSV is written UTF-8 with a BOM (`utf-8-sig`) for clean opening in Excel.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Completed successfully; report written and the completion notification was attempted. |
| `1` | Runtime failure, including inaccessible content directory, Portal connection failure, lookup failure that prevents safe classification, or another unrecovered processing failure. An abort email is sent for handled abort cases. |
| `2` | Argument/configuration error (no output path could be resolved, or no content directory could be resolved). Also argparse's exit code if you pass an unrecognized argument. |

## Notes and caveats

- **There is no `--target` argument.** This is the one script in this project that does not register or use a `--target` flag. Passing `--target agol` will be rejected by argparse as an unrecognized argument.
- **There is no test mode.** Every run scans the full Portal roster and the full content directory; there is no flag to limit scope.
- **Org ownership drives orphan detection.** Only items owned by real org users are treated as "Portal items" for matching. Esri/system-owned content is classified as `SYSTEM` rather than orphaned.
- **`SYSTEM` rows are excluded from the CSV.** System/Esri-owned folders are counted in the summary but not written to the spreadsheet; only org-owned and truly orphaned rows appear.
- **A `MATCHED` row doesn't always come from the initial enumeration.** Some `MATCHED` rows come from the direct API fallback lookup catching an org-owned item the bucketed search missed.
- **Orphans warrant verification.** An `ORPHAN` row means a disk folder had no matching Portal item via the API — but always verify before deleting. This is a reporting tool, not a cleanup tool.
- **Blank size cells.** `PORTAL_ONLY` rows leave `folder_size_bytes`, `folder_size_mb`, and `folder_path` empty. If loading into a tool that expects numeric columns, treat empty as zero/not-applicable.
- **No per-row PLATFORM column.** The platform is reflected in the file name and log rather than as a per-row column.