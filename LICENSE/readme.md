# NAMED_USER_LICENSE_INVENTORY.py

**ArcGIS Pro Named-User License Inventory — ArcGIS Online (AGOL) or ArcGIS Enterprise Portal**

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why Inventory License Use](#why-inventory-license-use)
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
- [Observed runtime](#observed-runtime)
- [Output file](#output-file)
- [Exit codes](#exit-codes)
- [Notes and caveats](#notes-and-caveats)

---

# For Report Readers

## Overview

This report answers a straightforward question for your entire organization: **"Who has an ArcGIS Pro license, and are they actually using it?"**

The script looks at **every single user account** in your ArcGIS Online organization or ArcGIS Enterprise Portal — not just people who recently showed up in a usage graph — and creates a spreadsheet showing, for each user:

- Whether they have a Pro license, and which tier (Basic, Standard, or Advanced)
- Which add-ons (extensions) like Spatial Analyst or 3D Analyst are turned on for them
- Whether their license is checked out for offline use, and since when
- The last time they actually opened and used ArcGIS Pro
- Their role (Administrator, Publisher, User, Viewer, etc.)

The result is sent as a spreadsheet (CSV file) attached to an email, along with a summary and a detailed log of what the scan found.

## Why Inventory License Use

This script is a useful inventory question is whether ArcGIS Pro licenses and related entitlements are assigned and being used as expected.

ArcGIS Pro licenses are expensive and limited — most organizations buy a fixed number of "seats," and every seat assigned to someone who isn't using it is a seat nobody else can use. This is the core reason to inventory license use rather than just trust that assignments are correct: a license is a recurring, finite cost, and the gap between "assigned" and "actually used" is pure waste that grows silently. Seats get handed out when someone joins a project or team, but nothing ever reclaims them when the project ends or the person moves on, so an organization steadily drifts toward a state where a meaningful fraction of its paid seats are held by people who no longer open Pro. That drift is invisible day to day; the only way to see it is to deliberately measure every seat against actual usage, which is what this scan does.

Without a regular, complete inventory, it's nearly impossible to answer basic management questions like:

- Who currently holds a license?
- Who has a license but hasn't opened Pro in months?
- Which licenses are checked out offline — are they still active field work, or forgotten checkouts?
- Where would reassigning unused seats free up capacity for new users?

The reason this particular scan is worth running — rather than glancing at a built-in usage dashboard — is that it reconciles two things no single built-in view puts together: the full roster of who holds a license, and a trustworthy signal of whether each of them is genuinely using Pro. It starts from every organization user rather than from a usage log, so it can see the people a usage-based view structurally cannot: the licensed user who hasn't generated recent activity is exactly the person you most want to find, yet that person is the one a "who used Pro lately" report omits. The scan then layers up to three distinct last-used signals and records which one produced each value, so a stale date comes with the context needed to judge whether it is real inactivity or merely a gap in the data. It also captures offline checkouts and their age, turning a license state that is otherwise easy to forget — a seat quietly held in disconnected mode — into a visible, datable row. Taken together, these let an administrator distinguish a genuinely reclaimable seat from active-but-quiet field work, which is the distinction that makes reclamation safe.

Finally, this scan is worth running on a schedule because license utilization is never static and the cost of not knowing is continuous. New users arrive, projects end, extensions get assigned and forgotten, and offline checkouts age — each period's inventory is a fresh, complete picture rather than an extrapolation from recent activity, and comparing periods shows whether utilization is improving or whether unused seats are accumulating faster than they are reclaimed. Because the scan is strictly inventory-only and never revokes or reassigns anything, it is safe to run regularly as a standing cost-governance check: it produces the evidence a reclamation decision needs without ever being the thing that acts on it, so a complete, honest snapshot is always on hand when budget, renewal, or capacity questions come up.

## Why it's a problem

A report built only from "who recently used Pro" data quietly misses everyone who *hasn't* generated recent activity — which in a large organization can be most of your licensed users. That incomplete view can mislead you: a user who doesn't show up in a usage report looks identical to a user who was never given a license in the first place.

This report avoids that trap by **starting from every organization user**, not from a usage log. It shows the complete picture.

Also important: **don't confuse two different kinds of activity** tracked separately in this report:

- **`last_login`** = login to the website or mobile app. This has nothing to do with whether the person opened ArcGIS Pro.
- **`pro_last_used_session`** = an actual ArcGIS Pro desktop session.

Someone can log into the website daily while their Pro session sits untouched for months. That mismatch is exactly the pattern worth investigating.

## Why removal requires caution

This report is **inventory only** — it never revokes, reassigns, or disables any license. Before acting on what you find here, remember:

- **A blank or "never" `pro_last_used_session` doesn't always mean someone stopped needing Pro.** It might mean their most recent session falls outside the reporting window, or — if you're using ArcGIS Enterprise Portal — that no usage data source exists for Portal (unlike AGOL, which has a broader usage report fallback).

- **An offline checkout that looks old might still be active field work.** Someone OTHER_ORGng fieldwork without internet for weeks will look identical in this report to someone who forgot to check the license back in. Always ask before assuming the seat is abandoned.

- **Seasonal and project staff** may have normal, expected gaps between uses.

- **Always verify with the user or their supervisor** before reclaiming a license, rather than acting on this report alone.

## Recommendations for investigation

- **Start with `has_pro_license = no`.** These users hold no Pro license at all. If you expected broader coverage, this quickly shows the gap.

- **Compare `pro_last_used_session` against `last_login`.** A stale Pro date next to a recent website login is the strongest single signal that a seat might be reclaimable.

- **Check `taken_offline = yes` rows for stale checkouts.** The `disconnected_since` column shows how long ago the offline checkout began. A very old date paired with a stale `pro_last_used_session` is worth a direct conversation with that user.

- **Use `all_entitlements` as your complete record.** The `pro_license` and `pro_extensions` columns are friendly summaries. `all_entitlements` is the full, unfiltered list from the system — nothing gets lost.

- **Check `pro_last_used_source` before jumping to conclusions.** It tells you which of three data signals actually produced that user's `pro_last_used_session` date — useful context if a value looks surprising.

- **If a recently active user looks stale, widen the reporting window.** See [Usage](#usage) for the `--app-usage-window` option, which controls how far back the fallback usage data looks.

## Column descriptions

| Column | What it means | Where it comes from |
| --- | --- | --- |
| `PLATFORM` | `PORTAL` or `AGOL` — which system this user came from. | The `--target` option used to run the report. |
| `username` | The user's account name. | User directory. |
| `full_name` | The user's display name. | User directory. |
| `user_level` | The user's license level type. | User directory. |
| `role` | Friendly role label (`Administrator`, `Publisher`, `User`, `Viewer`), or a custom role name if your organization has defined one. | User directory + custom role lookup. |
| `has_pro_license` | `yes` if the user holds **any** Pro entitlement (a tier or an extension); `no` otherwise. | Derived from the user's complete entitlement list. |
| `pro_license` | The user's Pro tier: `ArcGIS Pro Basic`, `ArcGIS Pro Standard`, or `ArcGIS Pro Advanced`. Blank if none. | Classified from their entitlements. |
| `pro_extensions` | Semicolon-separated list of assigned Pro extensions (e.g., `Spatial Analyst; 3D Analyst`). Blank if none. | Classified from their entitlements; unrecognized codes still appear here. |
| `all_entitlements` | The complete, raw list of every entitlement code on this user's account. | Full audit trail, exactly as the system reported it. |
| `pro_last_used_session` | The most recent date the user had a real ArcGIS Pro desktop session. Resolved from up to three signals — see `pro_last_used_source`. Shows `never` if no usable value was found. | See `pro_last_used_source` column. |
| `pro_last_used_source` | Which signal supplied the `pro_last_used_session` value: `sessions-endpoint` (most reliable — direct Pro use record), `usage-report (fallback)` (AGOL only, broader usage summary), or `userEntitlements.lastLogin` (coarsest fallback, for inactive users). Blank if no signal had data. | Internal resolution logic — see [How it works](#how-it-works). |
| `last_login` | The user's most recent login to the website or mobile app. **Not** a sign they used Pro. Shows `never` if they've never logged in. | User account record. |
| `taken_offline` | `yes` if the user currently has their license checked out for offline use; `no` if that flag exists and is false; blank if unavailable. | Entitlement record. |
| `disconnected_session` | The name of the offline session, if one is active. | Entitlement record. |
| `disconnected_since` | When the offline checkout began. Shows `never` for a system placeholder value; blank if unavailable. | Entitlement record. |
| `disconnected_until` | When the offline checkout expires or is due back. Same formatting as `disconnected_since`. | Entitlement record. |

---

# For Administrators

## Dependencies on the shared UTILITIES package

This script imports from the following UTILITIES modules:
- `logging_setup.py`
- `gis_connect.py`
- `emailer.py`
- `restutils.py`
- `cli.py`
- `auth.py`
- `secure_session.py`

## Requirements

- ArcGIS API for Python (`arcgis`) and `requests`.
- A per-organization config TOML file with `[portal]`, `[agol]`, and `[common]` sections.
- The account password stored in the OS keyring under the configured keyring service and username, falling back to the `PORTAL_PASSWORD` environment variable.
- An account with administrative visibility to: enumerate every org user, read `/userEntitlements` for other users, and (AGOL only) read the org's `usage_reports.applications()`.

## Security: Certificate (TLS) Validation

This script **always verifies TLS certificates**. There is no option to disable certificate verification.

Two separate connections are involved:

1. **The ArcGIS SDK's connection** (used for role lookups and the usage report) — opened via `connect_gis()`, which **always** verifies certificates.

2. **This script's own direct REST calls** (user enumeration, `/userEntitlements`, `/sessions`) — uses a separate HTTP session whose certificate strategy is chosen explicitly via `--tls-mode`:
   - `standard` (default) — checks against the standard public list of trusted certificate authorities. Correct for AGOL and any Portal with a publicly issued certificate.
   - `os-trust-store` — checks against the operating system's own certificate store. Use this for an internal Enterprise Portal signed by your organization's certificate authority.
   - `custom-ca-bundle` — checks against a specific certificate file you provide with `--ca-bundle-path`.

Before any real data is requested, the script performs **one test connection** to confirm the certificate check succeeds, and logs a clear pass/fail result. If that test fails, the run stops immediately.

The authentication token is never written to the log file, console, email, or CSV — it is held in a wrapper object that only reveals its value at the exact moment needed.

### Network-boundary and request controls

- **SSRF / network-boundary defenses:** Direct REST calls are restricted to the authenticated Portal/AGOL `/sharing/rest/` path. URLs must use HTTPS, cannot contain user-info credentials, query strings, fragments, encoded path separators, path traversal, or unsafe control characters. Organization enumeration requires a validated authenticated organization ID.
- **Token handling:** The token is supplied through the shared `TokenProvider` and inserted only when the outgoing request is constructed. Caller-supplied `token` and `f` parameters are discarded and replaced by the controlled values. Authentication failures trigger one synchronized refresh/retry. Error text is passed through `redact_token()` before logging, console output, email, or CSV output.
- **Large/malformed responses:** Direct REST responses are streamed and limited to 64 MB. JSON decoding and object/list structures are validated before use. User search pagination validates totals, `nextStart`, usernames, organization IDs, and duplicate results rather than silently accepting malformed or inconsistent pages.
- **Python import-path/package shadowing:** The script locates the expected `UTILITIES` directory before importing the package, inserts the expected project root at the front of `sys.path` when it is not already present, checks the package origin with `importlib.util.find_spec()`, and refuses to import `UTILITIES` from an unexpected location. The script does **not** remove equivalent duplicate project-root entries from `sys.path`.
- **Redirect following:** Automatic redirect following is explicitly disabled with `allow_redirects=False`. A redirect may be considered only through the script's own validator; Portal redirects must remain on the original origin and authenticated REST path, and cross-origin redirects are refused. Up to three validated redirects are allowed rather than blindly following the server response.

## Configuration

The organization is selected at run time with the required `--config <org_toml_path>` argument, which supplies the org name and `--target portal` or `--target agol` to choose the matching config section.

Settings sourced from config: `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, and `email-from`. Any config value can be overridden on the command line; command-line flags always win. The account password is read from the OS keyring, falling back to the `PORTAL_PASSWORD` environment variable. The run also uses the selected organization target to validate that the configured Portal URL classifies as the expected `PORTAL` or `AGOL` platform.

## How it works

1. **Build a secure session and verify the TLS handshake** per the `--tls-mode` chosen, before any real data is requested.
2. **Connect** via `connect_gis()` (always TLS-verified regardless of `--tls-mode`), then resolve and validate the authenticated organization id.
3. **Find the ArcGIS Pro listing item id** via `gis.admin.license`, AGOL's `portals/self/purchases`, or (on Portal) provisioning-item content searches. If not found, entitlement values cannot be confirmed and affected users are treated as unknown for license status.
4. **Enumerate every org user** via paged, organization-scoped `portals/self/users/search`, respecting the resolved user cap. The strict REST client validates the result shape, total, pagination progress, duplicate usernames, and returned organization id.
5. **Fetch `/userEntitlements` for every user in parallel** using a thread pool and separate pre-created secure HTTP sessions.
6. **Fetch each user's `/sessions` data in parallel** — the primary source for `pro_last_used_session`.
7. **Fetch the org-wide Pro-desktop usage report once (AGOL only)** as a fallback for users with no sessions data.
8. **Resolve custom role names** via `gis.users.roles.all()`.
9. **Build one row per user**, combining user directory data, entitlements, offline/disconnected state, and the layered `pro_last_used_session` resolution (sessions endpoint -> usage report -> `lastLogin`). If `--assigned-only` is set, users with no entitlements are dropped.
10. **Write the CSV, then email** the log and CSV using the project-wide `max-email-mb` attachment policy.

A completed run can be labeled `INCOMPLETE` when some license lookups or supporting stages failed, but the script still returns exit code `0` after the inventory workflow and CSV write complete. The completion email identifies the affected stages so missing values are not mistaken for confirmed negatives.

## Test mode (default)

⚠️ **Test mode is ON by default.** In test mode:

1. The email subject is tagged `[TEST]`.
2. The user inventory is **capped at 50 users** unless `--max-users` explicitly overrides it.

Pass `--no-test` to remove both the cap and the `[TEST]` tag and inventory every user. An explicit `--max-users N` always takes precedence over the test/production default.

## Usage

### Scenario 1 — Initial deployment / connectivity check

**Situation:** You have just deployed the script and want to validate credentials, TLS, organization visibility, and a small sample before running against the whole organization.

Run the default test mode, which caps the roster at 50 users and tags the email `[TEST]`:

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal
```

**Expect:** Up to 50 users are inventoried. The CSV contains one row per selected user, and the completion email is tagged `[TEST]`. No claim should be made about unscanned users.

### Scenario 2 — Full organization license audit

**Situation:** A quarterly or annual license review requires every visible organization user rather than the 50-user test sample.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test
```

**Expect:** The user cap is removed and every enumerated organization user is processed. Groups are not relevant to this script. Any lookup gaps are retained as incomplete details rather than silently converted to confirmed negatives.

### Scenario 3 — Only users who have a Pro entitlement

**Situation:** You are preparing a license utilization report and only need rows for people who actually hold at least one Pro entitlement.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test --assigned-only
```

**Expect:** Users with no entitlement codes are omitted. The remaining rows show the Pro tier, extensions, complete entitlement codes, last-used data, and offline state.

### Scenario 4 — Widen the AGOL fallback usage window

**Situation:** A user's direct sessions record has no usable value, and you want the AGOL usage-report fallback to search a longer period.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test \
    --app-usage-window year
```

**Expect:** The org-wide AGOL Pro-desktop usage report is consulted for users whose sessions lookup produced no value. It is not fetched separately for each user.

### Scenario 5 — Limit the number of users while validating a large organization

**Situation:** You want more than the 50-user test default but still want a bounded run before committing to a complete organization inventory.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --max-users 500
```

**Expect:** The strict user enumeration starts from the organization-scoped directory, then stops once the configured user cap is reached. An explicit `--max-users` takes precedence over the test/production default. Values `<= 0` remove the cap.

### Scenario 6 — Internal Portal using an organizational CA

**Situation:** Your Enterprise Portal certificate is signed by an internal certificate authority that is already trusted by the operating system.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal --no-test \
    --tls-mode os-trust-store
```

**Expect:** The direct REST session and the ArcGIS SDK connection use the selected verification strategy. Certificate validation remains enabled.

### Scenario 7 — Validate against a specific PEM CA bundle

**Situation:** Security policy requires the Portal connection to trust a specific PEM certificate bundle.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target portal --no-test \
    --tls-mode custom-ca-bundle --ca-bundle-path /path/to/ca.pem
```

**Expect:** The PEM bundle is used for certificate verification. `--ca-bundle-path` is required when `custom-ca-bundle` is selected.

### Scenario 8 — Tune concurrency for a busy endpoint

**Situation:** A large run causes server throttling or excessive simultaneous requests, so you want to reduce concurrency.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --config org1.config.toml --target agol --no-test --workers 6
```

**Expect:** Per-user entitlement and session lookups use up to six concurrent worker threads and six pre-created secure worker sessions. Lower concurrency reduces simultaneous endpoint activity but can increase elapsed time.

### Scenario 9 — Review all command-line options

**Situation:** You are automating the script and want the current built-in command reference.

```bash
python NAMED_USER_LICENSE_INVENTORY.py --help
```

**Expect:** `argparse` prints the available options without running the inventory.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--config` | *(required)* | Path to the org-specific TOML config file. |
| `--target` | *(required)* | Which org to inventory: `portal` or `agol`. |
| `--no-test` | test mode ON | Run in PRODUCTION mode: removes the `[TEST]` tag and the 50-user cap. |
| `--max-users N` | `None` | Cap inventory to at most `N` users. Explicit `--max-users` always wins; `N <= 0` means no cap. |
| `--assigned-only` | off | Only emit rows for users who hold at least one Pro entitlement. |
| `--app-usage-window` | `year` | Window for the secondary Pro-desktop usage report: `week`, `14days`, `30days`, `60days`, `90days`, `6months`, or `year`. AGOL only. |
| `--workers N` | `12` | Thread count for parallel per-user `/userEntitlements` and `/sessions` calls. Allowed range: `1-64`. |
| `--trusted-service-origin HTTPS_ORIGIN` | none | Repeatable exact HTTPS origin allowed for token-bearing FeatureServer/MapServer requests. Normally unnecessary because the script uses Portal federation/AGOL origin policy. |
| `--tls-mode` | `standard` | TLS certificate verification strategy: `standard`, `os-trust-store`, or `custom-ca-bundle`. |
| `--ca-bundle-path` | `None` | Path to a PEM certificate bundle file. Required when `--tls-mode custom-ca-bundle`. |
| `--portal-url` | from config | Portal / AGOL URL. |
| `--username` | from config | Login username. |
| `--keyring-service` | from config | Keyring service holding the password. |
| `--smtp-server` | from config | SMTP relay for the completion email. |
| `--email-to` | from config | Recipient(s); single address or comma-separated list. |
| `--email-from` | from config | Sender address. |
| `--output-dir` | from config | Where the CSV and log are written. |

## Performance

- **User enumeration** is a single paged pass (100 users per page), with an early stop once `--max-users` is satisfied.
- **Per-user entitlements and sessions** run in parallel over a thread pool (default 12 workers) — one call per user per endpoint.
- **The Pro-desktop usage report** (AGOL only) is fetched **once** for the whole run, not per user, so widening `--app-usage-window` does not multiply requests.
- **Role name resolution** runs once and is cached.
- **Worker sessions are pre-created and borrowed one task at a time.** Sessions are not shared concurrently between worker tasks.
- **REST responses are streamed and bounded.** A response larger than 64 MB is rejected.

## Observed runtime

| Run type | Observed duration | Number of org users |
|----------|-------------------|----------------------|
| Portal   | ~3-5 minutes      | ~5,000               |
| AGOL     | ~3-10 minutes     | ~6,000               |

Actual runtime scales primarily with organization size and the number of worker threads. Raising `--workers` can speed up runs on organizations with headroom, but too high a value can trigger server-side throttling.

## Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp:

```
[ORG]_[PLATFORM]_NAMED_USER_LICENSE_INVENTORY_<ts>.csv
[ORG]_NAMED_USER_LICENSE_INVENTORY_<ts>.log
```

One row per organization user (or per user with a Pro entitlement, if `--assigned-only` is set). The script writes one main CSV plus the run log; unlike broader inventory tools in this repository, it does not produce a separate summary CSV.

The CSV filename uses the reserved run timestamp. If that log filename already exists, the script adds a numeric suffix to the timestamp and uses the same unique suffix for the CSV, preventing an accidental overwrite.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Normal completion path. The CSV was written and the inventory workflow reached the email/report stage. The email/report may still say `INCOMPLETE` when one or more license details could not be fully resolved. |
| `1` | Run aborted, including invalid target/platform, TLS handshake failure, Portal connection failure, output-path failure, or an unrecovered inventory/processing exception. |
| `2` | Argument validation failure. The current explicit example is `--tls-mode custom-ca-bundle` without the required `--ca-bundle-path`. |

A run that reaches the normal completion path after partial entitlement/session lookup failures returns `0`; those failures are surfaced in the report/email as `INCOMPLETE` so missing values are not mistaken for confirmed negatives.

## Notes and caveats

- **The Pro usage-report fallback is AGOL-only.** Enterprise Portal has no equivalent to `usage_reports.applications()`. On Portal, `pro_last_used_session` relies on the sessions endpoint and `userEntitlements.lastLogin` only.
- **`last_login` is a website login, not proof of Pro use.** See [Why it's a problem](#why-its-a-problem) — conflating these two columns produces false conclusions.
- **If the Pro listing item id can't be found, entitlement columns go blank for everyone**, but the run still completes with the full user roster.
- **Unrecognized entitlement codes are preserved.** Any code not recognized as a known Pro level or extension is still appended to `pro_extensions` and always appears in `all_entitlements`.
- **The role label fallback covers only standard role tokens.** An unrecognized role id that isn't a custom role name appears as the raw token.
- **Email attachment cap is configurable, project-wide.** If unset in config, the built-in default of 20 MB applies. The log is always attached regardless of size.
- **This is a snapshot, not a live view.** Data reflects the moment the scan ran.