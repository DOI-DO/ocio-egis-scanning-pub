# AGOL Credit Report — ArcGIS Online (AGOL) Only

Generate credit-usage reports for your ArcGIS Online organization. The tool requests Esri's organization credit report for the previous complete week or previous complete calendar month, cleans the returned data, and creates three CSV reports: detailed by-user data, totals by app/service column, and totals by user.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Credits](#why-inventory-credits)
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

This report shows where your ArcGIS Online credits were used during a selected reporting period. It is designed for people who need to understand usage, not for people who need to manage the script.

The tool produces three CSV files and emails a summary:

- **By-user** — the detailed Esri report, cleaned so it can be opened as a normal CSV. It shows each user and the credit-use columns returned by Esri.
- **By-app** — totals each credit-use column across all users.
- **By-user summary** — totals each user's credits across the credit-use columns and sorts users by total usage.

The report uses **week** or **month** periods. Week means the previous complete Saturday-through-Friday period. Month means the previous complete calendar month.

## Why Inventory Credits

Credits are a finite, prepaid resource in ArcGIS Online, and unlike disk space or user seats, they are consumed silently in the background by operations that may never surface in anyone's day-to-day view of the organization. Storage of hosted feature and tile layers, premium content access, spatial analysis, geocoding, routing, and demographic enrichment all draw down the same shared balance. Because these charges accrue continuously rather than at a single visible checkout moment, there is rarely a natural point at which someone notices them. A regular inventory creates that point deliberately.

Inventorying credits matters because consumption and responsibility are usually decoupled. The person who publishes a layer, the person who schedules an analysis, and the person who owns the budget are frequently three different people. A single ArcGIS Online workflow can use the same service or layer repeatedly across many users and applications, so the cost of one decision is smeared across the whole organization and never attributed back to its source. The detailed report re-establishes that link: it lets you see repeated usage patterns and connect credit activity back to the specific people and applications driving it, which is the prerequisite for any informed conversation about cost.

The scan is also worth performing on a schedule rather than only in response to a problem, because the most expensive credit patterns are the quiet, recurring ones. A scheduled geocoding job, an automated enrichment step in a data pipeline, a forgotten stored layer, or a dashboard that re-runs premium analysis on every load can each consume credits steadily without any single action looking unusual. These costs compound over weeks and are precisely the kind of thing that does not announce itself. By capturing usage every period, this report turns an otherwise invisible trend into a measurable one, so you can distinguish a legitimate one-time project charge from a slow leak before it reaches the credit balance or budget. In short, the inventory exists to give the organization visibility it would not otherwise have, early enough for that visibility to be actionable.

## Why it's a problem

Credit usage can be difficult to understand when it is spread across many people and many services. A scheduled job, repeated analysis, geocoding activity, premium service, or another paid operation can consume credits without being obvious from day-to-day work.

Without a regular report, an organization may not notice an unusual increase until the credit balance or budget is affected.

## Why removal requires caution

The report identifies usage; it does not tell you that a user, app, service, or workflow should be removed.

A high total can be completely legitimate. For example, a major mapping project, planned migration, or one-time analysis can create a large but expected credit charge. Use the report to identify what needs explanation before changing a workflow or disabling anything.

## Recommendations for investigation

1. **Start with the by-app CSV.** Look for the credit-use columns that contributed the largest totals during the period.
2. **Review the by-user summary.** The rows are sorted by `total_credits`, making unusual or unexpectedly high usage easier to spot.
3. **Use the detailed by-user CSV for context.** It shows which user had usage in which credit-use columns.
4. **Compare reporting periods.** A single report is a snapshot; week-to-week or month-to-month comparisons help distinguish a normal project from an unusual spike.
5. **Ask for operational context before taking action.** Confirm whether the work was planned, necessary, or associated with an automated process.

## Column descriptions

The Python file produces three different CSV structures. The exact credit-use columns in the detailed report are determined by the headers returned by Esri for that reporting period.

### By-user CSV

The output starts with `PLATFORM`, followed by the columns returned by Esri's credit report after the metadata rows are removed.

| Column | Description |
|---|---|
| `PLATFORM` | Always `AGOL`. Added by the script to identify the source platform. |
| Esri report columns | The remaining columns are copied from Esri's report header for the selected period. The first column is normally the user identifier, followed by credit-use columns returned by Esri. The exact set of columns can vary by report. |

### By-app CSV

| Column | Description |
|---|---|
| `PLATFORM` | Always `AGOL`. |
| `app` | The name of a credit-use column from the Esri by-user report. The script treats non-identity columns as credit-use columns and uses those names here. |
| `credits` | The total parsed credits in that column across all users for the selected period, rounded to two decimal places. |

### By-user summary CSV

| Column | Description |
|---|---|
| `PLATFORM` | Always `AGOL`. |
| `user` | The user identifier selected from the Esri report. The script looks for `username`, `user name`, `user`, `full name`, `fullname`, or `name`, then falls back to the first report column. |
| `total_credits` | The user's summed credit usage across the credit-use columns, rounded to two decimal places. Rows are sorted from highest total to lowest. |

