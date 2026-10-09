# DUPLICATE_LAYERS.py

**Duplicate Layer Scan — ArcGIS Enterprise Portal or ArcGIS Online**

Finds map layers that are duplicated across different feature services, explains each finding in plain English with a numeric score, and automatically filters out legitimate duplication (Survey123 forms, hosted views) so you can focus on actual cleanup opportunities.

## Table of Contents

### For Report Readers
- [Overview](#overview)
- [Why inventory duplicate layers](#why-inventory-duplicate-layers)
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

This tool audits every map layer published on your organization's Portal or ArcGIS Online and flags any layer that exists in more than one place. For each match, it creates one row in a spreadsheet (CSV) that:

- Names the two (or more) services that share the layer.
- Explains in plain English exactly *why* it thinks they're duplicates.
- Gives a **score from 0–100** — a "duplicate-probability" — so you can quickly tell a near-certain duplicate from a borderline one.
- Notes any reason to be cautious (like if the layer name is generic, such as "Points").

The tool automatically recognizes and excludes two types of duplication that are **expected and not a problem**:

1. **Survey123 companion services** — ArcGIS automatically creates a results service for every survey form.
2. **Hosted views** — layers deliberately built to show a filtered window into another original layer.

What remains in the report is what's worth a second look.

### Why inventory duplicate layers

When the same map layer exists in more than one service without anyone intending it, several problems arise:

**Confusion about which copy is current.** People editing the data may edit the wrong copy. Over time, the two copies drift apart and show different information.

**Cluttered storage and search results.** Duplicate copies waste space and make it harder for staff to find the layer they actually need.

**Stale data in maps and apps.** If a map or app is pointing at an old copy of a layer, the public or your staff may be looking at outdated information without anyone realizing it.

This report gives you a clear, explained list of exactly which layers are duplicated, where, and how confident the tool is that each one is a genuine accidental copy.

### Why it's a problem

The core issue: **you can't tell which copy is the "real" one**, so you don't know which one to update and which ones to delete.

### Why removal requires caution

**Some duplication is completely normal.** Do not delete anything from this report without reading the explanation first. The most common legitimate cases are:

- **Survey123 companion services.** When someone publishes a survey named `Trail Conditions 2024`, ArcGIS automatically creates a second, related service called `Trail Conditions 2024_results`. Both hold the same data layers by design — that is not a mistake.
- **Hosted views.** A "view" is a deliberate, read-only window into another service's data, often used to share a limited subset publicly while keeping the original private. A view and its source are supposed to share layers.

Because deleting the wrong copy could break a working survey or published map, **this tool never deletes anything — it only reports.** It also automatically screens out both legitimate cases before they ever appear as a "duplicate" in your CSV, using two layers of checking:

- **Automatic check #1 (view detection).** The tool asks the system: "is this a view, and if so, what is it a view of?" If a view and its source share a layer, that pairing is removed from the report before analysis starts.
- **Automatic check #2 (relationship backstop).** For anything that still looks like a duplicate, the tool asks: "are these two things formally linked in any other recognized way?" This catches Survey123 pairs and similar relationships. If the system confirms a link, that shared layer is dropped from the report.

**Bottom line:** anything that still appears in your final report has already passed both automatic sanity checks. It's worth your attention, in proportion to its score.

### Recommendations for investigation

A practical workflow:

1. **Sort by the `group_id` column.** This groups related services together so you review connected findings side by side.

2. **Look for `match_type = ALL` first.** This means every single layer in that service is duplicated elsewhere — a strong sign the *entire service* is redundant, not just one shared layer.

3. **Use the `dup_probability` score to prioritize:**
   - **80–100** — Very strong signal. The data volumes match closely, the structure matches, and usually the whole service is duplicated. Worth prioritizing for review.
   - **50–79** — A real structural match with partial supporting evidence. Worth investigating.
   - **Below 30** — Usually not worth acting on. Often means two services were built from the same template but now hold genuinely different data.

4. **Check the `schema_status` column.** If it says `mismatch`, the underlying field structures are different even though the name matched — strong evidence they are *not* true duplicates.

5. **Check the `generic_name` column.** If `True`, the layer's name is a common default (like "Polygons" or "Point") that many unrelated layers use by coincidence — treat a name-only match on these as weaker evidence.

6. **Always read the `summary` column.** Every row includes a full plain-English explanation of what matched, how data volumes compared, whether structures matched, and the final score.

**The strongest combination to look for:** `match_type = ALL`, a score in the **80s or 90s**, and `schema_status = match`. That's the clearest sign of a truly redundant service.

**Reasons to skip a row:** if the summary mentions counts that **diverge** significantly (usually means "same template, different real data") or if both copies are **empty** (zero records — the tool can't confirm anything either way).

### Column descriptions

| Column | What it tells you |
|---|---|
| `platform` | `PORTAL` or `AGOL` — which environment this came from |
| `title` | The service's name as published |
| `layer_name` | The actual, as-published name of the layer (original spelling and capitalization) |
| `summary` | A full plain-English explanation of the finding — what matched, how record counts compared, how field structures compared, any cautions, the final score, and whether this is part of a fully- or partially-duplicated service |
| `group_id` | A number linking every service that shares layers with this one; sort by this to review related services together |
| `match_type` | `ALL` — every layer in this service is duplicated elsewhere (the whole service is likely redundant). `SOME` — only part of this service's layers are duplicated |
| `dup_probability` | A whole number from **0–100**; higher means stronger evidence of a genuine, unintended duplicate |
| `feature_count` | The number of records (features) in *this* copy of the layer; blank if records were not counted or couldn't be read |
| `schema_status` | `match` — underlying field structure agrees with the other copies. `mismatch` — fields differ, which argues against a true duplicate. `unknown` — not enough information was available to compare |
| `generic_name` | `True` if the layer's name is a common, generic ArcGIS default (e.g., "Polygons," "Point," "Line") that many unrelated services might share by coincidence — a weaker signal on its own |
| `geometry` | The type of the layer: `point`, `polyline`, `polygon`, or `table` (for non-spatial data); blank if running in name-only mode |
| `spatial_ref` | The coordinate system code (e.g., `4326` for WGS84); `none` for tables (which have no coordinate system); blank if running in name-only mode |
| `dup_service_count` | How many services, in total, contain this exact layer |
| `service_layer_count` | How many layers this particular service has altogether — useful context for `ALL` vs `SOME` |
| `owner` | Username of the person or account that owns this service |
| `item_id` | The service's unique identifier in Portal/AGOL |
| `item_url` | A direct link to open this item's page in Portal or AGOL |

---

## For Administrators

### Dependencies on the shared UTILITIES package

This script uses the following helper files from `UTILITIES`:

- `gis_connect.py`
- `logging_setup.py`
- `restutils.py`
- `bucketing.py`
- `arcgis_rest.py`
- `emailer.py`
- `cli.py`
- `config.py`
- `auth.py`
- `secure_session.py`
- `exclusions.py`

### Requirements

- Python 3 with the ArcGIS API for Python (`arcgis`)
- Access to the target Portal or AGOL with permission to enumerate hosted feature services, read service definitions, query feature counts, read sub-layer schemas, and read item relationships
- Portal login credentials stored in keyring (service = `keyring-service`, user = `username`), with fallback to `PORTAL_PASSWORD` environment variable
- Reachable SMTP server for the completion email
- The shared `UTILITIES` package at `...\PYTHON\UTILITIES` (one level above this script's folder)
- (Optional) `truststore` package if using `--tls-mode os-trust-store`

### Security: Certificate (TLS) Validation

This script keeps TLS certificate verification enabled for both the direct REST session it creates and the ArcGIS SDK connection used for login and enumeration. The trust source for the script's direct REST calls is selected with `--tls-mode`:

- `standard` (default) — verifies against the standard public Certificate Authority list.
- `os-trust-store` — verifies against the operating system's certificate store and requires the `truststore` package. This is useful for an internal Enterprise Portal whose organizational CA is already trusted by the operating system.
- `custom-ca-bundle` — verifies against the PEM file supplied with `--ca-bundle-path`.

Before normal REST scanning begins, the script performs a one-time TLS handshake check to the configured Portal/AGOL URL. A failed verification stops the run. The separate ArcGIS SDK connection also verifies TLS; this script never provides a switch to disable certificate verification.

**SSRF / network-boundary defenses:** Every item-supplied hosted feature-service URL is validated before the authentication token is sent. HTTPS is required. For AGOL, the service hostname must be exactly `arcgis.com` or end in `.arcgis.com`, covering Esri hosted-service hosts such as `services*.arcgis.com` while rejecting unrelated domains. For Enterprise, the service hostname and port must match the configured Portal origin. This is a token-exfiltration boundary: a service URL that is not within the approved origin policy is rejected before the REST call.

**Token handling:** The authenticated token is obtained through the shared `TokenProvider`. The token is passed to the REST helper only after service URL validation, and exception text is passed through `redact_token()` before it is logged. The token is not intended to be exposed through the provider object's string representation. REST calls that target the authenticated Portal/AGOL base URL, such as `relatedItems`, use the known authenticated base URL rather than an item-supplied service URL.

**Large / malformed responses:** This file does not define its own HTTP response-size ceiling. Service-root, hosted-view-source, relationship, count, and schema responses are obtained through shared REST helpers and are validated for the response shape expected at each stage. For example, a service root must be a JSON object, hosted-view source data must be a JSON object, and a schema response must contain a readable `fields` list. Request failures and unexpected response shapes are counted so the run can be marked `INCOMPLETE` rather than silently treated as a clean inventory.

**Python import-path / package shadowing:** The script puts the expected project parent directory at the front of `sys.path` before importing `UTILITIES`. The current file does not remove equivalent path spellings first and does not verify the physical location of the imported `UTILITIES` package. Therefore, the stronger package-origin verification pattern is not implemented here.

**Redirect following:** The direct HTTP session sets `session.max_redirects = 0` before any authenticated scan requests. This prevents the session from following an HTTP redirect chain that could otherwise move a token-bearing request away from the approved destination.

**Read-only design:** The scan reads service definitions, counts, schemas, view-source information, and item relationships. It does not delete, modify, or update hosted services, layers, or Portal/AGOL items.

### Configuration

All connection, email, and output settings come from the org config `.toml` selected with the required `--config` argument, combined with the required `--target` argument (`portal` or `agol`). The script requires these config keys:

`portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`

Any value can be overridden on the command line; anything left off is filled from the chosen `--target` section (then `[common]`) of the org config. The password comes from keyring or the `PORTAL_PASSWORD` environment variable — never from the config file.

The optional `max-email-mb` setting (from config) controls the email attachment size ceiling. An optional `--exclude-group-id` argument resolves portal group ids into item ids that are dropped from the scan before resolution runs.

### How it works

**Layer matching:** Every layer gets a signature combining three things:

| Criterion | Why it matters |
|---|---|
| Layer name (case-insensitive) | The most basic sign of a copy |
| Geometry type (point, polyline, polygon, or table) | A "Roads" line layer is NOT the same as a "Roads" point layer |
| Spatial reference (coordinate system code, e.g., 4326) | Two layers with the same name/geometry but different coordinate systems are NOT considered duplicates |

Two layers must match on **all three** to be treated as the same layer (unless running with `--name-only`, which matches on name alone).

**Stage 0 — Hosted-view detection:** Before analysis runs, every service is checked for `isView=true`. For each confirmed view, `.../FeatureServer/sources` is queried to find its source service. Any layer key the view shares with its own source is removed from consideration — so that pairing can never be flagged as a duplicate.

**Duplicate grouping:** Every layer key held by 2+ services is a duplicate. Services are grouped into `group_id` clusters using union-find over shared layer keys, so chains of related services all land in the same group.

**Stage 2 — Relationship backstop:** For every service with a duplicated layer, the script queries the portal's `relatedItems` endpoint and drops any shared layer between two services the portal confirms are related. This removes Survey123 pairs and other legitimate relationships after Stage 0.

**Feature-count comparison (group-wide tiers):** For every duplicated layer, every copy's feature count is queried and compared group-wide, producing one of these tiers:

| Tier | Meaning |
|---|---|
| `exact` | Every readable copy has the identical, nonzero feature count |
| `close` | Every copy is within 5% tolerance of each other |
| `divergent` | The spread exceeds 5% tolerance — usually means "same template, different real data" |
| `empty` | Every copy has 0 features — uninformative; needs schema match to be conclusive |
| `unknown` | At least one copy's count couldn't be read |

**Schema comparison (only for top-tier count matches):** Layers with `exact`, `close`, or `empty` feature counts are eligible for field-by-field comparison. Each copy's field list (name + type, excluding system/editor-tracking fields) is compared against peers, producing `match`, `mismatch`, or `unknown`.

**Duplicate-probability scoring (0–100):** A single whole-number score combines all signals: base for matching on name/geometry/SR, boosts/penalties for count comparison, boosts/penalties for schema comparison, penalties for generic names, and adjustments for `ALL` vs `SOME` scope.

### Test mode (default)

⚠️ **Test mode is ON by default** and limits the later scan stages to the **first 250 hosted feature services**. Discovery still enumerates the whole organization. Add `--no-test` to scan **every** discovered hosted feature service.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol            # first 250 (test)
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test  # ALL services
```

### Usage

**Scenario 1: Quick sanity check**

You just set up the script and want to confirm the pipeline runs cleanly and the CSV looks reasonable before committing to a full scan. Uses all default checks (view detection, relationship check, feature counting, schema comparison) but limits to 250 services.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol
```

**Scenario 2: Monthly cleanup audit of the whole catalog**

A GIS admin wants a complete, authoritative inventory of every duplicated layer across all Portal-hosted feature services with full confidence scoring.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test
```

**Scenario 3: Fast structural-only scan, skipping expensive counts**

An admin wants a first look across all of AGOL to understand the shape of the duplication problem (name/geometry/SR matches) without waiting on feature-count queries.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --no-feature-count
```

**Scenario 4: Fastest possible run on a very large catalog**

An admin wants the quickest pass to get a rough count of overlapping names, knowing some legitimate pairs may still show up (view detection runs, but relationship check is skipped).

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --no-feature-count --no-relationship-check
```

**Scenario 5: Name-only matching, ignoring geometry and coordinate systems**

An admin is migrating data between coordinate systems and wants to find layers sharing a name regardless of geometry or spatial reference setup.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test --name-only
```

**Scenario 6: Speeding up a full scan on a fast connection**

Running from a machine with strong bandwidth directly to the Enterprise deployment, an admin raises the thread-pool size to cut wall-clock time.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target agol --no-test --workers 32
```

**Scenario 7: Routing one run's report to a different recipient**

A one-off scan was requested by a different team, and the admin wants to send that run's report to them without changing the config for every other run.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --no-test --email-to "thing@mail.com"
```

**Scenario 8: Connecting to an internal Enterprise Portal with an organizational certificate**

The target Portal uses a certificate issued by the organization's internal CA (already trusted at the OS level). The admin selects the OS trust store verification mode.

```bash
python DUPLICATE_LAYERS.py --config org1.config.toml --target portal --tls-mode os-trust-store
```

### Command-line options

| Option | Required? | Default | What it does |
|---|---|---|---|
| `--config` | **Yes** | — | Which org `.toml` to load |
| `--target {portal,agol}` | **Yes** | — | Which environment to scan |
| `--no-test` | No | test ON | Scan all services instead of the first 250 |
| `--name-only` | No | off | Match on layer name only; ignore geometry and spatial reference |
| `--no-feature-count` | No | counting ON | Skip feature-count queries (faster; scores stay at base) |
| `--no-schema-check` | No | check ON | Skip field/schema comparison for layers with matching counts |
| `--no-relationship-check` | No | check ON | Skip Stage 2 relationship check (faster; some legitimate pairs may remain) |
| `--no-view-detect` | No | detection ON | Skip Stage 0 view detection (falls back to relationship check only) |
| `--workers N` | No | `16` | Number of parallel threads for reading services, detecting views, counting features, and checking relationships |
| `--exclude-group-id` | No | none | One or more portal group ids whose member items are excluded from the scan |
| `--tls-mode {standard,os-trust-store,custom-ca-bundle}` | No | `standard` | TLS certificate verification strategy for this script's own REST calls |
| `--ca-bundle-path` | Only with `custom-ca-bundle` | — | Path to a PEM certificate bundle file |
| `--portal-url`, `--username`, `--keyring-service` | No | from config | Override connection settings |
| `--output-dir`, `--smtp-server`, `--email-to`, `--email-from` | No | from config | Override output and email settings |

### Performance

- **Enumeration** uses bucketed search to handle catalogs larger than the service result cap.
- **Reading service definitions**, **detecting views**, **counting features**, **fetching schemas**, and **checking relationships** all run in a thread pool (default 16, set with `--workers`) over a pooled, retrying HTTP session.
- **Feature counts, schemas, and relationships are only queried for services with duplicated layers** — the rest of the catalog is never touched, keeping expensive stages as small as possible.
- Use `--no-feature-count`, `--no-schema-check`, `--no-view-detect`, and/or `--no-relationship-check` for a faster run when you only need the structural duplicate list.

### Observed Runtime

> Actual runtime scales with catalog size, how many services have duplicated layers, `--workers`, and network latency to Portal/AGOL.

| Environment | Typical Duration | Number of Feature Services |
|---|---|---|
| Portal | ~3–10 minutes | ~5,500 |
| AGOL | ~20–50 minutes | ~18,000–20,000 |

### Output file

The CSV and log are named using the **resolved platform**:

```
<org>_AGOL_DUPLICATE_LAYERS_<timestamp>.csv     (when the platform is AGOL)
<org>_PORTAL_DUPLICATE_LAYERS_<timestamp>.csv   (when the platform is PORTAL)
```

If the platform is `UNKNOWN`, the script falls back to an `<org>_DUPLICATE_LAYERS_<timestamp>` prefix. A matching `.log` file is written in the same `output-dir`. There is **one row per (service × duplicated layer)**, so a service with three duplicated layers produces three rows. Only layers in **2 or more** services, and that survived Stage 0 and Stage 2, appear.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | The run completed and the completion email was sent. This includes runs flagged `INCOMPLETE`; the report is not authoritative, but schedulers still see a completed process. |
| `1` | Could not build the secure session, could not establish or verify the startup TLS connection, or could not connect to Portal/AGOL. A failure email is sent with the log attached. |
| `2` | Invalid command-line arguments (for example, `--tls-mode custom-ca-bundle` without `--ca-bundle-path`). |

### Notes and caveats

- **`ModuleNotFoundError: No module named 'UTILITIES'`** — The `UTILITIES` folder must be at `...\PYTHON\UTILITIES` (one level above this script's folder), not in the same folder as the script.

- **`dup_probability` / `feature_count` are lower/blank than expected** — You ran with `--no-feature-count` (count signal is skipped), or a count couldn't be read for one or more copies (`unknown` tier, noted in the `summary`).

- **Lots of `empty`-tier rows** — Layers with 0 features in every copy (blank templates or unused surveys) are expected; the score is capped lower unless the schema also matches.

- **A pair you expected to match is in different groups** — They may differ on geometry or coordinate system (check `geometry` and `spatial_ref` columns), or one is already resolved by Stage 0/Stage 2 as a legitimate view or relationship. Try `--name-only` to match on name alone.

- **A service and its hosted view aren't showing up as duplicates** — Working as intended. Stage 0 found `isView=true` and resolved the source via `FeatureServer/sources`, so shared layers were removed before analysis.

- **Run is slow on `--no-test`** — Feature counting, schema fetches, and the relationship check each add queries. Use `--no-feature-count`, `--no-schema-check`, and/or `--no-relationship-check`, or raise `--workers`.

- **No CSV attached to the email** — The CSV exceeded the `max-email-mb` cap even after zipping, so it was left on disk; the email body points to its location.

- **Output file has wrong platform prefix** — The prefix is derived from `get_platform(args.portal_url)`, falling back to `<org>_DUPLICATE_LAYERS` only if the platform is `UNKNOWN` (non-standard URL shape). Check that `portal-url` is correct.

- **`--tls-mode os-trust-store` fails with ImportError** — The `truststore` package is not installed. Install it with `pip install truststore`.

- **Run aborts with a TLS handshake failure** — The startup certificate check to the target host failed under the selected `--tls-mode`. Confirm the URL is correct and that the certificate mode matches how that server is actually secured.