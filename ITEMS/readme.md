# ITEM_INVENTORY.py

**Full Item + Group Inventory — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Items](#why-inventory-items)
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

`ITEM_INVENTORY.py` creates a **complete list of items stored in an ArcGIS organization** — every map, layer, app, file, and, when enabled, every group — whether that organization lives on ArcGIS Online (AGOL) or on an organization's own ArcGIS Enterprise Portal.

For each item and group, the tool records:
- Who owns it
- When it was created and last changed
- What type of content it is
- Its title, description, and sharing level
- A few other useful details

Everything is saved into CSV files that you can open in Excel or any spreadsheet program. A copy of the run log and the report files are also emailed automatically when the scan finishes.

The main item/group CSV puts items and groups into **one spreadsheet**. Group rows use the literal value `Group` in `item_type`, which makes them easy to find without adding a separate record-kind column to the main file. The `platform` column tells you whether the row came from Portal or AGOL.

A separate Feature Service CSV adds service kind and the latest edit date that the script can determine from the service's editor-tracking configuration. A third CSV summarizes how many records were found for each item type.

**This is a read-only report.** Running this tool does not create, change, share, or delete anything in the organization — it only looks and records what it finds.

## Why Inventory Items

Most of the other scans in this toolset answer a narrow question — which services are broken, which groups are stale, which field names collide. This script is not a visual duplicate-layer comparison tool. Its purpose is broader: it inventories the organization's content so administrators can see repeated, missing, stale, or otherwise unexpected content patterns in one place. The Feature Service inventory can also distinguish Hosted, Federated, and Registered service records. Where a targeted scan assumes you already know what problem you are looking for, this tool exists to answer the more basic question that comes before all of those: **"what does this organization actually contain, in detail, right now?"** That question is the foundation every other review rests on, which is why a full item inventory is worth running even when nothing is obviously wrong.

The reason this particular scan is necessary is that an ArcGIS organization has no built-in view that lets a person see all of its content at once in a form they can sort, filter, and compare. Content is browsed a page at a time through the website, and the fields that matter for governance — owner, sharing level, creation and modification dates, description completeness, view counts — are scattered across individual item pages rather than laid out in a single table. An organization accumulates thousands of items and groups over years, created by many people for many reasons, and no one holds a complete mental map of it. The only way to regain that map is to enumerate everything into one spreadsheet, which is exactly what this scan does: it turns a sprawling, click-through catalog into a single dataset that can answer whole-organization questions in seconds.

Two deliberate design choices make this inventory more useful than a plain item list, and each is a reason to run it. First, groups are included right alongside items on purpose. A group is a real piece of organizational content — someone created it, someone owns it, and it represents a decision about how content is organized and shared. Putting items and groups in the same file means one spreadsheet can answer "what does this organization own in total" without having to manually combine two separate reports, which is precisely what a governance review, a migration project, or a security check needs. Second, the Feature Service CSV adds another useful layer of detail: it shows which item records are Feature Services, how those services were classified as Hosted, Federated, or Registered, and the latest edit date detected from live service definitions when editor tracking is available — distinctions that matter for knowing where data actually lives and whether it is still being edited.

Finally, this scan is worth running on a schedule precisely because its value compounds over time. A single inventory is a complete, point-in-time snapshot of the organization — the kind of record you want to already have on hand when a question comes up later, whether that question is a security audit, a migration plan, an ownership change after someone leaves, or simply "what has changed since last quarter?" Comparing successive snapshots turns the inventory from a static catalog into a record of how the organization is evolving. Because the scan is strictly read-only and enumerates through search rather than touching items individually, it is safe and efficient to run regularly even against very large organizations, giving administrators a dependable, current baseline rather than a view assembled by hand under deadline pressure.

## Why it's a problem

Without a report like this, someone trying to understand an organization's content is limited to browsing the website a page at a time, or checking items one by one. That doesn't scale:

**You can't review what you can't see all at once.** Questions like these are hard to answer by clicking through a website, but easy once everything is in one spreadsheet:
- Which items are missing a description?
- Which items are still marked private after a reorganization?
- Who owns the most content?
- What hasn't been touched in years?

**Groups and items are usually reported separately**, which makes it hard to get one unified view of "everything in this organization" for a governance review, a migration project, or a security check.

**A snapshot in time is valuable on its own**, even without a specific problem in mind — it's the kind of record you want to already have on hand when a question comes up later.

For Feature Services, the extra service inventory can reveal differences between Hosted, Federated, and Registered records and can show when the most recent editor-tracked change was detected. Those details can help focus follow-up work without changing the underlying service.

## Why removal requires caution

This tool itself never deletes, changes, or shares anything — but the spreadsheet it produces is often the starting point for someone else's decision to clean up or remove content later, so handle the file carefully:

- **It includes private and internal-only content**, not just things that are publicly shared — the report reflects everything the connecting account can see, which for an administrator account can be most or all of the organization. Handle the output file with the same care you'd give the underlying data.

- **Descriptions, summaries, acknowledgments, and terms-of-use text are copied in as written** (with HTML formatting removed, but the wording otherwise preserved), so anything sensitive an item's owner typed into those fields will show up in the spreadsheet exactly as written.

- **This is a snapshot, not a live view.** The data reflects the exact moment the scan ran. Anything created, edited, or deleted afterward won't show up until the next run. Never treat a row in this file as proof that something is still safe (or still unsafe) to remove — always double-check the live item before acting on it.

- **A low view count or an old date is a reason to look closer, not a reason to delete.** Some of the most important content in an organization will naturally look quiet by these measures even though people depend on it every day.

- **A Feature Service edit date is also only a point-in-time finding.** A registered service may report `Editor tracking detection not available for registered services`, and other services can return an error or `no editor tracking found` when the live service metadata cannot provide a validated edit-date field. Those values should be reviewed rather than treated as proof that a service has never been used.

## Recommendations for investigation

- For anything beyond a quick connectivity check, ask whoever runs the scan for a **full run** (not the default quick/test run) — the quick run only looks at the first 250 items and skips groups, so it isn't representative of the whole organization.

- Use the `numViews`, `date_modified`, and `owner` columns as starting points, not as automatic cleanup rules. Recently viewed content may be active, while low-use reference data can still be important.

- Filter or sort on `item_type` to separate groups (rows where `item_type` is `Group`) from other items when you want to look at just one or the other.

- For Feature Services, use `record_kind`, `last_fs_edited`, and `rest_url` in the separate Feature Service CSV to focus on Hosted/Federated/Registered behavior and to investigate service-level edit activity.

- Before removing or changing sharing on anything based on this report, confirm the current state of that specific item or group directly in Portal or AGOL — this file is a point-in-time snapshot and may already be out of date by the time you act on it.

## Column descriptions

### Main item/group inventory CSV

The main inventory spreadsheet contains one row per item and one row per group when groups are included. It does **not** contain a `record_kind` column; group rows are identified by `item_type = Group`.

| Column | Description |
| --- | --- |
| `platform` | `PORTAL` or `AGOL` — which system this row came from. Always the first column. |
| `item_id` | The item's or group's unique identifier. |
| `owner` | The username that owns the item or group. |
| `date_created` | The date the item/group was created, rendered by the shared `epoch_to_date()` helper. |
| `date_modified` | The date the item/group was last changed, rendered by the shared `epoch_to_date()` helper. |
| `title` | The item's or group's title. |
| `item_type` | For an item, its actual type (for example `Web Map`, `Feature Service`, or `PDF`). For every group row, this is the literal value `Group`. |
| `description` | The item's or group's description, with HTML formatting removed so it reads as plain text. |
| `tags` | The item's or group's tags, joined together into a semicolon-separated string. |
| `summary` | The item's or group's short summary, with HTML formatting removed. |
| `acknowledgments` | Credits or acknowledgment text for the item, with HTML formatting removed. Blank for group rows. |
| `terms_of_use` | The item's terms-of-use text, with HTML formatting removed. Blank for group rows. |
| `numViews` | How many times the item has been viewed. Blank for group rows. |
| `protected` | Whether delete protection is enabled, rendered as `true` or `false`. |
| `access` | The sharing level, such as `private`, `org`, `shared`, or `public`. |

### Feature Service inventory CSV

The separate Feature Service CSV contains one row for each item identified as a `Feature Service` by the main inventory process.

| Column | Description |
| --- | --- |
| `platform` | `PORTAL` or `AGOL`. |
| `record_kind` | Service classification: `Hosted`, `Federated`, or `Registered`. |
| `item_id` | The Feature Service Portal/AGOL item ID. |
| `owner` | The username that owns the Feature Service item. |
| `date_created` | The item creation date. |
| `date_modified` | The item modification date. |
| `title` | The Feature Service item title. |
| `item_type` | The literal value `Feature Service`. |
| `last_fs_edited` | Latest edit date found from live service editor-tracking metadata, formatted `YYYY/MM/DD`, or a descriptive value such as `no editor tracking found`, `no records to track`, `Editor tracking detection not available for registered services`, or a failure message when the lookup could not be completed. |
| `rest_url` | The service REST URL, with token text redacted. |

### Item-type summary CSV

The summary file contains exactly two columns:

| Column | Description |
| --- | --- |
| `item_type` | Item type label. Feature Service rows are grouped by service classification as `Feature Service (Hosted)`, `Feature Service (Federated)`, or `Feature Service (Registered)`; non-Feature-Service item types use their normal item type, and the literal `Group` is used for group rows. |
| `count` | Number of rows represented by that `item_type` label. |

---

# For Administrators

## Dependencies on the shared UTILITIES package

Only these helper files in the `UTILITIES` folder are used by the script:

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

- Python 3.9+.
- ArcGIS API for Python (`arcgis`).
- `requests`, `urllib3`, and `keyring`.
- `truststore` when using `--tls-mode os-trust-store`.
- Organization TOML passed with `--config`.
- Portal/AGOL account with sufficient visibility.
- Network access to Portal/AGOL, ArcGIS service endpoints, SMTP.

## Security: Certificate (TLS) Validation

This script **always verifies TLS certificates**. There is no option to disable certificate verification.

By default, `--tls-mode standard` verifies against the standard public CA bundle. `os-trust-store` uses the operating-system certificate store, and `custom-ca-bundle` uses the PEM file supplied with `--ca-bundle-path`.

Before connecting, the script performs a real TLS handshake check. A failed certificate check aborts the run before inventory processing.

**SSRF / network-boundary defenses:** token-bearing requests must use HTTPS and are checked against the authenticated Portal/AGOL origin and explicitly trusted or discovered ArcGIS service origins. Service URLs must match recognized FeatureServer/MapServer paths. Portal proxy paths and federated service bases are validated before the organization token is sent. AGOL service destinations are limited to `*.arcgis.com` by the current policy. Unsafe URL features such as credentials, fragments, encoded path separators, traversal, and invalid paths are rejected.

**Token handling:** tokens are obtained through the shared token provider and added only at the request boundary. Supplied request parameters cannot replace the authentication token. Authentication failures cause one bounded refresh/retry cycle. Exception text is passed through `redact_token()` before it is used outside the request path, including logging and email notifications.

**Large/malformed responses:** direct REST responses are streamed and limited to 64 MB. `Content-Length` is checked when supplied, the streaming read enforces the same limit, response bodies must be valid JSON objects, and search results, totals, paging values, IDs, and organization identifiers are validated. Search result sets that hit the API cap are split into smaller date ranges; malformed or non-progressing searches abort instead of becoming a silent partial inventory.

**Redirect following is explicitly disabled at the HTTP layer** with `allow_redirects=False`. The client may process a redirect only through its own validation logic, and token-bearing redirects are required to remain on the original origin and pass the same Portal/service URL policy. Cross-origin redirects are refused.

**Python import-path/package shadowing:** the script determines the expected project root, inserts it at the front of `sys.path` when it is absent, uses `importlib.util.find_spec("UTILITIES")` to verify the package origin, and only then imports `UTILITIES`. It then refuses to run if the loaded package directory does not exactly match the expected `UTILITIES` directory. **The script does not remove equivalent duplicate project-root entries from `sys.path`.**

## Configuration

Every run requires an organization-specific TOML file and a target:

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol
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
portal-url = "https://org.maps.arcgis.com/home"
username = "agol_service_account"
keyring-service = "AGOL"
```

### Configuration Precedence

1. Command-line value
2. Selected target section: `[portal]` or `[agol]`
3. `[common]`
4. Script default, where applicable

### Required Settings

The shared configuration layer requires:

- `org-name`
- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

`max-email-mb` is optional and falls back to the shared default of 20 MB when not set.

Store the Portal/AGOL password in the OS keyring, not in TOML:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('AGOL', 'agol_service_account', '<password>')"
```

## How it works

1. **Resolve run scope.** The script validates arguments, establishes whether the target is `PORTAL` or `AGOL`, determines the effective item cap, and decides whether groups are included.
2. **Build and verify TLS.** A secure HTTP session is created using `--tls-mode`, followed by a real TLS handshake check against the configured Portal/AGOL URL.
3. **Connect and authenticate.** The shared `connect_gis()` helper creates the ArcGIS connection and a shared token provider.
4. **Build the trusted URL policy.** Portal/AGOL REST requests are restricted to the authenticated origin and validated ArcGIS service origins. Enterprise federation metadata is used to add trusted service bases; AGOL service URLs under `*.arcgis.com` are trusted by policy.
5. **Enumerate every organization item.** The script resolves the organization ID and performs strict, organization-scoped, date-bucketed searches beginning at January 1, 2000 and continuing through one year beyond the current date. Each page is validated and stored in a temporary SQLite-backed item store.
6. **Enumerate groups when enabled.** Group searches are organization-scoped and de-duplicated by ID. Large search ranges are split when the ArcGIS result cap is reached.
7. **Build the item and group CSV rows.** Item metadata is cleaned for spreadsheet output, group rows use `item_type = Group`, and HTML is removed from text fields.
8. **Build the Feature Service CSV.** Feature Services are classified as Hosted, Federated, or Registered, and the script optionally reads live service definitions to determine the latest editor-tracked date.
9. **Build the item-type summary.** Counts are calculated from the generated main and Feature Service rows.
10. **Write the three CSVs and email the results.** Large combined CSV output is zipped for email when required by the configured attachment cap; the uncompressed CSVs remain on disk.

### Feature Service classification

For main item records whose type is `Feature Service`:

- `Hosted` is used when `typeKeywords` identify a hosted service or when the service belongs to the identified hosting site.
- On AGOL, a Feature Service not identified as hosted is `Registered`.
- On Enterprise Portal, a matching federated server GUID/site is `Federated`.
- Otherwise the service is `Registered`.

### Editor-tracked latest edit date

When editor-tracking detection is enabled, Hosted and Federated Feature Services are queried for the configured edit-date field on each available sublayer/table. The script orders the query by that field descending and keeps the greatest valid timestamp found.

Registered Feature Services are not queried for this value and receive `Editor tracking detection not available for registered services`.

A missing editor-tracking configuration produces `no editor tracking found`; a service with no records to track can produce `no records to track`; malformed or failed service/layer/query lookups produce a descriptive failure value and an incomplete-run reason.

## Test mode (default)

**Test mode is ON by default.** It does two things:

1. **Caps the item inventory at the first 250 items**, selected deterministically by item ID order after strict organization-wide enumeration.
2. **Skips groups**, unless `--with-groups` is supplied.

The email subject is tagged `[TEST]` in this mode.

Pass `--no-test` for a full item inventory with no item cap and groups included by default.

`--max-items N` can override the item cap in either mode. A value `<= 0` means no cap.

## Usage

### Scenario 1 — Quick AGOL connectivity and sample inventory

**Situation:** A new organization configuration has just been created and you want to verify authentication, TLS, search visibility, and output without scanning the entire organization.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol
```

**Expect:** Up to the first 250 items are written to the main report, groups are skipped, and the email subject is marked `[TEST]`.

### Scenario 2 — Full Enterprise Portal inventory

**Situation:** A governance review needs every visible item and group instead of the 250-item test sample.

```bash
python ITEM_INVENTORY.py --config org2.config.toml --target portal --no-test
```

**Expect:** No item cap is applied and group inventory is enabled by default. All three CSVs are generated when the corresponding stages complete.

### Scenario 3 — Limit a very large organization for a controlled first run

**Situation:** You need more than the 250-item test sample, but you are not ready to process the entire organization.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --max-items 1000
```

**Expect:** The strict item inventory still enumerates the organization, but only the first 1,000 items by deterministic ID order are mapped to output rows. Groups remain controlled by the normal test-mode rule unless `--no-test` or `--with-groups` changes that scope.

### Scenario 4 — Items only for a faster governance review

**Situation:** You need the item catalog but do not need groups in this report.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --no-test --no-groups
```

**Expect:** A full item inventory is produced without the group pass.

### Scenario 5 — Include groups during the default test run

**Situation:** You want the quick 250-item test behavior but also need to confirm that group enumeration works before a production run.

```bash
python ITEM_INVENTORY.py --config org2.config.toml --target portal --with-groups
```

**Expect:** The item cap remains 250, but groups are included despite test mode.

### Scenario 6 — Disable Feature Service edit-date detection

**Situation:** You want the Feature Service inventory but do not need the extra live service/layer queries that calculate `last_fs_edited`.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --no-test --no-edit-tracking
```

**Expect:** Feature Service rows are still classified and reported. Editor-tracking detection is skipped, and Registered services retain their normal explanatory value for that column.

### Scenario 7 — Internal Portal with an operating-system trust store

**Situation:** The Enterprise Portal certificate is signed by an organizational CA already trusted by the operating system.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target portal --tls-mode os-trust-store
```

**Expect:** The script verifies Portal/ArcGIS certificates using the OS trust store. The `truststore` package is required.

### Scenario 8 — Use a custom CA bundle

**Situation:** The Portal certificate chains to an internal CA provided as a PEM file.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path "C:\\certs\\org-ca.pem"
```

**Expect:** Certificate validation uses the supplied PEM bundle. `--ca-bundle-path` is required with this TLS mode.

### Scenario 9 — Override the Portal/AGOL URL or credentials source

**Situation:** The same organization config is used in several environments and you need a one-run override without editing the TOML file.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target portal --portal-url https://portal-qa.example.com/portal --username qa_service_account --keyring-service PORTAL_QA
```

**Expect:** The command-line values take precedence over the configured values for that run.

### Scenario 10 — Override email delivery settings

**Situation:** A one-time review needs a different SMTP relay or recipients from the normal configuration.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --smtp-server smtp-review.example.com --email-to analyst@example.com,admin@example.com --email-from inventory@example.com
```

**Expect:** The command-line email settings are used for that run. Recipients may be supplied as a single address or a comma-separated list.

### Scenario 11 — Write reports somewhere else

**Situation:** An auditor or scheduled task requires the CSVs and log in a dedicated directory.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --output-dir "D:\\reports\\inventory"
```

**Expect:** The three CSVs and run log are created under the specified output directory, subject to normal write permissions.

### Scenario 12 — Change concurrency

**Situation:** A large inventory is placing too much load on the client or endpoint, so you reduce the shared connection-pool size.

```bash
python ITEM_INVENTORY.py --config org1.config.toml --target agol --no-test --workers 6
```

**Expect:** Up to six worker sessions are pre-created and used for concurrent editor-tracking requests; item/group enumeration itself remains search-driven.

### Scenario 13 — Review every available command-line option

**Situation:** You are installing the script on a new host and want the built-in usage text before setting up automation.

```bash
python ITEM_INVENTORY.py --help
```

**Expect:** `argparse` prints the current command-line options and exits without running an inventory.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | *(required)* | Path to the organization-specific TOML config file. |
| `--target` | *(required)* | Organization section/target: `portal` or `agol`. |
| `--no-test` | test mode ON | Run in production mode: removes the default 250-item cap and includes groups unless `--no-groups` is also supplied. |
| `--max-items N` | `None` | Cap the number of item rows written. Values `<= 0` mean no cap. |
| `--no-groups` | off | Skip group inventory. |
| `--with-groups` | off | Include groups even while test mode is active. |
| `--workers N` | `12` | Number of pre-created worker HTTP sessions/concurrent editor-tracking workers. Allowed range: `1-64`. |
| `--no-edit-tracking` | off | Skip live Feature Service editor-tracking/latest-edit detection. |
| `--trusted-service-origin HTTPS_ORIGIN` | none | Repeatable exact HTTPS origin that may receive the organization token for FeatureServer/MapServer requests. Normally federation metadata or AGOL policy supplies the needed trust. |
| `--tls-mode` | `standard` | TLS certificate verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | `None` | PEM CA bundle path. Required when `--tls-mode custom-ca-bundle`. |
| `--portal-url` | from config | Portal or AGOL URL override. |
| `--username` | from config | Login username override. |
| `--keyring-service` | from config | Keyring service holding the Portal/AGOL password. |
| `--smtp-server` | from config | SMTP relay override. |
| `--email-to` | from config | Recipient or comma-separated recipient list. |
| `--email-from` | from config | Sender address. |
| `--output-dir` | from config | Directory for CSVs and logs. |
| `-h`, `--help` | — | Display help and exit. |

## Performance

This script is built to inventory very large organizations quickly:

- **Items are enumerated through search rather than fetched individually.** Search results already contain the main detail fields, so the script does not issue a per-item metadata request for the main inventory.
- **Date-range bucketing avoids the item search result cap.** When a monthly search range reaches the configured cap, the range is subdivided and searched again.
- **The item catalog is disk-backed.** A temporary SQLite file keeps the complete discovered item set off the Python heap while preserving deterministic ID-order selection.
- **Groups are separately enumerated and de-duplicated by ID.**
- **Feature Service editor-tracking requests are concurrent.** Worker count is limited to 64 and each worker uses its own pre-created secure HTTP session.
- **REST connections use retries, pooling, and explicit response-size limits.**

Lower `--workers` when the client or endpoint is under pressure. Higher values can improve throughput for the concurrent Feature Service detail phase but do not remove the limits imposed by the service or network.

## Observed runtime

Actual runtime scales with the number of items and groups in the organization, the number of Feature Services, how many Hosted/Federated services require editor-tracking queries, network conditions, and the `--workers` value. Representative observed runs:

| Run type | Observed duration | Number of items |
|---|---|---:|
| Portal | ~1-5 minutes | ~20,500 |
| AGOL | ~1-5 minutes | ~100,000 |

These are operational observations, not guarantees.

## Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp and the org/platform prefix:

```text
[ORG]_[PLATFORM]_ITEM_INVENTORY_<ts>.csv
[ORG]_[PLATFORM]_FEATURE_SERVICE_INVENTORY_<ts>.csv
[ORG]_[PLATFORM]_ITEM_TYPE_SUMMARY_<ts>.csv
[ORG]_[PLATFORM]_item_inventory_<ts>.log
```

The actual casing of `<ORG>` and `<PLATFORM>` follows the values used by the shared utilities when the filenames are built.

The main CSV contains one row per item and included group. The Feature Service CSV contains one row per `Feature Service` item. The summary CSV counts the resulting item-type labels, with Feature Services separated by their `record_kind` classification.

If the combined CSVs exceed the configured `max-email-mb` cap, the script creates a single compressed ZIP for email when the ZIP itself fits under the cap. The uncompressed CSVs always remain on disk.

The completion email reports:
- Target, run mode (TEST/PRODUCTION), and TLS mode
- Effective item cap
- Whether groups were included
- Count of items/groups and Feature Services inventoried
- Output CSV paths or `(NOT WRITTEN)` if a CSV was not produced
- Standard run-timing block
- Incomplete editor-tracking or other processing reasons, when present

## Exit codes

- `0` — The inventory workflow reached the normal completion path. The email/report can still be marked `INCOMPLETE` when some details could not be fully inventoried.
- `1` — Fatal setup/processing/output failure, including output-path preparation, TLS/Portal connection failure, fatal inventory failure, or other unrecovered exception.
- `2` — Argument validation failure, including missing `--ca-bundle-path` when `custom-ca-bundle` is selected.

A run that finishes with some incomplete detail checks intentionally returns `0` because the overall inventory workflow completed; the generated report and email retain the `INCOMPLETE` indication.

## Notes and caveats

- **`platform` is validated from the requested target and authenticated Portal/AGOL URL.** A target/platform mismatch aborts the run.
- **Org-id resolution is required for the main item and optional group inventories.** If the organization ID cannot be established, the strict organization-scoped search is refused rather than falling back to an unscoped public search.
- **`item_type` is always `Group` for group rows.** The main inventory does not include a separate `record_kind` column.
- **Feature Service rows are selected from items whose type is exactly `Feature Service`.** Their `record_kind` is a separate classification used only in the Feature Service CSV and summary.
- **`last_fs_edited` is best-effort.** Registered services are not queried for editor-tracking information, and malformed service metadata or failed layer/query requests can produce a descriptive failure value and an incomplete-run reason.
- **The default test cap does not reduce discovery work.** The script still performs the strict organization-wide search and then selects the first 250 discovered items by ID order.
- **`--max-items` controls output selection.** It does not make the underlying search itself a sampled query.
- **Email attachment cap is configurable, project-wide.** If `max-email-mb` is unset in config, the shared default of 20 MB applies.
- **The log is always retained for email handling.** CSV attachments may be replaced by a ZIP when the combined size exceeds the configured cap and the ZIP fits.
- **The uncompressed CSVs always stay on disk**, even when a ZIP is created and attached.
- **TLS certificate verification is always on and cannot be disabled.** The only choice is which trust source is used.
- **Direct REST redirects are not automatically followed.** Any redirect that is considered is manually validated and must remain on the same origin under the configured Portal/service policy.
- **Large and malformed inputs are bounded.** REST responses are limited to 64 MB; item/group searches validate totals and pagination; `.msd` is not used by this script; temporary item data is stored in a file-backed SQLite catalog.
- **This is a snapshot, not a live view.** Results reflect the information available while the run was executing.
- **Read-only.** The script only reads and reports; it never modifies, shares, or deletes any item or group.
- **The import-path hardening verifies package origin.** It inserts the expected project root when absent and checks the `UTILITIES` package origin before importing it, but it does not remove duplicate equivalent project-root entries already present in `sys.path`.