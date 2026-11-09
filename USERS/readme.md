# USER_REPORT.py

**User Inventory Report — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal, with optional forest-wide Active Directory status**

`USER_REPORT.py` inventories every user account in an ArcGIS Online organization or an ArcGIS Enterprise Portal deployment — username, name, contact info, role, license type, provider, email-verification status, creation/last-login dates, a `new_user` recency flag, and item and group ownership counts — and writes the result to a timestamped CSV. Optionally, it can also cross-reference each account against **Active Directory, forest-wide**, adding two columns that report whether the underlying AD account is active, disabled, expired, or locked, and which organizational unit it lives in.

The script is **multi-org aware**: a required `--config <org_toml_path>` argument points at a specific organization's TOML config file, and the resolved organization name prefixes both the output filenames and the email subject line (e.g. `ORG1_PORTAL_USER_REPORT_20260826_1300.csv`).

Everything is logged to the console and a log file, and a completion email is sent with the log and CSV attached.

This script is strictly a **read-only inventory tool** — it never disables, deletes, or modifies any ArcGIS or Active Directory account.

---

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory Users](#why-inventory-users)
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

This report shows you a complete roster of everyone with a user account in your ArcGIS Online organization or ArcGIS Enterprise Portal. Think of it as a directory: one row per person (or service account), showing their name, contact info, the type of access they have, when their account was created, when they last logged in, and how much content they personally own.

If you turn on the optional Active Directory (AD) check, the report also tells you whether each person's underlying network account (the one they use to log into their computer) is still active, or whether it's been disabled, expired, or locked — which is a quick way to find ArcGIS accounts that may need cleanup.

This is a **snapshot in time** — account status, login history, and AD state all reflect the exact moment the report ran.

## Why Inventory Users

This script is not a duplicate-layer inventory tool. The requested heading is retained for consistency with the documentation structure used for this project; this utility inventories ArcGIS user accounts, their access characteristics, ownership counts, and optional Active Directory status. The reason user accounts specifically need to be inventoried is that every account is simultaneously a cost, a security exposure, and a potential single point of failure for the content it owns — and none of those three facts is visible from the one place administrators normally interact with accounts, which is the moment of creating or assigning one. After that, an account tends to persist untouched regardless of whether the person still needs it, still works there, or ever used it at all. A roster export is the deliberate act of looking at the whole population at once, which is the only way those three risks become visible together.

Keeping an accurate list of who has access to your GIS platform matters for several practical reasons:

- **Licenses cost money.** Every named user account uses a license "seat." Accounts that are no longer needed quietly waste budget.
- **Unused accounts are a security risk.** An account nobody is watching — especially one belonging to someone who has left the organization — is exactly the kind of forgotten door that creates risk.
- **Content ownership matters.** If one person owns dozens of maps or groups and their account is disabled or deleted, all of that content can become inaccessible until an administrator reassigns it.
- **Roles change, but records don't.** People get promoted, switch teams, or should have had access reduced after a project ended — none of that shows up except in a full report.
- **New accounts deserve a second look.** The report flags any account created in the last week, making it easy to confirm new accounts were expected.

What makes this particular scan worth running — rather than glancing at the user list in the admin console — is that it assembles, in one row per person, the handful of facts that together let you judge an account, facts that are otherwise scattered across different screens or not surfaced at all. Creation date next to last-login date next to role, license type, provider, and the count of items and groups the account owns turns a bare name into a decision: this account is an active editor, that one is a never-logged-in seat wasting a license, this one is a departed employee who still owns forty maps. The ownership counts in particular are something the console does not put in front of you, yet they are exactly what determines whether removing an account is a quick cleanup or a content-orphaning accident. The optional Active Directory cross-check extends the inventory past the boundary of ArcGIS itself: it answers the question ArcGIS alone cannot — whether the person behind an account still has a live network identity at all — so a GIS account whose owner was offboarded in AD months ago stops being invisible and becomes a flagged, actionable row.

Having this inventory in one place supports routine security reviews, helps answer audit questions quickly, and makes it worth running on a schedule rather than only when a problem forces the question. Drift in a user population is continuous and silent: people join, leave, change teams, and get license upgrades, and none of those events announces itself anywhere a casual observer would see. A periodic roster turns that drift into something comparable from one run to the next — new accounts to confirm, stale accounts to question, ownership concentrations to plan around — and the `new_user` flag makes the most recent changes trivially easy to cross-check against expected onboarding. Because the script is strictly read-only and never disables, deletes, or modifies any ArcGIS or AD account, it is completely safe to run as often as a license or security program requires, producing the evidence those decisions need without ever being the thing that acts on a live account.

## Why it's a problem

- **Licensing is expensive.** Every named user consumes a license slot. A stale account (someone who left, a contractor whose engagement ended, a forgotten test account) quietly wastes that cost until someone notices.
- **Dormant accounts are a security surface.** An account that hasn't logged in for a long time, especially one whose owner has left, is exactly the kind of credential attackers look for — it's less likely to be actively monitored, and if its AD identity is still enabled, the account remains a fully functional door into your content.
- **Ownership concentration is a risk.** A user who owns a large number of items or groups represents a single point of failure. If that account is disabled, deleted, or becomes unreachable, everything they own can become orphaned or inaccessible.
- **Drift happens silently.** Role changes, license upgrades, and provider shifts don't show up anywhere except in a full roster export like this one.
- **Unverified accounts are easy to miss.** An account that has never verified its email may indicate it was created but never actually claimed by its intended owner.
- **Unexpected new accounts deserve a second look.** A newly-flagged account nobody remembers approving is worth asking about.

## Why removal requires caution

The findings this report surfaces often lead to account removal or license reclamation, so a few cautions apply before acting on any row:

- **A stale-looking account is not automatically safe to remove.** An account that hasn't logged in for a long time might be a service account, an automation identity, or a seasonal employee who logs in only part of the year. Confirm what the account is actually used for before disabling or deleting it.
- **AD and ArcGIS identities can point at the same person through different accounts.** Before removing an ArcGIS account because its AD counterpart is disabled, confirm there isn't a second, still-valid AD account that simply wasn't found by this lookup.
- **Reassign ownership before removing an account.** The item/group counts in this report are a snapshot. Always transfer content ownership *before* disabling or deleting an account — removing it first can strand its content and require manual recovery.
- **`NOT_IN_AD` and `AD_ERROR` are not evidence of anything by themselves.** See the recommendations below before treating either as a reason to act.

## Recommendations for investigation

- **Start with accounts flagged `DISABLED` or `EXPIRED` in Active Directory.** These are people whose network identity is confirmed gone or terminated, but whose ArcGIS account is still active — the highest-value findings for both security and license reclamation.
- **Treat `LOCKED` as lower urgency than `DISABLED`/`EXPIRED`.** A locked account often just means someone mistyped their password recently and it will unlock itself, whereas `DISABLED` and `EXPIRED` reflect a deliberate administrative action.
- **`NOT_IN_AD` deserves its own look.** This means the lookup ran successfully but found no matching AD account — which can mean a service account, an external/federated account, or contact information that's out of sync. Don't assume this status means "safe" or "unsafe" without checking which case applies.
- **`AD_ERROR` rows need re-running, not acting on.** This status means the *lookup itself* failed for that one row. Re-run with the AD debug option turned on if these accumulate.
- **Compare last-login date against AD status.** A person still `ACTIVE` in AD but hasn't logged into Portal/AGOL in a long time is a license-reclamation candidate. Someone whose AD account is `DISABLED` is a different case entirely.
- **Check for unverified accounts.** An old account that's never verified its email and has never logged in may simply have never been claimed by its owner and could be a cleanup candidate.
- **Use the "new account" flag as an onboarding cross-check.** Compare recently-flagged rows against your known onboarding list for the week — a match confirms expected provisioning; an unmatched row is worth asking about.

## Column descriptions

**Base columns** (always present):

| Column | Description |
|---|---|
| `platform` | `PORTAL` or `AGOL`. |
| `username` | The user's login username. |
| `fullName` | The user's display name. |
| `firstName` / `lastName` | First and last name, where set. |
| `email` | The user's email address on file. |
| `email_verified` | Email-verification status for **built-in (`arcgis` provider) accounts only**: `verified`, `unverified`, or `unknown`. **Blank** for enterprise/SAML/social login accounts, since Esri does not track email verification for federated identities. |
| `role` | The user's role (e.g., `org_admin`, a custom role token). |
| `roleid` | The underlying role ID, if different from `role`. |
| `userLicenseTypeId` | The user's assigned license level/type ID. |
| `idpUsername` | The identity-provider username, if the org uses an external identity provider — also the primary value used for the AD lookup. |
| `disabled` | Whether the ArcGIS account itself is disabled (`True`/`False`), as reported by the platform. |
| `provider` | The account's authentication provider (e.g., `arcgis`, `enterprise`, a SAML provider name). |
| `date_created` | Account creation date, `YYYY-MM-DD` format. |
| `new_user` | `"yes"` if the account was created within the **7 days immediately preceding this run**, `"no"` otherwise. |
| `date_modified` | Account last-modified date, `YYYY-MM-DD` format. |
| `last_login` | Last login date, `YYYY-MM-DD` format, or `1969-01-01` if the account has never logged in. |
| `numItems` | Count of content items owned by this user (best-effort; blank if the count query failed after retries). |
| `numGroups` | Count of groups owned by this user (best-effort; same behavior as numItems). |
| `description` | The account's description field, if set. |

**AD columns** (present only when `--ad-check` is used):

| Column | Description |
|---|---|
| `ad_status` | One of `ACTIVE`, `DISABLED`, `EXPIRED`, `LOCKED`, `NOT_IN_AD`, or `AD_ERROR` — see [Recommendations for Investigation](#recommendations-for-investigation) for what each means. |
| `ad_ou` | The AD organizational unit path (the chain of OUs the account lives in), or blank if the account wasn't found or the lookup failed. |

---

# For Administrators

## Dependencies on the shared UTILITIES package

- `cli.py`
- `config.py`
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `restutils.py`
- `secure_session.py`

## Requirements

- ArcGIS API for Python (`arcgis`).
- A populated org-specific TOML config file passed via `--config`.
- Credentials in the OS keyring or `PORTAL_PASSWORD` fallback.
- SMTP access if completion/failure email is required.
- For `--ad-check`: Windows, domain join, reachable Global Catalog/domain controller, read access, and pre-installed approved `pywin32`.

## Security: Certificate (TLS) Validation

Every Portal/AGOL connection verifies the server certificate. There is no option to disable TLS certificate validation.

- **`standard` (default):** validates against the public CA bundle.
- **`os-trust-store`:** validates against the operating-system trust store and requires `truststore`.
- **`custom-ca-bundle`:** validates against the PEM file supplied with `--ca-bundle-path`.

Before authentication, the script performs a real TLS handshake check against `portal-url`. If the check fails, the run aborts and sends a failure notification. The ArcGIS SDK connection then uses the same selected verification mode.

### SSRF / network-boundary defenses

The configured Portal URL must be HTTPS and is validated before use. Unsafe URL forms are rejected, including embedded credentials, query strings, fragments, whitespace/control characters, and malformed ports. User enumeration and the independent REST completeness check use the same explicit organization ID (`orgid:<id>`), so the script does not intentionally fall back to an unscoped organization search. Returned user records are validated to ensure they remain in the authenticated organization scope.

### Token handling

The script obtains authentication through the shared `provider_for_gis()`/`TokenProvider` mechanism. Direct REST calls place the token in POST form data rather than in the request URL. Authentication failures can trigger a synchronized token refresh and one retry. Token-bearing error text is passed through `redact_token()` before logging or email.

### Large/malformed responses

The script validates JSON response structure, user-list entries, totals, pagination progress, duplicate usernames, and organization scope. It also verifies that the organization-scoped REST total matches the SDK result in full mode; an unresolved mismatch aborts rather than producing a truncated report.

There is **no explicit 64 MB response-size check implemented in `USER_REPORT.py` itself**. Any transport-level response-size limit comes from the shared secure-session implementation.

### Redirect following

`USER_REPORT.py` does **not explicitly set `allow_redirects=False`** on its direct REST `session.post()` calls. Redirect behavior is therefore delegated to the configured session/HTTP implementation rather than being an explicit control in this script. This should not be documented as a script-level redirect-hardening feature.

### Python import-path / package shadowing

The script inserts the parent project root at the front of `sys.path` when it is not already present. It does **not** remove equivalent duplicate project-root entries and it does **not** verify the imported `UTILITIES` package origin with `find_spec()` or a post-import path check. The current protection is limited to inserting the expected root first.

## Configuration

A specific organization is selected at run time with the required `--config <org_toml_path>` argument. Within that file, the required `--target portal` or `--target agol` argument chooses the matching section for connection, email, and output settings.

Settings sourced from config are `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, and `email-from`. `email-to` may be a single address or a comma-separated list. Any config value can be overridden on the command line, and a command-line flag always wins.

The resolved organization name (`org_name` / `org_name_safe`) from the config file is used to prefix output filenames and the email subject line.

The Active Directory options (`--ad-check`, `--dc`, `--ad-debug`), `--count-delay`, and TLS options (`--tls-mode`, `--ca-bundle-path`) are **command-line only**.

There is no configuration setting for the `new_user` window — it is fixed at 7 days.

## How it works

1. Logging is initialized in two steps: console-only first, then with a file handler once the output directory is resolved.
2. `--ad-check`/`--dc` are validated immediately — a `--ad-check` without `--dc` aborts with exit code `2` before any network connection.
3. The run's start time is captured for the `new_user` calculation — every row in a single run is judged against this same instant.
4. If `--ad-check` is set, the AD resolver is stood up and validated *before* connecting to Portal/AGOL. A failed AD resolver is treated as fatal.
5. A one-time TLS handshake proof runs against the target URL using the chosen `--tls-mode`. Failure aborts the run with a notification email.
6. Connect via `connect_gis()`, which always verifies TLS independently.
7. On AGOL, resolve the organization ID and add an `orgid:<id>` clause to the user search scope (on Portal, scoping is skipped as a no-op).
8. Build the search query, excluding Esri system accounts, and enumerate users.
9. For each user, sequentially: read `email_verified` status, `new_user` flag, and `numItems`/`numGroups` counts; perform AD lookup if enabled.
10. Write the CSV, then email the log and CSV together using the configured attachment size policy.

## Test mode (default)

**Test mode is ON by default.** It limits enumeration to the first **250 users** (`TEST_USER_LIMIT`). Pass `--no-test` to inventory the full roster (capped internally at 10,000 users).

Test mode affects **only** how many users are enumerated — it has no effect on whether `--ad-check` runs. If you pass both `--ad-check` and leave test mode on, you'll get AD lookups for the first 250 users only, which is a reasonable way to validate the AD resolver before committing to a full, slower run.

## Usage

**Scenario 1 — Monthly Portal license audit, quick sanity check first.**

You manage Portal for your organization and want to spot-check the report before running it against the full roster — confirming the config, credentials, and CSV columns all look right.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal
```

This runs in test mode (first 250 users), writes `ORG_PORTAL_USER_REPORT_<ts>.csv`, and emails the result.

**Scenario 2 — Full AGOL roster export for a quarterly license reconciliation.**

Your AGOL org has grown and finance needs a complete, current user list to reconcile against license invoices.

```bash
python USER_REPORT.py --config OTHER_ORGLDT.config.toml --target agol --no-test
```

This inventories the entire AGOL org (up to 10,000 users), including `numItems`/`numGroups` for every account. Expect this to take longer than test mode since those counts are looked up one user at a time.

**Scenario 3 — Security review: find ArcGIS accounts whose AD identity has been terminated.**

Security asked for a report identifying any Portal account that's still active even though the person's AD account was disabled or expired.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal --no-test \
    --ad-check --dc dc01.enterprise.com
```

This adds `ad_status`/`ad_ou` columns, cross-checking every user (forest-wide, via the Global Catalog on `dc01.enterprise.com`) against Active Directory using your current Windows login — no password prompt. Filter the resulting CSV for `ad_status` in `DISABLED`/`EXPIRED` to get the highest-priority findings.

**Scenario 4 — Validating the AD resolver before running against a large roster.**

You've just set up `--ad-check` on a new machine and want to confirm the ADSI connection works correctly before running it against thousands of users.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal \
    --ad-check --dc dc01.enterprise.com
```

Test mode stays on by default, so this validates the AD resolver against only the first 250 users — a fast, low-risk way to confirm connectivity before committing to a full run.

**Scenario 5 — Diagnosing unexpected `AD_ERROR`/`NOT_IN_AD` results.**

A handful of rows came back with unexpected AD statuses and you need to see exactly what LDAP filter was tried for each.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal --no-test \
    --ad-check --dc dc01.enterprise.com --ad-debug
```

This is verbose — one detailed block per user — so it's best reserved for troubleshooting.

**Scenario 6 — A full AGOL run is hitting Esri's rate limiter.**

Your last full-roster AGOL run logged repeated `Too many requests` warnings on the `numItems`/`numGroups` lookups, leaving some cells blank even after automatic retries.

```bash
python USER_REPORT.py --config OTHER_ORGLDT.config.toml --target agol --no-test --count-delay 0.3
```

Pacing the per-user count calls with `--count-delay` trades a longer run time for fewer throttling events and fewer blank count cells.

**Scenario 7 — Weekly new-account onboarding check.**

Your IT onboarding process creates new Portal accounts throughout the week. Run this once a week and filter the CSV for `new_user = yes` to confirm only expected accounts were created.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal --no-test
```

Cross-check the `new_user = yes` rows against your onboarding tickets for the week. An unmatched row is worth a follow-up question.

**Scenario 8 — Connecting to an internal Portal with an organizational CA certificate.**

Your Enterprise Portal's certificate is signed by an internal Certificate Authority that's trusted at the Windows OS level but isn't in the public CA bundle.

```bash
python USER_REPORT.py --config ORG.config.toml --target portal \
    --tls-mode os-trust-store
```

This validates the startup handshake against the OS certificate store rather than the public CA bundle.

## Command-line options

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Path to the organization-specific TOML config file. |
| `--target` | *(required)* | Which org to report on: `portal` or `agol`. |
| `--no-test` | test mode ON | Inventory the full organization-scoped roster, with completeness verified by matching the SDK result to independent REST totals, instead of the first 250. |
| `--ad-check` | off | Also look each account up in Active Directory (forest-wide) via ADSI. Requires `--dc`. |
| `--dc` | *(required if `--ad-check`)* | Domain controller hostname/FQDN to query. Its Global Catalog is searched, spanning the forest. |
| `--ad-debug` | off | Emit per-user AD lookup detail (LDAP filter and matched DN). Off by default because it's verbose. |
| `--count-delay` | `0` | Seconds to pause between users before the `numItems`/`numGroups` lookups. Raise this (e.g., `0.25`–`0.5`) on full runs if throttling occurs. |
| `--tls-mode` | `standard` | TLS verification strategy: `standard` (public CA), `os-trust-store` (OS store; requires `truststore`), or `custom-ca-bundle` (requires `--ca-bundle-path`). |
| `--ca-bundle-path` | none | Path to a PEM CA bundle. Required when `--tls-mode` is `custom-ca-bundle`. |
| `--portal-url` | from config | Portal / AGOL URL. |
| `--username` | from config | Login username. |
| `--keyring-service` | from config | Keyring service holding the password. |
| `--smtp-server` | from config | SMTP relay for the completion email. |
| `--email-to` | from config | Recipient(s). |
| `--email-from` | from config | Sender address. |
| `--output-dir` | from config | Where the CSV and log are written. |

There is no `--max-email-mb` flag — like every other script in this project, it is resolved solely from the org's TOML config file.

## Performance

- **User enumeration** uses the SDK search plus an independent organization-scoped REST total before and after the search in full mode. Test mode intentionally limits the SDK search to 250 users.
- **`numItems`/`numGroups` are counted per user, sequentially** — no thread pool. On a large full-mode roster, this is the slowest part of the run.
- **These per-user count calls can trigger Esri's rate limiter on a large run**, surfacing as `Too many requests`. Each call retries automatically with exponential backoff (up to 4 attempts) before giving up and leaving the cell blank.
- **`email_verified` and `new_user` add no extra HTTP round-trips.** Both are resolved entirely from the user-search results already fetched.
- **The AD check, when enabled, adds one (or two if the first lookup misses) Global Catalog search per user**, also sequential.
- **The one-time TLS handshake check adds a single connection at startup** — negligible compared to the rest of the run.

Progress is logged every 25 users, so a long run gives visible feedback.

## Observed Runtime

| Run Type | Duration | Approx. User Count |
|----------|----------|---------------------|
| Portal | ~5 minutes | ~5,000 |
| AGOL | ~5 minutes | ~6,000 |
| Portal + `--ad-check` (full) | ~15–25 minutes | ~5,000 (+ 1 GC query/user) |

Durations scale mainly with roster size and whether `--ad-check` is used.

## Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp and the resolved organization prefix:

```
[ORG]_[PLATFORM]_USER_REPORT_<ts>.csv
```

A matching log file, `[ORG]_USER_REPORT_<ts>.log`, is written alongside it.

One row per enumerated user. See [Column Descriptions](#column-descriptions) for the full list of columns.

## Exit codes

- **`2`** — Either `--ad-check` was passed without `--dc`, or `--tls-mode custom-ca-bundle` was passed without `--ca-bundle-path`. Raised immediately before any connection is attempted.
- **`1`** — Either the AD resolver could not initialize (pywin32 unavailable or ADSI bind failed), or the one-time TLS handshake check failed. A failure-notification email is sent (log attached, no CSV) and the run aborts before contacting Portal/AGOL.
- **`0` (implicit)** — A run that completes normally. This includes runs where individual `numItems`/`numGroups` lookups or per-user AD lookups failed after retrying — those failures are logged as warnings and reflected in the data rather than aborting.

## Notes and caveats

- **`platform` comes from `--target`, not the URL — by design.** This script explicitly treats `--target` as authoritative and only uses the URL as a sanity-check warning.
- **`new_user` uses a fixed 7-day window measured from run start.** Every row is judged against the same instant captured at the very start of the run.
- **Full mode verifies completeness rather than silently truncating.** The script gets an organization-scoped REST total before and after the SDK search and aborts if a stable total does not match the number of returned users. A changing roster is retried and, if completeness cannot be proven, the run is aborted.
- **No thread pool in this script.** User enumeration, item/group counting, and AD lookups are sequential, one user at a time. A full-mode run with `--ad-check` takes meaningfully longer than comparable parallelized scans.
- **`numItems`/`numGroups` can hit Esri's rate limiter on a large roster.** A full run makes one or two count calls per user with no concurrency. Each call retries automatically with exponential backoff (up to 4 attempts) before giving up and leaving the cell blank. If throttling still recurs across many users, add `--count-delay <seconds>` to pause between users.
- **`email_verified` is only meaningful for built-in accounts.** Enterprise/SAML/social-login accounts always get a blank value — this is intentional, as Esri doesn't track email verification for federated identities.
- **AD check fails fast instead of degrading silently.** If the ADSI resolver cannot initialize at all, the run sends a failure-notification email and aborts rather than producing a CSV where every `ad_status` reads `AD_ERROR`. Once the resolver *is* up, individual per-row lookup failures degrade gracefully to `AD_ERROR` for just that row.
- **There is no LDAP/Kerberos fallback for the AD check.** ADSI (via `pywin32`) is the only supported mechanism, authenticating as the current Windows login with no password ever collected or stored.
- **`pywin32` auto-installs, including post-install DLL registration.** If `pywin32` isn't importable when `--ad-check` runs, the script attempts `pip install pywin32` automatically and runs the post-install step if needed.
- **Organization-scope failure aborts the run.** If the authenticated organization ID cannot be resolved, the script stops rather than performing an unscoped REST completeness check.
- **A failed TLS handshake check aborts the run before any user data is touched.** This is deliberate fail-closed behavior.
- **Log filename is org-prefixed but not platform-prefixed.** Only the CSV filename carries the platform label in addition to the org prefix.
- **Email attachment cap is configurable per-org.** This script resolves `max-email-mb` from the org's TOML config. If the CSV exceeds that cap it is first zipped; only if the zip is still too large is it left on disk with a note in the email body. The log file is always attached.
- **This is a snapshot, not a live view.** Account status, login dates, AD state, and the `new_user` flag all reflect the moment the scan ran.