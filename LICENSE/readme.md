# ESRI License Inventory and Reconciliation Tools

This folder contains two complementary reporting tools:

- `NAMED_USER_LICENSE_INVENTORY.py` inventories ArcGIS Pro named-user entitlements and usage for an ArcGIS Online organization or ArcGIS Enterprise Portal.
- `ESRI_LICENSE_REPORT.py` reconciles exported user-type assignments across one or more Portal and/or ArcGIS Online CSV reports, preserving source assignments while reporting the highest-ranked user type once per safely reconciled account group.

| Tool | Main question |
| --- | --- |
| `NAMED_USER_LICENSE_INVENTORY.py` | Who has ArcGIS Pro entitlements, and what evidence is available that they are using Pro? |
| `ESRI_LICENSE_REPORT.py` | Which exported accounts can be matched across platforms or source reports, and what single highest-ranked user type should represent each reconciled group? |

The inventory tool queries a live organization. The reconciliation tool reads local CSV exports and does not connect to Portal or AGOL.

Do not assume the inventory tool's CSV can be used directly as reconciliation input. The reconciliation tool requires `platform`, `username`, `email`, and `userLicenseTypeId`; the inventory report documented here uses different fields for some information.

Every major heading below contains separate subsections for the two scripts, in this order:

1. `NAMED_USER_LICENSE_INVENTORY.py`
2. `ESRI_LICENSE_REPORT.py`

---

## Table of Contents

### For Report Readers

