# Attachment Size Scan - ArcGIS Online (AGOL) or ArcGIS Enterprise Portal

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Attachment Size?](#why-inventory-attachment-size)
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
- [Output Files](#output-files)
- [Exit Codes](#exit-codes)
- [Notes and Caveats](#notes-and-caveats)

---

## For Report Readers

### Overview

This scan checks ArcGIS Online or ArcGIS Enterprise Portal for hosted feature-service layers and tables that contain attachments, then reports individual attachments that meet or exceed the administrator-selected size threshold. Typical attachments include photos, PDFs, and other documents stored with features. The main CSV is intended for investigation and cleanup planning rather than automatic deletion.

The scan uses two stages. First, it identifies layers/tables that have attachments. Second, it queries attachment metadata, fills missing sizes when possible, and reports attachments at or above the requested threshold. The report includes the item owner, layer, feature `Object_ID`, attachment name, and layer URL so an administrator can investigate the source.

### Why Inventory Attachment Size?

While the scan focuses on large attachments, it can reveal repeated patterns. The same layer can contribute multiple report rows because it may contain multiple large attachments, and the same layer URL is de-duplicated internally for Stage 2 so attachment metadata is not queried more than once per unique layer URL. Repeated rows or repeated problem layers can show recurring workflow issues, such as people consistently uploading full-resolution phone photos instead of smaller versions. Spotting these patterns helps teams address the root cause rather than only cleaning up individual files.

### Why It's a Problem

Large attachments consume storage space and can slow down map and layer workflows when users open or work with features that have many large files. They can also increase backup and maintenance time. A service item can appear modest in content listings while storing a much larger attachment footprint underneath.

### Why Removal Requires Caution

**Files in this report are not automatically safe to delete.** The scan flags files based on size; it cannot determine whether an attachment is still required. A large photo may be an official inspection record, and a large PDF may be a legal or project document. Treat the report as a **starting point for investigation**, not a delete checklist.

### Recommendations for Investigation

- **Start with the biggest files.** A small number of very large attachments can account for a significant share of storage.
- **Look for patterns.** If the same layer or workflow keeps producing large files, investigate the upload process and source devices.
- **Find the owner.** Contact the owner shown in the report to confirm why the attachment exists and whether it is still needed.
- **Check for backups.** Before removing anything, confirm that required records are preserved elsewhere.
- **Consider alternatives to deletion.** Where appropriate, replace originals with smaller versions or move large documents to a storage system intended for them.

### Column Descriptions

The main attachment report contains exactly the following columns:

| Column | Meaning |
|--------|---------|
| **platform** | Platform label supplied by the script: `PORTAL` for ArcGIS Enterprise or `AGOL` for ArcGIS Online. |
| **title** | Title of the hosted feature-service item containing the layer/table. |
| **item_id** | ArcGIS item ID for the hosted feature service. |
| **owner** | Owner of the hosted feature-service item. |
| **layer_title** | Display name of the layer or table containing the attachment. |
| **layer_id** | Numeric layer/table ID within the feature service. |
| **service_url** | REST URL of the specific layer/table that was scanned. |
| **Object_ID** | Object ID of the feature or record associated with the attachment. |
| **Attachment_Name** | File name of the attachment. |
| **Attachment_Size** | Attachment size in MB, rounded to two decimal places. Only attachments at or above the configured `--threshold-mb` are included in this report. |

If Stage 1 encounters errors, a separate errors CSV is created. Its columns are built from the fields present in the encountered error records and commonly include `platform`, `title`, `item_id`, `service_url`, `owner`, `layer_id`, and `Error`.

---

## For Administrators

### Dependencies on the Shared UTILITIES Package

`__init__.py`  
`restutils.py`  
`bucketing.py`  
`arcgis_rest.py`

### Requirements

- Python with the ArcGIS API for Python and `requests`.
- Access to ArcGIS Online or ArcGIS Enterprise Portal.
- An organization-specific TOML configuration file.
- Credentials available through the configured `connect_gis()` and keyring/user settings.
- Permission to search hosted feature services and query attachment metadata.
- Writable output directory and a working SMTP relay.

### Security: Certificate (TLS) Validation

The script uses TLS verification for its authenticated Portal/AGOL REST traffic. The direct REST session uses the selected `--tls-mode`, and the ArcGIS SDK connection is also configured with the selected verification strategy. A TLS handshake is performed before operational REST traffic; a failed verification stops the run.

**SSRF and network-boundary defenses**

Before an authenticated service request, `_validate_service_url()` validates the destination. HTTPS is required in both environments. For ArcGIS Enterprise, the service hostname and port must match the Portal hostname and port. For ArcGIS Online, the destination must be within the Esri-controlled `arcgis.com` domain because hosted services can legitimately use different Esri hostnames from the Portal URL. Untrusted service destinations are refused before the token is sent.

**Token handling**

Authentication is centralized through the shared `TokenProvider`. The raw token is obtained only at REST call sites that need it and is not intentionally logged. Exception text is passed through `redact_token()` before it is written to logs or reports. The script therefore limits accidental exposure of tokens while still allowing the REST calls to authenticate.

**Redirect handling**

The direct REST session disables transparent redirect following by setting `session.max_redirects = 0`. This is intended to prevent authenticated requests from automatically following redirects that could move token-bearing requests to an unintended destination. Redirect-related failures are surfaced rather than silently followed.

**Large or malformed responses**

This script does not define a response-size limit of its own. Service-definition, sublayer, object-ID, attachment, and search response handling is delegated to the shared `UTILITIES` REST helpers. Administrators should therefore treat those helpers as part of the security boundary and keep their response parsing bounded and defensive. Within this file, malformed definitions and unexpected responses are caught and recorded rather than being allowed to terminate unrelated service work.

**Python import-path/package trust**

Before importing `UTILITIES`, the script removes equivalent spellings of the expected project root from `sys.path` and inserts the canonical project root first. It then verifies that the loaded `UTILITIES` package came from the expected `UTILITIES` directory. If the package resolves from another location, the script raises an import error instead of continuing with an unexpected helper package.

**What certificate validation means here**

Certificate verification protects the authenticated REST and SDK connections from untrusted TLS certificates. The script does not use TLS verification as a substitute for the service-URL trust checks above; both controls are applied for authenticated service access.

### Configuration

- `--config` selects the organization-specific TOML configuration.
- `--target` selects the `[portal]` or `[agol]` configuration section.
- `--portal-url`, `--username`, `--keyring-service`, `--output-dir`, `--smtp-server`, `--email-to`, and `--email-from` can override configuration values through the shared CLI/config helpers.
- `--threshold-mb` is required and is inclusive: attachments with size **greater than or equal to** the threshold are reported.
- `--exclude-group-id` accepts one or more comma-separated group IDs and excludes matching items before Stage 1 scanning.
- Email attachment limits are resolved through the shared configuration helpers.
- The platform label is derived from the resolved `portal-url`.

### How It Works

1. **Initialize** - Parse arguments, finalize configuration, create the output directory, start logging, and build the direct REST session with the selected TLS strategy.
2. **Verify TLS** - Perform the one-time handshake check before operational REST calls.
3. **Connect to GIS** - Authenticate with the ArcGIS SDK and obtain a managed token provider.
4. **Discover hosted feature services** - Resolve the authenticated organization ID and search with an organization-scoped hosted feature-service query.
5. **Apply exclusions** - Remove items identified by excluded group IDs.
6. **Stage 1: Find attachment-bearing layers/tables** - Read service and sublayer definitions, confirm `hasAttachments`, validate service URLs, and determine whether attachments are present. On Portal, an ambiguous fast-path `queryAttachments` response falls through to object-ID enumeration.
7. **Stage 2: Measure attachments** - Query attachment metadata, resolve missing sizes when supported by the shared REST helpers, and keep rows at or above the threshold.
8. **Write and email reports** - Write the main attachment CSV and, when Stage 1 errors occur, an errors CSV; then email the available reports and log within the configured attachment limit.

### Test Mode (Default)

**Test mode is enabled by default** and limits discovery to **500 hosted feature services**. This is intended for validating configuration, credentials, output, and email delivery before a full scan. Use `--no-test` for the full configured scan, subject to the script's 2,000,000-item safety limit.

### Usage

Each example below describes a practical scenario.

#### Scenario 1: Validate a new Portal configuration

You have a new organization TOML file and want to confirm that the Portal credentials, output path, threshold, and email workflow work before scanning the whole organization.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6
```

This uses default test mode and reports attachments at or above 6 MB from the first 500 discovered hosted feature services.

#### Scenario 2: Run a full ArcGIS Online scan

The test run succeeded and you now want to scan the full AGOL organization for attachments at or above 10 MB.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org2.config.toml --target agol --threshold-mb 10 --no-test
```

`--no-test` removes the 500-service test limit.

#### Scenario 3: Exclude one or more groups

You want to scan the organization but leave test, archived, or other grouped content out of the run.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --exclude-group-id GROUP_ID_1,GROUP_ID_2
```

The matching service items are excluded before Stage 1 processing.

#### Scenario 4: Use the OS trust store

Your Enterprise Portal certificate is signed by an organizational CA that is already trusted by the operating system running the scanner.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --tls-mode os-trust-store
```

#### Scenario 5: Use a custom CA bundle

Your Portal uses an internal CA that is not present in the default trust store, so you need to point the scanner at the PEM bundle explicitly.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --tls-mode custom-ca-bundle --ca-bundle-path C:\certs\internal-ca.pem
```

The selected CA bundle is passed to both the direct REST session and the ArcGIS SDK connection.

#### Scenario 6: Increase worker concurrency

You have a large service inventory and enough network/server capacity to increase concurrency.

```bash
python SCAN_ATTACHMENTS_SIZE.py --config org1.config.toml --target portal --threshold-mb 6 --no-test --workers 24
```

The default is 12 workers. Increase gradually and monitor Portal performance and network utilization.

### Command-Line Options

| Option | Description |
|---|---|
| `--config CONFIG_FILE` | Organization-specific TOML configuration. |
| `--target {portal,agol}` | Configuration target section; required for normal runs. |
| `--portal-url URL` | Portal/AGOL URL override. |
| `--username USERNAME` | Username override. |
| `--keyring-service SERVICE` | Keyring service override. |
| `--output-dir DIR` | Output directory override. |
| `--email-to ADDR` | Email recipient override. |
| `--email-from ADDR` | Email sender override. |
| `--smtp-server SERVER` | SMTP server override. |
| `--threshold-mb MB` | Required positive whole-number attachment threshold; inclusive (`>=`). |
| `--workers N` | Concurrent worker threads for service/layer processing; default 12. |
| `--no-test` | Disable test mode and scan the full configured inventory, subject to the 2,000,000-item safety limit. |
| `--exclude-group-id GROUP_IDS` | Comma-separated group IDs to exclude. |
| `--tls-mode {standard,os-trust-store,custom-ca-bundle}` | TLS verification strategy for the script's direct REST session; default `standard`. |
| `--ca-bundle-path PATH` | PEM CA bundle; required when `--tls-mode custom-ca-bundle`. |
| `--help` | Show command-line help. |

### Performance

The script uses multithreading for hosted-service and layer processing. Stage 1 reads service and sublayer definitions in parallel; Stage 2 processes unique layer URLs in parallel and uses the shared REST helpers to resolve attachment sizes. Exact throughput depends on service count, layer count, number of attachments, response time, and the `--workers` setting.

Unique layer URLs are de-duplicated before Stage 2 so the same layer endpoint is not measured repeatedly. Full mode uses the 2,000,000-item safety ceiling.

### Observed Runtime

The current Python file does not contain measured runtime benchmarks, so exact durations should be treated as environment-specific. Runtime is primarily driven by the number of discovered services, the number of layers with attachments, attachment-query volume, server response time, and worker count.

For planning, run the default 500-service test first and use its log timing to estimate your own environment before scheduling a full scan.

### Output Files

The script creates these files in the configured output directory:

- **`{org_prefix}_{platform}_ATTACHMENT_SIZE_{timestamp}.csv`** - Main report containing attachments at or above the threshold. Columns are exactly: `platform`, `title`, `item_id`, `owner`, `layer_title`, `layer_id`, `service_url`, `Object_ID`, `Attachment_Name`, `Attachment_Size`.
- **`{org_prefix}_{platform}_SCAN_ERRORS_{timestamp}.csv`** - Stage 1 error details, created only when Stage 1 records one or more errors. The exact columns are derived from the fields present in the error records.
- **`{org_prefix}_{platform}_attachment_size_scan_{timestamp}.log`** - Execution log, diagnostics, progress, and timing information.

The email includes the generated reports and log subject to the configured email attachment-size limit.

### Exit Codes

- `0` - Scan and reporting completed and the final email was sent.
- `1` - Fatal error, including TLS handshake failure, Portal connection failure, organization-scoping failure, or another unrecoverable runtime error.
- `2` - Invalid command-line usage or argument validation failure.

### Notes and Caveats

- The scan performs real REST calls and is not instantaneous. Avoid overlapping large scans against the same organization unless your environment can support the added load.
- Test mode is the default; use it to validate configuration and connectivity before a full scan.
- Organization scoping is mandatory for service inventory. If the authenticated organization ID cannot be resolved, Stage 1 aborts rather than performing an unscoped search.
- Permission errors and some inaccessible service/layer conditions are captured in the Stage 1 errors CSV and log; they do not automatically stop unrelated services from being processed.
- A layer URL that cannot be validated as a trusted HTTPS destination is not queried with the authentication token.
- Attachment size is reported in MB rounded to two decimal places, but threshold comparisons use the underlying byte value.
- The potential savings figure in the log/email assumes every reported attachment over 5 MB could be reduced to 5 MB; it is an estimate, not a guaranteed storage reduction.
- A layer with no returned attachment rows is not necessarily a sign of a problem; Stage 1 and Stage 2 use separate attachment-presence and size-measurement calls.
- TLS/certificate errors can leave results incomplete; the log and email flag TLS failures when they occur.