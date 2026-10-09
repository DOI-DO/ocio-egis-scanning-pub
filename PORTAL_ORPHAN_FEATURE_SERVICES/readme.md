# PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py

**Orphaned Hosted Service Inventory — ArcGIS Enterprise Portal (Portal only)**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Orphan Feature Services](#why-inventory-orphan-feature-services)
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

This tool looks at an ArcGIS Enterprise Portal and its hosting server, and finds map and feature services that are still running on the server but that Portal no longer has a matching catalog entry for.

Think of it like finding files sitting on a hard drive that no longer show up in the file browser — the data is still there, still taking up space and using resources, but nothing in the normal Portal system points to it anymore.

For every service flagged as potentially orphaned, the tool performs two critical safety checks before including it on the report:

1. **Is anything still using it?** It searches every item in Portal and every Web Map's underlying configuration to see if something is still quietly pointing at the orphaned service, even without a formal catalog entry.

2. **Does it still have data?** It directly asks the service whether it responds and whether it's actually holding records.

The result is a spreadsheet (CSV file) meant for a person to review. It is **not** a delete-ready list — it is the starting point for an investigation. The spreadsheet, log file, and email are all labeled with your organization's name, so if you manage multiple Portal environments, you can always tell which one a report belongs to.

**Important:** Every time this tool runs, it performs a **complete** scan and **always** emails the results. There is no "practice run" that skips sending email — make sure the right people are on the recipient list.

## Why Inventory Orphan Feature Services

This scanner is not a duplicate-layer comparison tool. It inventories potential **orphaned hosted FeatureServer and MapServer services** by comparing hosting-server services with Portal's catalog entries of the corresponding type, then checking for indirect Portal references and service data. The reason this inventory is needed at all is that the service running on the hosting server and the Portal item that is supposed to represent it are two separate objects, linked only by a reference that can break. When that link breaks, the service does not stop — it simply disappears from Portal's view while continuing to run, and because the normal tools an administrator uses only show what Portal's catalog knows about, the orphan becomes invisible to exactly the people responsible for managing it. The only way to find it is to step outside Portal's catalog and compare the hosting server's actual service list against what Portal claims to have, which is what this scan does.

An "orphaned hosted service" is a published map or feature service still running on the server, consuming memory, storage, and processing power — but Portal's catalog has lost track of it. In a healthy system, every published service has a matching Portal item that controls who can see it, who owns it, and how it's described. When that link breaks, the service keeps running as a "ghost" — invisible to the normal tools an administrator would use to manage content, yet still very real on the server. The scan deliberately compares **FeatureServer to `Feature Service` items and MapServer to `Map Service` items**. A matching service of the wrong type does not make the service safe; type is part of the orphan test.

This particular scan is worth running because detecting an orphan is deceptively hard to do correctly, and OTHER_ORGng it carelessly is dangerous. A naive comparison — "this service has no Portal item, so it's abandoned" — would produce a list that is both incomplete and unsafe to act on, because a service can have lost its formal catalog item while still being actively used: a Web Map, app, or dashboard may point directly at the service's URL without any catalog entry tying them together. This tool is built to resolve that ambiguity rather than paper over it. It cross-checks each candidate against Portal item references and the underlying JSON of every Web Map, and it directly queries the live service to see whether it responds and holds records, so the report distinguishes a genuinely abandoned ghost from one that something still depends on. That verification is the whole point: an orphan inventory is only useful if you can trust that a flagged row is a real candidate and not a trap that will break a working map when deleted.

Running this as a periodic inventory matters for two reasons that compound over time. First, orphans are produced by exactly the interrupted or partial operations an organization routinely performs — an item deleted but not its service (or the reverse), a failed publish or update, a service created directly on the server, or a migration that moved some things but not all — and a batch of orphans appearing together often traces to one failed operation worth fixing at its source, which only becomes visible when you inventory regularly and watch the trend. Second, there is a real security dimension: an orphaned service still has a live, reachable web address but sits outside Portal's sharing and permissions system, so it is easy to overlook when reviewing who can access what, even though it may still hold sensitive data. A recurring scan turns both the wasted-capacity problem and the unmanaged-exposure problem into something visible and accountable. Because the tool is strictly read-only, verifies before it reports, and deliberately keeps indeterminate results visible rather than guessing, it is safe to run regularly as a standing check — producing a trustworthy triage list without ever being the thing that deletes anything.

## Why it's a problem

An orphaned service is real infrastructure with no one officially accountable for it. It still uses server resources and may still be reachable over the network, but it doesn't show up in Portal's content lists, can't be managed through the usual sharing and ownership tools, and is easy to lose track of entirely. Over time, unnoticed orphans accumulate and quietly consume server capacity that the organization can't easily account for.

These situations typically happen because of an interrupted process — an item was deleted but the service behind it wasn't (or the reverse), a publish or update job failed partway through, someone created a service directly on the server instead of through Portal, or a migration moved some things but not everything. In each case, something is running with no clear record of why.

There's also a **security angle**. A service with no Portal item still has a live, reachable web address. Because it isn't part of Portal's normal sharing and permissions system, it may be easier to overlook when reviewing who can access what — even though it could still contain sensitive information. Cleaning these up reduces both wasted resources and unnecessary exposure.

## Why removal requires caution

Just because a service has lost its Portal item doesn't mean it's safe to delete. Some orphaned services are still being actively used — most commonly, a Web Map or another Portal item may be pointing directly at the service's address even without a formal catalog entry. Deleting a service that's still in use would break the maps, apps, or dashboards depending on it, often without warning.

That's exactly why this tool doesn't stop at "no matching item" — it actively checks Portal item references, Web Map data, service configuration, and live service behavior before the candidate is included in the report.

Even then, a reported row remains a **review candidate**, not proof that the service can be deleted. The report intentionally keeps an indeterminate result visible when association or data checks cannot be fully established.

## Recommendations for investigation

Treat this report as a checklist for further review, not as a "delete these now" list. Before removing anything:

- **If `ReferencingPortalItems` is not blank**, the service is still referenced by a Portal item or Web Map discovered by this scan. Track down the referencing item and update or retire it first.

- **If `DataCheck` shows `HasData (count=N)`**, the service responded and its first FeatureServer layer contained records. Confirm the data is backed up and understand whether other layers also contain data before deleting.

- **If `DataCheck` shows an `Error`**, the service may be broken or half-deleted already — but it could also be a temporary outage, certificate problem, permission issue, or other transient condition during the scan. Check it manually rather than assuming it's dead.

- **`Empty`, `NoLayers`, or `Responds` with no referencing items** are useful candidates for deeper review, but they are still not automatic deletion instructions. Check publish history, server logs, operational dependencies, and ownership before taking action.

- If you see a batch of orphans that all appeared around the same time, they may trace back to one failed operation (a bad migration, a broken publish job) that is worth fixing at the source.

- **If a service has an `item_id`**, review that Portal item directly. A stale or wrong-type catalog relationship can be important context even when the service was retained as an orphan candidate.

- **If the run is `INCOMPLETE`**, do not interpret the reported orphan count as a complete answer. Review the error reasons in the log and email and rerun after the missing coverage is resolved.

Only after this review should any row be handed off to a deletion tool.

## Column descriptions

The report is a spreadsheet (CSV file) that can be opened directly in Excel. Each row represents one hosted `FeatureServer` or `MapServer` that remained a report candidate after same-type Portal matching and association checks. The CSV columns are emitted in this exact order:

| Column | What it means |
| --- | --- |
| `ServiceName` | The service's name on the hosting server, including its folder if it's in one (e.g., `MyFolder/Trails`). This is the server-side service name, not necessarily the title shown in Portal. |
| `ServiceType` | Either `FeatureServer` or `MapServer`. The orphan comparison is type-specific: a `FeatureServer` is compared with Portal `Feature Service` items, and a `MapServer` is compared with Portal `Map Service` items. |
| `service_url` | The public hosting-server service URL used for reporting and data checks. The Portal token is only sent to validated HTTPS hosting destinations established from federation data. |
| `item_id` | The service's `serviceItemId`, when the service definition supplies one and it is valid. The value may refer to a stale, missing, wrong-type, or otherwise unresolved Portal item. Blank when the service did not return a usable service item ID. |
| `date_created` | The associated Portal item's creation date, when that item could be resolved and matched to the service type. Format: `YYYY/MM/DD`. Blank otherwise. |
| `date_modified` | The associated Portal item's last-modified date, when that item could be resolved and matched to the service type. Format: `YYYY/MM/DD`. Blank otherwise. |
| `ReferencingPortalItems` | A semicolon-separated list of Portal items or Web Maps found to reference the service identity, shown as `Title (itemId) [Type]`. A non-empty value is an important usage signal. Blank means no matching reference was found in the scanned Portal catalog or Web Map JSON. |
| `DataCheck` | The direct service check result. For a `FeatureServer`, this is `HasData (count=N)` when the first listed layer responds with a positive count, `Empty (count=0)` when that first layer has no records, or an `Error: <message>` when the check fails. For a `MapServer`, it is `Responds (N layers)`, `NoLayers`, or `Error: <message>`. When the service-item association was indeterminate, the text is prefixed with `INDETERMINATE ITEM ASSOCIATION;`. |

### What is not in the CSV

The run summary and email contain additional diagnostic counts that are not CSV columns, including the number of Portal catalog items, hosting services, URL-orphan candidates, same-type `serviceItemId` matches removed, reported orphan rows, Web Map errors, data-check errors, item-detail errors, catalog warnings, TLS/CERT errors, authentication errors, transport errors, throttling/transient errors, and indeterminate service-item associations.

`SYSTEM` is also not a CSV row type. System/Esri-owned Portal content is excluded from the orphan report and is accounted for separately during candidate resolution.

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
- `auth.py`
- `secure_session.py`

## Requirements

- Access to an ArcGIS Enterprise Portal with **organization-wide item visibility** and permission to read the Portal federation/hosting-server information.
- Python with the ArcGIS API for Python and `requests`.
- A password stored in OS keyring (service = `--keyring-service`, user = `--username`), or exported as `PORTAL_PASSWORD` environment variable.
- The shared `UTILITIES` package reachable from the project layout.
- The org-specific `--config` TOML file.
- Read access to the Portal hosting server admin/service endpoints and the network path(s) used by the authenticated Portal federation configuration.

**Portal only.** This script requires an Enterprise Portal hosting server; it does not support ArcGIS Online and there is no `--target` flag.

## Security: Certificate (TLS) Validation

This script **always verifies TLS certificates**. There is no option to disable certificate verification.

Two connections are involved:

1. **The ArcGIS SDK connection** used for Portal authentication uses the selected verification mode through `connect_gis()`.
2. **The direct REST sessions** used for Portal catalog, federation, hosting-server, service, and Web Map requests use the same selected TLS strategy through `build_secure_session()`.

The available modes are:

- `standard` (default) — verifies against the bundled/public CA trust source.
- `os-trust-store` — verifies against the operating-system certificate store.
- `custom-ca-bundle` — verifies against the PEM CA bundle supplied with `--ca-bundle-path`.

Before the scan begins, `verify_tls_handshake()` performs a one-time connection check. If certificate validation fails, the inventory aborts before normal scanning starts.

### SSRF / network-boundary defenses

The script applies explicit URL policy before direct REST requests:

- Credential-bearing requests require HTTPS.
- Portal API paths are constrained to `/sharing/rest/` or `/portaladmin/`.
- Hosting admin URLs must be within the authenticated federation registry's hosting-server administrative roots.
- Hosting service URLs must be within the authenticated hosting-server federation roots.
- URL user-info, query strings, fragments, encoded slashes/backslashes, control characters, and path traversal are rejected.
- Organization-wide user/item discovery uses validated organization identifiers and strict pagination/result checks.
- Portal/hosting URLs on different hosts are supported when those hosts are established by the authenticated federation registry.
- A hosting-server redirect cannot downgrade a token-bearing request from HTTPS to HTTP.

### Token handling

Tokens are held through the shared `TokenProvider` and are exposed only when an outgoing authenticated request is constructed. Caller-supplied `token` and `f` parameters are removed and replaced with controlled values.

An authentication failure can trigger one synchronized token refresh/retry. Error messages and diagnostics are sanitized through `redact_sensitive_data()` before they are used in logs, email, or other report text.

### Large / malformed responses

Direct REST responses are streamed and limited to **64 MB**. JSON is decoded and checked before use. Search results validate result-array shape, totals, pagination progress, duplicate IDs, and other required fields. Portal catalog discovery is date-bucketed so a result set reaching the 10,000-item search cap is recursively subdivided instead of silently truncated.

Hosting catalog folder/service records, federation server records, service definitions, user records, and other REST objects are checked for expected types before the data is used.

### Redirect following is explicitly disabled

HTTP requests use `allow_redirects=False`. Redirects are handled explicitly by the script rather than being followed automatically.

A redirect is accepted only after validation against the same security policy. Portal redirects must remain on the authenticated Portal origin and approved API path. Hosting admin redirects must remain under an authenticated federation admin root. Hosting service redirects must remain under an authenticated service root and cannot downgrade a token-bearing request to HTTP. The maximum number of redirects is 3.

### Python import-path / package shadowing

The script walks upward from its own location to find the expected `UTILITIES` package, inserts that project root at the front of `sys.path` when it is not already present, locates the `UTILITIES` package with `importlib.util.find_spec()`, and verifies that the package origin matches the expected `UTILITIES` directory before importing it.

**Equivalent duplicate copies of the project root are not removed from `sys.path` in this version.** The implemented protection is package-origin verification, not complete `sys.path` de-duplication.

## Configuration

The organization is always Portal. The required `--config <org_toml_path>` file supplies the Portal connection, credential, email, and output settings.

Required settings:

- `portal-url`
- `username`
- `keyring-service`
- `output-dir`
- `smtp-server`
- `email-to`
- `email-from`

`max-email-mb` is not resolved by this script. The built-in 20 MB value is passed to the shared attachment helper and cannot be changed with this script's CLI.

### Example configuration

```toml
[common]
org-name = "ORG"
output-dir = "C:\\temp\\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "sender@example.com"

[portal]
portal-url = "https://portal.example.com/portal"
username = "portal_service_account"
keyring-service = "PORTAL"
```

Store the Portal password in the OS keyring rather than in TOML:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
```

A `PORTAL_PASSWORD` environment variable can be used as the configured/shared authentication fallback.

### Configuration precedence

1. Command-line value.
2. `[portal]` configuration value.
3. `[common]` configuration value.
4. Shared/script default where applicable.

### Output location resolution

The output directory comes from the command line or configuration. The script creates the directory if necessary and reserves a unique CSV/log filename pair before scanning begins.

## How it works

1. **Establish verified TLS sessions and connect to Portal.** The script builds a secure direct REST session, verifies the TLS handshake, and authenticates through the ArcGIS SDK using the same trust strategy.
2. **Validate the authenticated Portal and inventory principal.** The Portal URL is normalized and the authenticated GIS URL is checked. The account must be a standard administrator or have the required organization-wide item visibility privilege before orphan classification can proceed.
3. **Read federation data.** The script identifies the `HOSTING_SERVER` record and establishes its public, administrative, and service URL roots. Multiple distinct hosting servers cause the run to abort instead of choosing one arbitrarily.
4. **Build the complete Portal catalog.** Portal `Feature Service`, `Map Service`, and related items are enumerated through strict date-bucketed searches. The catalog is stored in a temporary SQLite database so the full item payload is not required to remain in normal Python memory.
5. **Index Portal service identities.** Service URLs are reduced to stable service-path/type identities. Authoritative matches use authenticated hosting-server roots; other same-path references are retained for reporting and ambiguity checks.
6. **Enumerate hosted services.** The authenticated hosting server's admin catalog is walked recursively, skipping `System` and `Utilities`, and every `FeatureServer` and `MapServer` is collected.
7. **Select orphan candidates.** A service becomes a candidate when no same-type Portal item is authoritatively matched to its service identity.
8. **Scan Web Maps.** Every Portal Web Map's data is read in parallel. Operational layers, basemap layers, and nested layer groups are inspected for matching hosting-service identities.
9. **Verify each candidate.** Candidate services are queried through each validated hosting-server alias until one succeeds. The service definition's `serviceItemId` is then checked against the complete Portal catalog or by a direct Portal item lookup when needed.
10. **Run the service data check.** For FeatureServer services, the first listed layer is queried with `returnCountOnly=true`. For MapServer services, the service root response and number of listed layers are reported.
11. **Write the report and send email.** One row is written for each retained candidate. The CSV and log are then handled through the shared email attachment policy and the completion email is sent.

### Same-type `serviceItemId` verification

A service's `serviceItemId` is authoritative when it identifies an existing Portal item of the expected type whose normalized service identity matches the service being checked. Those confirmed same-type matches are removed from the final orphan report.

When the item association cannot be established completely, the original orphan-candidate reporting behavior is preserved but the run is marked `INCOMPLETE` and the `DataCheck` is prefixed with `INDETERMINATE ITEM ASSOCIATION;` where applicable.

### Data-check behavior

The data check intentionally preserves the original service-level contract:

- **FeatureServer:** only the first listed layer is queried for a count.
- **MapServer:** the service root is checked and the number of layers is reported.

A multi-layer FeatureServer can therefore report `Empty (count=0)` when its first layer is empty even if another layer contains records.

## Test mode (default)

**This script does not have a test mode.** There is no `--test` or `--no-test` flag. Every run performs the full Portal catalog scan, the full hosting-server service scan, the complete Web Map reference scan, and the candidate verification/data-check phases. Every run writes a report and sends the completion email.

Because the scan is read-only, the safest validation approach is to use an appropriate non-production or controlled Portal/content environment, or to review the resulting CSV and log carefully after the first run.

## Usage

**Scenario: "I want to run the standard orphan scan for this Portal using the configured settings."**

This is the normal scheduled or administrative use case. The configuration file provides the Portal URL, credentials, output directory, and email settings.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml
```

**Expect:** A complete inventory of Portal catalog items, hosting-server FeatureServer/MapServer services, Web Map references, service associations, and service data checks. The CSV and log are written and the completion email is sent.

**Scenario: "I manage multiple Portal organizations and need to run this scan against a different org."**

Use the organization-specific TOML file for the Portal you want to inspect.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config OTHER_ORGLDT.config.toml
```

**Expect:** The Portal URL and credentials come from that organization configuration, and the resulting filenames/email subject use the configured organization name.

**Scenario: "I want this run's CSV and log written to a specific location for a one-off review."**

Use `--output-dir` to override the configured output directory for one run.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --output-dir "D:\reports"
```

**Expect:** The scan scope does not change; only the report/log destination changes.

**Scenario: "This Portal has a very large Web Map catalog and the scan is taking a while — I want to speed it up."**

The Web Map reference scan is concurrent. Raise `--workers` when the Portal and client system have sufficient headroom.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --workers 24
```

**Expect:** More Web Map requests can run concurrently during that phase. This increases load as well as potential throughput.

**Scenario: "The hosting server is under load and I want to reduce concurrent requests."**

Lower `--workers` to reduce the number of concurrent Web Map requests.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --workers 8
```

**Expect:** The same full scan runs, but the Web Map reference phase uses fewer concurrent worker threads.

**Scenario: "Our internal certificate authority is not in the public trust store and the default TLS check is failing."**

Use the operating-system trust store when the organization's CA is already installed and trusted there.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --tls-mode os-trust-store
```

**Expect:** Both the TLS handshake check and subsequent HTTP/SDK connections use the OS trust store. Certificate validation remains enabled.

**Scenario: "I have a specific certificate file to validate against."**

Supply the PEM bundle explicitly.

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config ORG.config.toml --tls-mode custom-ca-bundle --ca-bundle-path /path/to/ca.pem
```

**Expect:** The supplied PEM CA bundle is used for certificate verification. `--ca-bundle-path` is required with `custom-ca-bundle`; otherwise argument validation fails.

**Scenario: "I just need to confirm which version is deployed or see the available options."**

```bash
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --help
python PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --version
```

**Expect:** `--help` displays the current options without scanning. `--version` prints the script version and exits.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | *(required)* | Path to the organization-specific TOML file. |
| `--portal-url` | from config | Enterprise Portal URL. |
| `--username` | from config | Portal login username. |
| `--keyring-service` | from config | Keyring service containing the Portal password. |
| `--smtp-server` | from config | SMTP relay for the completion email. |
| `--email-to` | from config | Recipient or comma/semicolon-separated recipient list. |
| `--email-from` | from config | Sender address. |
| `--output-dir` | from config | Output directory for the timestamped CSV and log. |
| `--workers` | `16` | Concurrent Web Map readers. Valid range: `1-64`. |
| `--tls-mode` | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | `None` | PEM CA bundle. Required when `--tls-mode custom-ca-bundle`. |
| `--version` | — | Print the script version and exit. |

**There is no `--target` option.** This is a Portal-only script and hard-codes its target as Portal.

## Performance

Two parts of the scan are intentionally concurrent:

- **Web Map reference scanning.** Every Web Map is read through a worker pool so service URLs embedded in operational layers, basemap layers, and nested layer groups can be indexed concurrently.
- **Orphan association lookups.** Service definitions for orphan candidates may be tried through multiple validated hosting-server aliases, and the request work uses the worker-session pool where applicable.

Portal item enumeration uses date-range bucketed search so organizations with more than 10,000 searchable items do not silently lose catalog entries at the standard Portal result cap.

The script also bounds work and data volume with safety ceilings for Portal items, hosting services, hosting folders, folder depth, and JSON response size.

Increasing `--workers` may shorten Web Map scanning on larger Portals. Lower it when the hosting server or client host shows timeouts, throttling, or resource pressure.

## Observed Runtime

| Run type | Observed duration | Number of hosted services scanned |
|----------|-------------------|-----------------------------------|
| Portal   | ~5-10 minutes     | ~5,500                            |

Actual runtime varies with Portal size, Web Map count, service count, service-alias responses, network latency, and the `--workers` setting.

## Output file

Two primary files are written to the resolved output directory. Their names use the organization prefix and `YYYYMMDD_HHMM` timestamp. If a collision occurs, a numeric suffix is added to the timestamp so the CSV and log remain a matched pair.

```text
[ORG]_ORPHAN_HOSTED_SERVICE_<YYYYMMDD_HHMM>.csv
[ORG]_PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES_<YYYYMMDD_HHMM>.log
```

For example:

```text
ORG_ORPHAN_HOSTED_SERVICE_20261002_1308.csv
ORG_PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES_20261002_1308.log
```

The CSV is UTF-8 with a BOM (`utf-8-sig`) and contains one header row followed by the retained orphan-candidate rows.

A header-only CSV is still written when no rows remain, so downstream tooling has a predictable file to open.

Both the CSV and log are included in the completion email subject to the built-in **20 MB total attachment ceiling**. If the attachment ceiling is exceeded, the shared attachment helper may create a ZIP when appropriate; the original files remain on disk.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The inventory completed with no recorded diagnostic errors and produced the report. |
| `1` | The run was `ABORTED`, or the completed inventory was `INCOMPLETE` because one or more Web Map, service-data, item-association, catalog, TLS, authentication, transport, or other diagnostic errors were recorded. The completion email is still sent. |
| `2` | Invalid invocation/configuration, including an Enterprise Portal/platform mismatch, missing required output/content setting supplied by configuration helpers, or missing `--ca-bundle-path` when `custom-ca-bundle` is selected. Argparse also uses `2` for unrecognized arguments. |

An `INCOMPLETE` result does not automatically remove reported rows; it means one or more checks were indeterminate and the report must not be treated as a complete orphan determination.

## Notes and caveats

- **Hosting server only.** Only the federated server identified as `HOSTING_SERVER` is inventoried. Other federated servers are excluded.
- **Portal only.** ArcGIS Online is not supported and there is no `--target` flag.
- **Full scan only.** There is no test mode or dry-run flag. Every invocation scans the configured Portal/hosting environment and sends the completion email.
- **Review before deleting.** The CSV is a triage list, not a delete list.
- **Type-specific matching matters.** A `FeatureServer` is matched against `Feature Service` Portal items, and a `MapServer` is matched against `Map Service` items.
- **`serviceItemId` verification is authoritative when confirmed.** Existing same-type Portal matches are removed from the orphan set.
- **Indeterminate association does not automatically discard the candidate.** The original candidate reporting behavior is retained, while the run becomes `INCOMPLETE` and the data check is marked with `INDETERMINATE ITEM ASSOCIATION;` where applicable.
- **`ReferencingPortalItems` is evidence of usage, not proof of ownership.** A non-empty value means the scan found a Portal item or Web Map with the same normalized service identity.
- **First layer only for FeatureServer data checks.** `DataCheck` does not query every layer of a multi-layer FeatureServer.
- **MapServer checks report layer count only.** `Responds (N layers)` confirms the service root responded and listed N layers; it does not prove every layer is healthy or empty.
- **Point-in-time snapshot.** Portal catalog results, Web Map references, service definitions, and data checks reflect the moment of the run.
- **Portal catalog completeness is guarded.** A bucket that reaches the 10,000-result search cap is subdivided; if completeness cannot be established, the run aborts instead of silently truncating.
- **Large responses are bounded.** Direct REST JSON is limited to 64 MB.
- **Redirects are explicitly controlled.** Automatic redirect following is disabled; validated redirects may be processed only within the configured security boundaries, with a maximum of three redirects.
- **TLS verification is always on.** Both the ArcGIS SDK and direct REST sessions use verified certificate handling.
- **`os-trust-store` may require the shared `truststore` package.** Use it only where the operating-system certificate store is the intended trust source.
- **The Portal token is protected.** It is not intentionally emitted to log/email/CSV text, and token-bearing diagnostics are redacted.
- **System/Esri content is excluded from the report.** Those records can be counted during reconciliation but are not emitted as orphan rows.
- **Authenticated visibility matters.** The script requires an organization administrator or custom role with sufficient organization-wide item visibility before it will classify orphan services.
- **Federation aliases are intentional.** The hosting server can expose public, admin, and direct-server URLs on different hosts; URLs reported by the authenticated federation registry are supported within the explicit trust policy.
- **No equivalent `sys.path` entries are removed.** The script inserts the expected project root only when absent and verifies the actual `UTILITIES` package origin before import.
- **The output attachment limit is built in for this script.** The script does not resolve `max-email-mb` from TOML or CLI.
- **Email is always attempted.** A scan result and its email notification are separate concerns; the local CSV/log remain the primary record if email delivery is unavailable.