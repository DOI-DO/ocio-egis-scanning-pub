# SCAN_WEB_MAPS.py

**Broken Web Map Layer Scan — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

This script checks every web map in an organization to see if its layers still work. Web maps are built from layers that point to services elsewhere in the system. This tool opens every map, follows every layer pointer, and confirms the service on the other end actually responds.

The scan keeps the web-map/layer classifications and output columns used by this report. Catalog discovery is organization-scoped by default, strictly paged, and fails closed rather than silently omitting failed search buckets or unresolved items. Layer probes are anonymous-first; the organization token is sent only to explicitly trusted HTTPS Portal/federation scopes or approved ArcGIS Online service/item-data paths.

---

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Web Maps](#why-inventory-web-maps)
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

This report lists web maps with broken layers. A web map is a saved map that people open and look at — it's built from multiple layers, where each layer is really just a pointer to a service running somewhere else in your system. This scan opens every map, checks every layer pointer, and confirms the service on the other end actually answers.

A row is written when one of two things needs attention: a specific layer could not be verified as healthy, or the web map itself could not be opened/read. A completely healthy web map does not create a row in the broken-layer report.

The scan checks operational layers and basemap layers, walks nested layer groups, and probes each unique layer URL only once per run. That means the same broken service can be identified once and then associated with every map that uses it.

## Why Inventory Web Maps

Web maps are what people actually use. When a web map looks broken, it's rarely the map's own fault — it's usually something underneath it failing. This scan turns "wait for someone to complain" into "know about broken maps before anyone notices." It tells you which maps are affected, exactly which layer is failing and why, and — because many maps share the same underlying service — which single fix will repair the most maps at once.

The reason web maps specifically are worth inventorying is that they sit at the very top of the dependency stack, where failures are most visible and least self-explanatory. A web map is not a self-contained document; it is an assembly of pointers to services that live elsewhere, so its health is entirely at the mercy of things it does not control. That makes a web map the place where a problem *surfaces* even though it is almost never the place where the problem *lives*. The person looking at a blank map has no way to know that the real cause is a deleted service, an expired database connection, or a lapsed certificate two layers down — and the web map, viewed on its own, gives no outward sign of the broken reference inside it until someone opens it. An inventory is the only way to look inside every map ahead of that moment, following each pointer to confirm the service on the other end actually answers rather than waiting for a user to discover it the hard way.

This script is not a duplicate-layer comparison tool. The requested heading is retained for consistency with the surrounding inventory documentation. Its actual purpose is to inventory broken web-map layer references and identify the underlying service or URL problem behind them. The particular value of running it as an inventory — rather than reacting to reports one map at a time — comes from the many-to-one relationship between maps and services: because a single service is typically referenced by many maps, checking every map at once lets the report collapse a confusing scatter of "broken maps" down to the small number of shared endpoints actually at fault. Probing each unique layer URL exactly once and then associating the verdict with every map that uses it is what produces that collapse: a cluster of ten broken maps becomes one broken service to fix, and the scan points straight at it. Reasoning about maps individually can never reveal that structure, because the shared cause is only visible when the whole population is examined together.

Running this on a schedule rather than only after complaints matters because broken web maps are both high-visibility and high-trust-cost. A broken map fails in front of the user, who usually cannot tell whether the fault is theirs or the system's, so every undiscovered broken map is a quiet erosion of confidence in the GIS program. A periodic scan finds these before users do, and — because each row carries the specific `error`, the exact failing `layer_url`, and the map's owner and modification date — it hands an administrator a technical finding already connected to a likely cause and the right person to involve. The report also distinguishes a genuinely broken service from a merely unverifiable or security-blocked one, so a transient restart or an authentication boundary is not mistaken for a dead endpoint. Because the scan is strictly read-only and probes anonymously first — sending the organization token only to explicitly trusted destinations — it is safe to run regularly as a standing health check on the layer of the system users see most and understand least.

The report also includes the map owner, creation date, and modification date so an administrator can connect a technical finding to the people and content lifecycle behind the map.

## Why it's a problem

- **A broken layer signals something underneath.** A map's layers point at real services. When a layer stops responding, something below it changed or failed: the service was deleted, renamed, or moved; the server hosting it went down; a database connection expired; or a security certificate became invalid. Because one broken service is often used by many maps, this scan can point straight at the one underlying problem affecting a whole cluster of "broken" maps.
- **Broken web maps are highly visible and frustrating.** Unlike a technical problem buried deep in the system, a broken map fails right in front of the person using it — blank space where a layer should be, a dashboard that won't draw, a map that looks empty. Most users can't tell whether the problem is on their end or the system's. Finding these problems before users do protects their experience and their trust in the GIS program.
- **A single service failure can affect many maps.** Because Phase 2 probes each unique `layer_url` once, the report can reveal when many maps share the same failing endpoint.
- **Not every failure has the same meaning.** The `error` field distinguishes broken HTTP responses, transport failures, authentication problems, unsafe destinations, unreadable data, and other outcomes. That helps the administrator investigate the actual cause instead of treating every failure as a missing service.

## Why removal requires caution

A broken layer is not necessarily the map's fault, and it doesn't mean the map should be deleted. Because a layer is really just a reference to a service running somewhere else, "broken" usually means the *service* has a problem, not that the map was built incorrectly. The map may still be actively used and valuable even while one layer inside it is temporarily unreachable. Removing the whole map because one layer is down can take away something people still rely on.

A broken result can also be a temporary condition. A service may be restarting, a network path may have been unavailable for a moment, a certificate may have been changed during the scan, or authentication may be required but unavailable within the approved trust boundary. The report is therefore an investigation aid, not an automatic cleanup instruction.

For unreadable maps (`section = WEBMAP`), the scan could not inspect the layer references at all. That is a different problem from a specific broken layer and should be investigated as a web-map readability/data issue before any content decision is made.

## Recommendations for investigation

- **Start with the `error` column.** It tells you specifically what went wrong (a "not found" message, a timeout, a certificate problem), which usually points straight at the real cause.
- **Group rows by `layer_url`.** Because each unique layer address is checked only once per run, seeing the same URL show up under many different maps means one broken service is affecting all of them — fixing that one service repairs every map at once.
- **Use the `owner` column to find the right person** — either the person who can fix the underlying service, or the map's owner.
- **Check whether the problem might be temporary** (a service that happened to be restarting) before assuming something needs to be permanently fixed. A single timeout is not proof something is gone for good.
- **Use `date_modified` to judge how actively a map is maintained.** A map edited recently with a newly broken layer is a stronger candidate for attention than an old map nobody has touched in years.
- **Treat `UNVERIFIABLE_AUTH` differently from a normal broken service.** It means the scan could not safely establish authenticated access within its approved security scope. It is not proof that the layer itself is unavailable.
- **Treat `BLOCKED_*` results as security-policy outcomes.** A URL can be blocked because of an unsafe address, malformed URL, unapproved network scope, or another explicit safety rule. Investigate the layer reference and trust boundary rather than immediately changing the web map.
- **Review `WEBMAP` rows separately.** These rows mean the map document itself could not be read, so there may be many unchecked layers hidden behind the unreadable map.

### Scenario: one service is breaking many maps

You find ten or more rows with the same `layer_url`. That usually means the maps share one service endpoint. Start with that service, its availability, certificate, DNS, and permissions rather than editing each map individually.

### Scenario: a layer fails only because authentication is unavailable

The `error` begins with `UNVERIFIABLE_AUTH`. Review whether the destination is a known Portal/federated service or an approved AGOL service/item-data path. The script intentionally withholds the organization token from external destinations it cannot trust.

### Scenario: a map itself cannot be opened

The row has `section = WEBMAP` and blank `layer_name`/`layer_url`. Start by opening the map directly in Portal/AGOL and reviewing the map's stored data. Until the map can be read, the report cannot claim that all of its layer references were checked.

## Column descriptions

Each row represents either one broken layer, or one web map that could not be opened/read at all.

| Column | Description |
|---|---|
| `platform` | `PORTAL` or `AGOL` — where the map lives. |
| `title` | The web map's title as it appears to users. |
| `item_id` | The map's unique internal ID. |
| `owner` | The username of the person who owns/manages the map. |
| `date_created` | When the map was originally created (`YYYY/MM/DD`). |
| `date_modified` | When the map was last edited (`YYYY/MM/DD`). Recent edit + new failure is worth prioritizing. |
| `section` | Where the problem was found: `operationalLayer` (data layer), `baseMapLayer` (background layer), or `WEBMAP` (the whole map couldn't be read). |
| `layer_name` | The name of the specific broken layer. Blank when `section` is `WEBMAP`. |
| `layer_url` | The web address of the service the broken layer points to. Blank when `section` is `WEBMAP`. |
| `error` | The specific reason the layer (or map) failed — this is the most useful column for figuring out what went wrong. |

The CSV field order is exactly the order shown above.

---

# For Administrators

## Dependencies on the shared UTILITIES package

- `auth.py`
- `bucketing.py`
- `cli.py`
- `emailer.py`
- `exclusions.py`
- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `secure_session.py`

## Requirements

- Python with the ArcGIS API for Python (`arcgis`) and `requests` installed.
- The shared `UTILITIES` package reachable from the project root.
- An organization-specific TOML configuration file passed with `--config`.
- A `portal` or `agol` target passed with `--target`.
- Portal/AGOL credentials in the configured OS keyring.
- Permission to search, read, and resolve the organization's web maps and their layer definitions.
- Network access to the target Portal/AGOL endpoints and SMTP relay.

The optional `--tls-mode os-trust-store` requires the shared `truststore` package. `--tls-mode custom-ca-bundle` requires a readable PEM CA bundle. `--allow-private-layer-host` is optional and should be used only for explicitly approved hosts.

## Security: Certificate (TLS) Validation

Every connection made by this script verifies TLS certificates. There is no option to disable certificate validation.

Three verification strategies are available through `--tls-mode`:

- **`standard`** (default) — validates against the standard/public CA bundle.
- **`os-trust-store`** — validates against the operating-system certificate store and requires `truststore`.
- **`custom-ca-bundle`** — validates against the readable PEM certificate bundle supplied with `--ca-bundle-path`.

Before scanning begins, `verify_tls_handshake()` performs a one-time real TLS handshake against the configured Portal/AGOL URL. If that check fails, the run aborts before scanning begins.

### SSRF / network-boundary defenses

The layer probe implementation is contained in `SCAN_WEB_MAPS.py` so that the security-critical URL handling is deployed with the scanner itself.

Layer URLs are normalized before use. The script rejects unsafe URL forms including excessively long URLs, embedded credentials, prohibited control characters, malformed hostnames/ports, credential-like URL path values, path traversal, prohibited address classes, and disallowed network scopes.

DNS results are resolved before probing and the HTTP connection is pinned to validated addresses while preserving the original hostname for TLS/SNI validation. Loopback, link-local, multicast, unspecified, and reserved destinations are prohibited. Enterprise private addresses can be permitted only inside an explicitly trusted Portal/federation path or via `--allow-private-layer-host`; anonymous web probes on standard web ports can reach private DNS results, but that does not grant permission to send organization credentials.

Authenticated probes are allowed only for approved HTTPS Portal/federation scopes or for exact AGOL HTTPS service roots/item-data paths established from the scanned maps and Esri-controlled `*.arcgis.com` infrastructure. External non-AGOL destinations remain anonymous-only.

### Token handling

Layer probes are **anonymous first**. If an anonymous request succeeds, the organization token is never sent.

When authentication is required, the token is sent by **POST form data**, not in the URL. The token is supplied by the shared `TokenProvider`. A synchronized refresh/retry is allowed only for token-related 498/499 authentication failures.

Error text and diagnostic URLs are passed through token/credential redaction before they are written to logs, email, or the CSV. A SHA-256 token fingerprint is retained only internally to determine whether a failed authenticated request used the current token before a refresh.

### Large/malformed responses

Direct layer responses are streamed and limited to **64 MB**. `Content-Length` is checked when available, and the streaming read enforces the same limit.

JSON is decoded only after the byte limit is enforced. Non-JSON success responses that decode as an ordinary `JSONDecodeError` are intentionally treated as `IGNORED_NON_JSON` rather than broken-layer findings because the scanner permits reachable non-ArcGIS JSON resources such as GeoJSON. Other malformed/non-JSON cases remain broken responses.

Web map layer-document parsing is also bounded by **100,000 layer URL references per map** and **128 levels of nested layer depth**, preventing a malformed map from creating unbounded recursion or memory usage.

### Redirect following is explicitly disabled

HTTP requests use `allow_redirects=False`. Redirects are handled explicitly by the scanner so each destination is re-normalized, re-validated, DNS-checked, and evaluated against the authentication trust policy before the next request is sent.

Up to **4 redirect hops** are allowed. An authenticated redirect that leaves an approved Portal/federation scope is blocked. ArcGIS Online can allow a same-resource redirect across HTTPS `*.arcgis.com` hosts when the same item or service root can be proven.

### Python import-path/package shadowing

The script locates the expected project root and `UTILITIES` package without importing it first. It inserts the expected project root at the front of `sys.path` when it is not already present, then calls `importlib.util.find_spec("UTILITIES")` and verifies that the import origin is exactly the expected `UTILITIES/__init__.py` before importing the package.

This prevents an unexpected `UTILITIES` package elsewhere on the import path from silently replacing the expected shared package.

The current implementation **does not remove equivalent duplicate project-root entries from `sys.path`**. Its protection is expected-root insertion plus pre-import package-origin verification.

## Configuration

A specific organization is selected at run time with the required `--config <org_toml_path>` and `--target portal` or `--target agol` arguments.

The selected config provides `org-name`, `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, and `email-from`. `max-email-mb` can also be supplied through the shared configuration because the script passes it to `get_max_email_mb()`.

Command-line values override configuration values. `--exclude-group-id`, `--allow-private-layer-host`, `--tls-mode`, `--ca-bundle-path`, worker settings, and timeout settings are command-line controls.

### Example configuration

```toml
[common]
org-name = "ORG"
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
portal-url = "https://org.maps.arcgis.com"
username = "agol_service_account"
keyring-service = "AGOL"
```

Store the password in the OS keyring instead of the TOML file:

```bash
python -c "import keyring; keyring.set_password('PORTAL', 'portal_service_account', '<password>')"
python -c "import keyring; keyring.set_password('AGOL', 'agol_service_account', '<password>')"
```

## How it works

1. Enumerate web map items from the catalog using strict date-range bucketed search. Organization-owned scanning is the default.
2. Apply group exclusions immediately after enumeration so excluded maps do not proceed into the scanning phases.
3. **Phase 1** — Read every web map's JSON in parallel, walking operational and basemap layers, including nested groups, and collecting layer URLs.
4. Build a trust policy from the authenticated home Portal, Portal federation metadata where available, and exact AGOL service/item-data references for the platform being scanned.
5. Normalize and index every collected layer reference. Unsafe references are rejected before network access.
6. **Phase 2** — Probe every unique layer URL exactly once in parallel: anonymous first (no credentials), then authenticated only when the anonymous response requires authentication and the destination is explicitly trusted. The probe caches the resulting verdict.
7. For each network target, DNS is resolved and validated before connecting. The original hostname is retained for TLS/SNI while the connection is pinned to the validated IP address.
8. **Phase 3** — Assemble per-map results from the Phase 2 verdict cache. A map can therefore produce multiple rows when multiple layer references are broken.
9. Write the CSV with every failed check and every unreadable map.
10. Summarize the run and email the log and CSV according to the configured attachment policy.

### Web-map discovery

By default, the script restricts the search to the authenticated organization's Web Map items using `orgid:<organization-id>`. `--all-owners` intentionally disables this organization-only filter so publicly/shared or externally owned web maps that the account can access can also be checked.

Full-mode discovery is designed to avoid silent loss at the Portal search cap. Individual searches are paged strictly, duplicate IDs are rejected within a query, result totals are validated, and date buckets are subdivided whenever a bucket reaches the 10,000-result API cap.

The full scan has a safety ceiling of **2,000,000 web maps**. Reaching that ceiling causes the run to abort rather than writing a silently capped report.

### Layer probing

The same `layer_url` is normalized and evaluated once per run even when many maps reference it. This is the main performance optimization and also makes repeated failures easier to interpret.

Anonymous probes use no credentials. If the endpoint responds with an authentication requirement, the scanner evaluates whether that exact destination is allowed to receive an organization token. Approved Enterprise destinations come from the authenticated Portal/federation configuration. Approved AGOL destinations are exact HTTPS service roots or exact item-data paths from the scanned maps on Esri-controlled `*.arcgis.com` infrastructure.

### Output interpretation

A layer is counted as healthy when the endpoint returns HTTP 200 with a JSON object that does not contain an ArcGIS error payload. This is intentionally broader than requiring ArcGIS-specific keys because web maps can legitimately reference non-ArcGIS JSON resources such as GeoJSON.

`IGNORED_NON_JSON` and `IGNORED_RESOURCE_MEDIA_JPEG` outcomes are not emitted as broken-layer rows. They are counted in diagnostics instead.

## Test mode (default)

**Test mode is ON by default.** It limits enumeration to the first **250 unique web maps**. Pass `--no-test` for full mode, which allows up to the **2,000,000 web-map safety ceiling**.

Test mode validates credentials, organization discovery, connectivity, TLS verification, exclusion behavior, layer probing, and email delivery without attempting a complete organization inventory.

The test limit applies to web-map enumeration. It does not turn the layer scanner into a simulation — the selected maps and their layer URLs are actually read and probed.

## Usage

**Scenario 1 — First-time sanity check.**

You've just added a new organization config and want to confirm credentials, connectivity, TLS trust, and email delivery work before committing to a full run.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal
```

This runs in test mode (250 maps), fast and low-impact. A successful completion email confirms everything is working.

**Scenario 2 — Full inventory with standard throughput.**

You want a complete picture of every web map in your Portal organization.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test
```

**Scenario 3 — Full AGOL run with higher concurrency.**

Your AGOL organization has ~9,000 web maps and you want faster throughput.

```bash
python SCAN_WEB_MAPS.py --config org2.config.toml --target agol --no-test --workers 24
```

**Scenario 4 — Scanning beyond your own organization on AGOL.**

You suspect some externally-owned or publicly-shared maps that your staff use might also have broken layers.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target agol --no-test --all-owners
```

**Scenario 5 — Excluding known intentionally-empty maps.**

Your organization maintains demo or training maps with intentionally broken layers, and you don't want them cluttering the report.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --exclude-group-id abc123
```

**Scenario 6 — Excluding multiple groups.**

Two teams maintain their own known exceptions.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --exclude-group-id "abc123,def456"
```

**Scenario 7 — Running against an internal Portal with an organizational CA certificate.**

Your Portal's certificate is issued by your organization's internal Certificate Authority and is already trusted at the OS level.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --tls-mode os-trust-store
```

**Scenario 8 — Running with a specific CA bundle file.**

You have a PEM file for your internal CA but it's not in the OS trust store.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

**Scenario 9 — Allowing an approved private layer host.**

A known internal service uses split-horizon DNS or a private IP address, and your security process has approved the exact hostname for anonymous layer probing.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --allow-private-layer-host gis-internal.example.com
```

The option permits anonymous probing of that exact host; it does not automatically permit organization credentials to be sent to an external or otherwise unapproved destination.

**Scenario 10 — Giving URL probes a longer timeout.**

Some approved services are known to take longer than the default 15 seconds to answer, and you want to distinguish a slow service from an immediately unreachable endpoint.

```bash
python SCAN_WEB_MAPS.py --config org1.config.toml --target portal --no-test --url-timeout 60
```

**Scenario 11 — Reviewing the current command-line interface.**

You are deploying or automating the script and want the exact current options without running a scan.

```bash
python SCAN_WEB_MAPS.py --help
```

## Command-line options

| Argument | Required | Default | Description |
|---|---:|---|---|
| `--config` | Yes | none | Path to the organization-specific TOML configuration file. |
| `--target` | Yes | none | `portal` or `agol`. |
| `--no-test` | No | test mode on | Enables full mode; removes the 250-map limit and uses the 2,000,000-item safety ceiling. |
| `--all-owners` | No | organization-scoped | Scan every reachable web map, not just organization-owned. |
| `--exclude-group-id` | No | none | One or more comma-separated group IDs to exclude from scanning. |
| `--allow-private-layer-host` | No | none | Explicitly allow this exact layer hostname to be probed when DNS resolves it to a private address. May be repeated. Anonymous probes only. |
| `--workers` | No | `12` | Concurrent workers for web-map JSON reads and item resolution. Maximum `64`. |
| `--url-workers` | No | `24` | Concurrent workers for unique layer URL probes. Maximum `64`. |
| `--url-timeout` | No | `15` | Per-layer-URL timeout in seconds. Maximum `300`. |
| `--tls-mode` | No | `standard` | TLS verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | Only with `--tls-mode custom-ca-bundle` | none | Path to a readable PEM CA bundle file. |
| `--portal-url` | No | from config | Overrides the configured Portal/AGOL URL. |
| `--username` | No | from config | Overrides the configured login username. |
| `--keyring-service` | No | from config | Overrides the configured keyring service. |
| `--output-dir` | No | from config | Overrides the configured output directory. |
| `--smtp-server` | No | from config | Overrides the configured SMTP relay. |
| `--email-to` | No | from config | Overrides the recipient(s). |
| `--email-from` | No | from config | Overrides the sender address. |

## Performance

The scan runs in **three phases**, designed so a layer URL shared by many maps is checked only once:

1. **Phase 1 — Read every web map's JSON in parallel** using `--workers` threads.
2. **Phase 2 — Probe every unique layer URL exactly once, in parallel** using `--url-workers` threads. Maps sharing a URL reuse the cached result.
3. **Phase 3 — Assemble per-map results from the cache** with no further network calls.

Item resolution and map-JSON reads use `--workers` (default 12). URL probes use `--url-workers` (default 24), with a short timeout (`--url-timeout`, default 15s) so unreachable endpoints fail fast.

The run log reports how many URL checks the cache saved (`url_cache_hits` = total layers checked minus unique URLs probed).

Task submission is bounded at roughly three times the relevant worker count so a very large catalog does not create an unbounded queue of in-memory futures.

The scanner also bounds each web map to 100,000 layer URL references and 128 levels of nested layer depth.

## Observed Runtime

| Run Type | Duration | Approx. Web Maps |
|----------|----------|------------------|
| Portal | ~15 minutes | ~2,500 |
| AGOL | ~20 minutes | ~9,000 |

Runtime scales with the number of web maps and unique layer URLs. Phase 2 only probes each URL once.

The source code does not guarantee these durations. They are operational observations from the documented runs and can vary with network latency, service responsiveness, map complexity, worker count, and AGOL/Portal throttling.

## Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp and the resolved organization prefix:

```text
[ORG]_[PLATFORM]_WEBMAP_BROKEN_LAYERS_<ts>.csv
[ORG]_[PLATFORM]_web_map_scan_<ts>.log
```

One row per broken layer, plus one row per web map that could not be read at all (`section = "WEBMAP"`, with `layer_name`/`layer_url` blank).

A completely clean run (no broken layers and no unreadable maps) produces **no CSV** — only the log. Check the run summary counts to confirm.

The CSV fields are written in exactly this order:

```text
platform
title
item_id
owner
date_created
date_modified
section
layer_name
layer_url
error
```

The completion email reports organization, platform, run timestamp, mode, TLS mode, org-only state, maps checked and broken, layers checked and broken, unique URLs, and timing.

If findings exist and the shared attachment cap is exceeded, `gather_attachments()` may package the report according to the configured `max-email-mb` policy. The local CSV and log remain the primary artifacts.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success — the scan completed and the completion email was sent, regardless of findings. A completed scan can still be labeled `INCOMPLETE` in the email/log when some maps or layers could not be conclusively checked. |
| `1` | Aborted — the script could not build a TLS session, the startup TLS handshake check failed, the GIS connection failed, required Portal/federation discovery failed, or another fatal condition prevented a trustworthy scan. An abort email is sent before exit. |
| `2` | Invalid arguments — for example, `--tls-mode custom-ca-bundle` passed without `--ca-bundle-path`, an out-of-range worker/timeout value, or another argument validation error. |

## Notes and caveats

- **A broken layer is not always the map's fault.** Because layers reference services elsewhere, "broken" usually points at a service, server, or data-store problem rather than the web map itself.
- **Third-party layer URLs are probed safely.** Layer probes are anonymous-first, with DNS validation/pinning, explicit redirect revalidation, bounded responses, and safe address checking. An external domain remains anonymous-only; the organization token is not sent there.
- **Anonymous probes can reach split-horizon/private web destinations on standard web ports.** Private DNS resolution by itself does not grant authentication scope. Loopback, link-local, multicast, unspecified, and reserved destinations remain prohibited.
- **Explicit private hosts are narrowly approved.** `--allow-private-layer-host` permits anonymous probing for the exact named host when DNS resolves it to a private address. It does not authorize organization credentials for arbitrary external destinations.
- **Organization scoping is the default.** Both Portal and AGOL searches are organization-scoped unless `--all-owners` is supplied. If an organization ID cannot be established while org-only scanning is requested, the run aborts rather than silently falling back to an unscoped inventory.
- **Group exclusions apply before any work.** Web maps belonging to excluded groups are filtered immediately after enumeration, before Phase 1, so they cost nothing and never appear in the report.
- **AGOL drops the `federated` concept.** Organization scoping and authentication are handled through explicit AGOL `*.arcgis.com` service/item-data trust rules rather than Portal federation metadata.
- **Email attachment cap is configurable, project-wide.** This script resolves `max-email-mb` from the chosen org config and passes it into `gather_attachments()`. If the CSV exceeds that cap it can be packaged before email according to the shared attachment helper's policy.
- **No CSV is written at all on a fully clean run.** `write_dict_csv()` skips writing when there are zero rows, so a run with no broken layers and no unreadable maps produces no CSV — only the log.
- **Email body omits the portal URL.** The target URL remains available in the operational log for troubleshooting.
- **Result order is not deterministic.** Rows are appended as concurrent work completes and are not sorted before CSV writing.
- **`IGNORED_NON_JSON` is not a broken-layer finding.** A reachable non-JSON response can be valid for a non-ArcGIS JSON resource such as GeoJSON, so that common case is intentionally excluded from the broken report.
- **`IGNORED_RESOURCE_MEDIA_JPEG` is not a service failure.** Local `./resources/media/*.jpeg` references are intentionally excluded from service probing because they are web-map media resources rather than service URLs.
- **`UNVERIFIABLE_AUTH` is not proof of an unavailable service.** It means authenticated access could not be established within the approved trust boundary.
- **`BLOCKED_*` is a security-policy outcome.** The URL was blocked by URL/address/trust validation and was not treated as an ordinary service outage.
- **A `WEBMAP` row means the web-map document itself could not be read.** Its layers were therefore not all available for checking.
- **Full catalog discovery is fail-closed.** A search bucket at the 10,000-result cap is subdivided; duplicate IDs, inconsistent totals, empty pages before the declared total, invalid IDs, cross-organization results, and pagination failures are treated as scan errors rather than accepted as complete.
- **Full mode has a 2,000,000-web-map safety ceiling.** The script aborts if it reaches that ceiling rather than writing a silently capped report.
- **Layer-document depth and reference counts are bounded.** More than 128 nested levels or more than 100,000 URL references in one web map causes that map's layer extraction to fail rather than exhausting recursion/memory.
- **REST responses are bounded at 64 MB.** This limit applies to direct layer/service checks and prevents an unexpectedly large response body from being read without bound.
- **Redirect following is not automatic.** Requests use `allow_redirects=False`; redirects are re-normalized and re-validated before another request is made, and the redirect chain is limited to four hops.
- **Authentication is POST-only for layer probes.** The organization token is not placed in layer request URLs.
- **TLS verification is always enabled.** `--tls-mode` selects the trust source; it never disables certificate validation.
- **Import-path hardening verifies the package origin.** The expected project root is inserted first when absent, `UTILITIES` is checked with `find_spec()`, and the import is refused if it does not originate from the expected directory.
- **Equivalent project-root duplicates are not removed.** The current import hardening does not de-duplicate every matching `sys.path` entry.
- **This tool is read-only.** It reads Portal/AGOL catalog data and referenced layer endpoints and writes a report; it does not modify web maps, services, records, sharing, or security settings.