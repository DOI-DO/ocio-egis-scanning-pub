# SCAN_BROKEN_HOSTED_SERVICES.py

**Broken Feature Service Scan — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

This script checks every hosted, federated, and registered feature service it can find and reports the ones that are not working. It is read-only: it never repairs, restarts, unshares, modifies, or deletes anything.

---

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Feature Services](#why-inventory-feature-services)
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

This report lists feature services (map layers published to ArcGIS Online or Portal) that are not currently working. The scan checks four simple things for each service:

1. Does the service respond at all?
2. Does it contain at least one layer or table?
3. Does every layer and table work correctly?
4. Can a simple "how many records do you have" query succeed?

If the answer to any question is "no," the service appears in this report. Services that pass all checks are **not** listed — they only show up in the summary counts in the email.

Services are classified by where they live:

- **hosted** — stored directly in the organization's own hosting infrastructure.
- **federated** — running on a separate ArcGIS Server connected to Portal.
- **registered** — pointing at an external ArcGIS Server not part of your core infrastructure.

> **Important:** This is a leads list, not definitive proof of a defect. A temporarily slow service, one under maintenance, or one restricted to certain accounts can look identical to a genuinely broken service in this report. Treat every row as "worth a second look," not "ready to delete."

## Why Inventory Feature Services

The central reason to run this scan is that a broken feature service almost never reports itself. The service keeps its catalog item, its title, its thumbnail, and its sharing settings whether or not it still works, so from the catalog everything looks normal right up until someone opens the map that depends on it. The failure is discovered downstream — by an end user hitting a blank map, a dashboard showing no data, or a field crew unable to collect — rather than by anyone looking at the service itself. Because a feature service is typically a shared building block that many maps, apps, and workflows draw on, a single broken service radiates failures outward to everything built on top of it, and those dependents fail in ways that look unrelated. An inventory inverts that discovery process: instead of learning about breakage one user complaint at a time, you proactively enumerate the broken services first and fix the shared cause once.

This particular scan is worth performing because feature services break silently and for many different reasons, and no single surface shows all of them. A publishing job can be interrupted partway through, a data store can go offline, source data can be moved or renamed out from under a layer, a federated server can drop out, an external registered provider can change or retire an endpoint, or a routine update can damage a service that worked yesterday. None of these announce themselves in the Portal item, and several of them — especially data-source and automation problems — produce a service that exists, lists cleanly, and only fails when actually queried. By exercising each service the way a real client would (reading the service root, reading every layer and table definition, and running an actual count query), the scan reaches the failure that passive catalog browsing cannot see. It also spans hosted, federated, and registered services in one pass, so problems that live on a separate server or an external provider are surfaced alongside local ones rather than falling into a blind spot.

Running this as a regular inventory rather than a reactive investigation changes the economics of reliability. A broken service is not free while it sits unnoticed: it is still indexed, backed up, and carried through migrations, and the way it fails — hanging until timeout, consuming connections and retries — can be more expensive than a service that fails fast. A periodic scan catches these before they accumulate and before a user reports them, and it turns a confusing multi-symptom outage into a grouped, diagnosable list. Because each row carries its source, scope, owner, dates, error text, and a permission-issue hint, the report supports root-cause triage directly: a cluster of federated failures points at one server, a cluster under one owner points at a broken automated publishing job, and a recently-modified-then-broken service points at a bad update. The scan is deliberately read-only and over-inclusive — a leads list, not a delete list — so it is safe to run often as a standing availability check, giving administrators current evidence about which of their shared services are actually serving data rather than merely appearing to.

## Why It's a Problem

- **They still consume resources.** A broken service is still indexed, listed, backed up, and maintained — it costs almost as much as a working one.
- **They create expensive failures.** Timeouts and retries consume server connections and time. A service that hangs ties up resources far longer than one that fails fast.
- **They break things downstream.** A single broken service can silently break every map, app, dashboard, or workflow that depends on it. Fixing the service once is more efficient than troubleshooting each dependent item separately.
- **They slow down maintenance.** Backups, migrations, and catalog maintenance still have to deal with broken services, which can delay routine operations.
- **They can signal bigger problems.** Repeated failures can point to an interrupted publishing job, broken automation, a data-store issue, or a server that's gone offline.

## Why Removal Requires Caution

This report is a starting point, not a delete checklist. The same-looking failure can have very different causes:

- **It might just be temporarily unavailable.** The scan uses a 30-second timeout, so a slow or restarting service can appear broken even though it recovers moments later.
- **A `Likely_Permission_Issue = TRUE` flag may only mean the scanning account lacks access** — not that the service is broken for everyone else. Confirm using an account with proper access.
- **A registered service may belong to someone else.** Deleting the Portal item only removes the local reference — it doesn't fix the remote service.
- **A cluster of federated failures may point to a server outage,** not multiple bad services. Deleting items would hide the real infrastructure problem.
- **The catalog item and the underlying service are two different things.** An item can look perfect while its service has stopped working.
- **A single failure is weaker evidence than repeated failures.** Re-run the scan before concluding a service is permanently broken.

Use the `Scope`, `SOURCE`, `error`, `Likely_Permission_Issue`, item dates, and owner columns together to understand what happened before taking action.

## Recommendations for Investigation

- **Double-check manually, or re-run the scan.** A service that fails consistently across multiple runs is a much stronger candidate for action.
- **Group rows by `service_url` and `Scope`.** This tells you whether the whole service is down or just specific layers/tables within it.
- **Set permission-flagged rows aside for their own review.** Filter on `Likely_Permission_Issue` and check whether the scanning account's login token might have expired mid-run.
- **Group by `SOURCE`.** A cluster of `federated` failures may point to one server being down; `registered` failures may point to an external provider's problem.
- **Group by `owner`.** If the same person owns many broken services, that often points to a broken automated publishing job rather than unrelated problems.
- **Check the creation and modification dates.** A recently modified service that's now broken may have been damaged by a failed update. An old, untouched failure may simply be an abandoned reference nobody cleaned up.
- **Ask about interrupted publishing or automation.** Scheduled jobs that refresh a service can leave it broken if stopped partway through.
- **Ask about the underlying data source.** A failed layer can reflect missing data, corrupted data, or source data that was moved or renamed.
- **Coordinate with the service owner and anyone who depends on it** before repairing or retiring anything.

When a run is marked **INCOMPLETE**, do not interpret the absence of a service from the broken-service CSV as proof that it is healthy. Review the listed discovery, transport, TLS, authentication, policy, and indeterminate-check reasons in the email and log.

## Column Descriptions

Each row represents one failed check — not necessarily one broken service. A service with several failed layers produces multiple rows.

| Column | Description |
|---|---|
| `platform` | `PORTAL` or `AGOL`. |
| `SOURCE` | Where the service lives: `hosted`, `federated`, or `registered`. |
| `title` | The service item's name, or a name derived from the URL if no item exists. |
| `item_id` | The catalog item's unique ID, or blank if there's no item. |
| `service_url` | The web address of the service that was checked. |
| `owner` | The username of whoever owns the item, or blank if there's no item. |
| `date_created` | The date the item was created (`YYYY/MM/DD`), or blank. |
| `date_modified` | The date the item was last modified (`YYYY/MM/DD`), or blank. |
| `Scope` | How specific the failure is: `SERVICE` (the whole service failed), `LAYER`, or `TABLE` (a specific piece of it failed). |
| `layer_name` | The name of the specific layer or table that failed. Blank for service-wide failures. |
| `layer_id` | The numeric ID of the specific layer or table that failed. Blank for service-wide failures. |
| `layer_url` | The web address of the specific layer/table, or the service's address for service-wide failures. |
| `error` | A sanitized description of what went wrong — an error message, timeout, TLS/certificate problem, URL-policy block, authentication problem, or other diagnostic information. |
| `Likely_Permission_Issue` | `TRUE` or `FALSE`. A best-guess flag based on the error text, meant to help prioritize review. Permission-flagged rows are still included in the report — this is never used to exclude results. |

---

# For Administrators

## Dependencies on the Shared UTILITIES Package

- `auth.py`
- `bucketing.py`
- `cli.py`
- `config.py`
- `emailer.py`
- `exclusions.py`
- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `secure_session.py`
- `stack_discovery.py`

## Requirements

- Python with the ArcGIS API for Python (`arcgis`) and `requests` installed.
- The shared `UTILITIES` package reachable from the expected project directory.
- An organization-specific TOML configuration file (`--config`).
- A `portal` or `agol` target selected with `--target`.
- Required settings: `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, and `email-to` / `email-from` as supplied by the shared configuration helpers.
- A Portal/AGOL account that can search the organization and inspect feature-service items.
- Network access to Portal/AGOL, relevant hosting/federated/registered services, and the SMTP relay.
- Write access to the output directory.
- Optional: `truststore` when using `--tls-mode os-trust-store`.

## Security: Certificate (TLS) Validation

Every authenticated Portal/AGOL HTTP request made by this script uses TLS, and certificate validation is enabled. There is no option to disable certificate verification.

Three verification strategies are available through `--tls-mode`:

- **`standard`** (default) — validates against the standard public CA bundle.
- **`os-trust-store`** — validates against the operating system trust store; requires the `truststore` package.
- **`custom-ca-bundle`** — validates against a specific PEM CA bundle supplied through `--ca-bundle-path`.

Before scanning begins, the script performs a real TLS handshake against the configured Portal/AGOL URL using the selected strategy. A failed handshake aborts the run rather than continuing with an unverified connection. The ArcGIS SDK connection established through `connect_gis()` is also TLS-verified.

### SSRF / network-boundary defenses

The script treats credential delivery and anonymous service probing separately.

For **authenticated** service requests, the destination must be HTTPS and must be inside an approved trust boundary:

- the configured Portal/AGOL origin;
- authenticated Portal federation origins established from the Portal's federation metadata;
- an exact operator-supplied `--trusted-service-origin`; or
- an AGOL `*.arcgis.com` destination on HTTPS.

For **untrusted, external, private, internal, or plain-HTTP service URLs**, the scanner still performs a health check, but it does so anonymously. This allows legitimate public registered services to be checked without sending the organization's token to them.

An exact `--anonymous-service-origin` can additionally force a destination to remain anonymous even when it would otherwise qualify for authenticated access.

Candidate service URLs are validated before requests are made. Path traversal, encoded slash/backslash tricks, user-info credentials, query strings, fragments, invalid hosts/ports, and unsupported service URL forms are rejected.

Federated catalog traversal is restricted to federation roots reported by the authenticated Portal. Catalog redirects are manually validated and cannot change the approved authentication policy.

### Token handling

The organization token is supplied by the shared `TokenProvider`. It is added only at request construction time for requests classified as authenticated.

Recognized authentication failures such as HTTP 401, 403, 498, 499, invalid-token, expired-token, and token-required responses trigger one controlled token-refresh/retry path under a lock. Anonymous service requests never receive the organization token.

Error text is passed through `redact_token()` before logging, email, or report output.

### Large / malformed responses

The direct REST client enforces a **64 MB maximum JSON response size**. It checks the declared `Content-Length` when present and also enforces the limit while streaming the response body, preventing an unexpectedly large response from being loaded without a bound.

The client also validates that JSON responses are objects, that REST error structures are handled explicitly, and that expected search, federation, catalog, service-root, layer, and count structures have the required shapes. Malformed responses are treated as errors or indeterminate checks; they are not silently interpreted as healthy services.

Federated catalog walking also has explicit limits of **100,000 catalog directories** and **16 levels of catalog depth**.

### Python import-path / package shadowing

The script determines the expected project root, inserts it into `sys.path` before importing `UTILITIES`, and verifies that the imported package was resolved from the expected `UTILITIES` directory. This protects against a different `UTILITIES` package being silently selected from another location.

The current script does **not** remove equivalent path spellings from `sys.path` before inserting the expected root. That stronger cleanup step is not implemented here.

### Redirect following

Redirect following is explicitly disabled on direct REST requests with `allow_redirects=False`. Redirect targets are resolved and validated manually. The client allows at most three redirects and requires the redirect destination to preserve the same authenticated or anonymous security policy.

## Configuration

A specific organization is selected with the required `--config` argument and the required `--target portal` or `--target agol` argument.

The shared configuration supplies the connection, output, and email settings. Command-line overrides take precedence over configuration values.

Typical settings include:

```toml
[portal]
portal-url = "https://portal.example.com/portal"
username = "portal_service_account"
keyring-service = "PORTAL"
output-dir = "C:\\reports"
smtp-server = "smtp.example.com"
email-to = "gis-admin@example.com"
email-from = "gis-reports@example.com"

[agol]
portal-url = "https://example.maps.arcgis.com"
username = "agol_service_account"
keyring-service = "AGOL"
output-dir = "C:\\reports"
smtp-server = "smtp.example.com"
email-to = "gis-admin@example.com"
email-from = "gis-reports@example.com"
```

The optional `--all-owners` flag changes the search from organization-scoped to all reachable feature-service items. This is deliberately broader and should be used only when that inventory is required.

`--sources` controls the discovery categories `hosted`, `federated`, and `registered`. The default is all three. AGOL has no federated source, so `federated` is removed automatically when AGOL is selected.

`--exclude-group-id` resolves group memberships before scanning. Group exclusions remove item-backed discoveries by item ID and matching service URLs from excluded group members.

The email attachment limit is resolved from the organization configuration through the shared `get_max_email_mb()` helper; there is no script-level `--max-email-mb` option.

## How It Works

1. Parse arguments, resolve configuration, create the output directory, and start logging.
2. Determine the platform from the configured Portal/AGOL URL and validate it against `--target`.
3. Build a TLS-verified `requests.Session` using the selected `--tls-mode` and perform the startup TLS handshake check.
4. Connect to the GIS through `connect_gis()` and acquire the shared token provider.
5. Build the trusted Portal/federation/service destination policy.
6. Resolve requested group exclusions.
7. Discover Portal federation servers when running against Enterprise Portal.
8. Enumerate feature-service items from organization-scoped search unless `--all-owners` is selected.
9. Walk federated ArcGIS Server REST catalogs when `federated` is enabled.
10. Merge and de-duplicate discovered service targets by normalized URL.
11. Health-check targets concurrently: read the service root, read every reported layer/table definition, and run `returnCountOnly` queries against each layer/table.
12. Preserve every confirmed failed check in the broken-service result. Authentication uncertainty, transient failures, transport failures, and URL-policy failures can make the overall run `INCOMPLETE`.
13. Write the broken-service CSV when failed checks exist.
14. Send the completion email with the log, available report, run diagnostics, and timing.

### Service health rules

A service is considered healthy only when:

- the service root responds successfully;
- its service root contains at least one valid layer or table;
- each reported layer/table definition can be read; and
- each layer/table accepts a `returnCountOnly=true` query and returns a valid count.

A service-root failure produces a `SERVICE` row. A failed layer metadata or count check produces a `LAYER` or `TABLE` row as appropriate.

A response that cannot be conclusively classified can mark the service **indeterminate**. Previously confirmed broken rows are retained even when a later check becomes indeterminate.

### Discovery and completeness

Feature-service item search uses strict date-range bucketing and organization scoping by default. Search totals, pages, item IDs, and organization IDs are validated so the scan does not silently accept malformed or cross-organization data.

Full mode can inspect up to **2,000,000 scan targets**. Reaching that safety ceiling causes the run to abort because a complete inventory cannot be guaranteed.

## Test Mode (Default)

**Test mode is ON by default.** It limits the later scan stages to the first **250 unique scan targets** in the established discovery order.

Use `--no-test` for full mode. Full mode raises the internal target limit to **2,000,000**.

Test mode validates configuration, credentials, TLS verification, discovery, exclusions, health checks, output, and email delivery. It is not a complete inventory.

## Usage

### Scenario 1 — Quick sanity check on a new Portal configuration

You have just created a configuration and want to confirm authentication, TLS trust, discovery, health checks, CSV handling, and email delivery without scanning the entire environment.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal
```

This uses test mode and checks up to 250 unique targets.

### Scenario 2 — Same validation against ArcGIS Online

You want to validate an AGOL configuration before scheduling a full scan.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org2.config.toml --target agol
```

### Scenario 3 — Full Enterprise inventory

The test run succeeded and you need a complete inventory of the configured Portal environment.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal --no-test
```

### Scenario 4 — Full AGOL inventory with more concurrency

The organization has about 20,000 hosted services and you want more parallel health checks.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org2.config.toml --target agol --no-test --workers 24
```

### Scenario 5 — Limit discovery sources

You only need hosted and federated sources during an Enterprise troubleshooting exercise and do not want the registered-service discovery path.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal --sources hosted,federated
```

### Scenario 6 — Focus on federated catalog discovery

You are looking for federated FeatureServer endpoints that may exist in the server catalog even when there is no matching Portal item.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal --sources federated
```

### Scenario 7 — Exclude known groups

A Portal group contains deliberately retired or test items that should not be part of the operational scan.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --exclude-group-id "abc123def456,789ghi012jkl"
```

### Scenario 8 — Review all reachable feature-service items

You need a broader inventory than the normal organization-only search and intentionally want feature-service items owned outside the organization scope used by the default run.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --no-test --all-owners
```

This removes the organization ID clause from item discovery. Use it only when a broader inventory is appropriate.

### Scenario 9 — Override output location and email recipient

A one-off troubleshooting run needs to write to a separate directory and deliver the results to a different team.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --output-dir "D:\\reports" --email-to "service-owners@example.com"
```

### Scenario 10 — Use the operating-system certificate trust store

Your internal Enterprise Portal uses a certificate issued by an organizational CA that is already trusted by the operating system.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --tls-mode os-trust-store
```

### Scenario 11 — Use a custom CA bundle

The scanning host does not have the organization's internal CA in the OS trust store, but you have a PEM bundle file.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --tls-mode custom-ca-bundle \
    --ca-bundle-path "C:\\certs\\internal-ca.pem"
```

### Scenario 12 — Keep a public service anonymous

A registered service is legitimate but should never receive the organization's token, even if its HTTPS origin is also present in the trust configuration.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --anonymous-service-origin https://services.example.com
```

### Scenario 13 — Add an approved HTTPS service origin

A legitimate service is behind a reverse proxy whose exact origin is not present in normal federation metadata. You have independently verified that the origin is authorized to receive the organization token.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --config org1.config.toml --target portal \
    --trusted-service-origin https://services.example.com
```

### Scenario 14 — Show the current command-line interface

You are configuring a scheduled task or troubleshooting an option and want the built-in help text.

```bash
python SCAN_BROKEN_HOSTED_SERVICES.py --help
```

## Command-Line Options

| Argument | Required | Default | Description |
|---|---:|---|---|
| `--config` | Yes | none | Path to the organization-specific TOML configuration file. |
| `--target` | Yes | none | `portal` or `agol`. |
| `--no-test` | No | test mode on | Enables full mode and removes the 250-target limit, up to the 2,000,000 safety ceiling. |
| `--all-owners` | No | organization-scoped | Remove the `orgid` restriction from feature-service item discovery. |
| `--sources` | No | `hosted,federated,registered` | Comma-separated discovery categories. Valid values: `hosted`, `federated`, `registered`. AGOL automatically drops `federated`. |
| `--exclude-group-id` | No | none | One group ID or comma-separated group IDs to exclude. |
| `--workers` | No | `12` | Concurrent health-check workers. Valid range: 1–64. |
| `--trusted-service-origin` | No | none | Repeatable exact HTTPS origin authorized to receive the organization token for service checks. Must be an origin only, with no path. |
| `--anonymous-service-origin` | No | none | Repeatable exact HTTP/HTTPS origin that must always be checked anonymously. |
| `--tls-mode` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | Conditional | none | PEM CA bundle path; required with `--tls-mode custom-ca-bundle`. |
| `--portal-url` | No | from config | Overrides the configured Portal/AGOL URL. |
| `--username` | No | from config | Overrides the configured login username. |
| `--keyring-service` | No | from config | Overrides the configured keyring service. |
| `--output-dir` | No | from config | Overrides the output directory. |
| `--smtp-server` | No | from config | Overrides the SMTP relay. |
| `--email-to` | No | from config | Overrides the email recipient(s). |
| `--email-from` | No | from config | Overrides the email sender. |

There is no script-level `--max-email-mb` option. The email attachment cap comes from the organization configuration through the shared `get_max_email_mb()` helper.

## Performance

The health-check phase is multithreaded using `--workers` (default: 12; maximum: 64). The script uses:

- A per-request timeout of 30 seconds.
- Two configured HTTP retries in the secure session.
- A maximum JSON response size of 64 MB.
- Lightweight `returnCountOnly` queries rather than downloading feature records.
- URL-based de-duplication before health checks.
- Early return when a service root fails, avoiding unnecessary layer requests.
- Bounded worker submission so the full target inventory is not queued into memory at once.
- A disk-backed SQLite target store for large target inventories.

Discovery can add work before the health-check pool runs. A Portal run may perform organization-scoped item searches using date buckets and walk federated REST catalogs. AGOL skips federation traversal.

Increasing `--workers` can improve throughput but increases concurrent load on Portal, AGOL, hosting servers, and federated servers. The appropriate setting depends on the environment and server capacity.

## Observed Runtime

Existing observations are approximate and depend on enabled discovery sources, service count, layer count, network latency, throttling, and worker count.

| Run Type | Duration | Approx. Services |
|---|---|---:|
| Portal | ~5–15 minutes | ~5,500 |
| AGOL | ~40–90 minutes | ~20,000 |

A run with many federated catalog directories or slow registered services can take longer than these examples.

## Output File

Written to the resolved output directory with an organization prefix, platform label, and `YYYYMMDD_HHMM` timestamp:

```text
[ORG]_[PLATFORM]_BROKEN_SERVICES_YYYYMMDD_HHMM.csv
[ORG]_[PLATFORM]_broken_service_scan_YYYYMMDD_HHMM.log
```

The broken-service CSV is created **only when at least one failed check is retained**. A clean scan produces no CSV, only the log.

The CSV fields are, in order:

```text
platform,SOURCE,title,item_id,service_url,owner,date_created,date_modified,Scope,layer_name,layer_id,layer_url,error,Likely_Permission_Issue
```

Each row represents one failed service, layer, or table check. One service can therefore produce multiple rows.

The completion email reports organization, platform, run timestamp, mode, TLS mode, organization-only state, enabled sources, excluded groups, services checked and broken, indeterminate services, layers checked and broken, broken rows, item-resolution errors, catalog errors, TLS/certificate errors, authentication errors, URL-policy blocks, transient failures, transport failures, report path, and timing.

If the run is **INCOMPLETE**, confirmed broken rows are retained, but the email warns that omitted services have not necessarily been proven healthy.

A connection failure, TLS session-build failure, failed startup handshake, failed organization-scope resolution, search-completeness failure, or other run-level exception triggers an abbreviated abort email with the log attached.

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | The scan, report/email workflow completed. This includes an `INCOMPLETE` result when individual discovery or service checks failed but the run itself completed and the completion email was sent. |
| `1` | A run-level failure occurred, such as TLS/session setup failure, startup handshake failure, GIS connection failure, inability to guarantee search completeness, or another unrecoverable exception. An abort email is attempted. |
| `2` | Command-line or required-argument validation failure, such as missing `--config`, `--target`, or `--ca-bundle-path` when required. |

## Notes and Caveats

- **The report is intentionally over-inclusive.** It is a candidate list, not definitive proof. Recheck URLs or compare repeated runs before acting.
- **Healthy services are not listed.** They appear only in email summary counts.
- **Permission-like failures are retained.** `Likely_Permission_Issue` is a triage hint and never excludes a row.
- **Invalid-token service-root failures are not treated as confirmed broken services.** They are handled as authentication uncertainty so the report is not falsely inflated by an expired scanner token.
- **Contained uncertainty makes the result `INCOMPLETE`.** TLS, transport, transient, authentication uncertainty, policy, item-resolution, and catalog problems can make absence from the broken CSV non-authoritative.
- **Group exclusions apply to item-backed discoveries and matching service URLs.** Catalog-only federated services can remain in scope when there is no matching excluded item.
- **`--sources` controls discovery categories.** Hosted/registered item discovery and federated catalog discovery follow different code paths.
- **AGOL has no federation discovery.** A requested `federated` source is automatically removed on AGOL.
- **Organization scoping is the default.** `--all-owners` deliberately removes the organization ID restriction and should only be used when a broader inventory is required.
- **Search completeness is strict.** Full mode stops at 2,000,000 targets rather than labeling a capped inventory complete.
- **Test mode checks only the first 250 unique targets.** It is not a complete inventory.
- **Source classification is based on configured/federation roots plus hosted-service keywords.** An unusual reverse-proxy topology may require `--trusted-service-origin` for tokenized service access.
- **External and private service URLs can still be scanned.** They are checked anonymously unless explicitly authorized for authenticated access.
- **Plain HTTP is anonymous-only.** The organization token is never sent over HTTP.
- **Redirects are not followed automatically.** Direct REST requests use `allow_redirects=False`; redirect destinations are validated manually.
- **JSON responses have a 64 MB limit.** Larger or malformed responses are treated as errors and do not become false health confirmations.
- **Federated catalog traversal is bounded.** The scan stops catalog growth at 100,000 directories or 16 levels of depth and records the condition as an incomplete/error state.
- **No-layer roots are treated as broken.** A service root that responds successfully but contains neither layers nor tables produces a `SERVICE` failure.
- **This is an availability check, not a content-quality check.** A service can pass all health checks while its data is stale, wrong, incomplete, or empty.
- **Short timeouts increase false positives.** A slow service exceeding the 30-second request timeout can appear broken even if it eventually responds.
- **The `Likely_Permission_Issue` flag is text-based.** It can be wrong in either direction and is intended only for triage.
- **Worker exceptions are contained.** An unexpected service-check exception is recorded as an indeterminate check and does not stop remaining workers.
- **Result order is not deterministic.** Rows are written as completed checks are returned and are not sorted for business meaning.
- **Worker count is validated.** Use an integer from 1 through 64.
- **This is a point-in-time snapshot.** Service health can change immediately after the scan.
- **Certificate validation is separate from service availability.** A TLS/certificate problem can mean the endpoint is reachable but not trusted by the scanner; use the appropriate `--tls-mode` for an Enterprise environment with internal CAs.
- **The imported `UTILITIES` package is origin-checked.** The current script verifies the package location but does not remove equivalent project-root path spellings before insertion.