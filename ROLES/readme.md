# PORTAL_ROLES

Generate a privilege matrix for all custom roles in ArcGIS Enterprise Portal or ArcGIS Online (AGOL). The script produces an Excel workbook showing, at a glance, exactly which privileges each custom role grants.

## Table of contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Roles](#why-inventory-roles)
- [Why it's a problem](#why-its-a-problem)
- [Why removal requires caution](#why-removal-requires-caution)
- [Recommendations for investigation](#recommendations-for-investigation)
- [Column descriptions](#column-descriptions)

### For Administrators
- [Dependencies on the shared UTILITIES package](#dependencies-on-the-shared-utilities-package)
- [Requirements](#requirements)
- [Security: certificate (TLS) validation](#security-certificate-tls-validation)
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

This report is a simple grid. Down the left side is a list of privileges — the individual permissions that control what a person can do in the organization, such as publishing a map, sharing content, or deleting other people's accounts. Across the top are the names of the custom roles in your organization. Wherever a role grants a privilege, the grid shows a checkmark.

This is a privilege inventory for custom roles, not a duplicate-layer inventory. The requested duplicate-layer heading is retained for consistency with the documentation structure used for the surrounding inventory tools.

Reading the report is straightforward: pick a role's column and scan down it to see everything that role can do, or pick a privilege's row and scan across it to see which roles grant that privilege. Roles are arranged from left to right by how many privileges they grant, so the least-powerful roles appear on the left and the most-powerful roles on the right.

The report only includes **custom roles** — the roles your organization created. The standard built-in roles that come with the software (Viewer, Data Editor, and Facilitator) are left out so the report stays focused on the roles your administrators are responsible for maintaining.

## Why Inventory Roles

Over time, organizations accumulate custom roles. Someone creates a role for a specific project, a team requests a variation of an existing role, or a role is cloned and lightly tweaked. Months or years later, nobody remembers exactly what each role grants or why it exists. This report makes the invisible visible: instead of clicking through each role one at a time in the administrative console, you get every role's full set of privileges on a single page you can review, print, or share.

The reason roles specifically need to be inventoried is that a role is the mechanism that turns a short list of privilege codes into real power held by real people, and that power is invisible from everywhere it matters. A role's name is chosen to describe a job function, not its contents, so "Field Editor" or "Program Lead" gives no hint of the specific permissions packed inside it — and the permissions themselves are machine-readable codes buried in the role definition, not something anyone sees while assigning the role or OTHER_ORGng daily work. The result is that what a role actually grants and what people assume it grants can drift far apart without anyone noticing. An inventory is the only practical way to close that gap: it reads every role's true privilege set and lays it out in a form a human can actually reason about, so the organization is deciding based on what the roles really do rather than on what their names imply.

This particular scan is worth running because the matrix format answers questions that are effectively impossible to answer by inspecting roles one at a time. Although the surrounding project documentation often uses inventory language associated with duplicate layers, this script inventories role definitions and their privileges so administrators can see repeated, overlapping, or unexpectedly broad permission sets. Put every role side by side in one grid and the patterns jump out: two roles that are nearly identical and should probably be merged, a role that quietly carries an administrative privilege nothing about its name suggests, or a privilege that is granted far more widely across roles than anyone intended. Reading down a column shows everything a role can do; reading across a row shows every role that grants a given privilege — the second view in particular is what lets you answer "who can delete accounts?" or "which roles can change other members' roles?" at a glance, which is exactly the kind of question an audit or a security review asks and exactly the kind that clicking through roles individually answers slowly and unreliably.

Having this inventory in one place supports routine security reviews, helps answer audit questions quickly, and makes it far easier to design new roles that follow the principle of least privilege — granting people only what they need to do their jobs and nothing more. It is worth producing on a schedule rather than only in a crisis because role definitions change as projects come and go and as roles are cloned and tweaked, so over-provisioning accrues gradually and silently; a periodic matrix turns that slow drift into something visible and comparable from one review to the next. Because the script only reads role definitions and writes a workbook — it never alters a role, a privilege, or a member assignment — it is completely safe to run as often as a governance program wants, producing an authoritative, shareable snapshot of exactly who can do what without any risk to the live environment.

## Why it's a problem

Roles that grant more than they should are a genuine security concern. If a role quietly includes powerful administrative privileges — the ability to delete accounts, change other members' roles, or manage organization-wide settings — then everyone assigned that role holds those powers, whether or not they ever need them. That is a larger attack surface: if any one of those accounts is compromised, the damage they can do is as broad as the privileges they were granted.

The problem is easy to overlook because privileges are not visible day to day. A role named after a job function gives no hint of the specific permissions hiding inside it. Without a report like this, over-provisioned roles tend to go unnoticed until an audit or an incident forces the question.

## Why removal requires caution

It is tempting to look at a role with many privileges and start trimming, but privileges cannot be removed casually. People are actively relying on them. Removing a privilege from a role instantly affects every member assigned that role, and the effects may not be obvious right away — a scheduled process might fail, a workflow someone depends on might break, or a person might suddenly be unable to do part of their job.

Some privileges also work together. Removing one without understanding how it interacts with others can produce confusing partial failures rather than a clean loss of a single capability. The safe approach is to treat this report as the starting point for a conversation, not as a to-do list of things to delete.

## Recommendations for investigation

When a role looks over-provisioned, work through it deliberately:

- **Confirm who is affected.** Find out how many members hold the role and who they are before considering any change.
- **Ask what the role is actually used for.** Talk to the team or owner who requested it. The intended purpose often explains privileges that look unnecessary at first glance.
- **Compare similar roles.** If two roles do nearly the same thing, the report makes the differences easy to spot, and consolidating them may be safer than editing either one.
- **Change in a test setting first.** Where possible, try privilege changes in a non-production environment or on a small pilot group before applying them broadly.
- **Change one thing at a time.** Adjust a single privilege, confirm nothing broke, then move on. This makes it far easier to identify the cause if something does go wrong.

## Column descriptions

The output workbook has one sheet, named **MATRIX**, laid out as follows.

| Location | Contents |
| --- | --- |
| **Column C** (privilege labels) | The plain-language name of each privilege, one per row. These labels are defined in the template and are never changed by the script. |
| **Row 1, from column D onward** (role names) | The name of each custom role, one per column. Roles are ordered by privilege count, fewest on the left and most on the right. |
| **Grid cells** | A checkmark (✓) where a role grants the privilege in that row. An empty cell means the role does not grant it. |

---

# For Administrators

## Dependencies on the shared UTILITIES package

- `cli.py`
- `config.py`
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `secure_session.py`
- `auth.py`

## Requirements

- Python 3 with the `openpyxl`, `requests`, and `arcgis` packages installed.
- The `truststore` package only if you use `--tls-mode os-trust-store`.
- The shared `UTILITIES` package present as a sibling directory of this script.
- `ROLES_TEMPLATE.xlsx` present in the same directory as the script, containing a sheet named `MATRIX` with privilege labels in column C starting at row 2.
- A configuration file and a credential stored in the OS keyring (see [Configuration](#configuration)).

## Security: certificate (TLS) validation

All network traffic is encrypted over HTTPS, and the identity of the server is verified before any data is exchanged.

Two independent connections are secured. The arcgis SDK connection, established through `connect_gis()`, **always** verifies the server's TLS certificate — there is no option to disable it. Separately, this script makes its own direct REST calls to read roles, and the certificate strategy for those calls is chosen explicitly with `--tls-mode`:

- **`standard`** (default) — verify against the bundled public certificate authority list. Correct for AGOL and for any Portal whose certificate is issued by a public CA.
- **`os-trust-store`** — verify against the operating system's own certificate store. Use this for an internal Enterprise Portal whose certificate is signed by an organizational CA that the machine already trusts. Requires the `truststore` package.
- **`custom-ca-bundle`** — verify against a specific PEM certificate file supplied with `--ca-bundle-path`. That same bundle is passed to the SDK connection so both trust the same authority.

Before any real API traffic, the script performs a one-time TLS handshake check against the target host. If the handshake cannot be verified, the run aborts immediately and sends an email rather than proceeding over a connection it cannot trust. Authentication tokens are held in a token provider and are redacted from any error text that reaches the log.

### SSRF / network-boundary defenses

The script normalizes and validates the configured Portal/AGOL base URL before it is used. The URL must use HTTPS, must have a hostname, and must not contain embedded username/password credentials, a query string, a fragment, encoded slash/backslash separators, backslashes, control characters, or path traversal components. REST calls are then constructed beneath the validated Portal `/sharing/rest` base through `_sharing_rest_url()`.

The script also removes equivalent copies of the expected project root from `sys.path` before inserting the expected root first. This reduces the chance that an unrelated earlier package location wins import resolution.

### Token handling

The ArcGIS connection token is wrapped by `provider_for_gis()` and supplied through the token provider when role REST calls are built. The code supports a single synchronized token refresh/retry for HTTP 401/403 responses and ArcGIS token error codes 498/499. REST error messages are passed through `redact_token()` before they are surfaced in the log or email.

The script does not place the authentication token in the CSV or generated workbook.

### Large / malformed responses

The role endpoint response is parsed as JSON and must be a JSON object. Role entries must be objects with an `id` and/or `name`, privileges must be a list, and role fields are checked for control characters. Pagination integers are parsed without silent truncation, `nextStart` must make forward progress, and the endpoint's reported total must remain consistent.

There is **no explicit local 64 MB response-size limit implemented in `PORTAL_ROLES.py`**. Large-response bounding, if supplied by the shared HTTP utility, is therefore outside this script's own validation logic. A malformed or structurally inconsistent role response fails the run rather than being treated as a complete role inventory.

### Redirect following

Automatic redirect following is explicitly disabled for direct role REST calls with `allow_redirects=False`. A redirect response is treated as a failure, so a token-bearing request cannot silently follow an unvalidated redirect to another destination.

### Python import-path / package shadowing

The script computes the expected project root, removes equivalent normalized copies of that root from `sys.path`, inserts the expected root at position 0, then uses `importlib.util.find_spec("UTILITIES")` to inspect the package origin. It refuses to import the package when the resolved directory is not the expected `UTILITIES` directory.

After import, it performs a second origin check against `UTILITIES.__file__` before continuing. This is the implemented package-shadowing protection in this version.

## Configuration

The script is driven by a TOML configuration file passed with `--config`, plus a `--target` of either `portal` or `agol`. Settings are read from the matching `[portal]` or `[agol]` section combined with a shared `[common]` section, so a single file can describe both environments for one organization.

The configuration supplies the portal URL, the username, the keyring service name used to retrieve the stored password, the output directory, and the email settings (SMTP server, from-address, and recipient list). Any email setting can be overridden on the command line.

## How it works

Every privilege in ArcGIS is identified internally by a machine-readable code such as `portal:user:createItem` or `portal:admin:deleteUsers`. Roles report their privileges as lists of these codes. People, however, need readable labels like "Create, edit, and delete own content."

The script bridges the two using an embedded mapping (`PRIVILEGE_DATA`) that pairs each privilege code with the exact human-readable label used in the template. When the script runs, it reads the labels already present in column C of the `MATRIX` sheet, looks up each label's corresponding privilege code, then checks each role's reported list of codes. Where a role's list contains the code for a given row, the script places a checkmark in that role's column. Matching is case-insensitive, so minor differences in capitalization between the template and the portal do not prevent a match. Column C itself is only read — the script never rewrites, reorders, or adds to it.

Because the mapping is embedded directly in the script, no separate spreadsheet of privilege definitions is required at run time.

## Test mode (default)

There is no separate test mode for this script; a single run inventories all custom roles, which is fast even for organizations with many roles. See [Observed runtime](#observed-runtime).

## Usage

Run the script for a single target environment at a time.

**Scenario: routine review of an ArcGIS Online organization.** Your organization is hosted on AGOL with a standard public certificate. Produce the matrix and have it emailed to the recipients in your config:

```
python PORTAL_ROLES.py --config ORG.config.toml --target agol
```

**Scenario: an internal Enterprise Portal trusted by your domain.** Your Portal sits behind an organizational certificate authority that your Windows machines already trust. Verify against the OS certificate store:

```
python PORTAL_ROLES.py --config ORG.config.toml --target portal --tls-mode os-trust-store
```

**Scenario: an Enterprise Portal with a certificate from a private CA not installed on the machine.** Point the script at the CA's PEM bundle so both this script and the SDK connection trust it:

```
python PORTAL_ROLES.py --config ORG.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
```

**Scenario: sending the report to a different recipient for a one-off audit.** Override the email settings from the command line without editing the config file:

```
python PORTAL_ROLES.py --config ORG.config.toml --target portal --email-to auditor@example.com --smtp-server mail.example.com
```

**Scenario: directing the workbook to a controlled audit folder for a one-time review.** Use the command-line output directory override so the configured default does not need to change:

```
python PORTAL_ROLES.py --config ORG.config.toml --target portal --output-dir "D:\reports\roles"
```

**Scenario: checking the current command-line interface during deployment.** Display the complete current argument list without starting a role inventory:

```
python PORTAL_ROLES.py --help
```

## Command-line options

| Option | Description |
| --- | --- |
| `--config` | Path to the organization's TOML configuration file. Required. |
| `--target` | Which environment to scan: `portal` or `agol`. Required. |
| `--tls-mode` | Certificate verification strategy for both direct role REST calls and the ArcGIS SDK connection: `standard` (default), `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | Path to a PEM CA bundle. Required when `--tls-mode` is `custom-ca-bundle`; ignored otherwise. |
| `--email-to` | Override the recipient list from the config file. |
| `--smtp-server` | Override the SMTP server from the config file. |
| `--output-dir` | Override the output directory from the config file. |

Common Portal, email, and output options are provided by the shared `UTILITIES` package; run the script with `--help` for the complete list.

## Performance

The script makes a small number of REST calls to list roles and their privileges, then writes a single Excel file. It does not scan content, layers, or attachments, so its workload is proportional only to the number of roles in the organization — a very small quantity in practice.

## Observed runtime

Runs typically complete in a few seconds. Even organizations with dozens of custom roles finish well under a minute, since the total work is a handful of role queries plus writing one workbook.

## Output file

A single Excel workbook is written to the configured output directory, named with the organization prefix, target, and a timestamp, for example `ORG_ROLES_portal_20260926_1714.xlsx`. It is a copy of `ROLES_TEMPLATE.xlsx` with the `MATRIX` sheet populated as described in [Column descriptions](#column-descriptions). A log file is written alongside it, and if email is configured, the workbook and log are sent to the configured recipients.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The run aborted (for example: template missing, TLS handshake failed, could not connect, no roles found, or the template could not be populated). An explanatory email is sent where possible. |
| `2` | Invalid command-line arguments (for example: `--tls-mode custom-ca-bundle` without `--ca-bundle-path`). |

## Notes and caveats

- Only custom roles are reported. The built-in Viewer, Data Editor, and Facilitator roles are excluded by name.
- Column C in the template is authoritative for which privileges appear and in what order. If a privilege is not listed in the template, it will not appear in the report even if a role grants it.
- The privilege label in the template must match a label in the script's embedded mapping. If the portal introduces a new or renamed privilege that isn't in the template, that privilege simply won't be shown; the labels in the template and the embedded mapping should be kept aligned.
- If the template contains the same privilege label on two different rows, both rows will show identical checkmarks, because matching is by label text.