- [Overview](#overview)
- [Why reconcile licenses across multiple platforms](#why-reconcile-licenses-across-multiple-platforms)
- [Why it's a problem](#why-its-a-problem)
- [Why removal requires caution](#why-removal-requires-caution)
- [Recommendations for investigation](#recommendations-for-investigation)
- [Column descriptions](#column-descriptions)

### For Administrators

- [Dependencies on the shared UTILITIES package](#dependencies-on-the-shared-utilities-package)
- [Requirements](#requirements)
- [Security](#security)
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

# For Report Readers

## Overview

### NAMED_USER_LICENSE_INVENTORY.py

This report answers:

> Who has an ArcGIS Pro license, and are they actually using it?

The script starts from the organization's user directory rather than a recent-usage list. For every selected user in an ArcGIS Online organization or ArcGIS Enterprise Portal, it reports:

- Whether Pro entitlements are assigned.
- The Pro tier: Basic, Standard, or Advanced.
- Assigned extensions, such as Spatial Analyst or 3D Analyst.
- Whether a license is checked out for offline use.
- Available offline-checkout dates.
- The most recent available evidence of an ArcGIS Pro desktop session.
- Website or mobile-app login activity, recorded separately from Pro use.
- The user's role.

The script writes a CSV and emails it with a summary and detailed log. It is inventory-only and does not revoke, reassign, or disable licenses.

### ESRI_LICENSE_REPORT.py

This report answers:

> Which accounts in our exported Portal and AGOL reports represent a safely matched account group, and what highest-ranked user type should we count for that group?

The script reads one or more exported user CSVs and produces a consolidated report. It supports:

- One Portal report.
- One AGOL report.
- One Portal report compared with one AGOL report.
- Multiple reports from the same platform.
- Multiple reports from both platforms.
- Files containing a mixture of Portal and AGOL rows.

The report preserves each source account's username, display name, and user-type IDs. It also produces one selected `license_reported` value per output row.

A safely matched group appearing in multiple sources becomes one row. Its highest-ranked user type is counted once in the report's selected-type totals rather than counting each source assignment separately.

The script reads local files only. It does not query live systems, change assignments, measure Pro usage, or send email.

## Why reconcile licenses across multiple platforms

### NAMED_USER_LICENSE_INVENTORY.py

A useful license review starts by establishing what is assigned and what evidence exists that those assignments are being used.

A recent-usage report alone cannot answer that question because it can omit licensed users who have generated no recent activity. The inventory script starts with the organization roster and then adds entitlement, usage, and offline-checkout information.

This helps administrators investigate:

- Assigned Pro entitlements with no recent usage evidence.
- Old offline checkouts.
- Extensions assigned to users who may no longer need them.
- Differences between website activity and Pro desktop activity.
- Gaps that require further verification before reassignment.

The script inventories one selected platform per run. It does not itself reconcile the same account holder across separate Portal and AGOL inventories.

### ESRI_LICENSE_REPORT.py

The purpose of reconciliation is to avoid treating every exported account occurrence as a different license-reporting subject.

The same account holder can appear in Portal, AGOL, and multiple exports. Those appearances can contain different user-type assignments. Adding the raw source totals together can therefore count the same safely matched group several times and place it in several user-type categories.

The script separates two concepts:

- Source assignments: everything reported by each input source.
- Reconciled reporting type: one highest-ranked type selected for each output row.

For example:

| Source | Email | Username | Source user type |
| --- | --- | --- | --- |
| Portal | `jsmith@example.com` | `jsmith@portal.example` | `creatorUT` |
| AGOL | `jsmith@example.com` | `jsmith@agol.example` | `GISProfessionalStdUT` |

When these are unambiguous matches, they become one output row:

| Reconciled group | `license_reported` | Contribution to selected-type totals |
| --- | --- | --- |
| `jsmith@example.com` + `jsmith` | `GISProfessionalStdUT` | One Professional Standard row |

The group is not counted once as Creator and again as Professional Standard. The highest-ranked assignment represents that group in the consolidated user-type summary, while both original source assignments remain available for audit.

This is a reporting rule, not a licensing-rights determination. It does not establish that one purchased license covers both platforms, that a lower assignment can be removed, or that platform-specific access is unnecessary.

The rule applies once per output row—not unconditionally once per person or email address. Different username prefixes and ambiguous matches remain separate.

## Why it's a problem

### NAMED_USER_LICENSE_INVENTORY.py

A report built only from recent Pro activity can miss the users who are most important to investigate: people who hold entitlements but have not generated recent activity.

The report also distinguishes two different kinds of activity:

- `last_login` — website or mobile-app login.
- `pro_last_used_session` — the most recent available evidence of an ArcGIS Pro desktop session.

A recent website login does not prove that the user recently opened Pro. Someone can log into the website daily while not using Pro for months.

Missing usage evidence also needs context. It can indicate inactivity, but it can also reflect limitations in the available reporting sources.

### ESRI_LICENSE_REPORT.py

Two opposite errors can distort a cross-platform license review:

1. Counting every source occurrence independently can overcount safely matched groups.
2. Merging accounts too aggressively can hide legitimate separate accounts.

Email alone is not sufficient evidence for merging. An email address can be associated with multiple usernames, including administrative or other separate accounts.

The script requires agreement on both:

- Normalized email.
- Normalized username prefix before the first `@`.

It does not use display names for matching.

Repeated exports introduce another distinction: the same account in two files is two source-account occurrences, even when those occurrences safely reconcile into one output row.

For that reason, source-account counts and selected user-type totals answer different questions and should not be treated as interchangeable.

## Why removal requires caution

### NAMED_USER_LICENSE_INVENTORY.py

This report never removes or reassigns licenses.

Before acting on a finding:

- A blank or `never` `pro_last_used_session` does not always mean Pro is no longer needed. The relevant activity may fall outside the available reporting window.
- Portal does not have the AGOL usage-report fallback described below.
- An old offline checkout may still support active field work.
- Seasonal or project staff can have legitimate gaps between sessions.
- Verify business need with the user or supervisor before reclaiming an entitlement.

Use the report as evidence for investigation, not as an automatic removal list.

### ESRI_LICENSE_REPORT.py

Selecting one highest-ranked reporting type does not make the other source assignments unnecessary.

An account may still need a lower-ranked assignment on another platform to access that platform's content, groups, applications, or workflows.

Before changing assignments:

- Confirm that the matched accounts belong to the intended account holder.
- Review the business purpose of each platform account.
- Check platform-specific access and applicable licensing arrangements.
- Resolve ambiguous matches manually.
- Treat blank user types as missing information, not as proof that no assignment is needed.
- Do not assume that a consolidated reporting count establishes purchased-seat requirements.

The script never removes accounts, revokes licenses, transfers entitlements, or changes user types.

## Recommendations for investigation

### NAMED_USER_LICENSE_INVENTORY.py

- Review `has_pro_license` alongside the entitlement details and completion status. Missing lookup information should not be treated as a confirmed negative.
- Compare `pro_last_used_session` with `last_login`. A recent website login with stale Pro usage evidence is worth investigating.
- Review `taken_offline = yes` and `disconnected_since` for old checkouts.
- Use `all_entitlements` as the complete entitlement audit trail.
- Check `pro_last_used_source` before interpreting a usage date.
- If expected AGOL activity is missing, review the `--app-usage-window` setting.
- Read the completion email and log for `INCOMPLETE` stages before drawing conclusions.

### ESRI_LICENSE_REPORT.py

- Review `AMBIGUOUS_PREFIX_NOT_MERGED` rows first. They were intentionally left separate.
- Compare source `*_userLicenseTypeId` values with `license_reported` to explain the selected highest type.
- Use `multiple_accounts_same_instance` to identify emails with multiple accounts within one source.
- Use `multiple_accounts_same_platform` to review email-wide account occurrences across all supplied exports for a platform.
- Consult the logged source-prefix mapping before interpreting numbered columns.
- Count `license_reported` once per output row for the consolidated user-type summary.
- Do not count all source type fields as separate entries in that consolidated summary.
- Do not sum account-count columns down the CSV. Their email-wide values repeat on every row for the same email.
- Remember that repeated snapshots can increase platform-wide account counts without representing additional people.

## Column descriptions

### NAMED_USER_LICENSE_INVENTORY.py

| Column | What it means | Where it comes from |
| --- | --- | --- |
| `PLATFORM` | `PORTAL` or `AGOL`, identifying the selected platform. | The run's selected target. |
| `username` | Account name. | User directory. |
| `full_name` | Display name. | User directory. |
| `user_level` | User license-level value. | User directory. |
| `role` | Friendly standard-role label or custom role name when resolved. | User directory and custom-role lookup. |
| `has_pro_license` | `yes` when any Pro entitlement is present, including a tier or extension; `no` otherwise. Interpret with lookup-completion information. | Complete entitlement list. |
| `pro_license` | Pro tier: Basic, Standard, or Advanced. Blank when no tier is identified. | Entitlement classification. |
| `pro_extensions` | Semicolon-separated extension assignments. Unrecognized entitlement codes are retained here. | Entitlement classification. |
| `all_entitlements` | Complete raw entitlement-code list. | Entitlement response. |
| `pro_last_used_session` | Most recent available Pro desktop-use date. Shows `never` when no usable value is found. | Layered usage resolution. |
| `pro_last_used_source` | Signal supplying the date: `sessions-endpoint`, `usage-report (fallback)`, or `userEntitlements.lastLogin`. Blank when no signal has data. | Internal resolution logic. |
| `last_login` | Website or mobile-app login date, not proof of Pro use. Shows `never` when no login is recorded. | User account record. |
| `taken_offline` | `yes` when an offline checkout is reported; `no` when the flag is available and false; blank when unavailable. | Entitlement record. |
| `disconnected_session` | Offline-session name, when available. | Entitlement record. |
| `disconnected_since` | Offline-checkout start. Shows `never` for a system placeholder; blank when unavailable. | Entitlement record. |
| `disconnected_until` | Offline-checkout expiration or return date, using the same conventions. | Entitlement record. |

### ESRI_LICENSE_REPORT.py

#### How accounts are reconciled

The script performs normalization, source-record consolidation, and cross-source matching as separate steps.

1. Normalize email by trimming whitespace and applying case-insensitive case folding.
2. Trim the full username and derive `username_base` from the portion before the first `@`, also case-folded.
3. Within each file/platform source instance, consolidate repeated records sharing the same normalized email and exact trimmed full username.
4. Group source accounts by `(email, username_base)`.
5. Merge a group across multiple source instances only when every participating instance contributes exactly one account.
6. Keep ambiguous or unmatched accounts separate.
7. Verify that every distinct source account is represented exactly once.

Full username casing is preserved and is significant for duplicate-record consolidation within one source. For example, `JSmith` and `jsmith` are distinct source-account keys, even though their normalized prefixes are the same.

Display names are not used for matching.

#### Ambiguous matches

A group is ambiguous when:

- It occurs in more than one source instance; and
- At least one participating instance contains multiple accounts with that email and username prefix.

In that situation, every account in the group is written separately with:

```text
AMBIGUOUS_PREFIX_NOT_MERGED
```

The script does not select a preferred candidate.

If a group occurs in only one source instance, its accounts also remain separate rather than being merged within that instance.

#### How user types are reconciled

For each output row, the script collects all distinct nonblank user-type IDs from the accounts represented on that row.

It selects the highest value using this fixed ranking:

| Rank, lowest to highest | Exact user-type ID |
| ---: | --- |
| 1 | `viewerUT` |
| 2 | `editorUT` |
| 3 | `fieldWorkerUT` |
| 4 | `creatorUT` |
| 5 | `GISProfessionalStdUT` |
| 6 | `GISProfessionalAdvUT` |

The selected ID is written to `license_reported`.

| Types represented on one output row | `license_reported` |
| --- | --- |
| `viewerUT`, `creatorUT` | `creatorUT` |
| `creatorUT`, `GISProfessionalStdUT` | `GISProfessionalStdUT` |
| `GISProfessionalStdUT`, `GISProfessionalAdvUT` | `GISProfessionalAdvUT` |
| Blank, `creatorUT` | `creatorUT` |
| All blank | Blank |

Blank IDs are not inferred. A nonblank ID outside this exact, case-sensitive ranking causes validation to fail instead of being silently mapped or discarded.

The same selection rule applies when duplicate records for a single source account contain several different supported IDs.

#### Why the highest type is counted only once

The script's selected-type summary counts output rows by `license_reported`.

A safely reconciled group contributes one row and therefore one selected type, regardless of how many source assignments it contains.

This avoids counting the same matched group under several categories. The original assignments remain visible in the source fields, so consolidation does not erase the evidence supporting the selected result.

This is not global deduplication by email. Separate username prefixes, single-instance accounts, and ambiguous matches can produce several rows for the same email.

#### Source-prefix conventions

`<PREFIX>` in the table below means a source-instance prefix:

- One AGOL source: `AGOL`
- One Portal source: `PORTAL`
- Multiple AGOL sources: `AGOL_1`, `AGOL_2`, and so on
- Multiple Portal sources: `PORTAL_1`, `PORTAL_2`, and so on

A source instance is a file/platform combination. A mixed-platform file can supply both an AGOL instance and a Portal instance.

The log records the mapping between prefixes, files, and platforms.

#### CSV fields

| Column | What it means | Where it comes from |
| --- | --- | --- |
| `email` | Trimmed, case-folded email used in matching. Several rows can share this value. | Input `email`. |
| `username_base` | Case-folded username portion before the first `@`. Used together with email for matching. | Input `username`. |
| `platform` | Platforms represented on this row: `AGOL`, `PORTAL`, or both separated by a vertical bar. | Accounts included on the row. |
| `source_instances` | Source prefixes represented on this row, separated by vertical bars in processing order. | Participating sources. |
| `<PREFIX>_username` | Original trimmed full username for this source account. Blank if no account from that source participates on the row. | Input `username`. |
| `<PREFIX>_fullName` | Sorted, distinct nonblank display names for this source account, separated by vertical bars. Blank if unavailable. | Optional input `fullName`, consolidated across duplicate records. |
| `<PREFIX>_userLicenseTypeId` | Sorted, distinct nonblank source type IDs, separated by vertical bars. These are the source values, not the ranked selection. | Input `userLicenseTypeId`, consolidated across duplicate records. |
| `license_reported` | One highest-ranked supported type for the accounts on this row. Blank when all their IDs are blank. | Fixed ranking above. |
| `AGOL_account_count_for_email` | Number of distinct source accounts with this email across all supplied AGOL instances, regardless of username prefix or participation on this row. Zero if none. | Email-wide AGOL count. |
| `PORTAL_account_count_for_email` | Equivalent count across all supplied Portal instances. | Email-wide Portal count. |
| `<PREFIX>_account_count_for_email` | Number of distinct accounts with this email in this specific source instance, regardless of prefix or participation on this row. | Email-wide per-instance count. |
| `multiple_accounts_same_platform` | Platforms whose email-wide count exceeds 1, separated by vertical bars; otherwise `NONE`. | Platform counts. |
| `multiple_accounts_same_instance` | Source prefixes whose email-wide count exceeds 1, separated by vertical bars; otherwise `NONE`. | Per-instance counts. |
| `cross_platform_match` | Reconciliation outcome, including same-platform and single-source cases. | Matching logic and supplied sources. |

Both platform-wide count fields always exist, including when one platform was not supplied.

When a platform has only one source, its unnumbered per-instance count field has the same name as the platform-wide count field and appears only once. Multiple sources receive numbered per-instance count fields in addition to the platform-wide count.

Counts are calculated after duplicate-record consolidation within a source but before cross-source matching.

Consequently:

- The same account exported in two sources counts once in each source.
- Those two occurrences may still merge into one output row.
- Email-wide counts repeat on every output row with that email.
- A source's email-wide count can be positive even when that row's source username is blank.
- Multiple-account flags identify account occurrences requiring review, not proven duplicate people.

#### Match-status values

| `cross_platform_match` value | Meaning |
| --- | --- |
| `EMAIL_AND_USERNAME_PREFIX` | Safe email-and-prefix match across multiple instances spanning both AGOL and Portal. |
| `EMAIL_AND_USERNAME_PREFIX_SAME_PLATFORM` | Safe match across multiple instances of the same platform. Other supplied platforms may exist without participating in this row. |
| `AMBIGUOUS_PREFIX_NOT_MERGED` | The group spans multiple instances and at least one has multiple candidates. Every account in that group remains separate. |
| `OTHER_PLATFORM_NOT_SUPPLIED` | Only one source instance was supplied, so no cross-source match is possible. |
| `NO_MATCHING_ACCOUNT_ON_OTHER_INSTANCE` | Multiple instances of only one platform were supplied, but this email/prefix group occurs in only one instance. |
| `NO_MATCHING_ACCOUNT_ON_OTHER_PLATFORM` | Both platforms were supplied, but this email/prefix group occurs in only one instance. |

These statuses describe the supplied exports, not verified absence from a live system.

---

# For Administrators

## Dependencies on the shared UTILITIES package

### NAMED_USER_LICENSE_INVENTORY.py

- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `restutils.py`
- `cli.py`
- `auth.py`
- `secure_session.py`

### ESRI_LICENSE_REPORT.py

- `__init__.py`

## Requirements

### NAMED_USER_LICENSE_INVENTORY.py

- ArcGIS API for Python and `requests`.
- Shared `UTILITIES` package.
- Organization-specific TOML configuration.
- Credentials in the OS keyring or `PORTAL_PASSWORD`.
- Administrative visibility to users, entitlements, sessions, and applicable AGOL usage reports.

### ESRI_LICENSE_REPORT.py

- Python 3 and the shared `UTILITIES` package, including its dependencies.
- CSVs containing `platform`, `username`, `email`, and `userLicenseTypeId`; optional `fullName`.
- Nonblank email and username/prefix; platform `AGOL` or `PORTAL`.
- Read access to inputs and write access to the output folder.

## Security

### NAMED_USER_LICENSE_INVENTORY.py

#### Certificate validation

Certificate validation remains enabled. There is no option to disable verification.

The script uses:

- An ArcGIS SDK connection opened through `connect_gis()`.
- Separate secure HTTP sessions for its direct REST requests.

The direct REST verification strategy is selected with:

| Mode | Purpose |
| --- | --- |
| `standard` | Standard public CA trust. |
| `os-trust-store` | Operating-system certificate trust, useful for an organizational CA. |
| `custom-ca-bundle` | Explicit PEM bundle supplied through `--ca-bundle-path`. |

A startup handshake check must succeed before the inventory proceeds.

#### SSRF/network-boundary defenses

Direct REST calls are restricted to the authenticated Portal/AGOL `/sharing/rest/` path.

Validation rejects unsafe URL forms, including:

- Non-HTTPS destinations.
- User-info credentials.
- Query strings or fragments in endpoint URLs.
- Encoded path separators.
- Path traversal.
- Unsafe control characters.

Organization enumeration requires a validated authenticated organization ID.

#### Token handling

Authentication is supplied through the shared token-provider mechanism. The token is inserted when constructing the request rather than being included in logged endpoint URLs.

Caller-supplied `token` and `f` values are replaced with controlled values.

Authentication failures permit one synchronized refresh/retry. Error text is sanitized through `redact_token()` before logging, email, or CSV output.

#### Large/malformed responses

Direct REST responses are streamed and limited to 64 MB.

JSON and expected object/list structures are validated. User enumeration checks:

- Totals.
- Pagination progress.
- `nextStart`.
- Usernames.
- Organization IDs.
- Duplicate results.

Malformed or inconsistent pages are not silently accepted.

#### Python import-path/package shadowing

The existing inventory documentation describes locating the expected `UTILITIES` directory, inserting the project root when absent, checking the package origin with `importlib.util.find_spec()`, and refusing an unexpected origin.

It does not describe removing equivalent duplicate project-root entries.

Do not describe this as the stronger sequence of removing all equivalent roots, inserting the expected root first, importing the package, and verifying the loaded package afterward unless that exact sequence is confirmed in the deployed inventory script.

#### Redirect following

Automatic redirect following is explicitly disabled with `allow_redirects=False`.

The script can consider up to three redirects through its own validator. Accepted redirects must remain within the permitted origin and authenticated REST path. Cross-origin redirects are refused.

### ESRI_LICENSE_REPORT.py

#### Network-boundary defenses, tokens, and redirects

This script performs local CSV analysis and makes no HTTP requests.

It therefore does not implement or require an HTTP request policy for this workflow:

- No service URLs are contacted.
- No Portal or AGOL authentication tokens are used.
- No TLS sessions are created.
- No HTTP redirects are followed.
- There is no `allow_redirects=False` request setting because there are no requests.

The inventory script's network-security features should not be attributed to this script.

#### Malformed input and resource limits

CSV validation rejects:

- Missing required headers.
- Duplicate headers after normalization.
- Rows with more fields than the header.
- Missing required field values caused by incomplete rows.
- Invalid platforms.
- Platform values conflicting with an explicit platform flag.
- Blank emails.
- Blank usernames or username prefixes.
- Repeated resolved input paths.
- Unsupported nonblank user-type IDs.

The script does not define an explicit file-size, row-count, or memory ceiling. Accounts and output rows are held in memory.

The script itself does not explicitly neutralize spreadsheet formulas. Behavior inside the shared CSV writer cannot be verified without its implementation.

#### Python import-path/package shadowing

The script searches its directory and parent directories for:

```text
UTILITIES/__init__.py
```

It inserts the containing project root at the front of `sys.path` only if that exact root string is not already present, then imports from `UTILITIES`.

It does not:

- Remove equivalent copies of the project root.
- Move an already-present root to the front.
- Independently verify the loaded package's origin after import.

The stronger import-hardening sequence listed above is not implemented in the supplied reconciliation script.

Keep the script's directory, parent directories, Python environment, and shared package trusted.

#### Output safeguards

The script:

- Rejects repeated resolved input paths.
- Refuses an output path equal to an input path.
- Refuses an existing output unless `--overwrite` is supplied.
- Writes to a temporary file in the destination directory.
- Replaces the final destination after the temporary CSV is successfully written.
- Attempts cleanup of temporary files and newly reserved destinations after handled failures.

## Configuration

### NAMED_USER_LICENSE_INVENTORY.py

Use:

```text
--config <org_toml_path>
--target portal|agol
```

The target selects the corresponding organization configuration section.

Configuration includes:

- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

Command-line values override configuration values.

The password is read from the OS keyring, falling back to `PORTAL_PASSWORD`.

The selected target is also used to validate that the configured URL classifies as the expected platform.

### ESRI_LICENSE_REPORT.py

All settings are command-line arguments.

The default output folder is:

```text
C:\temp
```

No TOML configuration, organization credentials, keyring lookup, or SMTP settings are used.

Inputs are collected in this order:

1. All `--agol` files.
2. All `--portal` files.
3. All `--input` files.

Order within each flag group is retained. A mixed-platform file contributes AGOL before Portal.

Prefix numbering follows this processing order, not the interleaving of different flag names on the command line.

The script imports these helpers from the `UTILITIES` package interface:

- `ensure_output_dir`
- `setup_logging`
- `write_dict_csv`

Only `__init__.py` is identifiable as the imported package file from the supplied script. The underlying helper-module filenames cannot be verified without the shared package source and are not guessed in the dependency list.

## How it works

### NAMED_USER_LICENSE_INVENTORY.py

1. Build secure HTTP sessions and verify the TLS handshake.
2. Connect through `connect_gis()` and resolve the authenticated organization ID.
3. Find the ArcGIS Pro listing item through available licensing or provisioning sources.
4. Enumerate organization users with strict pagination and the resolved user cap.
5. Fetch user entitlements in parallel.
6. Fetch user sessions in parallel.
7. Fetch the organization-wide Pro application-usage fallback once on AGOL.
8. Resolve custom role names.
9. Build one row per selected user.
10. Write the CSV and email the report and log.

The Pro last-used resolution order is:

```text
Sessions endpoint
    -> AGOL application-usage fallback
    -> userEntitlements.lastLogin
```

The report records which source supplied the value.

With `--assigned-only`, users without entitlement codes are omitted.

A run can complete with exit code `0` while being labeled `INCOMPLETE` because supporting lookups failed. Review the completion email and log before interpreting missing values.

### ESRI_LICENSE_REPORT.py

1. Parse arguments.
2. Locate and import `UTILITIES`.
3. Initialize logging.
4. Resolve and validate input paths and the destination.
5. Detect input encoding and delimiter.
6. Validate headers and rows.
7. Normalize emails and username prefixes.
8. Consolidate duplicate records within each file/platform source.
9. Assign source prefixes and log their mappings.
10. Calculate email-wide account counts.
11. Group accounts by email and username prefix.
12. Merge safe cross-instance matches.
13. Keep unmatched and ambiguous accounts separate.
14. Select one highest-ranked user type per output row.
15. Verify every distinct source account is represented exactly once.
16. Write the CSV through a temporary file.
17. Log account totals, match statistics, missing-type warnings, and counts by `license_reported`.

Recognized headers are trimmed and matched case-insensitively.

The reader tries UTF-8, including a byte-order mark, then Windows-1252. It detects comma, semicolon, or tab delimiters.

Extra columns are ignored. Alternate field names are not automatically translated: `full_name` is not an alias for `fullName`, and `user_level` is not an alias for `userLicenseTypeId`.

## Test mode (default)

### NAMED_USER_LICENSE_INVENTORY.py

Test mode is enabled by default.

In test mode:

- The email subject contains `[TEST]`.
- The inventory is capped at 50 users unless `--max-users` overrides that cap.

Use `--no-test` to remove the test tag and default cap.

An explicit `--max-users N` takes precedence. Values of zero or less remove the cap.

### ESRI_LICENSE_REPORT.py

There is no test mode, production switch, or user cap.

The script processes all records in the supplied exports.

For a small validation run, use representative CSVs containing known cases such as:

- A safe cross-platform match.
- Different user types for a matched group.
- An unmatched account.
- An ambiguous prefix group.
- Blank user-type values.

The script does not modify live systems.

## Usage

### NAMED_USER_LICENSE_INVENTORY.py

#### Scenario 1 — Initial connectivity check

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal
```

Expect up to 50 users under the default test cap and a `[TEST]` completion email.

#### Scenario 2 — Full organization inventory

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test
```

Expect all enumerated users unless an explicit cap is supplied.

#### Scenario 3 — Only accounts with Pro entitlements

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test --assigned-only
```

Expect users without entitlement codes to be omitted.

#### Scenario 4 — Select the AGOL fallback usage window

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test \
    --app-usage-window year
```

The organization-wide fallback report is fetched once, not once per user.

#### Scenario 5 — Explicit user cap

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --max-users 500
```

The explicit cap overrides the default test-mode cap.

#### Scenario 6 — Operating-system certificate trust

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal --no-test \
    --tls-mode os-trust-store
```

Certificate validation remains enabled.

#### Scenario 7 — Custom CA bundle

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal --no-test \
    --tls-mode custom-ca-bundle --ca-bundle-path /path/to/ca.pem
```

#### Scenario 8 — Reduce concurrency

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test --workers 6
```

Lower concurrency reduces simultaneous endpoint activity but can increase runtime.

#### Scenario 9 — Show help

```bash
python NAMED_USER_LICENSE_INVENTORY.py --help
```

### ESRI_LICENSE_REPORT.py

Run from the folder containing the script and replace example paths with actual export paths.

#### Scenario 1 — Compare Portal and AGOL

```bash
python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv" --agol "C:\temp\agol_users.csv"
```

Expect source assignments side by side, safe matches combined, and one highest type per reconciled row.

#### Scenario 2 — Analyze one Portal report

```bash
python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv"
```

#### Scenario 3 — Analyze one AGOL report

```bash
python ESRI_LICENSE_REPORT.py --agol "C:\temp\agol_users.csv"
```

A single-source run still reports account counts and selected user types, but cannot perform cross-source matching.

#### Scenario 4 — Compare multiple Portal reports

```bash
python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" "C:\temp\portal_2.csv"
```

Expect numbered Portal prefixes and safe same-platform matches.

#### Scenario 5 — Compare multiple AGOL reports

```bash
python ESRI_LICENSE_REPORT.py --agol "C:\temp\agol_1.csv" "C:\temp\agol_2.csv"
```

#### Scenario 6 — Compare multiple reports from both platforms

```bash
python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" "C:\temp\portal_2.csv" --agol "C:\temp\agol_1.csv" "C:\temp\agol_2.csv"
```

Expect numbered prefixes for both platforms. The log identifies which file each prefix represents.

#### Scenario 7 — Detect platforms automatically

```bash
python ESRI_LICENSE_REPORT.py --input "C:\temp\mixed_users.csv" "C:\temp\other_users.csv"
```

Every row must still contain a valid platform value.

Header-only files must use `--agol` or `--portal`, because their platform cannot be inferred.

#### Scenario 8 — Repeat flags and choose an output folder

```bash
python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" --portal "C:\temp\portal_2.csv" --agol "C:\temp\agol_users.csv" --output-dir "C:\temp\license_reports"
```

Each flag accepts multiple files and may be repeated. Supply each file only once.

#### Scenario 9 — Allow replacement of a same-minute output

```bash
python ESRI_LICENSE_REPORT.py --agol "C:\temp\agol_users.csv" --overwrite
```

Without `--overwrite`, an existing generated destination causes the run to stop.

#### Scenario 10 — Show help

```bash
python ESRI_LICENSE_REPORT.py --help
```

## Command-line options

### NAMED_USER_LICENSE_INVENTORY.py

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | Required | Organization-specific TOML file. |
| `--target` | Required | `portal` or `agol`. |
| `--no-test` | Test mode on | Remove the test tag and default 50-user cap. |
| `--max-users N` | `None` | Explicit user cap; zero or less removes the cap. |
| `--assigned-only` | Off | Include only users with entitlement codes. |
| `--app-usage-window` | `year` | AGOL fallback window: `week`, `14days`, `30days`, `60days`, `90days`, `6months`, or `year`. |
| `--workers N` | `12` | Concurrent entitlement/session workers; range `1–64`. |
| `--trusted-service-origin HTTPS_ORIGIN` | None | Repeatable trusted service origin, as documented for the inventory tool. |
| `--tls-mode` | `standard` | `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | `None` | PEM bundle required for custom-bundle mode. |
| `--portal-url` | From config | Portal or AGOL URL. |
| `--username` | From config | Login username. |
| `--keyring-service` | From config | Password keyring service. |
| `--smtp-server` | From config | Completion-email relay. |
| `--email-to` | From config | Recipient or comma-separated recipients. |
| `--email-from` | From config | Sender address. |
| `--output-dir` | From config | CSV and log destination. |

### ESRI_LICENSE_REPORT.py

| Option | Default | Meaning |
| --- | --- | --- |
| `--agol CSV [CSV ...]` | None | One or more AGOL-only exports; repeatable. |
| `--portal CSV [CSV ...]` | None | One or more Portal-only exports; repeatable. |
| `--input CSV [CSV ...]` | None | Automatically detected or mixed-platform exports; repeatable. |
| `--output-dir PATH` | `C:\temp` | Destination folder. |
| `--overwrite` | Off | Permit replacement of an existing timestamped output. |
| `-h`, `--help` | — | Print help without analysis. |

At least one input flag is required.

Explicit platform flags validate all rows against the specified platform. Use `--input` for mixed-platform files.

## Performance

### NAMED_USER_LICENSE_INVENTORY.py

- User enumeration is paged, with 100 users per page.
- Enumeration respects the resolved user cap.
- Entitlements and sessions are fetched in parallel.
- The default worker count is 12.
- The AGOL application-usage fallback is fetched once per run.
- Custom-role information is resolved once.
- Worker sessions are pre-created and borrowed one task at a time.
- REST responses are streamed and bounded.

Increasing concurrency can reduce runtime when the service has capacity, but excessive concurrency can cause throttling.

### ESRI_LICENSE_REPORT.py

Processing is local and sequential. There are no network calls or worker-thread settings.

Runtime and memory use depend on:

- Input-file sizes and row counts.
- Number of distinct source accounts.
- Number of source instances.
- Number of output rows.
- Sorting and duplicate-value consolidation.
- Number of dynamically generated source columns.

Encoding is checked before CSV parsing. Accounts, matching groups, counts, and output rows are held in memory.

Each output row contains fields for every supplied source instance, so a large number of inputs also increases report width and memory requirements.

There is no explicit input-size or memory limit.

## Observed Runtime

### NAMED_USER_LICENSE_INVENTORY.py

The existing documentation records these approximate observations:

| Platform | Observed duration | Organization users |
| --- | --- | ---: |
| Portal | Approximately 3–5 minutes | Approximately 5,000 |
| AGOL | Approximately 3–10 minutes | Approximately 6,000 |

These are observations, not guarantees. Organization size, endpoint responsiveness, lookup completeness, and concurrency affect runtime.

### ESRI_LICENSE_REPORT.py

No measured runtime was supplied for this script.

The inventory script's Portal and AGOL timings do not apply because this script processes local CSVs rather than querying organization endpoints.

Benchmark representative exports in the intended environment instead of assigning an unsupported runtime estimate.

## Output file

### NAMED_USER_LICENSE_INVENTORY.py

The documented output names are:

```text
[ORG]_[PLATFORM]_NAMED_USER_LICENSE_INVENTORY_<ts>.csv
[ORG]_NAMED_USER_LICENSE_INVENTORY_<ts>.log
```

The timestamp uses:

```text
YYYYMMDD_HHMM
```

The CSV contains one row per selected organization user, or one row per user with entitlement codes when `--assigned-only` is used.

The script writes one main CSV and a log, not a separate summary CSV.

The CSV uses the reserved run timestamp. If the log filename already exists, a numeric suffix is added and shared with the CSV to avoid an accidental overwrite.

The report and log are emailed under the shared attachment-size policy.

### ESRI_LICENSE_REPORT.py

The output name is:

```text
ESRI_LICENSE_REPORT_YYYYMMDD_HHMM.csv
```

It is written under `--output-dir`, defaulting to:

```text
C:\temp
```

The timestamp comes from the local system clock and has minute precision.

There is no automatic numeric collision suffix. If the generated destination exists:

- Without `--overwrite`, the run stops.
- With `--overwrite`, replacement is permitted after successful temporary-file writing.

The CSV contains:

- One row per safely reconciled account group; or
- One row per retained unmatched or ambiguous account.

It is not necessarily one row per email or person.

Field order is:

1. `email`
2. `username_base`
3. `platform`
4. `source_instances`
5. Each source's username, display-name, and type fields
6. `license_reported`
7. Platform-wide account counts
8. Per-instance account counts, where not already represented
9. Multiple-account flags
10. `cross_platform_match`

Groups follow sorted email/prefix order. Accounts retained separately within a group follow source order and username order.

A valid empty result produces a header-only CSV.

Logging is delegated to:

```python
setup_logging(None, "ESRI_LICENSE_REPORT")
```

Log naming and placement follow that shared helper. The log records source mappings, totals, matching results, warnings, and selected-type counts.

There is no separate summary CSV and no completion email.

## Exit codes

### NAMED_USER_LICENSE_INVENTORY.py

| Code | Meaning |
| ---: | --- |
| `0` | Normal completion path after CSV writing and reaching the reporting/email stage. The report may still be labeled `INCOMPLETE`. |
| `1` | Aborted run, including connection, TLS, output, or unrecovered processing failures. |
| `2` | Argument-validation failure, such as custom-bundle mode without a required bundle path. |

Exit code `0` does not mean every supporting lookup succeeded. Review the completion status and log for missing stages.

### ESRI_LICENSE_REPORT.py

| Code | Meaning |
| ---: | --- |
| `0` | Analysis completed and the CSV was written, including a valid header-only result. Successful `--help` also returns normally without analysis. |
| `1` | Input validation or handled analysis/output failure, such as an unsupported type, repeated input, missing file, reconciliation failure, or output collision. |
| `2` | Argument-parser failure, including missing input flags, invalid options, or explicit parser-error paths for locating/importing `UTILITIES`. |

Validation occurs before the final report write.

On handled failures, the script attempts to remove temporary files and any newly reserved destination. With `--overwrite`, the existing destination is replaced only after successful temporary CSV writing.

## Notes and caveats

### NAMED_USER_LICENSE_INVENTORY.py

- The application-usage fallback is AGOL-only.
- Portal relies on the sessions endpoint and entitlement `lastLogin` fallback.
- Website login is not proof of Pro use.
- Failure to find the Pro listing item can leave entitlement details unavailable.
- Unrecognized entitlement codes are preserved.
- Custom-role lookup failures can leave raw role tokens.
- Missing supporting data can result in an `INCOMPLETE` report even with exit code `0`.
- The shared email attachment cap defaults to 20 MB when no configured value is supplied.
- Results are a snapshot of the organization during the run.
- Verify findings before reclaiming entitlements.

### ESRI_LICENSE_REPORT.py

- The purpose is consolidated user-type reporting across supplied exports, not live-system verification.
- The highest-ranked type is selected once per output row to avoid counting a safely matched group in multiple type categories.
- This reporting rule does not establish purchased-license requirements, entitlement portability, or permission to remove other assignments.
- Separate username prefixes and ambiguous matches can leave multiple rows for one email.
- A source instance is a file/platform combination, not an automatically discovered organization or server.
- Repeated snapshots are separate sources. The script does not select the newest export or perform date-aware change detection.
- Source-account counts include occurrences across exports; selected-type totals count output rows.
- Email-wide counts and multiple-account flags repeat on all rows sharing that email.
- Do not sum repeated email-wide count columns down the report.
- Matching requires both normalized email and username prefix.
- Display names are retained for review but are not matching evidence.
- Different emails or prefixes are not automatically repaired or inferred.
- Blank email, blank username, or blank username prefix causes validation failure.
- Blank type IDs remain missing information.
- Unsupported nonblank type IDs stop validation.
- Required headers must be present even when type values are blank.
- Known header names are matched case-insensitively; alternate names are not translated.
- Header-only inputs require `--agol` or `--portal`.
- Supply each input file only once.
- The script does not report Pro usage dates, extensions, offline checkouts, billing calculations, or automated reclamation decisions.
- No accounts, assignments, or live-system settings are modified.
