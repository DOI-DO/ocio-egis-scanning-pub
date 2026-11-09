# PORTAL_PUBLIC_EDIT_SCAN.py

**Scan AGOL for hosted feature services that allow unauthenticated (public) editing capabilities.**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Public Data Collection](#why-inventory-public-data-collection)
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

# For Report Readers

## Overview

This scan identifies hosted feature services in your ArcGIS Online organization that are publicly shared with editing capabilities enabled. When a service is public with editing enabled, anyone on the internet—without logging in—can potentially modify data in that service. The scan classifies exactly what actions the public can perform: add new records, query/view records, update existing records, or delete records.

Read-only public services (viewing only, with no create, update, or delete capability) are not included in the report, because this tool is specifically looking for unauthenticated editing exposure.

The report is intended for security and data-governance review. It is a read-only inventory: the script does not change sharing, service capabilities, records, or permissions.

## Why Inventory Public Data Collection

This script inventories whether a hosted feature service is configured so unauthenticated users can edit it. The reason this needs to be inventoried deliberately is that public write access is one of the few misconfigurations in an ArcGIS organization that exposes risk to the entire internet, yet it is invisible from every surface an administrator normally looks at. A service's sharing level and its editing capabilities are two independent settings, and the combination that matters here — publicly shared *and* editable without authentication — produces no warning, no banner, and nothing in day-to-day use that distinguishes it from a safe public read-only service. The only way to know which of your public services are actually writable by anonymous users is to check each one's effective capabilities directly, which is exactly what this scan does.

Most organizations share services for viewing and analysis, not for the public to modify. Public editing capabilities are usually enabled intentionally for specific use cases, such as crowdsourced data collection or community feedback. However, it's easy for these settings to be misconfigured or left enabled longer than necessary. A single service can acquire public write access through several independent paths: a sharing change that makes an editable service public, an editing capability turned on for a service that was already public, a template or copy that inherited an editable configuration, or a crowdsourcing workflow that was set up for a one-time event and never locked back down. None of these announce themselves afterward, so the exposure persists silently until someone goes looking — and the cost of not looking is that anonymous users may already be adding, changing, or deleting data. A regular inventory helps you verify that public editing is intentional and necessary, rather than an oversight that could corrupt your data.

This particular scan is worth running because it is precise about the one thing that determines severity: which specific actions the public can perform. It does not merely flag "public and editable" — it separates create, query, update, and delete into distinct findings, so a benign public submission form (create only) is clearly distinguishable from a service where anonymous users can alter or destroy existing records. That precision is what lets a reviewer triage: public delete is a high-priority emergency, public update is serious, and public create may be an entirely intended crowdsourcing workflow. Crucially, the scan determines capability correctly rather than naively — it merges the capabilities reported by the service root with those of every sublayer, so a service that looks locked down at the top level but exposes editing on an individual layer is still caught. A check that only read the service-level string would miss exactly those partial exposures that are easiest to overlook manually.

Running this as a recurring inventory matters because public write exposure is both high-consequence and reversible-by-accident. Configurations drift as services are republished, copied, re-shared, and reconfigured, so a service that was safe last quarter can become publicly editable this quarter without anyone intending it — and unlike many governance findings, this one carries active, ongoing risk the entire time it goes unnoticed: data integrity damage, unaccountable anonymous edits, possible data loss, resource consumption from unwanted submissions, and downstream propagation into any map, app, or dashboard that consumes the service. A periodic scan converts that standing, invisible risk into a dated, reviewable list, and because the tool is strictly read-only and never changes sharing or capabilities, it is safe to run as often as a security program requires — producing the evidence a remediation decision needs without ever being the thing that alters a live public workflow. The script evaluates not only the service-level capability string but also the capabilities returned by its sublayers. Effective capabilities are merged across the service and its sublayers, so the report reflects whether any supported layer exposes create, update, or delete capability.

## Why it's a problem

Public editing creates multiple risks depending on what actions are enabled:

- **Data Integrity**: Anyone can add, modify, or delete records without being authenticated through the organization. This can introduce spam, incorrect information, or unwanted changes.
- **No User Accountability**: The public-editing setting does not identify each anonymous editor in the way an authenticated user account does.
- **Data Loss**: If delete is enabled, records can be removed by unauthenticated users without requiring an organization login.
- **Bandwidth and Storage**: Public create/edit capability can allow unwanted submissions that consume service, storage, and processing resources.
- **Downstream Impacts**: If other systems, dashboards, maps, or applications rely on this data, unwanted changes can propagate through the broader workflow.
- **Compliance and Governance**: Depending on the data and organizational requirements, unauthenticated write access may be inconsistent with security or data-governance controls.

### Public Delete Access

If a service shows `TRUE` for **The Public Can View and Delete Edits**, treat that result as a high-priority item for manual review because unauthenticated users can potentially remove records.

Public delete access means an unauthenticated internet user may be able to remove data from the service without first signing into your organization. The exact impact depends on the dataset, backups, validation controls, downstream dependencies, and whether the delete capability is intentionally part of a public workflow.

Review services with public delete access promptly. Confirm whether the configuration is intentional, identify the workflow that requires it, and consider whether stronger access controls or narrower editing permissions are appropriate before changing the live service.

## Why removal requires caution

Simply disabling public editing may break applications or workflows that depend on it. Before making changes:

- **Identify Users**: Determine what is actually using the public editing capability (mobile apps, web forms, embedded applications, Survey123-style workflows, or other client applications).
- **Assess Impact**: Understand which workflows will stop working if create, update, or delete is disabled.
- **Plan Migration**: If the service truly needs public editing, investigate authentication or constrained access controls before removing the existing workflow.
- **Test First**: Test proposed changes on a staging or non-production copy of the service where practical.
- **Communicate**: Notify users and data stewards before changing a live public-editing workflow.

Remember that this report only tells you what the service definition exposes at the time of the scan. It does not prove who is actively using the service or whether the current setting is intentional.

## Recommendations for investigation

1. **Review the Data**: Look at recent edits in the service to understand whether they appear legitimate or suspicious.
2. **Check Permissions**: Confirm whether public editing was intentional or a configuration mistake.
3. **Review Existing Governance Records**: Check the organization's approved data-collection or public-editing workflow documentation.
4. **Identify Dependencies**: Search for web maps, apps, dashboards, forms, or other clients that use this service before disabling editing.
5. **Set Constraints**: If public editing is necessary, review validation rules, attribute domains, required fields, geometry constraints, and other controls.
6. **Consider Versioning or Other Recovery Controls**: If the workflow supports it, use controls that make accidental or unwanted changes easier to detect and recover from.
7. **Monitor Activity**: Establish appropriate monitoring or periodic review for services that intentionally allow anonymous edits.
8. **Re-run after changes**: After correcting a public-editing configuration, run the scan again to confirm the service no longer appears in the report.

### Scenario: a public survey-style submission service

A community-facing application intentionally lets members of the public submit new records. The appropriate investigation is not simply “delete public editing.” Confirm that the service is the intended target, determine whether **create** is the only capability required, review whether **update** or **delete** are also enabled, and check whether the supporting application depends on those extra capabilities. The report gives the team a starting point for that review.

### Scenario: a public map accidentally exposes update/delete

A service was intended to be public for viewing, but its configuration also permits update and delete. Use the report to identify the exact capabilities, verify that the application does not require them, and then make the smallest necessary configuration change. Re-run the scan afterward.

## Column descriptions

The CSV columns are written in exactly this order:

| Column | Meaning |
|--------|---------|
| `PLATFORM` | Always `AGOL` (ArcGIS Online); this scan only runs against AGOL. |
| `ITEM_ID` | Unique identifier of the hosted feature service item. |
| `TITLE` | Human-readable name of the service as shown in AGOL. |
| `OWNER` | AGOL username of the account that owns and manages this service. |
| `URL` | Web address of the hosted feature service REST endpoint. |
| `CAPABILITIES` | Combined raw REST capability strings collected from the service definition and any sublayer definitions that provide a `capabilities` value. |
| `The Public Can Submit New Edits` | `TRUE` when the effective capability set contains `create`; otherwise `FALSE`. |
| `The Public Can View Edits` | `TRUE` when the effective capability set contains `query`; otherwise `FALSE`. |
| `The Public Can View and Update Edits` | `TRUE` when the effective capability set contains `update`; otherwise `FALSE`. |
| `The Public Can View and Delete Edits` | `TRUE` when the effective capability set contains `delete`; otherwise `FALSE`. |

The report contains only services where at least one of the create, update, or delete flags is `TRUE`. A public service that only has `query` capability is therefore excluded from the CSV.

---

# For Administrators

## Dependencies on the shared UTILITIES package

Only the helper file names used by this script are listed here:

- `cli.py`
- `config.py`
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `restutils.py`
- `secure_session.py`
- `auth.py`
- `arcgis_rest.py`

## Requirements

- Python 3.9+.
- ArcGIS API for Python (`arcgis`) and `requests`.
- AGOL organization; this script does not support Enterprise Portal.
- Account able to enumerate organization hosted Feature Services.
- OS keyring credential or `PORTAL_PASSWORD` environment variable.
- SMTP access for completion email.
- `truststore` when using `--tls-mode os-trust-store`.

## Security: Certificate (TLS) Validation

This script always verifies TLS certificates. Certificate verification cannot be disabled.

The same selected verification strategy is used for the script's direct HTTP session and passed to the ArcGIS SDK connection through `connect_gis()`:

- **`standard` (default):** Uses the standard/public CA bundle.
- **`os-trust-store`:** Uses the operating system certificate store; useful for an internal organizational CA and requires `truststore`.
- **`custom-ca-bundle`:** Uses the PEM CA bundle supplied with `--ca-bundle-path`.

Before the scan begins, `verify_tls_handshake()` performs a one-time TLS handshake against the configured Portal URL. A failed handshake stops the run before service enumeration.

### SSRF / network-boundary defenses

This script's direct REST client is constrained to the configured AGOL/Portal origin and `/sharing/rest/` API boundary. Direct URLs are validated before requests are sent and unsafe URL forms such as embedded credentials, query strings, fragments, encoded slash/backslash separators, control characters, and path traversal are rejected.

The organization is resolved first and item searches are scoped with `orgid:<authenticated-org-id>`. Returned search items are checked for cross-organization results before they are accepted.

The public service-definition and sublayer calls reuse the validated direct-session path. The current script does not add arbitrary external service origins to a trust list; its primary operation is organization-scoped AGOL hosted Feature Service enumeration.

### Token handling

Authentication is provided through the shared `provider_for_gis()` token provider. The request layer removes caller-supplied `token` and `f` parameters before inserting the controlled token and JSON response format.

An authentication failure can trigger one synchronized token refresh/retry. Token-bearing error text is redacted through the shared security helper before it is used for diagnostics.

### Large/malformed responses

Direct REST responses are streamed and limited to 64 MB. Declared content length is checked where available, the byte limit is enforced during streaming, and the body must decode as a JSON object before use.

Portal search responses, item IDs, organization IDs, result lists, totals, and pagination progress are validated. A malformed or inconsistent catalog response is not silently treated as a complete result set.

### Redirect following is explicitly disabled

Direct REST requests use `allow_redirects=False`. Redirects are handled explicitly so the script can validate the destination before continuing. A validated redirect remains subject to the authenticated Portal origin and `/sharing/rest/` path policy, and no more than three redirects are permitted.

### Python import-path/package shadowing

The script places the parent project directory at the front of `sys.path` before importing `UTILITIES`. The current implementation does not remove equivalent duplicate project-root entries first and does not independently verify the loaded `UTILITIES` package origin with `find_spec()`.

That means the requested package-shadowing control of “remove equivalent copies, insert the expected root first, import UTILITIES, then verify the loaded package came from the expected directory” is **not fully implemented in this version**. Only the expected-root insertion is present.

## Configuration

The script uses an organization-specific TOML file supplied through `--config`. The selected `--target` must be `agol` for this utility.

### Example configuration

```toml
[common]
org-name = "ORG1"
output-dir = "C:\\temp\\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "sender@example.com"
max-email-mb = 20

[agol]
portal-url = "https://www.arcgis.com"
username = "agol_service_account"
keyring-service = "AGOL"
```

The password is not stored in the TOML. Store it in the OS keyring, for example:

```bash
python -c "import keyring; keyring.set_password('AGOL', 'agol_service_account', '<password>')"
```

`PORTAL_PASSWORD` can be used as the environment-variable fallback.

### Configuration precedence

1. Command-line value.
2. Selected `[agol]` setting.
3. `[common]` setting.
4. Shared/default value where applicable.

The script resolves `max-email-mb` through the shared utility. If it is not set, the shared default of 20 MB is used.

## How it works

1. **Parse and validate options.** The script requires `--config` and `--target agol`. If `custom-ca-bundle` is selected, `--ca-bundle-path` is also required.
2. **Establish verified TLS.** A secure REST session is created and the Portal URL is checked with a real TLS handshake.
3. **Connect to AGOL.** The ArcGIS SDK connection uses the same TLS verification mode and the configured credentials.
4. **Resolve exclusions.** Any `--exclude-group-id` values are resolved through the shared utility and the resulting item IDs are removed before processing.
5. **Enumerate hosted Feature Services.** `bucketed_hosted_fs()` performs an organization-only inventory of hosted Feature Service items.
6. **Filter to public services.** Only items whose `access` value is `public` are considered.
7. **Apply test-mode limit.** The default run examines the first 25 public services. `--no-test` removes that limit.
8. **Read service definitions.** Each selected service is read, and its sublayer definitions are inspected for additional capability strings.
9. **Classify capabilities.** The combined capability set is mapped to the four user-facing public-permission flags.
10. **Filter the report.** Public services are written only when Create, Update, or Delete is enabled. Public read-only services are excluded.
11. **Write and email results.** The CSV is written and passed to the shared attachment helper, and the completion email is sent with the report when there are findings.

### Effective capability logic

The script starts with the service root's `capabilities` value and adds any non-empty `capabilities` values from the service's sublayers. The values are joined into one comma-separated capability string before classification.

The public-action flags are determined as follows:

| Capability | Report flag |
|---|---|
| `create` | `The Public Can Submit New Edits` |
| `query` | `The Public Can View Edits` |
| `update` | `The Public Can View and Update Edits` |
| `delete` | `The Public Can View and Delete Edits` |

## Test mode (default)

**Test mode is ON by default.** It limits processing to the first **25 publicly shared hosted Feature Services** after the normal hosted-service inventory and public-access filter.

Use test mode to verify:

- The AGOL credentials are valid.
- The TLS trust configuration is correct.
- The account can enumerate hosted Feature Services.
- Service definitions and sublayer definitions can be read.
- The resulting CSV and email look correct.

Pass `--no-test` to inspect all publicly shared hosted Feature Services returned by the organization-scoped inventory.

## Usage

### Scenario 1 — First-time validation

**Situation:** You have just deployed the script and want a small sample before scanning every public Feature Service.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol
```

**Expect:** Up to 25 public hosted Feature Services are checked. Only services with create, update, or delete capability are written to the CSV.

### Scenario 2 — Full security inventory

**Situation:** A security or governance review needs all public editing exposures in the organization.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --no-test
```

**Expect:** All organization-scoped public hosted Feature Services are evaluated, not just the first 25.

### Scenario 3 — Investigate a potentially high-risk public service

**Situation:** The report shows a service with public delete capability and you want the next complete scan to ensure the current state is captured.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --no-test
```

**Expect:** The service is re-evaluated from its current service and sublayer definitions. The resulting row shows the raw capability string and the individual public-action flags.

### Scenario 4 — Exclude a known approved group

**Situation:** Your organization has a documented demo/training group whose Feature Services should not be part of this security inventory.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --exclude-group-id abc123xyz
```

**Expect:** Items in that group are excluded before public-service analysis. Multiple group IDs can be supplied as supported by the shared `--exclude-group-id` argument.

### Scenario 5 — Reduce or increase concurrency

**Situation:** The Portal is busy and you want to reduce simultaneous service-definition and sublayer requests, or you have sufficient capacity and want more concurrency.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --workers 8
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --workers 24
```

**Expect:** The scan scope stays the same. The worker setting affects the concurrent service-definition processing workload.

### Scenario 6 — Use the OS certificate store

**Situation:** Your Enterprise tooling uses an organizational CA already trusted by the operating system, and you are validating a compatible deployment configuration.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --tls-mode os-trust-store
```

**Expect:** The secure REST session and ArcGIS SDK connection use the OS trust store, with certificate validation still enabled.

### Scenario 7 — Use a specific PEM CA bundle

**Situation:** Your organization supplies a CA bundle file that must be used for certificate validation.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
```

**Expect:** The supplied PEM bundle is used for both the direct REST session and SDK connection. The script exits with argument error status if the CA path is omitted.

### Scenario 8 — Override the output directory for a review

**Situation:** You want a one-off report written into an audit or security-review folder without editing the organization's TOML file.

```bash
python PORTAL_PUBLIC_EDIT_SCAN.py --config ORG.config.toml --target agol --output-dir "D:\reports\public-edit"
```

**Expect:** The CSV and run log are written beneath the specified output directory.

## Command-line options

| Option | Required | Default | Meaning |
|---|---:|---|---|
| `--config FILE` | Yes | — | Path to the organization configuration TOML. |
| `--target NAME` | Yes | — | Must be `agol`; selects AGOL configuration. |
| `--no-test` | No | Test mode | Scan all public hosted Feature Services instead of the first 25. |
| `--tls-mode MODE` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path PATH` | Only for custom bundle | — | PEM CA bundle path. Required with `custom-ca-bundle`. |
| `--exclude-group-id ID` | No | — | Exclude items in one or more AGOL groups. |
| `--workers N` | No | `16` | Worker concurrency used by the service-definition analysis. Valid range: `1-64`. |
| `--portal-url URL` | No | config | Portal/AGOL URL override. The target must still classify as AGOL. |
| `--username USERNAME` | No | config | Login username override. |
| `--keyring-service NAME` | No | config | Keyring service override. |
| `--output-dir PATH` | No | config | Output and log directory override. |
| `--smtp-server HOST` | No | config | SMTP server override. |
| `--email-to ADDRESS` | No | config | Recipient or comma-separated recipient list override. |
| `--email-from ADDRESS` | No | config | Sender override. |

## Performance

The script first enumerates hosted Feature Services through the shared bucketed inventory helper and then filters to public services. Service definitions and their sublayer definitions are analyzed using concurrent worker processing controlled by `--workers` (default 16).

Because the script reads every selected service's REST definition and may read each sublayer definition, services with many sublayers require more requests and can take longer.

The default 25-service test mode is intentionally small so configuration checks can finish quickly. Full mode has no test cap, subject to the shared hosted-service inventory safety limits.

Direct REST calls use streaming, retries, connection pooling, and a 64 MB response cap.

## Observed Runtime

Representative operational guidance:

| Run type | Expected behavior |
|---|---|
| Test mode | Up to 25 public hosted Feature Services; intended for quick validation. |
| Full scan | All organization-scoped public hosted Feature Services; duration depends on service count and sublayer complexity. |

Actual runtime depends on the number of hosted Feature Services, the number of public services, sublayer count, network latency, AGOL API responsiveness, and `--workers`.

## Output file

The main CSV is written using this naming pattern:

```text
{org-name}_PUBLIC_EDIT_SCAN_agol_{timestamp}.csv
```

The log file uses:

```text
{org-name}_PUBLIC_EDIT_SCAN_agol_{timestamp}.log
```

The CSV contains one row for every publicly shared hosted Feature Service that has at least one of Create, Update, or Delete enabled after combining service-level and sublayer capabilities.

Read-only public services are not included in the CSV.

The completion email includes the scan summary and the CSV attachment when the shared email attachment policy allows it. The log is also handled by the shared attachment helper.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Scan completed through the normal workflow. |
| `1` | Fatal operational failure, such as connection failure, TLS verification failure, or another unrecovered runtime error. |
| `2` | Invalid invocation, including missing `--ca-bundle-path` when `custom-ca-bundle` is selected. |

## Notes and caveats

- **AGOL Only**: This script only works with ArcGIS Online. It does not scan Enterprise Portal.
- **Target must be AGOL**: `--target agol` is required. Other target values are rejected by the script.
- **Public access is the first filter**: Only services with `access=public` are evaluated for the report.
- **Read-Only Services Excluded**: Services that are publicly shared but have query-only capabilities (no create, update, or delete) are excluded from the report.
- **Layer-Level Capabilities**: The script checks both service-level and layer-level capabilities and merges the non-empty capability strings to determine effective public permissions.
- **Sublayer Analysis**: For services with sublayers, each sublayer is queried for its `capabilities` value when one is available.
- **A positive flag means the capability string contained that action**: The report flags are derived directly from `create`, `query`, `update`, and `delete` capability names.
- **Test Mode Recommended**: Start with test mode to validate configuration before running a full scan.
- **Full scans can be large**: The script scans all public hosted Feature Services when `--no-test` is used.
- **Performance**: Services with many sublayers generate more REST calls and can take longer to analyze. Consider running during off-peak hours when practical.
- **Group exclusions are early**: Excluded item IDs are resolved before public filtering and service analysis so excluded services do not consume analysis time.
- **TLS verification is always enabled**: `--tls-mode` chooses the trust source; it does not disable certificate validation.
- **TLS failures are distinct**: Certificate verification errors are surfaced separately from ordinary transport/runtime failures.
- **Redirects are explicitly disabled at the HTTP layer**: The script validates redirect destinations itself rather than allowing the HTTP client to follow them automatically.
- **Large responses are bounded**: Direct REST responses are limited to 64 MB and must decode to JSON objects.
- **Searches are organization-scoped**: The authenticated organization ID is required and used to constrain inventory searches.
- **The current import-path hardening is partial**: The script inserts the parent project directory at the front of `sys.path`, but it does not remove equivalent duplicate roots or independently verify the imported `UTILITIES` package origin.
- **This report is read-only**: The script does not modify service capabilities, sharing, records, or Portal items.
- **Investigate before changing anything**: A flagged capability may be intentional for a legitimate public data-collection workflow. Verify the business requirement and dependent applications before changing the live service.