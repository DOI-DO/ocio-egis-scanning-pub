# ACCOUNT_ACTIVITY.py

## Table of Contents

- [For Report Readers](#for-report-readers)
  - [Overview](#overview)
  - [Why Inventory Account Actions](#why-inventory-account-actions)
  - [Why It's a Problem](#why-its-a-problem)
  - [Why Removal Requires Caution](#why-removal-requires-caution)
  - [Recommendations for Investigation](#recommendations-for-investigation)
  - [Column Descriptions](#column-descriptions)
- [For Administrators](#for-administrators)
  - [Dependencies](#dependencies)
  - [Requirements](#requirements)
  - [Security](#security)
  - [Configuration](#configuration)
  - [How It Works](#how-it-works)
  - [Test Mode](#test-mode)
  - [Usage](#usage)
  - [Command-Line Options](#command-line-options)
  - [Performance](#performance)
  - [Observed Runtime](#observed-runtime)
  - [Output Files](#output-files)
  - [Exit Codes](#exit-codes)
  - [Notes and Caveats](#notes-and-caveats)

---

## For Report Readers

### Overview

ACCOUNT_ACTIVITY.py generates audit reports of user account changes in ArcGIS Online (AGOL) or ArcGIS Enterprise Portal. It captures who made a change, what changed, when it happened, and produces a CSV file that documents these events in a format suitable for review, compliance verification, and investigation.

The script runs against two different systems depending on which you're auditing:
- **AGOL mode** reconstructs before/after snapshots of role and license changes by comparing user-report CSVs taken at different times.
- **Portal mode** scans Portal log files to find user account creation, deletion, role changes, and enable/disable events, then looks up the human-readable names of any custom roles.

### Why Inventory Account Actions

User account changes are sensitive operations that should follow your organization's change management procedures. Keeping an auditable record of who did what, when, and why is essential for several reasons:

**Compliance and Governance**: Many regulations (such as those in federal agencies) require documented evidence that account changes were authorized and followed established procedures. An audit trail proves you have a controlled account management process.

**Security and Risk Management**: Unauthorized account changes are a common attack vector. An attacker with credentials might create new administrator accounts, disable legitimate users, or escalate privileges. By regularly reviewing account-change logs, you can detect suspicious activity that might indicate a breach or compromised account.

**Change Management Verification**: Your organization likely has a change control process that requires approval before administrative actions. This report lets you verify that actual account changes match the approved change requests—if a change happened that wasn't approved, you've found a gap in your process.

**Troubleshooting and Accountability**: When users report problems with their accounts or access levels, the audit trail shows exactly when and how their role changed, helping you trace the root cause and confirm the change was made intentionally.

### Why It's a Problem

Account changes are permanent or very difficult to reverse. If someone grants themselves elevated privileges or disables a critical account, the damage happens immediately. Unlike some other system changes that can be undone with a rollback, account state changes often have real-world consequences:

- A user locked out of their account loses access to critical information and services.
- An improperly granted administrator role gives someone power they shouldn't have.
- A user's license type changed by mistake can affect their ability to use the system or cost your organization extra money.
- A user deleted accidentally requires recovery procedures that may not restore all associated data.

Because these changes happen at the human/business level (not just the system), they often require investigation and sometimes manual correction.

### Why Removal Requires Caution

Before you delete or reverse an account change, understand the impact:

- **Was it authorized?** Check the change request or email approval that authorized it. If you can't find authorization, it may have been unauthorized.
- **Is there a business reason?** Someone might have changed a user's role because the person's job changed. Reversing it without understanding the context could break their workflow.
- **Who was affected?** If you reverse a role change, that user loses the permissions they were given. Other team members might depend on the user having those permissions.
- **Does it conflict with other changes?** Sometimes multiple changes happen in sequence. Reversing one might undo the combined effect that was intended.

Always investigate what led to the change, who requested it, and what business outcome was expected before reversing it.

### Recommendations for Investigation

When you find an account change in the report:

1. **Check the date and time**: Compare it to your change management system. Is there an approved change request from around that date?

2. **Identify the actor**: The "actor" column shows who made the change. Is that person someone who should have permission to make account changes? (Typically, portal administrators or IT staff.)

3. **Verify the change matches the request**: If there's an approved request to change a user's role from "Viewer" to "Editor," verify that's exactly what the report shows. If the report shows a different change, investigate why.

4. **Check for patterns**: If the same person made multiple changes in a short period, that might be normal (e.g., an IT team provisioning new users). If unusual names or times appear, investigate further.

5. **Look for reversals**: If a user was granted a role and then immediately had it removed (or vice versa), this might indicate a test, a mistake that was corrected, or someone experimenting with permissions.

6. **Cross-reference with incidents**: If you've had a security incident or user complaint, check the date. Account changes around that time might be related.

7. **Ask the actor**: If a change seems suspicious, contact the person who made it. They can explain the business reason and point you to the formal request.

### Column Descriptions

The CSV output contains the following columns:

| Column | Description |
|--------|-------------|
| **change_type** | The type of account change: `create` (new user added), `delete` (user removed), `role` (role/permission level changed), `license` (license type changed), `update` (user profile updated by someone other than the user), `disable` (user account locked), or `enable` (user account unlocked). |
| **username** | The username or account identifier of the user whose account was changed. |
| **actor** | The username of the person who made the change. This is typically a portal administrator or IT staff member. For role/license changes in AGOL, this is the person who clicked the button. For Portal log events, this is recorded in the audit log. |
| **action_date** | The date the change occurred (MM/DD/YYYY format for Portal logs, YYYY/MM/DD format for AGOL reports). Time-of-day is not included. |
| **summary** | A human-readable sentence describing the change, e.g., "jsmith@example.com changed the role of ajones from Viewer to Editor on 09/15/2026" or "bwilson disabled by admin@example.com on 2026-09-15". |
| **ad_ou** | (Optional, only if --ad-check was used) The Active Directory organizational unit (OU) where the user's account resides in the company directory, such as "OU=GIS Users,DC=example,DC=com". This helps identify the user's department or division. Empty if the user was not found in Active Directory or if AD lookup was not run. |

---

## For Administrators

### Dependencies

This script depends on the following files from the shared UTILITIES package:

- `restutils.py` — REST API helpers, HTTP session management, platform detection
- `logging_setup.py` — Shared console/file logging with credential redaction
- `gis_connect.py` — Hardened ArcGIS connection with TLS verification
- `secure_session.py` — Explicit TLS verification strategy (standard CA, OS trust store, or custom CA bundle)
- `ad_resolver.py` — Optional forest-wide Active Directory lookup via ADSI/pywin32
- `cli.py` — Shared CLI argument parsing and config file integration
- `config.py` — Multi-org TOML configuration file loader
- `emailer.py` — SMTP email sender with attachment size policy
- `auth.py` — Credential/token wrapping and sensitive-data redaction

### Requirements

**Minimal**:
- Python 3.7+
- ArcGIS Python SDK (arcgis package)
- keyring package (for credential storage)
- For Portal mode: Active Portal deployment with accessible log files or log file access via UNC share
- For AGOL mode: AGOL user-report CSVs and activity-report CSVs
- Email/SMTP configuration in the org's config.toml file

**Optional**:
- For --ad-check: pywin32 package, Windows platform, access to a domain controller
- For --tls-mode os-trust-store: truststore package
- For --tls-mode custom-ca-bundle: a PEM-formatted CA bundle file on disk

### Security

**Certificate (TLS) Validation**: The script uses an explicit `--tls-mode` parameter to choose a TLS verification strategy: `standard` (certifi's bundled public CA list, suitable for AGOL and public CAs), `os-trust-store` (the operating system's native certificate store, required for internal Portal deployments with organizational CAs), or `custom-ca-bundle` (a specific PEM CA bundle file for non-standard CAs). Before any ArcGIS login attempt, a one-time `verify_tls_handshake()` check is run to prove the server's certificate is valid, and the same `verify_mode` is passed into `connect_gis()` so the SDK's own connection pool is covered as well. Mismatched or invalid certificates cause the script to abort immediately rather than proceeding with an unverified connection.

**SSRF/Network-Boundary Defenses**: The portal URL is validated as an absolute HTTPS URL with a resolvable hostname, no embedded credentials, no query string or fragment, and no whitespace or control characters. On Portal mode, the configured portal URL is cross-checked against the `--target` parameter; if the URL classifies as AGOL but the target is `portal`, the script refuses to proceed. This prevents accidental scanning of the wrong environment.

**Large/Malformed Responses**: CSV files larger than 512 MB are refused outright; individual log files larger than 1 GB are skipped; individual log lines longer than 4 MB are skipped; CSV row count is capped at 50 million. JSON parsing is defensive and never trusts the shape of the input—missing fields default to empty strings or safe defaults.

**Token Handling**: Every caught exception's text is passed through `redact_token()`, which strips token=, sig=, and other credential-shaped parameters from exception messages, log output, and email bodies. This prevents tokens from accidentally appearing in logs or error reports.

**Python Import-Path / Package Shadowing**: The script removes any equivalent copies of the project root from `sys.path`, inserts the expected root at the *front* of `sys.path` so the genuine sibling UTILITIES package always wins over any same-named module elsewhere, imports UTILITIES, and then verifies that the loaded package actually came from the expected directory. This prevents a malicious or accidentally-present `UTILITIES` package elsewhere on the path from being loaded instead.

**Redirect Following**: Redirect following is explicitly disabled on the script's own REST session. This prevents a 3xx response from bouncing a verified request to an unintended (possibly internal or attacker-controlled) host.

### Configuration

Create or use an existing `--config` org TOML file with these required sections:

```toml
[common]
org-name = "ORG1"
smtp-server = "mail.example.com"
email-to = "audit@example.com"
email-from = "reports@example.com"

[portal]
portal-url = "https://gis.example.com/portal"
username = "admin@example.com"
keyring-service = "PORTAL_CREDS"

[agol]
portal-url = "https://OTHER_ORGldt.maps.arcgis.com"
username = "agol_admin@OTHER_ORGldt.maps.arcgis.com"
keyring-service = "AGOL_CREDS"
```

The script uses `--target` to select which section (`[portal]` or `[agol]`) to read. Passwords are stored in the system's credential manager (keyring) under the configured service name, never in the TOML file. See UTILITIES/config.py for full details on multi-org configuration.

### How It Works

**AGOL Mode** (`--target agol`):
1. Loads every AGOL user-report CSV in `--reports-dir` that has "AGOL" in its filename and carries a parseable YYYYMMDD[_HHMM] date token.
2. For each activity action (role change, license change, user enable/disable/delete/update), finds the closest user-report snapshots before and after the action.
3. Diffs the before/after snapshots to show what changed (e.g., "role changed from Viewer to Editor").
4. Optionally looks up each user in Active Directory to populate the `ad_ou` column.
5. Produces a CSV with columns: `change_type, username, actor, action_date, summary, ad_ou`.

**Portal Mode** (`--target portal`):
1. Connects to the Portal via the hardened TLS-verified `connect_gis()` method.
2. Fetches the live Portal role list to build a roleId → roleName lookup (so custom role IDs are translated to readable names).
3. Scans Portal log files in one or two directories (for HA deployments) for DELETE_USER, UPDATE_USER (role changes), and CREATE_USER events.
4. Optionally recurses into subdirectories and filters by file extension (default .log and .txt).
5. De-duplicates identical events within a single directory (to collapse rolled log files) and across two directories (for HA deployments).
6. Optionally looks up each user in Active Directory.
7. Produces the same CSV output.

Both modes email the CSV and log file to the configured recipients.

### Test Mode

There is no explicit test mode flag; the script runs in full mode every time. However, you can perform small-scale testing by:
- Supplying a small subset of user-report CSVs or log directories.
- Using a limited date range in the activity report.
- Running against a test Portal or AGOL environment.

### Usage

**AGOL Mode**:
```bash
python ACCOUNT_ACTIVITY.py --config org1.config.toml --target agol \
    --activity OrganizationActivityDaily_20260930.csv \
    --reports-dir ./user_reports
```

**Portal Mode (single node)**:
```bash
python ACCOUNT_ACTIVITY.py --config org1.config.toml --target portal \
    --logs-dir ./logs
```

**Portal Mode (HA pair, de-duplicated)**:
```bash
python ACCOUNT_ACTIVITY.py --config org1.config.toml --target portal \
    --logs-dir \\node1\arcgisportal\logs \\node2\arcgisportal\logs \
    --recursive
```

**With Active Directory lookup**:
```bash
python ACCOUNT_ACTIVITY.py --config org1.config.toml --target agol \
    --activity OrganizationActivityDaily_20260930.csv \
    --reports-dir ./user_reports \
    --ad-check --dc dc01.example.com
```

### Command-Line Options

**Required**:
- `--config ORG_TOML_PATH` — Path to the org-specific config TOML file.
- `--target {portal,agol}` — Which environment to audit.

**AGOL mode only**:
- `--activity ACTIVITY_CSV` — Path to the AGOL Activity report CSV.
- `--reports-dir DIR` — Folder containing AGOL user-report CSVs (at least two required).

**Portal mode only**:
- `--logs-dir DIR [DIR ...]` — One or two Portal log directories.
- `--recursive` — Recurse into subdirectories (default: top level only).
- `--ext EXT` — File extension to scan (repeatable; default: .log and .txt).

**Optional (both modes)**:
- `--portal-url URL` — Override the config-file portal URL (Portal mode only).
- `--username USERNAME` — Override the config-file username (Portal mode only).
- `--keyring-service SERVICE` — Override the config-file keyring service (Portal mode only).
- `--output-dir DIR` — Directory for output CSV and log (default: script directory).
- `--smtp-server SERVER` — Override the config-file SMTP server.
- `--email-to ADDR` — Override the config-file recipient (single or comma-separated).
- `--email-from ADDR` — Override the config-file sender.
- `--tls-mode {standard,os-trust-store,custom-ca-bundle}` — TLS verification strategy (default: standard).
- `--ca-bundle-path PATH` — Path to a PEM CA bundle (required if --tls-mode is custom-ca-bundle).
- `--ad-check` — Enable forest-wide Active Directory lookup.
- `--dc DOMAIN_CONTROLLER` — Domain controller hostname/FQDN (required if --ad-check is used).
- `--ad-debug` — Emit Active Directory lookup diagnostics.

### Performance

**AGOL Mode**:
- Speed is limited by CSV file I/O and user-report file count. Parsing 10 activity reports against 5 user-report snapshots typically completes in seconds to a few minutes depending on report size and disk speed.

**Portal Mode**:
- Speed is limited by Portal login time (1–5 seconds depending on network latency and Portal load), role-list fetch (typically < 1 second for < 100 custom roles), and log file scanning (proportional to file count and size). Scanning a single node with 10 log files of 100 MB each typically takes 1–2 minutes.

**Active Directory Lookup**:
- Each user lookup is a forest-wide Global Catalog search via ADSI. Expect 100–500 ms per user depending on forest size and domain controller response time. For a report with 100 unique users, add 10–50 seconds if --ad-check is enabled.

### Observed Runtime

- **AGOL, small report (< 100 events)**: 5–10 seconds
- **AGOL, large report (1000 events) with AD check**: 1–2 minutes
- **Portal, single node, 5 log files**: 30–45 seconds
- **Portal, HA pair (two nodes), large log set, with AD check**: 2–5 minutes

### Output Files

Two files are produced in `--output-dir` (or the script directory by default):

1. **`AGOL_ACCOUNT_ACTIVITY_<timestamp>.csv`** or **`PORTAL_ACCOUNT_ACTIVITY_<timestamp>.csv`** — The audit report containing columns: `change_type, username, actor, action_date, summary, ad_ou`. Rows are sorted by date and change type for readability.

2. **`AGOL_ACCOUNT_ACTIVITY_<timestamp>.log`** or **`PORTAL_ACCOUNT_ACTIVITY_<timestamp>.log`** — The detailed run log showing all parsing, matching, and AD lookup activity.

Both files are emailed to the configured recipients.

### Exit Codes

- **0** — Success. At least one event was found and reported, or zero events were found (both are normal outcomes).
- **1** — Run aborted due to a caught error (e.g., missing config file, bad credentials, no user reports found, log directory not found). Check the attached log file for the specific error.
- **2** — Configuration error (e.g., missing required config keys, invalid URL format, bad TLS mode). Fix the config and retry.

### Notes and Caveats

1. **AGOL user-report naming**: Only CSV files with "AGOL" (case-insensitive) in the filename and a parseable YYYYMMDD[_HHMM] date token are loaded. Other files in the directory are ignored. Two reports are required as a minimum to compute before/after deltas.

2. **Portal role-name translation**: The script fetches the live Portal role list at run time. If a role was custom and has since been deleted, the report will show "(unresolved)" next to the role ID. Built-in role keywords (e.g., "org_admin") are always translated to their display names.

3. **HA Portal de-duplication**: Events logged identically by both nodes in an HA pair are collapsed into a single row. The comparison is exact: same `change_type`, `username`, `actor`, `action_date`, and `summary`. If the nodes log slightly differently (e.g., different timestamps), the event appears twice.

4. **Active Directory availability**: AD lookup requires the `pywin32` package and Windows ADSI access to a domain controller. If the script is run from a non-Windows system or without --ad-check, the `ad_ou` column is empty. If a user is not found in AD, the `ad_ou` column is empty for that row.

5. **Email delivery**: If email delivery fails, the script logs the error and exits with code 1. Check SMTP server connectivity and credentials in the config file.

6. **Large reports**: If the activity or user-report CSV exceeds 512 MB, it is refused. If a Portal log file exceeds 1 GB, it is skipped. If a single log line exceeds 4 MB, it is skipped. These limits are defensive; logs should never approach these sizes in normal operation.

7. **Timestamp format**: AGOL activity reports use YYYY/MM/DD format; Portal log files use MM/DD/YYYY format. The script handles both; output is sorted correctly regardless of source.

8. **Self-updates in AGOL**: If a user updates their own profile via the AGOL UI, the update is skipped (actor == username). Only updates by someone other than the user are reported.

9. **Role-change detection in Portal**: Only messages matching the exact regex pattern "Role for user <name> has been changed from '<old>' to '<new>'" are detected. Variations in message format may not be caught.

10. **No partial reports**: If the run fails partway through, no CSV is written. The email notification will indicate failure and provide the error from the log file.