---

# For Administrators

## Dependencies on the shared UTILITIES package

`__init__.py`  
`auth.py`  
`cli.py`  
`config.py`  
`emailer.py`  
`gis_connect.py`  
`logging_setup.py`  
`restutils.py`  
`secure_session.py`

## Requirements

- Python with `arcgis`, `requests`, and the shared `UTILITIES` package.
- An ArcGIS Online organization and credentials able to generate organization credit reports.
- An organization TOML configuration file.
- Writable output directory and working SMTP delivery.

## Security: certificate (TLS) validation

The script uses TLS verification for both its ArcGIS SDK connection and its own direct REST session. The REST session verification mode is selected with `--tls-mode`:

- `standard` — certifi's bundled public CA certificates; the normal choice for AGOL.
- `os-trust-store` — the operating system trust store; requires the `truststore` package.
- `custom-ca-bundle` — an explicit PEM CA bundle supplied through `--ca-bundle-path`.

Before report traffic, the script performs a TLS handshake check against the configured AGOL URL. A failed certificate verification aborts the run. The ArcGIS SDK connection is also configured with the selected verification mode.

**SSRF/network-boundary defenses:** this script enforces AGOL-only operation by checking the configured Portal URL before connecting. It does not implement a separate destination allowlist for arbitrary layer/service URLs because it does not scan layer URLs; its direct REST calls are constructed against the configured AGOL portal and authenticated user's report endpoints.

**Token handling:** the report token is managed through the shared `TokenProvider`. The token is not included in object representations or normal logging, and exception text is passed through `redact_token()` before logging/emailing. The REST report calls do use the raw token at the request site because the ArcGIS endpoint requires authentication.

**Large/malformed responses:** this file does not define an independent maximum HTTP response size or a general malformed-response policy. JSON decoding and REST response handling are performed directly or through the shared `restutils` helper depending on the operation. The report download is read into memory before CSV/ZIP decoding, so output size is primarily constrained by the upstream Esri report and available memory.

**Python import-path/package shadowing:** the current script inserts the project parent directory at the front of `sys.path` before importing `UTILITIES`, but it does not remove equivalent duplicate entries or verify the imported package's physical location. The README therefore does not claim that stronger package-origin verification is present in this script.

**Redirect following:** the current direct report download explicitly uses `allow_redirects=True`. Redirects are therefore followed by the `requests` client for that download path. This is important when evaluating token exposure because the fallback download passes the token as a query parameter. The ArcGIS SDK download path is handled by the SDK.

## Configuration

