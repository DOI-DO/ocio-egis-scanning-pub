# Shared Credentials Scan

**Scan a Portal/AGOL organization for service items with embedded (shared) credentials to remote hosted services.**

---

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Embedded Credentials](#why-inventory-embedded-credentials)
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

## For Report Readers

### Overview

This scan identifies service items in your Portal or ArcGIS Online organization that contain embedded (shared) credentials. These are authentication details that allow a Portal service to access a remote hosted service without going through normal permission controls.

Think of it this way: instead of asking "Are you authorized to access this data?" each time, the Portal service has a pre-written pass card (credentials) that grants it automatic access. While convenient, this approach creates security and tracking problems.

The current scanner checks these candidate item types:

- Feature Service
- Map Service
- Image Service
- WMS
- WMTS
- Vector Tile Service
- Scene Service
- Geocoding Service
- Geometry Service
- Geodata Service
- Network Analysis Service
- Geoprocessing Service
- Relational Database Connection
- API Key

The scan is intentionally read-only. It identifies findings and reports them for investigation; it does not remove credentials, change sharing, or modify the discovered items.

### Why Inventory Embedded Credentials

Embedded credentials are, by design, invisible during normal operation, and that invisibility is precisely why they need to be inventoried deliberately. A service item with stored credentials works silently and correctly every day, so there is no routine moment at which anyone is prompted to ask what credential it holds, what remote system that credential reaches, or who is allowed to use it. The convenience that makes embedded credentials attractive is the same property that makes them accumulate unnoticed: once one is created, it keeps working without maintenance or review, and the organization gradually builds up a population of credential-bearing items that no single person can enumerate from memory. This scan exists to convert that hidden population into an explicit, reviewable list.

The inventory matters because an embedded credential is not merely a convenience feature — it is a standing grant of access that sits outside the organization's normal permission model. When a service item carries stored credentials, anyone with access to that item effectively inherits the embedded account's access to the remote service, regardless of whether they would be authorized to reach that remote service directly. That means the real security boundary is no longer the remote service's own permission settings; it is the sharing configuration of every item that embeds a credential to it. Those two things drift apart over time. An item may be re-shared, made public, or handed to a new owner, and in OTHER_ORGng so silently widen access to a backend system whose true owner never approved and may not even know the connection exists. An inventory is the only practical way to find these grants before someone else does.

There are three further reasons this specific scan is worth running on a schedule rather than treating credentials as a one-time setup concern. First, embedded credentials create accountability gaps: because the remote service sees only the embedded account, its logs cannot attribute activity to the actual human users, so an inventory is the organization's substitute for the audit trail the remote service cannot provide. Second, embedded credentials are a reliability liability as well as a security one — when the underlying account's password rotates, expires, or is disabled, every item that embedded the old credential can fail at once with no obvious common cause, and a current inventory turns that confusing multi-service outage into a quick lookup. Third, this scanner is careful to distinguish a genuine clean result from a result produced by failed checks, so running it regularly builds confidence over time: a successful, complete run is positive evidence about the organization's posture, while an incomplete run is flagged rather than mistaken for an all-clear. Taken together, these make a recurring, read-only inventory the safest and most reliable way to keep embedded-credential risk visible and bounded.

### Why it's a problem

Service items with shared credentials are a security risk for several critical reasons:

- **Bypass of Access Controls**: Credentials embedded in a service item bypass the access controls put in place by the actual owner of the remote service. This can result in access to the data by people that the actual owner did not intend to grant access to.

- **Unknown Access**: Without the owner of the remote service's knowledge, these services can be viewed by anyone who has access to the service item that contains embedded credentials. When full access to a service item with shared credentials is granted to the public, the identity of the person actually accessing the remote service is completely unknown to the service owner.

- **No Per-User Logging**: Access logs on the remote service show only the account that was embedded in the item, making it impossible to identify who is actually accessing the service. This eliminates audit trails and accountability.

- **Hidden Dependencies**: The true owner of the connection is hidden. You cannot tell who is actually accessing the remote service or for what purpose.

- **Credential Drift**: When credentials are updated at the source, embedded versions may not be updated, causing service failures with no warning.

- **Shared Secrets**: Multiple people may know or have access to the same embedded credentials, increasing the risk of exposure and unauthorized use.

- **Unintended Data Access**: These services can expose data to people the actual remote service owner never intended to grant access to, potentially violating data sharing agreements or compliance requirements.

There is also an important scan-quality issue: a check can fail without proving that credentials are present. The script therefore distinguishes successful checks from check errors and marks the run incomplete when one or more candidate checks fail.

### Why removal requires caution

Simply removing embedded credentials without a plan will break the affected service items. When a service item with embedded credentials is changed to not use credentials, consumers of the item will be presented with a login screen for the system that hosts the service, and a login will be required before the content of the service is accessible.

Before removing embedded credentials, you must establish an alternative authentication method and test thoroughly:

- **Modern ArcGIS API Keys**: Use scoped API keys with minimally-scoped permissions and explicit privilege and referrer restrictions
- **Inter-Organization Collaboration**: Establish trusted relationships between Portal organizations to share services securely
- **More Permissive Views**: Create a view of the data that is more acceptable for sharing with a broader audience
- **Dedicated Service Accounts**: Create and use an account designated for public/broader access points (such as kiosks in building lobbies or at conferences) that is not tied to a specific person and can have very limited access

Plan the migration carefully, test the new authentication method, and notify users before making the change.

The scanner itself performs a particularly important safety check for credential-bearing service URLs: the item-supplied URL is validated as HTTPS and against the configured trusted Portal/AGOL boundary **before the organization token is sent to it**.

### Recommendations for investigation

1. **Identify the Owner**: Contact the item owner to understand what remote service is being accessed and why
2. **Assess Criticality**: Determine if this item is actively used or if it's legacy
3. **Audit Access**: Check Portal logs to see which users are accessing the item most frequently
4. **Plan Replacement**: Work with the owner and remote service administrator to implement a better authentication method
5. **Test First**: Implement and test the new authentication method before removing the embedded credentials
6. **Document**: Record why this item needed embedded credentials in case similar patterns emerge

For API Key findings, also review the reported privileges, HTTP referrers, and redirect URIs. For proxy-service findings, review the backend source URL and confirm the business owner of that remote service.

### Column descriptions

The current Python file writes these columns, in this exact order:

| Column | Meaning |
|--------|---------|
| `item_id` | The unique identifier of the Portal/AGOL service item containing embedded credentials. The script validates candidate IDs as 32-character ArcGIS item IDs before checking them. |
| `title` | Human-readable name of the service item. |
| `owner` | Portal/AGOL username of the account that owns or manages this item. |
| `type` | Item category. Candidate service types include Feature Service, Map Service, Image Service, WMS, WMTS, Vector Tile Service, Scene Service, Geocoding Service, Geometry Service, Geodata Service, Network Analysis Service, Geoprocessing Service, Relational Database Connection, and API Key. |
| `service_url` | Web address of the service item when one is available. For a proxy finding this is the proxy service URL. The scanner validates a service URL before making an authenticated request to it. |
| `item_home_url` | Direct link to the ArcGIS item page in Portal/AGOL. |
| `platform` | `PORTAL` for ArcGIS Enterprise or `AGOL` for ArcGIS Online, based on the configured target URL. |
| `privileges` | Privileges associated with an API Key finding, when returned by the registered-app information. Blank for ordinary service-item findings. |
| `http_referrers` | HTTP referrer restrictions associated with an API Key, when returned. Blank for ordinary service-item findings. |
| `redirect_uris` | Redirect URI restrictions associated with an API Key, when returned. Blank for ordinary service-item findings. |
| `backend_source_url` | The backend remote service URL when a service uses the recognized `utility.arcgis.com` proxy pattern. Blank for other findings. |

> The CSV is only written when positive findings exist. If no findings are produced, the email explains that no positive findings were found among the successfully checked candidates; it does not imply a complete negative result when check errors occurred.

---

## For Administrators

### Dependencies on the shared UTILITIES package

The script uses the following helper files from the shared `UTILITIES` package:

- `__init__.py`
- `auth.py`
- `cli.py`
- `config.py`
- `emailer.py`
- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `secure_session.py`

### Requirements

- Python 3.9+ recommended for the type annotations and standard-library features used by the script.
- ArcGIS API for Python (`arcgis`) and `requests`.
- Access to the target ArcGIS Online organization or ArcGIS Enterprise Portal.
- Credentials sufficient to search the selected organization and inspect the candidate item metadata/endpoints.
- Shared `UTILITIES` package in the project layout expected by the script.
- Writable output directory and reachable SMTP server.

### Security: Certificate (TLS) Validation

The scanner treats TLS verification as a required security control. The administrator chooses the verification strategy with `--tls-mode`:

1. **Standard (default)** — verifies HTTPS certificates using the standard public CA set.
2. **OS Trust Store** — verifies using the operating system trust store and requires the `truststore` package.
3. **Custom CA Bundle** — verifies using the PEM bundle supplied by `--ca-bundle-path`.

Before authenticated scanning starts, the script performs a TLS handshake check against the configured Portal/AGOL URL. A failed check aborts the run.

**SSRF / network-boundary defenses:** The scan requires an authenticated organization ID before it will search for candidates, and full/test searches are explicitly organization-scoped. Candidate item IDs are validated before use. For service URLs, the script requires HTTPS and validates the destination hostname against trusted Portal/federation hosts; AGOL permits Esri's `*.arcgis.com` boundary. The organization token is not sent to a service URL until that validation succeeds. The recognized proxy exception is limited to the exact hostname `utility.arcgis.com` rather than an arbitrary URL substring.

**Token handling:** Authentication is provided through the shared `TokenProvider`. A stale token is refreshed once under a lock when a REST request returns an authentication failure, which prevents concurrent workers from repeatedly refreshing the same token. Tokens are not intentionally logged, and token-bearing error text is passed through `redact_token()` before logging or email.

**Large/malformed responses:** The current script does not define an independent maximum HTTP response size. It does validate response shapes at important boundaries: search responses must contain lists of object results, REST errors are parsed explicitly, credential-info responses must be JSON objects, and registered-app responses must be JSON objects. Malformed or failed responses become check errors rather than positive findings. The actual HTTP response-size protections, if any, are delegated to the shared REST/session implementation.

**Python import-path/package shadowing:** The script puts the expected project root into `sys.path` before importing `UTILITIES`, then verifies that the imported `UTILITIES` package came from the expected `UTILITIES` directory. This prevents an unrelated package of the same name from silently replacing the intended shared package.

> The current implementation does not remove equivalent project-root entries before inserting the expected root. The package-origin verification is present; equivalent-path cleanup is not.

**Redirect following:** Direct REST credential checks explicitly disable automatic redirects with `allow_redirects=False`. Redirect targets are inspected and revalidated manually, with a maximum redirect count, before another request is made. This is especially important because the request carries an organization token.

### Configuration

Create an organization-specific TOML file and pass it with `--config`. The selected `--target` chooses the configuration section (`portal` or `agol`).

Required settings are:

`portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`

Example:

```toml
[portal]
portal-url = "https://your-portal.example.com/portal"
username = "your-username"
keyring-service = "your-keyring-service"
output-dir = "C:\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "reports@example.com"

[agol]
portal-url = "https://yourorg.maps.arcgis.com"
username = "your-username"
keyring-service = "your-keyring-service"
output-dir = "C:\reports"
smtp-server = "smtp.example.com"
email-to = "recipient@example.com"
email-from = "reports@example.com"
```

Passwords are handled through the shared authentication/keyring configuration rather than stored directly in the TOML.

`max-email-mb` can be supplied through the shared configuration to control attachment handling.

### How it works

1. **Validate the target** — The script classifies the configured URL and requires it to match the requested `--target` (`portal` or `agol`).
2. **Build secure REST session** — The selected TLS mode is applied to the direct REST session, which uses connection pooling and retries.
3. **Verify TLS** — A one-time TLS handshake check is performed before live scanning traffic.
4. **Connect to GIS** — The ArcGIS SDK connection is created with the selected TLS configuration and a managed token provider.
5. **Build trusted destinations** — The configured Portal host, the exact `utility.arcgis.com` proxy host, and federated server/admin hosts for Portal are assembled into the trusted service-host boundary. AGOL allows Esri's `.arcgis.com` suffix.
6. **Resolve the organization** — The authenticated organization ID is required. The script refuses to run an unscoped credential inventory.
7. **Apply exclusions** — Requested `--exclude-group-id` groups are resolved completely; a failed exclusion lookup aborts rather than silently scanning items that should have been excluded.
8. **Search for candidate items** — The search covers the service item types listed above plus API Key items. Test mode uses a configurable candidate count; full mode uses strict date-range bucketing to enumerate the full organization-scoped result set.
9. **Validate candidates** — Each returned candidate must be an object with a valid 32-character item ID and an expected item type.
10. **Check for embedded credentials** — Service items are checked through the credentials info endpoint and, when necessary, by requesting the service URL and looking for the stored-credential validation error. The recognized `utility.arcgis.com` proxy pattern reports its backend source URL. API Key items use registered-app information to determine whether a live API key/client-secret configuration exists and capture its privileges and restrictions.
11. **Refresh stale tokens when needed** — Authentication failures trigger a controlled token refresh and one retry.
12. **Write findings and send email** — Positive findings are written to the CSV. The log and CSV are gathered under the shared email attachment policy. If any candidate check failed, the email marks the run as incomplete and warns that a zero-finding result is not authoritative.

### Test mode (default)

**Test mode is on by default.** The script checks the first **25 candidate items** by default. The candidate limit can be changed with `--test-count`, up to the shared `DEFAULT_CAP`.

Use `--no-test` to scan the full organization-scoped candidate population.

Test mode is intended to verify configuration, authentication, TLS, search, credential checks, output, and email delivery before a full scan. It still performs real authenticated checks against the selected candidates.

### Usage

**Scenario 1: Quick sanity check**

You have just configured a new organization and want to validate the connection and check a small number of candidate items before a full inventory.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target portal
```

This checks the first 25 candidates by default.

**Scenario 2: Use a larger test sample**

The first 25 candidates are not representative enough for your organization, so you want to inspect 100 candidates without launching a full scan.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target agol --test-count 100
```

**Scenario 3: Full organization scan**

The sanity check succeeded and you need a complete organization-scoped inventory of service items and API Key items.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target agol --no-test
```

Full mode uses strict organization-scoped search and date-range bucketing so a result-cap collision does not silently become a partial report.

**Scenario 4: Exclude a known group**

Your organization keeps demonstration or other deliberately excluded items in a dedicated group.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target portal --no-test --exclude-group-id GROUP_ID_1,GROUP_ID_2
```

The script resolves each requested group and aborts if an exclusion group cannot be resolved, rather than silently broadening the scan.

**Scenario 5: Use the operating-system trust store**

The runtime machine already trusts the organizational CA used for the Enterprise Portal.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target portal --tls-mode os-trust-store
```

**Scenario 6: Use a custom CA bundle**

Your Enterprise Portal uses an internal certificate authority that is not in the normal public CA bundle.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target portal --tls-mode custom-ca-bundle --ca-bundle-path "C:\certs\internal-ca.pem"
```

**Scenario 7: Increase concurrency**

Your environment has sufficient Portal/network capacity and you want more credential checks running in parallel.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target agol --no-test --workers 32
```

The allowed range is 1–64 workers.

**Scenario 8: Redirect one run's report to a different recipient**

A security review team wants the report for a particular run without changing the normal recipient list in the configuration.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --config org1.config.toml --target portal --no-test --email-to "security@example.com"
```

**Scenario 9: Show current command-line options**

You are integrating the scanner into a scheduled process and want the current command-line help.

```bash
python PORTAL_SHARED_CREDENTIALS_SCAN.py --help
```

### Command-line options

| Option | Required? | Default | Description |
|---|---|---|---|
| `--config FILE` | Yes | — | Path to the organization TOML configuration. |
| `--target {portal,agol}` | Yes | — | Selects the configuration section and scan environment. |
| `--no-test` | No | Test mode | Scan the full organization-scoped candidate population instead of the test sample. |
| `--test-count N` | No | `25` | Number of candidate items to check in test mode; range is `1` through the shared `DEFAULT_CAP`. |
| `--workers N` | No | `12` | Credential-check concurrency; range is `1` through `64`. |
| `--portal-url URL` | No | from config | Override the configured Portal/AGOL URL. |
| `--username USERNAME` | No | from config | Override the configured username. |
| `--keyring-service SERVICE` | No | from config | Override the configured keyring service. |
| `--output-dir DIR` | No | from config | Override the output directory. |
| `--smtp-server SERVER` | No | from config | Override the SMTP server. |
| `--email-to ADDRESSES` | No | from config | Override completion-email recipient(s). |
| `--email-from ADDRESS` | No | from config | Override completion-email sender. |
| `--exclude-group-id IDS` | No | none | Comma-separated group IDs whose members are excluded from the candidate population. |
| `--tls-mode MODE` | No | `standard` | TLS verification mode: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path PATH` | Required only for `custom-ca-bundle` | none | PEM CA bundle used when `--tls-mode custom-ca-bundle` is selected. |

### Performance

Credential checking is performed concurrently using a bounded thread pool. The default of 12 workers balances speed with server load; the maximum is 64.

Each candidate can require multiple authenticated REST requests. Service items may use a credentials info-file check followed by a service URL check; API Key items require a registered-app information request. Failed authentication can add a controlled retry after token refresh.

Search performance is also bounded deliberately. Full mode uses organization-scoped date-range bucketing and aborts when a bucket cannot be safely split further. This favors completeness and avoids silently accepting a partial result set.

### Observed Runtime

The existing project observations are:

- **Test mode (25 items)**: approximately 30 seconds to 2 minutes.
- **Full scan (10,000 items)**: approximately 2 to 8 hours, depending on worker count, network latency, remote-service response time, Portal/AGOL performance, and the number of requests required by each item.

These are reference observations, not guarantees. Large organizations, slow remote services, filtered network paths, and higher authentication-check volume can increase elapsed time.

### Output file

When findings exist, the script writes:

**CSV Report:**

```text
{org-name-safe}_SHARED_CREDENTIALS_SCAN_{target}_{timestamp}.csv
```

The current CSV columns are, in order:

```text
item_id,title,owner,type,service_url,item_home_url,platform,privileges,http_referrers,redirect_uris,backend_source_url
```

The CSV contains one row per item with embedded/shared credentials. API Key findings can populate the privilege/referrer/redirect fields; recognized proxy findings can populate `backend_source_url`.

**Log File:**

```text
{org-name-safe}_SHARED_CREDENTIALS_SCAN_{target}_{timestamp}.log
```

The log contains search progress, candidate-check results, authentication/REST errors, findings, completeness status, and timing information. The log filename is reserved atomically with mode `0600` so a simultaneous run gets a unique timestamp suffix rather than overwriting an existing log.

**Email:**

The configured recipient receives a summary and the available CSV/log attachments, subject to the shared email attachment policy. When check errors occur, the subject and body explicitly say the scan is incomplete.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The scan ran to completion and the results email was sent. This can include an **INCOMPLETE** report when individual candidate checks failed. A zero exit code means the process completed; it does not mean that every candidate was checked successfully. |
| `1` | Fatal or controlled abort, including TLS/session setup failure, failed startup TLS verification, connection failure, inability to resolve the organization boundary, failed required exclusion resolution, or another unrecoverable runtime failure. |
| `2` | Invalid command-line arguments, including an incompatible TLS option such as `custom-ca-bundle` without `--ca-bundle-path`. |

### Notes and caveats

- **Credential Validation Errors**: Some remote services may return the recognized stored-credential validation error even when the underlying business use is not what you expected. Review positive findings carefully and confirm ownership and intended access.
- **Proxy Services**: Services using the recognized exact `utility.arcgis.com` proxy hostname are flagged with their `backend_source_url` so the actual remote service can be investigated.
- **API Keys**: API Key items are checked through `registeredAppInfo`. The report captures `privileges`, `http_referrers`, and `redirect_uris` when the live API key information indicates a non-personal API key or client secret.
- **Service URL security**: The organization token is sent to an item-supplied service URL only after HTTPS and trusted-host validation succeeds. AGOL trusted destinations can be under the Esri `*.arcgis.com` domain; Portal mode additionally uses the configured/federated service-host boundary.
- **Performance**: Scanning large organizations may take several hours. Consider running during off-peak hours and adjust `--workers` gradually.
- **Token Redaction**: Error messages passed through `redact_token()` are sanitized before being logged or emailed.
- **Token refresh**: Authentication failures trigger one controlled refresh/retry path. Repeated authentication failures do not cause an unbounded refresh loop.
- **Network Dependencies**: Slow network connectivity or remote-service timeouts will extend scan duration and can produce incomplete results.
- **Test Mode Recommended**: Always start with test mode to verify configuration before running a full scan.
- **Organization scoping is mandatory**: The script will not run an unscoped credential inventory. If the authenticated organization ID cannot be resolved, the run aborts.
- **Search completeness is strict**: Full-mode buckets that cannot be safely split further at the shared result cap cause an abort rather than a quietly incomplete inventory.
- **Exclusion failures are strict**: If a requested exclusion group cannot be resolved or read, the scan aborts rather than silently scanning items that were supposed to be excluded.
- **Malformed candidates are errors**: Candidate records that are not objects, have invalid item IDs, or have unexpected item types are treated as check errors rather than findings.
- **Malformed responses are not findings**: A non-object JSON response or failed REST operation is treated as an error condition, not as evidence that credentials exist.
- **TLS verification is always enabled**: The script does not provide a mode that disables certificate validation. The `--tls-mode` option selects the trust source.
- **No destructive remediation**: This tool only inventories and reports. It does not delete credential-bearing items or alter their authentication configuration.
- **Point-in-time inventory**: Results describe the organization and candidate items available during the scan. A later change to an item or credential requires another scan.