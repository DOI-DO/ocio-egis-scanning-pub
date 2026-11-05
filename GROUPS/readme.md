# GROUPS_SCAN.py

Find ArcGIS groups in ArcGIS Online (AGOL) or ArcGIS Enterprise Portal that are at least 90 days old, inventory their content and non-owner membership, and report whether each group is stale, active, or indeterminate. The script writes a CSV and emails the results — it never changes anything.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Groups](#why-inventory-groups)
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

This report lists ArcGIS groups in your organization that are **90 days old or older**. Every valid group discovered in that age range is reported, including groups that are administrative/protected from members leaving and groups for which one or both counts could not be measured.

A group's `status` explains whether the measured content and membership make it look active or stale. If a count could not be determined, the status is `Indeterminate` and the affected count cell is left blank.

This is a strictly read-only tool — it never changes, deletes, or modifies groups, memberships, sharing, or content. It just reads what exists and makes a list so someone with the right knowledge and permissions can decide what to do about old groups.

## Why Inventory Groups

Groups are easy to create and almost never cleaned up, so they accumulate quietly in a way nobody is responsible for noticing. The report is intended to make it easy to identify old groups that may need review; it does not inspect ArcGIS layers for duplicate fields or duplicate service layers. Over time, organizations accumulate groups that were created for a project, event, or team that no longer exists. Groups are created for a specific purpose — to share maps, apps, and data with a particular set of people — but when that purpose ends, the group is often left behind and forgotten. Nothing in ArcGIS prompts anyone to revisit a group once its project wraps up, so the only way the organization ever regains a clear picture of which groups still matter is to deliberately inventory them.

The reason to inventory groups specifically — rather than just their content — is that a group is an access-control boundary, not merely a folder. A group determines who can see and share a set of maps, apps, and data, so a forgotten group is a forgotten sharing decision that quietly persists long after anyone remembers why it was made. An old group that is still `public` or `org`-shared, or one whose membership no longer matches who should have access, is a governance and sometimes a security concern that is invisible until someone looks. By pairing each group's age with its measured content, its non-owner membership, its sharing `access` level, and its administrative/protection flags in one row, this report turns a scattered collection of standing access decisions into a single reviewable list — which is exactly what a periodic governance or audit review needs and what browsing groups one at a time cannot provide.

This scan is designed to make that review safe and trustworthy rather than merely convenient, and that design is itself a reason to run it. It reports every qualifying group — including administrative groups and groups whose counts could not be measured — rather than silently dropping anything, so the inventory is complete by construction and a reviewer is never misled into thinking a group does not exist. Crucially, it distinguishes "measured as empty" from "could not be measured": a group whose content or membership could not be read is marked `Indeterminate` with a blank count instead of being reported as zero, so the report never manufactures false evidence that a group is abandoned. Because the scan is strictly read-only and classifies rather than acts, it is safe to run on a schedule as a standing governance check, giving administrators a current, honest snapshot of which old groups are genuinely active, which look stale and merit a conversation, and which could not be assessed and need a closer manual look.

The age threshold is **90 days** by default. It is a source-code setting (`AGE_DAYS = 90`), not a command-line option.

## Why It's a Problem

Old groups aren't dangerous by themselves, but they add clutter. They make searching harder ("Which group was it? There are 500..."), they complicate governance reviews, and they can hide truly active collaboration spaces in a sea of older ones.

A group is considered a candidate because its creation date is at least 90 days in the past. The report then measures two things:

- **Shared content** — maps, layers, apps, and other items shared to the group.
- **Non-owner membership** — unique people in the group's `admins` and `users` lists, excluding the group owner.

The resulting status is:

- **Stale: No content or members** — no shared content and no members besides the owner.
- **Stale: no members** — no members besides the owner, even if content is shared.
- **Stale: no content** — no shared content, even if other people are members.
- **Active** — at least one shared item and at least one non-owner member.
- **Indeterminate** — one or both counts could not be determined.

An old group with content and members is still reported as `Active`; it is not silently removed from the inventory because it is old.

## Why Removal Requires Caution

**Being on this list is not the same as being safe to delete.** Think of this report as a "worth a second look" list, not a to-do list for deletion.

A few important things to know:

- **Administrative groups are reported.** The `administrative` and `leavingDisallowed` columns show whether ArcGIS identified the group as protected from members leaving. Those groups are not automatically removed from this report.
- **Unknown counts are not zero.** If the script cannot measure a group's content or membership, it leaves the affected count blank and marks the status `Indeterminate`. Do not interpret a blank count as proof that the group is empty.
- **An owner-only group shows `0` members.** The owner is deliberately excluded from the member count, so the group may technically have one person associated with it while `memberCount` is still zero.
- **An old group can still be intentionally active.** A group may support recurring work, future work, automation, templates, seasonal activity, or controlled administrative processes.

### Why an empty group is not necessarily a bad group

It's tempting to assume "old and empty" means "forgotten and safe to delete," but there are many legitimate reasons a group could look this way:

- **Set up ahead of time.** A group for a new project or event may be created well before people and content are actually added.
- **Created automatically.** Some organizations' processes automatically create standard groups for new projects — even for projects that never need them.
- **Reserved for later use.** A group might exist purely so a future workflow or script has a ready destination to share things to.
- **Seasonal or recurring work.** Groups tied to yearly events or seasonal programs sit empty between active periods.
- **Mid-reorganization.** Content and members may have been deliberately removed while the group is kept to be repopulated later.
- **Permission gaps or measurement failures.** The account running the scan may not have complete visibility, or a REST/SDK count operation may have failed. In this version those cases are retained as an `Indeterminate` result rather than being treated as zero.

Treat every row on this report as "worth investigating," not "confirmed safe to remove."

## Recommendations for Investigation

Before taking any action on a group listed in this report:

1. **Check the log file** for that group's ID and review any discovery, administrative-flag, item-count, member-count, or TLS errors.
2. **Open the group directly** in ArcGIS Online or Portal using an account with appropriate administrative permissions and confirm the current members and content yourself.
3. **Remember the creator is never counted as a member.** A group with only its original creator will show `memberCount = 0`.
4. **Look at all the columns together** — creation date, last-modified date, access level, invitation setting, administrative state, item count, and member count — rather than deciding from just the `status` column.
5. **Contact the group's owner** (listed in the report) or the relevant team/department and ask if the group's current state is intentional.
6. **Search for references** to the group in scripts, apps, workflows, or templates before assuming it is unused.
7. **Test on a copy first.** If deletion is decided, test the intended administrative process in a non-production environment where practical.
8. **Keep the report and log** as documentation of what was checked and when.

## Column Descriptions

The CSV contains exactly the columns below, in this order:

| Column | What it means |
|---|---|
| `platform` | The detected platform for the successful run: `PORTAL` for ArcGIS Enterprise Portal or `AGOL` for ArcGIS Online. The script validates that the selected `--target` matches the detected platform before scanning. |
| `item_id` | The group's unique ArcGIS item/group ID. Use this value to look the group up directly. |
| `title` | The group's display name. |
| `owner` | The ArcGIS username that owns the group. |
| `date_created` | The group's creation date, rendered by the shared `epoch_to_date()` helper. |
| `date_modified` | The group's last-modified date, rendered by the shared `epoch_to_date()` helper. It may be blank when unavailable. |
| `memberCount` | The number of unique members from the `admins` and `users` lists, excluding the group owner. Blank when the member count could not be measured. |
| `itemCount` | The number of items returned by the ArcGIS group content collection. Blank when the item count could not be measured. |
| `status` | Classification from the two measured counts: `Stale: No content or members`, `Stale: no members`, `Stale: no content`, `Active`, or `Indeterminate`. |
| `administrative` | Boolean administrative/protection flag derived from `leavingDisallowed`. `True` means the group is marked so members cannot leave normally; blank means that setting could not be determined. |
| `leavingDisallowed` | The raw report value corresponding to the group's `leavingDisallowed` setting. It mirrors the `administrative` value in this CSV. |
| `isInvitationOnly` | Whether the group requires an invitation to join. Blank when the source value is unavailable. |
| `access` | The group's sharing level, typically `private`, `org`, or `public`. Blank when unavailable. |

Every valid discovered group in the requested age range produces exactly one CSV row. Administrative groups are included. A group with an unmeasured item or member count remains in the report with a blank count and `Indeterminate` status rather than being omitted.

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

- `cli.py`
- `config.py`
- `gis_connect.py`
- `auth.py`
- `secure_session.py`
- `logging_setup.py`
- `restutils.py`
- `emailer.py`

## Requirements

- Python 3.9+ with ArcGIS API for Python.
- `requests`, `urllib3`, and `keyring`.
- `tomli` on Python 3.10 or earlier if required by the shared configuration package; Python 3.11+ provides standard-library `tomllib`.
- Shared `UTILITIES` package in the expected project layout.
- Organization-specific TOML via `--config`.
- Password in the OS keyring or `PORTAL_PASSWORD` environment variable.
- Account able to authenticate, list groups, and read group definitions/membership.
- Network access to Portal or AGOL REST endpoints and SMTP.
- Write access to the configured output directory.

## Security: Certificate (TLS) Validation

This script always verifies server certificates. Certificate verification cannot be disabled.

- **ArcGIS SDK connection:** `connect_gis()` uses the selected verification mode.
- **Direct REST connection:** the script's own HTTP sessions use the same selected verification mode for group discovery, group-definition reads, and membership reads.
- **`standard` (default):** verifies against the public CA bundle.
- **`os-trust-store`:** verifies against the operating-system trust store.
- **`custom-ca-bundle`:** verifies against the PEM file supplied with `--ca-bundle-path`.

Before scanning begins, the script performs a one-time TLS handshake against the Portal/AGOL URL with the configured verification settings. If that verification fails, the run aborts before the inventory begins.

**SSRF / network-boundary defenses:** direct REST paths are constructed only under `/sharing/rest/`; Portal URLs must use HTTPS; path traversal, query strings, fragments, user-info credentials, and unsafe path forms are rejected. Organization discovery is explicitly scoped to the authenticated organization ID for both AGOL and Enterprise Portal. The group search is date-bounded and cannot intentionally fall back to an unscoped organization-wide/public search.

**Token handling:** the token is provided through a `TokenProvider`, is inserted at the request boundary, and callers cannot replace or smuggle an alternate token through request parameters. Authentication failures trigger one synchronized refresh-and-retry. Error text used for logging/email is passed through `redact_token()` so token-bearing text is not intentionally exposed there.

**Large or malformed responses:** direct JSON responses are streamed and capped at 64 MB. `Content-Length` is validated when present, the byte limit is enforced during streaming, response bodies must decode as JSON objects, ArcGIS error objects are validated, and group-search paging/total values are checked for consistency and forward progress. Malformed or capped search results abort the organization-wide inventory rather than silently becoming a partial successful scan.

**Redirect following:** `allow_redirects=False` is explicitly used for direct REST requests. A redirect response is refused rather than followed, so the request cannot silently move to another host or path.

**Python import-path/package shadowing:** the script calculates the expected project root and inserts it at the front of `sys.path` when that root is not already present. It then imports `UTILITIES` and verifies that the loaded package came from the expected `UTILITIES` directory. This version **does not remove equivalent duplicate project-root entries** from `sys.path`; it only inserts the expected root when absent and verifies the loaded package location.

## Configuration

Every run requires an organization-specific TOML file and a target:

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal
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

1. Command-line option
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

`max-email-mb` is optional; the shared default is 20 MB when it is not supplied.

Store passwords in the OS keyring, not in TOML:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('AGOL', 'agol_service_account', '<password>')"
```

### Changing the Age Threshold

The threshold is a source-code constant:

```python
AGE_DAYS = 90
```

To change it, edit the file and replace `90` with the desired number of days. The calculation uses elapsed time in milliseconds (`now - AGE_DAYS * 24 hours`), not a calendar-date boundary.

## How It Works

### Run Sequence

1. Parse command-line arguments and load the organization TOML.
2. Validate required settings and create the output directory.
3. Determine the target platform and validate that `--target` matches it.
4. Build the TLS-verified startup session and run the one-time TLS handshake check.
5. Connect to Portal/AGOL using the selected TLS verification mode.
6. Obtain a `TokenProvider` and tune the ArcGIS SDK session.
7. Build worker HTTP sessions using the same TLS policy.
8. Resolve the authenticated organization ID; abort rather than search without it.
9. Enumerate all groups in the age range with strict, organization-scoped, date-bounded searches.
10. Validate and de-duplicate discovered groups by group ID.
11. Resolve `leavingDisallowed` for every group; retain administrative groups in the report.
12. Measure item counts and non-owner member counts concurrently.
13. Classify each group as `Active`, a `Stale:` variant, or `Indeterminate`.
14. Write exactly one CSV row per valid discovered group.
15. Send the completion email with the CSV and log according to the shared attachment policy.

### Enumeration and Filtering

The script calls the Portal REST group endpoint:

```text
<portal-url>/sharing/rest/community/groups
```

Group discovery is organization-scoped for **both** AGOL and Enterprise Portal using:

```text
orgid:<authenticated-org-id>
```

The search is also constrained to a `created` time range. The scanner starts at epoch zero and ends at the 90-day cutoff, so it can enumerate old groups without relying on a single large result set.

The search page size is 100 groups. If a date range reaches the ArcGIS search cap of 10,000 results, the range is subdivided recursively until the result sets can be read without exceeding the cap.

Search totals, `nextStart`, result-list shapes, group IDs, and organization IDs are validated. Repeated IDs within a paged range, totals that change while paging, non-progressing pagination, malformed group entries, or an unsplittable capped range cause the scan to abort rather than produce a false complete inventory.

Results are de-duplicated by group ID.

### Item and Member Counts

**Item count:** Uses `gis.groups.get(group_id).content()` and counts the returned collection. A lookup failure is recorded as an item-count error, and the corresponding CSV `itemCount` is left blank.

**Member count:** Queries:

```text
<portal-url>/sharing/rest/community/groups/<group-id>/users
```

It reads both `admins` and `users`, normalizes usernames case-insensitively, removes the owner, and counts the unique remaining users. A lookup or validation failure is recorded as a member-count error, and the corresponding CSV `memberCount` is left blank.

**Administrative state:** `leavingDisallowed` is taken directly from the discovery result when available or resolved from the group definition when it is not. A lookup failure is recorded as an administrative-flag error, and both `administrative` and `leavingDisallowed` are blank for that row.

All three measurement categories are contained per group. One group with an error does not prevent other groups from being measured, but any such error makes the overall run `INCOMPLETE`.

### Staleness Classification

| Item count | Non-owner member count | Status |
|---:|---:|---|
| 0 | 0 | Stale: No content or members |
| > 0 | 0 | Stale: no members |
| 0 | > 0 | Stale: no content |
| > 0 | > 0 | Active |
| blank | any | Indeterminate |
| any | blank | Indeterminate |

Administrative status does not suppress a row. Administrative groups are included and are represented through the `administrative` and `leavingDisallowed` columns.

## Test Mode (Default)

`GROUPS_SCAN.py` has **no test mode**. Every run performs the full organization-scoped discovery and measurement of all groups at least 90 days old that are visible to the authenticated account.

Use a non-production organization/account when validating behavior. Lowering `--workers` reduces concurrency but does not reduce scan scope.

## Usage

Each scenario below describes a common operational situation, the command to run, and what to expect.

### Scenario 1 — Routine Portal Governance Sweep

**Situation:** Your team performs a periodic Enterprise Portal governance review and wants a complete inventory of groups that have reached the 90-day age threshold.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal
```

**Expect:** The script performs the complete old-group inventory, writes one CSV row per valid discovered group, includes administrative groups, and emails the CSV/report summary. If some counts cannot be measured, those rows remain in the CSV with blank count cells and `Indeterminate` status, and the run is marked `INCOMPLETE`.

### Scenario 2 — Routine AGOL Governance Sweep

**Situation:** The same review is needed for your ArcGIS Online organization instead of Enterprise Portal.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target agol
```

**Expect:** Discovery is scoped to the authenticated AGOL organization ID. The scanner does not intentionally fall back to a public or unscoped search if that organization ID cannot be established.

### Scenario 3 — Endpoint Throttling

**Situation:** The service endpoint is returning throttling responses or the host is under resource pressure when 32 workers run concurrently. You reduce concurrency to make the run gentler.

```bash
python GROUPS_SCAN.py --config OTHER_ORG.config.toml --target agol --workers 16
```

**Expect:** The scan still covers the same group population, but fewer group lookups run concurrently. This can reduce endpoint pressure at the cost of longer elapsed time.

### Scenario 4 — Custom Output Location

**Situation:** A governance or audit process requires the generated CSV and log to be placed in a specific report directory for this run.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal --output-dir "D:\reports"
```

**Expect:** The script writes the timestamped CSV and log under the specified output directory, provided the account has write permission there.

### Scenario 5 — Additional Email Recipients

**Situation:** A governance review is underway and you want to send the same run results to both an analyst and an administrator without changing the TOML file.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal --email-to "analyst@example.com;admin@example.com"
```

**Expect:** The command-line `--email-to` value overrides the configured recipients for this run. The existing report and log handling remains unchanged.

### Scenario 6 — Internal Portal Using the OS Trust Store

**Situation:** Your Portal certificate chains to a certificate authority that is already trusted by the operating system, and you want the script to use that trust source for TLS verification.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal --tls-mode os-trust-store
```

**Expect:** The startup handshake and subsequent connections use the OS trust-store verification mode, subject to the capabilities of the shared secure-session utility. Certificate validation remains enabled.

### Scenario 7 — Internal Portal Using a Custom CA Bundle

**Situation:** Your organization distributes a specific PEM CA bundle and the CA is not installed in the host's standard public trust store.

```bash
python GROUPS_SCAN.py --config ORG.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\org-ca.pem"
```

**Expect:** The startup handshake and scan connections use the specified PEM bundle. Omitting `--ca-bundle-path`, or supplying it with another TLS mode, causes argument validation to fail with exit code `2`.

## Command-Line Options

| Option | Required | Purpose |
|---|---:|---|
| `--config TOML_PATH` | Yes | Path to the organization-specific TOML file. |
| `--target {portal,agol}` | Yes | Selects the target configuration and validates the platform. |
| `--portal-url URL` | No | Portal/AGOL URL override. |
| `--username USERNAME` | No | Login username override. |
| `--keyring-service NAME` | No | Keyring service override. |
| `--output-dir PATH` | No | Report/log directory override. |
| `--smtp-server HOST` | No | SMTP relay override. |
| `--email-to ADDRESS_LIST` | No | Email recipient override (comma/semicolon-separated). |
| `--email-from ADDRESS` | No | Email sender override. |
| `--workers N` | No | Concurrent worker threads. Default: `32`; allowed range: `1-64`. |
| `--tls-mode MODE` | No | TLS verification mode: `standard`, `os-trust-store`, or `custom-ca-bundle`. Default: `standard`. |
| `--ca-bundle-path PATH` | Only with `custom-ca-bundle` | PEM CA bundle file path. |
| `-h`, `--help` | No | Display help and exit. |

## Performance

- Group discovery is sequential by date range, but capped result ranges are recursively subdivided to avoid the 10,000-result search limit.
- Search pages contain up to 100 groups.
- Administrative-flag lookups, item counts, and member counts use bounded concurrency.
- Default worker count is `32`; the allowed range is `1-64`.
- Worker REST sessions use streamed responses and a 64 MB JSON size cap.
- Direct REST requests use a 30-second timeout constant and secure-session retries configured by the shared utility.
- Group membership names are de-duplicated before counting.

Lower `--workers` when an endpoint throttles or when the client host has limited resources.

## Observed Runtime

> These are operational observations, not guarantees. Runtime depends on the number of visible groups, the number of old candidates, Portal/AGOL latency, count operations, worker count, and endpoint behavior.

| Run type | Observed duration | Groups |
|---|---|---:|
| Portal | ~1-3 minutes | ~2,500 |
| AGOL | ~1-3 minutes | ~5,000 |

## Output File

A successful run attempts to write a CSV and a log. A ZIP may be created by the shared attachment helper if the CSV exceeds the configured email attachment cap.

**CSV:**
```text
<ORG>_<PLATFORM>_GROUPS_INVENTORY_<YYYYMMDD_HHMM>.csv
```

**Log:**
```text
<ORG>_<PLATFORM>_groups_inventory_<YYYYMMDD_HHMM>.log
```

**Optional ZIP (if report exceeds email cap):**
```text
<ORG>_<PLATFORM>_GROUPS_INVENTORY_<YYYYMMDD_HHMM>_attachments.zip
```

Example:
```text
ORG_PORTAL_GROUPS_INVENTORY_20260829_1921.csv
ORG_PORTAL_groups_inventory_20260829_1921.log
```

The CSV is UTF-8 with one header row followed by exactly one row for each valid discovered group in the requested age range. A clean run with no qualifying groups still writes the header-only CSV and can exit `0` when the file write succeeds. Rows are not explicitly sorted.

`date_created` and `date_modified` are rendered by the shared `epoch_to_date()` helper. `date_modified` is blank when the source value is unavailable.

## Exit Codes

| Exit code | Meaning |
|---:|---|
| `0` | The scan completed with a successfully written CSV. A header-only CSV is valid when no qualifying groups were found, and a successful complete scan returns `0`. |
| `1` | Run aborted because of an operational/configuration/connection/TLS failure, **or** the scan completed with one or more discovery/administrative/count errors, **or** the CSV could not be written. Check the log and email. |
| `2` | Argument or configuration validation failed, including missing `--config`, missing `org-name`, invalid target, missing `--ca-bundle-path` for `custom-ca-bundle`, or supplying `--ca-bundle-path` with another TLS mode. |

## Notes and Caveats

- **This is an inventory, not a deletion tool.** It never modifies groups, memberships, sharing, or content.
- **There is no test mode.** Every run performs the full discovery and measurement of all qualifying groups; `--workers` changes concurrency, not scope.
- **The age threshold is source-controlled.** `AGE_DAYS = 90` is not a CLI or TOML setting; edit the source to change it.
- **Discovery is organization-scoped.** Both Portal and AGOL searches require an authenticated organization ID. The script refuses an unscoped search.
- **Search results are strictly validated.** Malformed pages, inconsistent totals, non-progressing pagination, repeated IDs within a search range, and unsplittable result caps abort the inventory instead of producing a partial successful result.
- **Visibility affects every result.** The inventory and counts are limited by what the authenticated account can see and read.
- **Administrative groups are included.** `leavingDisallowed: true` is reported through both `administrative` and `leavingDisallowed`; it does not automatically remove the row from the CSV.
- **Member count excludes the owner.** An owner-only group has `memberCount = 0`.
- **Admins and users are de-duplicated.** Membership is counted once per person, case-insensitively, across both arrays.
- **Count failures become blank/indeterminate, not zero.** A failed item or member lookup leaves the corresponding CSV count blank and contributes to an `INCOMPLETE` result.
- **Administrative-flag failures are retained.** When `leavingDisallowed` cannot be resolved, the group remains in the report with blank administrative columns and contributes to an `INCOMPLETE` result.
- **Missing creation dates can pass the age filter.** A falsey or zero creation value can be interpreted as epoch zero and therefore meets the age threshold; review such rows carefully.
- **Date comparison uses elapsed time.** The cutoff is `now - AGE_DAYS * 24 hours`, expressed in milliseconds.
- **`--workers` is range-validated.** Values from `1` through `64` are accepted.
- **The script uses private ArcGIS SDK attributes.** Test after upgrading the ArcGIS API for Python.
- **TLS verification is always on.** `--tls-mode` selects the trust source; it does not disable certificate validation.
- **Redirects are disabled for direct REST calls.** A redirect response is refused rather than followed.
- **Large JSON responses are bounded.** The direct REST client rejects responses over 64 MB and validates JSON/object structure before use.
- **Tokens are redacted from operational text.** Token-bearing error text is sanitized before logging/emailing through the shared redaction helper.
- **The import path is verified, not cleaned of duplicates.** The script inserts the expected project root when absent, then checks that the imported `UTILITIES` package came from the expected directory. It does not remove equivalent duplicate roots from `sys.path`.
- **The CSV is always attempted.** A clean run produces a header-only file when no qualifying groups are found.
- **Platform is derived from the Portal/AGOL URL.** The selected target must match the detected platform for the run to proceed.
- **The report cap excludes the log.** The shared `gather_attachments()` helper applies the email attachment policy to the report files while the log is retained separately.
- **ZIP files are not assumed to be temporary.** If the shared attachment helper creates one, its retention is determined by that helper/output handling.
- **Email failure does not by itself change a successful scan result.** The shared email helper handles delivery errors according to its own logging behavior; the local CSV remains available on disk.