- `--config` selects the organization TOML file.
- The script is hard-wired to the `[agol]` target; there is no `--target` choice.
- Required settings are `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, and `email-from`.
- Command-line values override configuration values when the shared CLI/config helpers expose those options.
- Passwords are resolved through the configured keyring mechanism; the script does not store passwords in the TOML.
- `--period` is required and must be `week` or `month`.
- `--timeout-minutes` controls the report-generation wait period and defaults to 10 minutes.

## How it works

1. **Validate configuration and platform.** The script finalizes configuration, forces the target to AGOL, and refuses to run when the configured URL is not classified as AGOL.
2. **Establish TLS.** It builds the secure REST session, verifies the configured AGOL endpoint, and connects through the ArcGIS SDK.
3. **Resolve the reporting period.** `week` becomes the previous complete Saturday-through-Friday period. `month` becomes the previous complete calendar month. The current day is not included.
4. **Request the report.** The script calls the AGOL credits-report endpoint and asks Esri to generate an organization credit report.
5. **Handle an existing report.** If Esri says the requested report already exists, the script extracts the existing report item ID, verifies that it belongs to the authenticated user and appears to be a report item, permanently deletes it, and retries report creation.
6. **Poll for completion.** The status endpoint is checked every 5 seconds until the report completes, fails, or reaches the configured wall-clock timeout. Authentication failures trigger token refreshes, up to the configured retry limit.
7. **Download the report.** The script first tries the ArcGIS item download API and falls back to a direct REST download. ZIP payloads are opened and the first CSV entry is used.
8. **Clean and aggregate.** Metadata rows are stripped. The detailed by-user CSV receives a leading `PLATFORM` column. Credit-use columns are summed into the by-app report, and each user's credit-use columns are summed into the by-user summary.
9. **Email results.** The generated CSVs and log are gathered using the shared attachment-size policy and sent in a completion email.

## Test mode (default)

**There is no test or dry-run mode.** Every invocation requests a real Esri credit report. Duplicate-report handling can also permanently delete a matching existing report item before regeneration.

For a low-risk setup check, use a reporting period and configuration appropriate for your organization and inspect the generated CSVs and log before scheduling recurring runs.

## Usage

Each example below describes a common scenario.

### Scenario 1: Weekly scheduled report

You want a report covering the last complete Saturday-through-Friday period, such as a Monday morning operational report.

```bash
python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week
```

### Scenario 2: Monthly management report

You want the previous calendar month's credit usage for a monthly review.

```bash
python AGOL_CREDIT_REPORT.py --config org2.config.toml --period month
```

### Scenario 3: Large organization needs more generation time

Esri's asynchronous report for your organization sometimes takes longer than the 10-minute default.

```bash
python AGOL_CREDIT_REPORT.py --config org1.config.toml --period month --timeout-minutes 45
```

### Scenario 4: Write reports to a specific directory

You need the generated CSVs and log in a temporary investigation directory rather than the configured default.

```bash
python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week --output-dir "C:\\temp\\reports"
```

### Scenario 5: Use the operating-system trust store

Your runtime system already trusts the certificate chain you want to use for the AGOL connection.

```bash
python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week --tls-mode os-trust-store
```

### Scenario 6: Use a custom CA bundle

Your environment requires a specific PEM CA bundle for TLS validation.

```bash
python AGOL_CREDIT_REPORT.py --config org1.config.toml --period week --tls-mode custom-ca-bundle --ca-bundle-path C:\\certs\\internal-ca.pem
```

### Scenario 7: Review available options

You are validating a deployment or troubleshooting configuration and want the built-in usage help.

```bash
python AGOL_CREDIT_REPORT.py --help
```

## Command-line options

| Option | Default | Description |
|---|---|---|
| `--config` | required | Organization TOML file. |
| `--period` | required | `week` for the previous complete Saturday-through-Friday period, or `month` for the previous complete calendar month. |
| `--timeout-minutes` | `10` | Maximum wall-clock time to wait for report generation. Polling occurs every 5 seconds. |
| `--portal-url` | from config | AGOL organization URL. |
| `--username` | from config | Username used to request the organization report. |
| `--keyring-service` | from config | Shared keyring service identifier. |
| `--smtp-server` | from config | SMTP relay used for completion email. |
| `--email-to` | from config | Email recipient or recipients. |
| `--email-from` | from config | Email sender. |
| `--output-dir` | from config | Directory for the CSVs and log. |
| `--tls-mode` | `standard` | TLS verification strategy for direct REST calls: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | none | PEM CA bundle required with `--tls-mode custom-ca-bundle`. |

## Performance

This script is not a broad parallel scanner. The expensive part is the server-side Esri report job.

- Report generation is asynchronous and is polled every 5 seconds.
- There is no worker pool and no `--workers` option.
- The local cleanup and two rollups are in-memory CSV processing and are normally much faster than report generation.
- A duplicate report can add a delete request and a second report-generation request.

## Observed runtime

Runtime is primarily controlled by Esri's report-generation time and organization size. Existing project observations are approximately:

| Run type | Typical duration | Main factor |
|---|---:|---|
| Weekly | 1–5 minutes | Esri report-generation time |
| Monthly | 3–15 minutes | Larger server-side report |

These figures are observations, not guarantees. Use the log's timing information when establishing a schedule for a specific organization.

## Output file

The output directory receives these files:

- `<ORG>_AGOL_CREDIT_REPORT_BY_USER_<period-slug>_<timestamp>.csv` — cleaned detailed report with `PLATFORM` followed by Esri's report columns.
- `<ORG>_AGOL_CREDIT_REPORT_BY_APP_<period-slug>_<timestamp>.csv` — `PLATFORM`, `app`, and `credits`.
- `<ORG>_AGOL_CREDIT_REPORT_BY_USER_SUMMARY_<period-slug>_<timestamp>.csv` — `PLATFORM`, `user`, and `total_credits`, sorted by total usage descending.
- `<ORG>_AGOL_CREDIT_REPORT_<timestamp>.log` — execution log.

The email includes the generated CSVs and log subject to the shared `max-email-mb` attachment policy.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The primary by-user CSV was successfully generated and the completion email was sent. |
| `1` | The run did not complete the primary report workflow successfully, or another fatal runtime error caused an abort. |
| `2` | Invalid command-line usage, including an invalid period or missing CA bundle when `custom-ca-bundle` is selected. |

## Notes and caveats

- **AGOL only.** The script forces `args.target` to `agol` and refuses a configured URL that is not classified as AGOL.
- **No dry run.** Every run contacts Esri and generates a real report.
- **Duplicate reports are permanently deleted.** When Esri reports that the requested report already exists, the script validates ownership/type and then permanently deletes that report item before regenerating it.
- **No custom date range.** The API usage in this script is limited to the `week` and `month` periods implemented here.
- **By-app column detection is heuristic.** Every report column except a fixed set of identity columns is treated as a credit-use column.
- **By-user detection is heuristic.** The script looks for common user column names and falls back to the first report column.
- **Malformed numeric credit values are ignored for rollup totals.** They are counted and logged, but do not contribute to the aggregate total.
- **The by-user CSV is the closest representation of Esri's report.** The by-app and by-user-summary files are derived from it.
- **TLS verification cannot be disabled.** Only the verification trust source can be selected.
- **The fallback download uses a token-bearing query parameter and explicitly follows redirects.** Treat network logs and proxies accordingly.
- **Email attachment size is controlled by the shared `gather_attachments()` policy.**