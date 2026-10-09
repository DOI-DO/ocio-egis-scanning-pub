# Replica Inventory Tools

This folder contains three replica-inventory tools, a correlation tool, and a comparison utility:

- **`INVENTORY_FS_REPLICAS.py`** — inventories **Feature Service replicas** across either an entire ArcGIS Enterprise deployment (Portal) or an ArcGIS Online organization (AGOL), by discovering every hosted feature service and reading its `/replicas`.
- **`DATA_STORE_INVENTORY_SDE_REPLICAS.py`** — inventories **feature-service replicas from the Portal Hosting Server Data Store's managed enterprise geodatabase (SDE)** directly, via an on-the-fly database connection. The live PRIMARY Data Store machine is auto-detected on every run (HA-aware).
- **`CORRELATE_SQLITE_REPLICAS.py`** — scans a Portal/AGOL organization for its own **SQLite Geodatabase items** (the downloaded replica files that Field Maps and other offline workflows register back into Portal/AGOL as items) and works out, for each one, **which live feature-service replica it came from** — and, crucially, **which of those downloaded files no longer have a matching live replica** (i.e. the replica was deleted on the server, leaving the downloaded item dangling). It discovers the items itself (no input CSV), is **read-only**, and never deletes anything.
- **`REPLICA_DELTA.ps1`** — cross-checks the two inventory reports, listing every replica ID and whether it appears in BOTH inventories or in only one.

They look at the same underlying replicas from different angles: `INVENTORY_FS_REPLICAS.py` from the REST/services side (one row per replica, per service, per server/org), and `DATA_STORE_INVENTORY_SDE_REPLICAS.py` from the database/SDE side (one row per replica in the managed geodatabase). Use them together to cross-check coverage, then run `REPLICA_DELTA.ps1` to reconcile them.

`CORRELATE_SQLITE_REPLICAS.py` comes at the problem from a **third** angle: rather than inventorying replicas that exist, it starts from the **downloaded replica items** (SQLite Geodatabase items it discovers in the organization) and asks, for each one, whether its originating replica is still alive on the service. Use it to find downloaded-data items whose server-side replica has already been removed.

**`INVENTORY_FS_REPLICAS.py` and `CORRELATE_SQLITE_REPLICAS.py` support both `--target portal` and `--target agol`.** `DATA_STORE_INVENTORY_SDE_REPLICAS.py` does not — the managed SDE Data Store it reads from only exists behind an ArcGIS Enterprise Portal deployment, so there is nothing on AGOL for it to inventory. All Python scripts are multi-org aware and require a `--config <org_toml_path>` argument.

Every major section below is broken into clearly-labeled, independently-linkable per-tool subsections in this fixed order: **FS** (`INVENTORY_FS_REPLICAS.py`) → **SDE** (`DATA_STORE_INVENTORY_SDE_REPLICAS.py`) → **Correlate** (`CORRELATE_SQLITE_REPLICAS.py`) → **Delta** (`REPLICA_DELTA.ps1`). Use the table of contents below to jump straight to any tool's subsection under any heading.

---

## Table of Contents

### For Report Readers
*(no technical background required — read this if someone handed you a CSV from these tools)*

- [Overview](#overview)
  - [FS: INVENTORY_FS_REPLICAS.py](#fs-inventory_fs_replicaspy)
  - [SDE: DATA_STORE_INVENTORY_SDE_REPLICAS.py](#sde-data_store_inventory_sde_replicaspy)
  - [Correlate: CORRELATE_SQLITE_REPLICAS.py](#correlate-correlate_sqlite_replicaspy)
  - [Delta: REPLICA_DELTA.ps1](#delta-replica_deltaps1)
- [Why inventory duplicate layers](#why-inventory-duplicate-layers)
- [Why it's a problem](#why-its-a-problem)
- [Why removal requires caution](#why-removal-requires-caution)
- [Recommendations for investigation](#recommendations-for-investigation)
- [Column descriptions](#column-descriptions)
  - [FS Column descriptions](#fs-column-descriptions)
  - [SDE Column descriptions](#sde-column-descriptions)
  - [Correlate Column descriptions](#correlate-column-descriptions)
  - [Delta Column descriptions](#delta-column-descriptions)

### For Administrators
*(setup, configuration, and operational details)*

- [Dependencies on the shared UTILITIES package](#dependencies-on-the-shared-utilities-package)
  - [FS Dependencies](#fs-dependencies)
  - [SDE Dependencies](#sde-dependencies)
  - [Correlate Dependencies](#correlate-dependencies)
  - [Delta Dependencies](#delta-dependencies)
- [Requirements](#requirements)
  - [FS Requirements](#fs-requirements)
  - [SDE Requirements](#sde-requirements)
  - [Correlate Requirements](#correlate-requirements)
  - [Delta Requirements](#delta-requirements)
- [Security: Certificate (TLS) Validation](#security-certificate-tls-validation)
  - [FS Security](#fs-security)
  - [SDE Security](#sde-security)
  - [Correlate Security](#correlate-security)
  - [Delta Security](#delta-security)
- [Configuration](#configuration)
  - [FS Configuration](#fs-configuration)
  - [SDE Configuration](#sde-configuration)
  - [Correlate Configuration](#correlate-configuration)
  - [Delta Configuration](#delta-configuration)
- [How it works](#how-it-works)
  - [FS How it works](#fs-how-it-works)
  - [SDE How it works](#sde-how-it-works)
  - [Correlate How it works](#correlate-how-it-works)
  - [Delta How it works](#delta-how-it-works)
- [Test mode (default)](#test-mode-default)
  - [FS Test Mode](#fs-test-mode)
  - [SDE Test Mode](#sde-test-mode)
  - [Correlate Test Mode](#correlate-test-mode)
  - [Delta Test Mode](#delta-test-mode)
- [Usage](#usage)
  - [FS Usage](#fs-usage)
  - [SDE Usage](#sde-usage)
  - [Correlate Usage](#correlate-usage)
  - [Delta Usage](#delta-usage)
- [Command-line options](#command-line-options)
  - [FS Command-line options](#fs-command-line-options)
  - [SDE Command-line options](#sde-command-line-options)
  - [Correlate Command-line options](#correlate-command-line-options)
  - [Delta Command-line options](#delta-command-line-options)
- [Performance](#performance)
  - [FS Performance](#fs-performance)
  - [SDE Performance](#sde-performance)
  - [Correlate Performance](#correlate-performance)
  - [Delta Performance](#delta-performance)
- [Observed Runtime](#observed-runtime)
- [Output file](#output-file)
  - [FS Output file](#fs-output-file)
  - [SDE Output file](#sde-output-file)
  - [Correlate Output file](#correlate-output-file)
  - [Delta Output file](#delta-output-file)
- [Exit codes](#exit-codes)
  - [FS Exit codes](#fs-exit-codes)
  - [SDE Exit codes](#sde-exit-codes)
  - [Correlate Exit codes](#correlate-exit-codes)
  - [Delta Exit codes](#delta-exit-codes)
- [Notes and caveats](#notes-and-caveats)
  - [FS Notes and caveats](#fs-notes-and-caveats)
  - [SDE Notes and caveats](#sde-notes-and-caveats)
  - [Correlate Notes and caveats](#correlate-notes-and-caveats)
  - [Delta Notes and caveats](#delta-notes-and-caveats)

---

# For Report Readers

This section is written for anyone who receives one of these CSV reports by email and needs to understand what it means — no scripting or database knowledge required.

## Overview

### FS: INVENTORY_FS_REPLICAS.py

Think of this report as a census of every **replica** (a copy of data set up for offline use or syncing) that exists across your organization's maps and feature services. It looks in two possible places:

- **Portal** — every server in your ArcGIS Enterprise deployment (the main "hosting" server plus any additional federated servers).
- **AGOL** — every hosted feature service in your ArcGIS Online organization.

Each row in the output is one replica, tagged with which service it belongs to, who owns the service, who created/uses the replica, when it was created, when it last synced, and whether it looks abandoned ("stale").

### SDE: DATA_STORE_INVENTORY_SDE_REPLICAS.py

This report looks at the exact same kind of information, but from a different vantage point: instead of asking the map/service layer "what replicas do you know about," it asks the **database** directly. Because services and databases can occasionally fall out of sync with each other, having both views lets you catch discrepancies neither one would show by itself. This report only applies to ArcGIS Enterprise (Portal) — ArcGIS Online has no equivalent database to inspect.

### Correlate: CORRELATE_SQLITE_REPLICAS.py

When someone takes a map offline — downloads an area in Field Maps, for example — the platform builds a **replica** on the server and hands back a **downloaded copy of the data** as a file. That downloaded file is registered back into your Portal or ArcGIS Online as its own item, of type **"SQLite Geodatabase."** So every offline download leaves behind **two linked things**: a replica living on the feature service, and a downloaded-data item living in your content.

This report **discovers all of those downloaded-data items in your organization itself** and, for each one, answers a single question: **is the replica it came from still alive on the server?**

- If **yes**, the item is **MATCHED** — healthy, with a live replica behind it.
- If **no**, the item is **ORPHANED** — the server-side replica has been deleted, but the downloaded-data item was left behind. These are the items this report is really designed to surface.

Think of it like matching warehouse delivery slips against what's actually on the shelves. The downloaded-data items are the delivery slips ("a copy of X was handed out on this date"); the live replicas are what's still on the shelf. This tool gathers up your whole stack of slips and, for each one, checks whether the matching item is still on the shelf — flagging every slip whose item is gone.

### Delta: REPLICA_DELTA.ps1

This is the "difference checker." It takes the two inventory reports (FS and SDE) and lines them up side by side by replica ID, then tells you, for each one: does it show up in **both** reports (healthy), or only in **one** of them (a mismatch worth looking into)?

> **Note:** this reconciliation only covers the main hosting server — it does not compare replicas that live on other (federated) servers in the deployment.

---

## Why inventory Replicas

Every time someone takes a map offline for field work, sets up automatic syncing, or configures two-way data collaboration between systems, the platform creates a **replica** — essentially a tracked, synced copy of the data. In a healthy workflow, that replica gets used, synced regularly, and then properly removed ("unregistered") once the work is done.

In practice, this cleanup step is often missed. A tablet gets lost, a project wraps up without anyone remembering to unregister its replica, an employee leaves, or an app crashes before it can clean up after itself. The replica is left behind, quietly consuming resources and drifting further out of sync, with nobody tracking it.

Inventorying replicas — counting them, tagging them with an owner, and flagging the ones that look abandoned — turns this invisible, slowly-growing problem into something visible and actionable.

## Why it's a problem

Leftover replicas are not just harmless clutter — they cause real, measurable problems:

- **They slow the whole system down, not just themselves.** Behind the scenes, the database keeps track of every change since a replica's last sync, in case it ever comes back online. A replica that never syncs again still forces the database to hold onto all of that historical change information indefinitely. Routine maintenance tasks (like the periodic database "cleanup" operation, called `Compress`) can't do their job properly while these replicas are still technically "waiting," which means those maintenance windows take longer and the whole system's day-to-day performance for *everyone* — not just the replica's owner — gets worse.
- **They put field data at risk.** Any edits made on a device since its last successful sync exist in exactly one place: that device. If the device is lost, stolen, or wiped before it syncs, that data is gone for good. A replica that hasn't synced in months is a strong signal that whatever was collected on that device may already be unrecoverable.
- **They make it harder to know who owns what.** Without a regular inventory, there's no way to tell which team, app, or person is responsible for a given replica, or which ones are safe to remove.
- **Orphans point to a bigger disagreement.** On Portal specifically, if a replica shows up in the services report but not in the database report (or vice versa), it means the two systems disagree about whether that replica still exists — usually the sign of a service that was deleted without properly cleaning up its database entry, or the reverse.
- **Downloaded-data items can outlive their replica.** When a server-side replica is deleted (or the service is republished, or the replica is unregistered after a sync), the **downloaded "SQLite Geodatabase" item it produced is not automatically removed**. It lingers in your content as a dead copy of data that can no longer sync with anything — consuming storage, cluttering search, and misleading anyone who finds it into thinking it's still a live offline dataset. `CORRELATE_SQLITE_REPLICAS.py` is what finds these.

**Why the 90-day cutoff matters.** These reports flag any replica that hasn't synced in more than 90 days as "Stale." That threshold is deliberately generous — field devices legitimately go offline for weeks at a time for all sorts of ordinary reasons (remote assignments, seasonal closures, hardware repairs). A replica that's still not synced *well past* that window is much more likely to represent something genuinely abandoned (a wiped device, a departed employee, an app that never cleaned up after itself) than a device that's simply between field trips. The longer a replica goes unsynced, the larger and riskier its eventual sync becomes, and the more irreplaceable field data is potentially at stake if that device is never recovered.

## Why removal requires caution

**Do not delete or unregister a replica just because a single report flags it.** A replica can look inactive simply because its device is on an infrequent sync schedule — for example, a seasonal field crew that only connects once a month — not because anyone abandoned it.

Before acting on anything in these reports:

- Cross-check the finding using the other report (services view vs. database view) if one is available.
- Look at how long it's actually been since the last sync, not just whether it's flagged.
- Try to identify a real owner or workflow behind the replica before assuming it's safe to remove.

The `REPLICA_DELTA.ps1` comparison tool exists specifically to prevent someone from acting on a false alarm that only one of the two reports would have shown. Likewise, an `ORPHANED` result from `CORRELATE_SQLITE_REPLICAS.py` is a candidate for review, not an automatic delete list — see [Correlate How it works](#correlate-how-it-works) for exactly how far that verdict can be trusted.

## Recommendations for investigation

- Treat rows marked **only in one report** (from the Delta comparison) as your top priority — the two systems disagree about them, which is unusual and worth understanding.
- Use the **owner** and **creation date** columns to trace a suspicious replica back to a real person, device, or workflow before deciding what to do with it.
- Prioritize replicas that are **both orphaned and long stale** over ones that are merely one or the other — a replica that's inconsistent between reports *and* hasn't synced in a year is a much stronger candidate for cleanup than one that's simply new.
- Encourage regular syncing as a matter of practice, not just as a housekeeping chore — the sooner data syncs, the less is ever at risk of being lost to a broken or lost device.
- When reviewing the SDE-side report, also note which database server and which account were used to produce it (recorded in the accompanying email) — if the wrong account was used, replica counts can appear artificially low or even zero.

---

## Column descriptions

### FS Column descriptions

These are the columns produced by `INVENTORY_FS_REPLICAS.py`, in the order they appear in the CSV:

| Column | Description |
|---|---|
| `server_site` | **Portal:** the name of the server the replica's service lives on. **AGOL:** the organization's base URL — there is only one "site" on AGOL. |
| `server_role` | **Portal:** `HOSTING_SERVER` for the deployment's main hosting server, `FEDERATED_SERVER` for any other server, or `TEST_SERVICE` when run with `--test-service`. **AGOL:** always `HOSTED` — every AGOL service is a hosted service, so there's no distinction to make. |
| `server_url` | **Portal:** the public URL of the server hosting the replica's service. **AGOL:** the organization's base URL (same value as `server_site`). |
| `service_name` | The name of the feature service, taken from its web address. |
| `service_url` | The full web address (FeatureServer URL) of the service the replica belongs to. |
| `service_owner` | The person/account that owns the feature service item. Blank if the service has no associated item, or the owner couldn't be resolved. |
| `replica_id` | The replica's unique identifier (a GUID when available — the same value you'd use to look it up directly against the service). |
| `replica_owner` | The person/account that created or uses the replica. This can be different from `service_owner`. |
| `date_created` | The date the replica was first created, in `YYYY/MM/DD` format. Blank if unknown. |
| `last_sync_date` | The date the replica last successfully synced, in `YYYY/MM/DD` format. Blank if unknown. |
| `replica_name` | The replica's given name, when one is available. |
| `status` | `Stale` if the last sync was more than 90 days ago, `Active` if within that window, blank if no sync date is available at all. |
| `synced` | A plain-language summary of sync recency: `never` if the replica has never actually synced, otherwise `N days ago`. |

### SDE Column descriptions

These are the columns produced by `DATA_STORE_INVENTORY_SDE_REPLICAS.py`, in the fixed order they are written to the CSV:

```text
platform, name, ID, type, role, service_owner, version, isSender,
datasets, hasConflicts, replica_id, service_url, spatialRelation,
unRegisterFlag, useArchiving, useGeometry, guid, date_created,
lastReceive, lastSend
```

| Column | Description |
|---|---|
| `platform` | Always `PORTAL` — this report only ever covers ArcGIS Enterprise. |
| `name` | The replica name returned by `ListReplicas`. |
| `ID` | The `ID` value returned by `ListReplicas`. |
| `type` | The replica's sync type, in plain terms: `checkout`, `one-way`, `two-way`, or `one-way-child-to-parent`. |
| `role` | The replica's role: `none`, `parent`, or `child`. |
| `service_owner` | The `owner` value returned by the replica object from `ListReplicas`, when available. It is the replica/service-side owner value exposed by the API response. |
| `version` | The database version associated with the replica. |
| `isSender` | Whether this replica is the "sender" side of the sync relationship. |
| `datasets` | A semicolon-separated list of the datasets included in the replica. |
| `hasConflicts` | Whether the replica currently has unresolved sync conflicts. |
| `replica_id` | The replica identifier selected from the supported ID fields; the code prefers a GUID-like value when one is available. |
| `service_url` | The `serviceName` value returned by the replica object. Despite the column name, it may be a service name rather than a complete URL. |
| `spatialRelation` | The replica's spatial filter relationship, in plain terms: `in-x`, `intersects`, or `undefined`. |
| `unRegisterFlag` | Whether the replica is flagged to be unregistered. |
| `useArchiving` | Whether the replica uses historical archiving. |
| `useGeometry` | Whether the replica includes geometry data. |
| `guid` | The GUID returned by the supplemental `GDB_REPLICAS` query, when available. |
| `date_created` | The replica creation date, formatted as `YYYY/MM/DD`; the script prefers `ListReplicas.creationDate` and falls back to the supplemental detail value. |
| `lastReceive` | The supplemental `GDB_REPLICAS` last-receive value, formatted as `YYYY/MM/DD` when available. |
| `lastSend` | The supplemental `GDB_REPLICAS` last-send value, formatted as `YYYY/MM/DD` when available. |
| `status` | `Stale` when the last sync is more than 90 days old, `Active` when within 90 days, or blank when no usable last-sync value exists. The threshold is fixed at 90 days in the script. |

This report has no column recording which database server was actually scanned or how it was found — that detail is only in the accompanying log and email, since a single SDE run connects to one resolved database host.

### Correlate Column descriptions

These are the columns produced by `CORRELATE_SQLITE_REPLICAS.py`, in the order they appear in the CSV. Every run writes a **full** CSV (one row per discovered SQLite item) and, when any confirmed orphans exist, a **second ORPHANS-only CSV** containing just those rows.

| Column | Description |
|---|---|
| `sqlite_item_id` | The item ID of the downloaded SQLite Geodatabase item (discovered by the organization scan). This is the item you would review or remove. |
| `sqlite_title` | The item's title. **Note:** the trailing code in the title is *not* the replica identifier — see the orphan-detection explanation below. |
| `sqlite_owner` | Who owns the downloaded item (from the item's search record). |
| `sqlite_date_created` | When the downloaded item was created (from the item's search record). |
| `sqlite_date_modified` | When the downloaded item was last modified (from the item's search record). |
| `sqlite_access` | The item's sharing level (private/org/public), from the item's search record. |
| `parent_service_url` | The feature service this download came from, read from the item's own record. This is the service that was checked for a matching live replica. |
| `item_replica_guid` | The replica's unique identifier (GUID), read from **inside the item's own record** — this is the real link to the live replica (see below). Blank only if the item record didn't contain one. |
| `match_status` | The headline result for this item: `MATCHED`, `ORPHANED`, `UNKNOWN_SERVICE`, `NO_SERVICE`, or `NO_GUID`. Defined in full below. |
| `service_reachable` | `yes` if the parent service answered when checked, `no` if it could not be read, blank if there was no service to check. The single most important column for trusting an `ORPHANED` verdict. |
| `service_live_replica_count` | How many live replicas the parent service actually had when checked. A `0` here next to an `ORPHANED` verdict means the service genuinely has no replicas left at all. |
| `detail` | A plain-language sentence explaining exactly why this row got its status and what, if anything, to check before acting. |
| `server_site` | The server the matched live replica lives on (filled only for `MATCHED` rows). |
| `service_name` | The feature service's name (filled for matched and orphaned rows). |
| `service_url` | The feature service's full web address. |
| `replica_id` | The matched live replica's own id on the service (filled only for `MATCHED` rows). |
| `replica_name` | The matched live replica's name (filled only for `MATCHED` rows). |
| `replica_owner` | Who created/uses the matched live replica (filled only for `MATCHED` rows). |
| `replica_created` | When the matched live replica was created, `YYYY/MM/DD` (filled only for `MATCHED` rows). |
| `replica_last_sync` | When the matched live replica last synced, `YYYY/MM/DD` (filled only for `MATCHED` rows). |

**The five `match_status` values:**

| Status | Meaning | Safe to act on? |
|---|---|---|
| `MATCHED` | The parent service still has a live replica with this item's GUID. The downloaded item is backed by a real, live replica. | Healthy — leave it alone. |
| `ORPHANED` | The parent service **was reachable and answered successfully**, but has **no** replica with this item's GUID. The replica was deleted; the downloaded item is dangling. | This is a cleanup **candidate** — but still verify first (see caveats). |
| `UNKNOWN_SERVICE` | The item had a valid GUID and a parent service, but **the service could not be read** (offline, 404, permissions, timeout). We genuinely don't know whether the replica still exists. | **No — do not treat as an orphan.** Re-check the service first. |
| `NO_SERVICE` | The item's record had a replica GUID but **no parent service address** to check it against. | No — needs manual lookup. |
| `NO_GUID` | The item's record had **no replica GUID at all** (it may not be a true offline-download replica item, or its record couldn't be read). | No — needs manual review. |

### Delta Column descriptions

| Column | Description |
|---|---|
| `replica_id` | The replica identifier used to match rows between the two reports. |
| `replica_name` | The replica's name (from the FS report, or the SDE report if the FS report doesn't have it). |
| `in_REST` | `True` if this replica ID appeared in the services (FS) report. |
| `in_SDE` | `True` if this replica ID appeared in the database (SDE) report. |
| `location` | The headline result: `BOTH`, `REST_ONLY`, or `SDE_ONLY`. |
| `service` | The service this replica belongs to, sourced from whichever report has it. |
| `replica_owner` | The replica's owner, sourced from whichever report has it. |
| `creation_date` | The replica's creation date, sourced from whichever report has it. |
| `fs_server_url` | The server this replica lives on, from the services report only. Always blank for `SDE_ONLY` rows. |
| `fs_last_sync_date` | The last time this replica synced, from the services report only. Always blank for `SDE_ONLY` rows. |

**How to read the results:** `REST_ONLY` and `SDE_ONLY` rows are your orphans — the two systems disagree about them. `BOTH` rows are healthy and consistent. Use `replica_owner`, `creation_date`, and `fs_last_sync_date` to trace an orphan back to a real workflow before deciding whether to act on it.

---

# For Administrators

## Dependencies on the shared `UTILITIES` package

### FS Dependencies

- `__init__.py`
- `bucketing.py`
- `restutils.py`
- `arcgis_rest.py`
- `secure_session.py`
- `auth.py`

### SDE Dependencies

- `__init__.py`
- `config.py`
- `datastore_config.py`
- `stack_discovery.py`
- `secure_session.py`
- `auth.py`

### Correlate Dependencies

- `__init__.py`
- `bucketing.py`
- `restutils.py`
- `arcgis_rest.py`
- `secure_session.py`
- `auth.py`

(Uses the same shared connection, logging, email, config, TLS-session, and token-provider helpers as the FS tool, plus the shared organization-id resolution and date-range bucketing helpers used to discover the org's SQLite Geodatabase items. It does **not** use the robocopy/azcopy/stack-discovery helpers.)

### Delta Dependencies

No shared `UTILITIES` helper files are used by `REPLICA_DELTA.ps1`.

## Requirements

### FS Requirements

- ArcGIS API for Python (`arcgis`), `requests`, and the shared `UTILITIES` package.
- Org-specific config TOML and OS-keyring credentials.
- Portal/AGOL account with sufficient inventory visibility.

### SDE Requirements

- ArcGIS Python environment with ArcPy, `keyring`, and `requests`.
- Shared `UTILITIES` package and org-specific Portal/Data Store config.
- Portal credentials plus Data Store database credentials; managed schema owner is preferred for replica visibility.

### Correlate Requirements

- ArcGIS API for Python (`arcgis`), `requests`, and the shared `UTILITIES` package.
- Org-specific config TOML and OS-keyring credentials (same connection settings as the FS tool).
- A Portal/AGOL account that can **enumerate the organization's items** (to discover the SQLite Geodatabase items), **read each item's record** (to open its parent service and replica GUID), **and read the parent feature services' replica lists**. An account that cannot see a given service will produce `UNKNOWN_SERVICE` for that service's items rather than a false orphan.
- No input file is required — the tool discovers the SQLite Geodatabase items itself by scanning the organization.

### Delta Requirements

- Windows PowerShell.
- Saved FS and SDE report CSVs with `replica_id` columns.

### Additional requirements detail

The original operational requirements remain applicable:

- **FS:** The shared `UTILITIES` package is normally one directory up (`.\UTILITIES`); the organization-specific TOML supplies the selected `[portal]` or `[agol]` settings plus `[common]`; the account password is stored in the OS keyring or supplied through `PORTAL_PASSWORD`; Portal scans require an account with the visibility needed by the federation endpoint; AGOL scans require sufficient visibility to enumerate hosted feature services and read item ownership.
- **SDE:** ArcPy, `keyring`, and `requests` are required; the shared `UTILITIES` package may be located by walking parent directories; the chosen TOML contains the Portal section, common settings, and Data Store keys; `portal-url`, `username`, and `keyring-service` are required because live Data Store discovery needs a Portal token; the managed-schema-owner DB credential is preferred for `ListReplicas` visibility; the configured admin/monitor credential remains part of Data Store settings and is the fallback when the managed-schema-owner pair is not configured; the Portal credential is needed only for the discovery token; the machine running the script needs network access to the Data Store PostgreSQL instance and whichever machine may be PRIMARY after an HA failover.
- **Correlate:** The shared `UTILITIES` package is normally one directory up (`.\UTILITIES`); the organization-specific TOML supplies the selected `[portal]` or `[agol]` settings plus `[common]` (same keys as the FS tool); the account password is stored in the OS keyring or supplied through `PORTAL_PASSWORD`; the account must be able to enumerate the organization's items, read each item's record, and read the parent services' replica lists; and the authenticated organization id must be resolvable, because the SQLite-item discovery search is organization-scoped and the run aborts rather than fall back to an unscoped search.
- **Delta:** Windows PowerShell, plus the saved CSV output from the Portal-side FS report and the SDE-side report.

## Security: Certificate (TLS) Validation

All three Python tools keep certificate validation enabled. The trust source is selected explicitly with `--tls-mode`; there is no option to disable certificate verification.

### FS Security

- **Certificate validation:** `standard-ca` is the default; `os-trust-store` uses the OS certificate store and requires `truststore`; `custom-ca-bundle` uses `--ca-bundle-path`. A real TLS handshake is checked before scanning.
- **SSRF / network-boundary defenses:** Portal and service destinations are validated before token-bearing requests. The shared secure REST layer applies URL/origin restrictions and rejects unsafe URL forms.
- **Token handling:** Authentication is supplied through the shared token-provider/session utilities and is not intentionally written to the log, console, email, or CSV.
- **Large/malformed responses:** Direct REST handling uses the shared secure-session layer for bounded streaming and JSON validation.
- **Redirect following:** Automatic redirect following is disabled in the secure request path.
- **Python import-path/package shadowing:** The current FS script inserts the expected project root at the front of `sys.path`, but it does not remove equivalent duplicate roots or independently verify the imported `UTILITIES` package origin.

### SDE Security

- **Certificate validation:** `standard-ca`, `os-trust-store`, and `custom-ca-bundle` are supported. A real TLS handshake against the Portal URL is verified before Data Store discovery and token use.
- **SSRF / network-boundary defenses:** Portal token use is limited to the configured hosting-server discovery workflow. Data Store hosts come from authenticated topology discovery or explicit `--db-host` and are processed through hostname helpers.
- **Token handling:** The Portal discovery token is immediately wrapped in `StaticTokenProvider` and is not intentionally printed or emailed.
- **Large/malformed responses:** Shared secure-session/discovery utilities handle direct REST responses; empty or ambiguous Data Store topology is rejected instead of guessed.
- **Redirect following:** Direct discovery calls use the shared secure-session behavior rather than blindly following redirects.
- **Python import-path/package shadowing:** The current SDE script inserts the expected project root when `UTILITIES` is found, but it does not remove duplicate roots or independently verify the loaded package origin after import.

### Correlate Security

- **Certificate validation:** identical model to the FS tool — `standard-ca` (default), `os-trust-store` (requires `truststore`), or `custom-ca-bundle` (requires `--ca-bundle-path`). A one-time real TLS handshake against the Portal/AGOL URL is verified before any item or service is read; a failed verification aborts the run.
- **SSRF / network-boundary defenses:** This tool carries its own hardened REST client and trusted-URL policy. Item discovery is strictly organization-scoped (`orgid:<id>`), search paging/totals/ids are validated, and cross-organization items are rejected. The organization token is sent to a service URL only after it validates as a FeatureServer/MapServer `/replicas` endpoint on a trusted origin (the authenticated Portal proxy path, a federated service base, or — on AGOL — an `*.arcgis.com` host). Unsafe URL forms (user-info, query/fragment, encoded separators, traversal) are rejected, and redirects are revalidated against the same origin/path policy with a hard cap.
- **Token handling:** authentication goes through the shared token-provider/session utilities, is inserted only at the request site (never in a logged URL), supports a single synchronized refresh/retry on an auth failure, and is passed through `redact_token()` before any logging, email, or CSV output.
- **Large/malformed responses:** direct REST responses are streamed and capped at 64 MB, must decode as JSON, and REST error payloads are classified explicitly.
- **Read-only by design:** the tool only issues read requests (searching items, reading item records, and listing replicas). It has no code path that unregisters, deletes, or modifies any replica or item.

### Delta Security

`REPLICA_DELTA.ps1` reads local CSVs only; it does not make network requests, use Portal tokens, or perform TLS validation.

## Configuration

### FS Configuration

Connection, email, and output settings are read from the **org-specific config TOML file** named by the required `--config` argument. Within that file, the required `--target portal` or `--target agol` argument chooses the matching section (`[portal]` or `[agol]`) plus `[common]`: `portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`, `max-email-mb`.

**Precedence (highest wins):** command-line argument → chosen target section → `[common]` section. Any value left blank in config **and** omitted on the command line aborts the run with a clear message. The password comes from the OS keyring (service = `keyring-service`, user = `username`), falling back to the `PORTAL_PASSWORD` environment variable — the same lookup mechanism regardless of target.

This script also resolves `max-email-mb` from the org config (via `get_max_email_mb(args.target, args.config)`) and passes it into `gather_attachments()` as `cap_mb` — the same project-wide setting used by every other `PORTAL_*` script. If `max-email-mb` is unset in config, the attachment cap falls back to `emailer.py`'s built-in default (`MAX_ATTACHMENTS_MB = 20`).

**Security — TLS/certificate validation.** This script never disables certificate checking. Every HTTP call goes over a session built with an explicit `--tls-mode`:

- `standard-ca` (default) — verifies against the standard public Certificate Authority list (correct for AGOL, or a Portal with a publicly-trusted certificate).
- `os-trust-store` — verifies against the operating system's own certificate store (for an internal Portal signed by an organizational CA already trusted at the OS level). Requires the `truststore` package.
- `custom-ca-bundle` — verifies against a specific PEM file you provide with `--ca-bundle-path`.

Before any real work begins, the script also performs a one-time, real TLS handshake against the target Portal/AGOL URL and logs a clear PASS/FAIL verdict (protocol, cipher, and certificate detail). If that verification fails, the run aborts immediately rather than proceeding over an unverified connection.

### SDE Configuration

Data Store, output, and email settings are read from the **org-specific config TOML file** named by `--config`, always using the fixed `[portal]` section (plus `[common]`) — there is no target-selection step since there is nothing to select between (the managed SDE Data Store only exists behind an Enterprise Portal deployment, so this tool has no `--target` flag at all). Data Store keys include `db-port`, `dbname`, `db-user`, `keyring-service-db`, `db-user-mu`, and `keyring-service-db-mu`; output/email keys include `output-dir`, `smtp-server`, `email-to`, `email-from`.

**`portal-url`, `username`, and `keyring-service` are required settings**, not optional overrides — `finalize_config()` validates them the same as `output-dir`/`smtp-server`/`email-to`/`email-from`, because live Data Store discovery needs a Portal token even though the SDE connection itself never uses `arcgis`. A run with these missing from both the command line and config aborts immediately with a clear message, before any Portal or database connection is attempted.

**`db-host` is intentionally not read from config for connecting** — the live primary is auto-detected on every run. A `hosting-url` setting (used only to reach the Data Store discovery endpoint) is read from the `[portal]`/`[common]` section, or can be overridden with `--hosting-url`.

**`db-user`/`keyring-service-db` are resolved but then overridden** by `db-user-mu`/`keyring-service-db-mu` whenever the latter pair is configured, since `ListReplicas` needs to connect as the managed schema owner to actually see replica rows. Both pairs are still independently validated: `db-user`/`keyring-service-db` by `missing_datastore_keys()` (since `get_datastore_settings()` always resolves them first), and `db-user-mu`/`keyring-service-db-mu` only insofar as their absence produces a logged warning and a fallback, not a hard failure.

**Precedence (highest wins):** command-line argument → `[portal]` section → `[common]` section. The DB password (admin/monitor account) comes from the OS keyring (service = `keyring-service-db`, user = `db-user`), falling back to `DATASTORE_PASSWORD`. The managed-schema-owner DB password comes from the OS keyring (service = `keyring-service-db-mu`, user = `db-user-mu`), also falling back to `DATASTORE_PASSWORD` if unset. The Portal password (needed only for the discovery token) comes from the OS keyring (service = `keyring-service`, user = `username`), falling back to `PORTAL_PASSWORD`.

**Security — TLS/certificate validation.** The Portal token request and the Data Store discovery REST call both go over a session built with an explicit `--tls-mode` (`standard-ca` by default, or `os-trust-store` / `custom-ca-bundle` — same three modes as the FS tool). A one-time real TLS handshake against `--portal-url` is verified before any token is requested; a failed verification aborts the run rather than proceeding insecurely. The short-lived Portal discovery token itself is wrapped immediately in a `StaticTokenProvider` so it can never be accidentally written to the log, console, email, or CSV.

### Correlate Configuration

Connection, email, and output settings are read from the org-specific config TOML named by `--config`, with `--target portal` or `--target agol` selecting the matching section plus `[common]` — **exactly the same keys, precedence, and password lookup as the FS tool** (`portal-url`, `username`, `keyring-service`, `output-dir`, `smtp-server`, `email-to`, `email-from`, `max-email-mb`; precedence command line → target section → `[common]`; password from keyring with `PORTAL_PASSWORD` fallback).

There is **no input-file argument** — the tool discovers the SQLite Geodatabase items to check by scanning the organization itself, so the only required inputs are `--config` and `--target`.

`max-email-mb` is resolved from config and applied to the email attachments the same way as the FS tool (both the full CSV and the orphans-only CSV are attached, subject to the same size-tiered zip/leave-on-disk policy).

**TLS/certificate validation** behaves exactly as described for the FS tool: never disabled, selected with `--tls-mode`, and proven with a one-time handshake check before any real work begins.

### Delta Configuration

There is no config file — all settings are supplied as script parameters (`-RestReport`, `-SdeReport`, `-OutputDir`). The only "configuration" baked into the script itself is the hard-coded `$HostingServerUrl` constant used to scope REST replicas to the hosting server; changing which server this script reconciles against requires editing that constant.

**Column names (as produced by the current Python scripts), which this script depends on:**

- REST report columns used: `replica_id`, `replica_name`, `replica_owner`, `service_url`, `service_name`, `date_created`, `last_sync_date`, `server_url`
- SDE report columns used: `replica_id`, `name`, `service_owner`, `service_url`, `date_created`

---

## How it works

### Shared UTILITIES roles used by the Python tools

The dependency section above intentionally lists only helper-file names. The existing purpose detail is retained here.

#### FS utility roles

| Helper | Purpose |
|---|---|
| `build_timestamp`, `start_timer`, `timing_block` | Timestamping and elapsed-time reporting. |
| `setup_logging` | Console + log-file logging. |
| `connect_gis` | Portal/AGOL connection using the keyring-stored password. |
| `send_email` | Sends the completion email with attachments. |
| `get_platform` | Derives the `PORTAL`/`AGOL` platform label from the portal URL. |
| `get_org_id` | Resolves the authenticated AGOL organization id. |
| `epoch_to_date` | Epoch-to-date formatting (`YYYY/MM/DD`, UTC) for `date_created`/`last_sync_date`. |
| `add_common_portal_args`, `add_email_args`, `add_output_dir_arg`, `add_target_arg`, `add_config_arg` | Argument wiring, including the required `--config` and `--target` flags. |
| `subject_line` | Email subject construction, including the org-name prefix. |
| `finalize_config` | Config merge, required-key validation, and `org-name`/`org-name_safe` resolution. |
| `get_max_email_mb` | Resolves the configured email attachment cap. |
| `ensure_output_dir` | Creates the output directory if needed. |
| `gather_attachments` | Collects the log + CSV for the email using the shared size-tiered policy (original → zipped → left on disk). |
| `build_pooled_session` / `build_secure_session` | Builds the pooled, retrying, TLS-verified HTTP session used for every REST call in this script. |
| `verify_tls_handshake` | One-time startup check proving the negotiated TLS handshake actually verified. |
| `bucketed_search`, `DEFAULT_CAP` (`bucketing.py`) | Date-range bucketed enumeration, used for AGOL hosted-service discovery. |
| `resolve_owners_batched` (`arcgis_rest.py`) | Resolves service item owners in batched search calls instead of one lookup per item. |
| `provider_for_gis` (`auth.py`) | Wraps the connection's auth token in a `TokenProvider` so it is never accidentally logged. |

The script also imports `arcgis.gis.GIS`, but only as a type hint — no `GIS` object is constructed directly (the connection goes through `connect_gis`).

#### SDE utility roles

| Helper | Purpose |
|---|---|
| `build_timestamp`, `start_timer`, `timing_block` | Timestamping and elapsed-time reporting. |
| `setup_logging` | Console + log-file logging. |
| `send_email` | Sends the completion email with attachments. |
| `ensure_output_dir` | Creates the output directory if needed. |
| `gather_attachments` | Collects the log + CSV for email (this script does not pass a `cap_mb`, so it uses `emailer.py`'s built-in default). |
| `write_dict_csv` | Shared CSV-writing helper (silently writes nothing if `rows` is empty). |
| `add_email_args`, `add_output_dir_arg`, `add_test_flag`, `add_config_arg`, `add_common_portal_args` | Argument wiring, including the required `--config` flag, the `--test`/`--no-test` toggle, and the required `--portal-url`/`--username`/`--keyring-service` for live Data Store discovery. |
| `subject_line` | Email subject construction, including the org-name prefix. |
| `finalize_config` | Config merge, required-key validation, and `org-name`/`org-name_safe` resolution. |
| `build_secure_session`, `verify_tls_handshake` (`secure_session.py`) | Explicit, caller-chosen TLS verification for the Portal token call and the Data Store discovery REST call, plus a startup handshake check. |
| `StaticTokenProvider` (`auth.py`) | Wraps the short-lived Portal discovery token immediately on acquisition so it is never exposed to logging by accident. |
| `add_datastore_args`, `get_datastore_settings`, `missing_datastore_keys` (`datastore_config.py`) | Data Store-specific argument wiring and settings resolution for the **non-host** keys (`dbname`, `db-user`, `db-port`, `keyring-service-db`); the host itself is always re-resolved by live discovery. |
| `discover_data_store` (`stack_discovery.py`) | Standalone (no `gis` object needed) live Data Store topology discovery; resolves the current PRIMARY machine, HA-aware. |
| `short_hostname`, `looks_like_fqdn` (`stack_discovery.py`) | Used to turn a discovered machine name into a connectable FQDN. |

This script uses **ArcPy** (`arcpy.management.CreateDatabaseConnection`, `arcpy.Describe`, `arcpy.da.ListReplicas`, `arcpy.ArcSDESQLExecute`) for the SDE work, and `requests` + the `UTILITIES` helpers above purely to acquire a Portal token and query Data Store topology — it does not import the ArcGIS API for Python at all.

#### Correlate utility roles

| Helper | Purpose |
|---|---|
| `build_timestamp`, `start_timer`, `timing_block` | Timestamping and elapsed-time reporting. |
| `setup_logging` | Console + log-file logging. |
| `connect_gis` | Portal/AGOL connection using the keyring-stored password. |
| `send_email` | Sends the completion email with attachments (full CSV + orphans-only CSV). |
| `get_platform` | Derives the `PORTAL`/`AGOL` platform label from the portal URL. |
| `get_org_id` | Resolves the authenticated organization id so the SQLite Geodatabase item discovery search is organization-scoped. |
| `epoch_to_date` | Epoch-to-date formatting (`YYYY/MM/DD`, UTC) for the discovered item dates and the matched replica's date columns. |
| `add_common_portal_args`, `add_email_args`, `add_output_dir_arg`, `add_target_arg`, `add_config_arg` | Argument wiring, including the required `--config` and `--target` flags. |
| `subject_line` | Email subject construction, including the org-name prefix. |
| `finalize_config`, `get_max_email_mb` | Config merge/validation and the email attachment cap. |
| `ensure_output_dir`, `gather_attachments` | Output-directory creation and the shared size-tiered attachment policy. |
| `DEFAULT_CAP` | The shared search result cap used by the strict date-range bucketing that enumerates the org's items. |
| `build_secure_session`, `verify_tls_handshake` (`secure_session.py`) | The pooled, retrying, TLS-verified HTTP session used for item discovery, reading item records, and listing replicas, plus the one-time startup handshake check. |
| `provider_for_gis` (`auth.py`) | Wraps the connection's auth token in a `TokenProvider` so it is never accidentally logged. |
| `redact_token` | Sanitizes token-bearing text before it reaches the log, email, or CSV. |

#### Delta utility notes

This script has **no dependency on the shared `UTILITIES` package** — it is pure PowerShell using only `Import-Csv` / `Export-Csv` and built-in .NET collections (`Dictionary`, `HashSet`, `StringComparer`). It depends on the **column names** the two Python inventory tools produce (see [FS Column descriptions](#fs-column-descriptions) and [SDE Column descriptions](#sde-column-descriptions)), but not on any shared code module.

### FS How it works

1. **Connect** to the chosen target via `connect_gis()`.
2. **On Portal**, enumerate servers and walk their catalogs to build the job list. **On AGOL**, resolve the org id and enumerate hosted feature services via bucketed search to build the same-shaped job list.
3. **Resolve service owners.** The distinct set of service item IDs is resolved to item owners using **batched search calls** (`id:(a OR b OR ...)`) rather than one lookup per item — typically a 50–100× reduction in round-trips for this phase. This step is identical on both targets.
4. **Scan replicas (two passes)** — identical on both targets:
   - **Pass 1** lists `/replicas` for every service in parallel. Replicas whose list response already carries owner, creation date, and last-sync become rows immediately.
   - **Pass 2** fetches per-replica detail (`/replicas/{id}`) only for replicas missing one of those fields, run in parallel with the busiest services scheduled first so long tails overlap. Detail calls request no geometry (`returnGeometry=false`) to stay lean.

All activity is logged to both the console and a log file. **Every phase is timed** via the script's own `PhaseTimer`, and a `PHASE TIMING SUMMARY` block at the end of the log shows exactly where the time went, sorted largest-first. On completion the script emails the log plus the CSV, using the shared, project-wide `max-email-mb` attachment policy — an oversized CSV is zipped and attached if the zip fits under the cap, and left on disk (with a note in the email body) only if even the zip is too large.

**Replica GUID vs. numeric ID.** The `/replicas` list endpoint returns the replica's GUID under the key `replicaID` on this deployment — not under a more obviously GUID-shaped field name. `GUID_KEYS` is widened to include the field names this deployment's list endpoint actually uses, and `get_replica_detail()` tries the GUID form first before falling back to a numeric id. `DETAIL_ID_KEYS` is pinned to `["replicaID"]` only, since on this deployment that field *is* the GUID the detail endpoint resolves against — this avoids an unnecessary second fallback detail call per replica. `GUID_KEYS`/`NUM_ID_KEYS` are retained for **display and de-duplication** (via `ID_KEYS = GUID_KEYS + NUM_ID_KEYS`) and as documentation of what other deployments might use.

The `replica_id` column reports the GUID when present — matching what you would paste into the REST endpoint. Pass 2 also logs how many detail calls returned a date versus came back blank, and warns if every detail call is blank (a signal that the GUID field name in your `/replicas` JSON differs from what `GUID_KEYS` expects, or that `DETAIL_ID_KEYS` needs adjusting for your deployment).

**Staleness.** A replica is marked `Stale` when its last sync is older than `STALE_AFTER_DAYS` (90 days), `Active` when within that window, and blank when no last-sync timestamp is available. The 90-day threshold can be edited in the script if desired. This applies identically on Portal and AGOL.

### SDE How it works

1. **Resolve non-host settings, then swap in the managed-schema-owner account.** Data Store settings other than `db-host` (`dbname`, `db-port`, `db-user`, `keyring-service-db`) plus output/email settings are resolved from the fixed `[portal]` config section, `[common]`, and the command line. The resolved `db-user`/`keyring-service-db` (the admin/monitor account) are then **overridden** with `db-user-mu`/`keyring-service-db-mu` whenever the latter pair is configured — the SDE/`ListReplicas` connection needs to run as the managed schema owner to see replica rows at all. Missing required values abort the run with a clear message.
2. **Detect live Data Store topology.** Unless `--db-host` is given, the script builds a TLS-verified session (per `--tls-mode`), performs a one-time handshake check against `--portal-url`, acquires a minimal Portal token, and calls `discover_data_store()` against the hosting server's admin REST API to resolve the current PRIMARY machine (HA-aware). Discovery failure or ambiguity aborts the run with a failure email at this step; no fallback to a config guess is attempted.
3. **Retrieve the DB password.** The password is read from the OS keyring (service = the resolved `keyring-service-db`, which is the `-mu` value when configured, otherwise the admin/monitor value; user = the correspondingly resolved `db-user`), falling back to the `DATASTORE_PASSWORD` environment variable.
4. **Build a temporary SDE connection (no checked-in `.sde`).** The script creates a *temporary* database connection file at startup with `arcpy.management.CreateDatabaseConnection` (PostgreSQL, `DATABASE_AUTH`), using the resolved live-primary host and the resolved (schema-owner, when configured) account. The file is written to a per-run temp directory and deleted in a `finally` block. The connection is validated with `arcpy.Describe` immediately after creation; this validation step is timed and the elapsed seconds are reported in both the log and the completion email.
5. **Enumerate replicas.** A single `arcpy.da.ListReplicas(sde_path, True)` call returns all replicas (including feature-service sync info); in test mode (the default) only the first 50 are kept.
6. **Fetch replica detail in one bulk query.** GUID, creation date, last-receive, and last-send are read with **one** bulk SQL round-trip against the replica system table (`GDB_REPLICAS`), keyed by replica name, instead of per-replica lookups. Several common column layouts are tried in order; if none can be read, those columns simply come out blank.
7. **Build rows and write CSV in one pass.** Integer property codes are mapped to text (`type`, `role`, `spatialRelation`), and the three date columns are rendered to a human-readable `YYYY/MM/DD` string.

On completion the script emails the log plus the CSV, and reports which Data Store host was used and how it was resolved, along with which account the SDE connection actually used.

**Date formatting.** The `date_created`, `lastReceive`, and `lastSend` columns are rendered to `YYYY/MM/DD` regardless of whether the source hands back a `datetime` object, an epoch-milliseconds (or epoch-seconds) value, or an already-formatted string.

### Correlate How it works

At a high level, the script runs in four steps:

1. **Discover the SQLite Geodatabase items itself.** The tool resolves the authenticated organization id and enumerates every item in the organization using strict, organization-scoped (`orgid:<id>`) date-range bucketed search — the same hardened enumeration ITEM_INVENTORY.py uses — then keeps only the items whose type is `SQLite Geodatabase`. This self-generated list is the set of downloaded-data items to check; **there is no input CSV.**
2. **Open each discovered item's own record and read two facts from it** — which feature service the download came from, and the replica's unique ID (GUID). (Explained in detail below.)
3. **Check only the services those items point at**, list the live replicas on each, and match each downloaded item's GUID against them.
4. Write the CSV(s) and email the report.

Because the tool builds the item list from the organization inventory and then only contacts the handful of services those items actually came from — not every service in the whole deployment — it still finishes in minutes, not hours.

#### How the SQLite-item discovery feeds the replica investigation

The item list is produced by the tool itself rather than supplied as a file. This is the first thing the run does, and it replaces the former `--input-csv` input entirely.

Concretely:

- **Discovery is organization-scoped and complete.** The tool resolves the authenticated organization id (`get_org_id`) and, for every month from January 1, 2000 to one year ahead, searches `orgid:<id> AND created:[<lo> TO <hi>]`. It pages each bucket strictly — validating the declared total, rejecting duplicate ids within a query, requiring pagination to advance, and refusing to exceed the shared result cap — and recursively subdivides any bucket that reaches the cap, so an organization with more items than the standard search cap is still fully enumerated rather than silently truncated. If the organization id cannot be resolved, the run aborts rather than fall back to an unscoped search, exactly as the FS tool does.
- **Discovered items are held in a disk-backed store, then filtered.** Every enumerated item is written to a temporary SQLite catalog (so a very large organization does not have to stay in memory), then read back in sorted-id order and filtered to the `SQLite Geodatabase` type. Each surviving item carries forward the same identifying fields the input CSV used to supply — `item_id`, `title`, `owner`, created/modified dates, and sharing access — now read straight from the item's own search record. From this point on, the per-item logic is identical to before: the tool opens each item's record, reads its parent service and real replica GUID, and matches that GUID against the live replicas of that item's own parent service.
- **Nothing else about the verdicts changes.** The five `match_status` outcomes, the `service_reachable`/`service_live_replica_count` safety columns, the per-row `detail` sentence, and the strict separation of `ORPHANED` from `UNKNOWN_SERVICE` all behave exactly as described below. The only thing that moved is the *source* of the item list: from an externally supplied CSV to an inventory the tool generates for itself.

The practical effect is that the tool is now self-contained — there is no separate export step to run first, no input file to keep in sync with the organization's current content, and no risk of adjudicating a stale item list. Each run always investigates the organization's SQLite Geodatabase items as they exist at that moment.

#### How orphan detection *really* works — a very detailed, plain-language walkthrough

This section explains, step by step and in everyday terms, exactly how the tool decides that a downloaded item is an "orphan," and — just as importantly — how confident you can be in that decision. It's worth reading carefully, because an orphan verdict can lead someone to delete data, and you should understand precisely what it does and does not prove.

**First, the thing that makes this hard: the title lies.**

Every downloaded item has a title that looks something like:

> `YOSE_SSP_Data_Collection_20260915-37a96bf09d844388bac31c52fe700a08`

It is extremely tempting to assume that the long string of letters and numbers on the end of the title *is* the replica's ID, and that you could match it against the server just by reading the title. **This assumption is wrong, and it is the single reason earlier versions of this tool matched nothing at all.** In reality, a single downloaded item carries *three different* long code strings, and they are all different from one another:

- the code on the end of the **title** (e.g. `37a96bf0…`),
- the code buried in the **file name** of the download (e.g. `bc4c418e…`),
- and the **real replica ID** (e.g. `439f4943…`), which is the only one the server actually recognizes.

Matching on the title's code is like trying to find a parcel in a warehouse by reading the *customer's reference number* off the delivery slip instead of the *warehouse's own barcode*. They're both real numbers printed on the same slip — but only one of them is the number the warehouse shelves are actually organized by. The tool therefore **ignores the title completely for matching purposes** (it keeps it only so the report is readable to a human) and instead goes after the real replica ID.

**Step 1 — Open each discovered item and read its true details.**

For each SQLite Geodatabase item the discovery step found, the tool opens that item's own official record on the portal (the same underlying information you'd see if you opened the item's page and looked at its full details). From inside that record it pulls out two specific facts:

1. **The parent feature service** — the exact service this download was made from. The item's record literally contains a pointer back to it (for example, `.../rest/services/Hosted/YOSE_SSP_Data_Collection_20260915/FeatureServer`). This is authoritative: it's the item telling us, in its own words, where it came from — we are not guessing from the name.
2. **The real replica ID (GUID)** — the actual server-recognized identifier of the replica this download represents (e.g. `439f4943…`). This, too, is stored inside the item's record. This is the "warehouse barcode" — the only code the server will recognize.

If an item's record is missing either of these, the tool does **not** guess. It marks that item `NO_GUID` (no real replica ID found) or `NO_SERVICE` (no parent service address found) and moves on. Those items are explicitly *not* called orphans, because the tool never had what it needed to make that judgment.

**Step 2 — Ask each parent service for its live replicas.**

Now the tool goes to each parent service that at least one item pointed at (and only those services) and asks it: *"List all the replicas you currently have."* The service hands back its current, live list.

There's one extra wrinkle the tool handles here. When a service lists its replicas, it initially identifies each one only by a short internal number (1, 2, 3…), **not** by the full GUID barcode we need to match against. So for each replica in the list, the tool does a second, more detailed lookup to retrieve that replica's full GUID. Only once it has the real GUID does a live replica count as something it can match against. (This detail step is the main reason the "list replicas" phase takes a few minutes rather than a few seconds — it's OTHER_ORGng a careful, full-barcode read of every live replica on every service your items reference.)

**Step 3 — Match each downloaded item against its own service's live list.**

This is the actual correlation, and it is deliberately narrow and strict:

> For a given downloaded item, the tool takes **that item's real replica GUID** and looks for it **only among the live replicas of that item's own parent service** — not across the whole deployment.

The reason it only checks the item's *own* parent service (rather than searching everywhere) is that the item itself told us exactly where it came from. Checking anywhere else would be both slower and less meaningful. Three outcomes are possible:

- **The GUID is found** in that service's live list → **MATCHED.** The downloaded item is backed by a real, currently-alive replica. The report fills in all the details of that live replica (its name, owner, creation date, last sync date) so you can see the healthy pairing.
- **The service answered, we read its full replica list successfully, and the GUID is *not* in it** → **ORPHANED.** The service is alive and we could see everything it has — and the replica this download came from simply isn't there anymore. That's a genuine dangling download: the replica was deleted on the server, but the downloaded item was left behind.
- **The service could not be read at all** (it was offline, returned an error, timed out, or the account lacked permission to see it) → **UNKNOWN_SERVICE.** This is the critical safety distinction, explained next.

**The most important safety feature: "the replica is gone" vs. "we couldn't check."**

These two situations look identical if you're careless — in both, the report has no live replica to show you — but they mean completely opposite things:

- **ORPHANED** means: *"We successfully asked the service for its complete list of replicas, and this one is genuinely not on it."* This is a real finding.
- **UNKNOWN_SERVICE** means: *"We couldn't get a straight answer from the service at all, so we have no idea whether the replica is still there."* This is **not** a finding — it's a failed check.

Earlier designs risked collapsing these two together, which would be dangerous: a service that was simply temporarily unreachable would make **every** download from it look orphaned, and someone acting on that report could delete hundreds of items whose replicas were perfectly fine. This tool keeps them strictly separate. It only ever says `ORPHANED` when it actually received and read a complete, successful replica list and confirmed the GUID's absence. Any failure to read the service becomes `UNKNOWN_SERVICE` instead, and those items are deliberately **left out of the orphans-only cleanup CSV**. Two columns let you confirm this for yourself on any row: **`service_reachable`** (`yes`/`no`) and **`service_live_replica_count`** (how many replicas the service actually reported). A trustworthy orphan row will always show `service_reachable = yes`.

**How reliable is an ORPHANED verdict, really?**

An `ORPHANED` result is **strong evidence**, but it is **not an absolute guarantee**, and it should be treated as a high-quality *candidate for review*, not an automatic delete list. Here is an honest accounting of what it does and doesn't prove.

What makes it *reliable*:

- It matches on the **real, server-recognized replica GUID** taken from the item's own record — not on the title, not on a guessed name. If a live replica with that exact GUID existed on the service, it would be found.
- It checks against the item's **own stated parent service**, taken from the item's own record — so it's comparing against exactly the right place, not a lookalike service.
- It **only fires on a confirmed, successful read** of the service's replica list. A service that couldn't be reached can never produce a false orphan — it produces `UNKNOWN_SERVICE` instead.
- The GUID comparison ignores capitalization and formatting differences, so a replica can't be falsely declared missing just because the two sides wrote the same GUID in a slightly different style.

What keeps it from being an absolute certainty — the honest caveats:

- **It's a snapshot in time.** The tool reads the live replicas at the moment it runs. If a replica is deleted *right after* the item's record is read but *before* the service is checked (or vice versa), timing alone could misclassify it. Running against a reasonably quiet system avoids this.
- **It trusts the service's replica list to be complete.** If a service were to return only a *partial* list of its replicas without reporting any error, a replica that genuinely exists could be missed and wrongly flagged orphaned. The `service_live_replica_count` column is your sanity check here — an unexpectedly low count next to many orphans on the same service is a signal to look closer.
- **A "deleted replica" is not the same as "safe to delete the item."** An orphaned download may still contain **field edits that were never synced back** before the replica was removed. Finding that the replica is gone tells you the *sync relationship* is dead — it does **not** tell you the downloaded data is worthless. Treat orphans as items to **investigate and consciously decide about**, never as an automatic purge list.

For these reasons the report attaches a plain-language `detail` sentence to **every** row explaining its verdict, writes confirmed orphans to a **separate CSV** so they're easy to review in isolation, and the email body reminds you to verify before acting. The design philosophy throughout is the same as the rest of this toolkit: **it is always better to flag something for a human to look at than to quietly make a deletion decision on the software's own authority.**

**In one sentence:** an `ORPHANED` verdict means *"we definitely reached this item's own parent service, read its complete live replica list, and the specific replica this download came from is definitely not in it"* — which is a reliable reason to **investigate** the item, and a deliberately insufficient reason, on its own, to **delete** it.

### Delta How it works

1. **Validate inputs.** The two report paths must exist as files and the output directory must exist, or the script aborts (via `throw`) with a message pointing back to the required upstream step.
2. **Validate headers.** Before loading any data, the script confirms both the REST report and the SDE report actually contain a `replica_id` column, and aborts immediately with the found column list if either is missing.
3. **Load and filter the REST report** into a case-insensitive dictionary keyed by `replica_id` (blank IDs skipped, IDs trimmed of whitespace). **Only rows whose `server_url` exactly matches the hard-coded hosting-server URL** (`$HostingServerUrl`) are kept — every row belonging to a sibling federated server is counted and dropped. The console output reports both how many REST IDs were kept and how many were excluded for this reason.
4. **Load the SDE report** into a case-insensitive dictionary keyed by `replica_id` (same skipping/trimming). SDE replicas are **not** server-scoped — they are all retained.
5. **Union the IDs** from both (already-filtered) reports.
6. **Classify each ID** as `BOTH`, `REST_ONLY`, or `SDE_ONLY`, and assemble service / owner / creation-date / replica-name from whichever report has the value (REST preferred, SDE fallback), plus two REST-only extras (`fs_server_url`, `fs_last_sync_date`) that are always blank for `SDE_ONLY` rows.
7. **Write the CSV**, sorted by `location` then `replica_id` so orphans group together, and print a summary (BOTH / REST_ONLY / SDE_ONLY counts, total, elapsed time).

Comparison is case-insensitive (`OrdinalIgnoreCase`) and IDs are trimmed before matching, so the GUID-based `replica_id` from the REST tool lines up with the `replica_id` from the SDE tool regardless of case or stray whitespace.

---

## Test mode (default)

### FS Test Mode

There is no default test mode that runs automatically — instead, `--test-service <FeatureServer URL>` opts into a single-service test run. A full run touches thousands of services and tens of thousands of replicas on Portal — and on AGOL can mean tens of thousands of services and 100,000+ replicas — so it is a poor way to verify a code change. `--test-service` points the script at **one** FeatureServer (Portal **or** AGOL) whose replicas you can independently confirm in a browser, and returns an answer in seconds. It reuses the exact same two-pass scan path as a full run, and logs a per-replica breakdown of id / created / last-sync / owner / status under a `TEST RESULTS` block, plus a count of how many replicas still have both dates blank. Server enumeration/catalog walking (Portal) or bucketed search (AGOL) are skipped entirely, but the CSV is still written to `output-dir` and **the log + CSV are emailed just like a full run**.

**Typical workflow:**

1. In a browser, open a known replica's detail endpoint (e.g. `.../FeatureServer/replicas/<GUID>`) and note its `creationDate` / `lastSyncDate`.
2. Run the script against just that service with `--test-service <FeatureServer URL>` (works the same whether the URL is a Portal or an AGOL service).
3. Find that replica's GUID in the log's `TEST RESULTS` block and confirm the dates are populated and match the browser.

### SDE Test Mode

Unlike the REST-side tool, **test mode is the DEFAULT here** — a test run scans only the first `TEST_LIMIT` (50) replicas. Pass `--no-test` for a full inventory of every replica. Note that Data Store discovery always runs in full regardless of test mode — the 50-replica cap applies only to the replica-listing step, since discovery itself is a single, cheap call.

### Correlate Test Mode

There is no dedicated test-mode flag, and because the tool now inventories the organization's SQLite Geodatabase items itself, there is no small input file to point it at for a cheap run — a run always investigates whatever SQLite items the organization currently has. To test the tool cheaply, run it against a non-production or small organization — ideally one with a handful of SQLite items whose replica status you can confirm in a browser (open an item's page to see its parent service and replica ID, then open that service's `/replicas` endpoint to see whether the replica is still listed). Because the tool only contacts the services those items reference, a small organization returns an answer in seconds and the `detail` column lets you confirm the verdict directly against what you see in the browser.

### Delta Test Mode

There is no test mode — this script always processes the full contents of whatever two CSVs you point it at. To "test" it, run it against a small pair of reports (for example, output from `--test-service` on the FS side and a `--test`-mode SDE run) before trusting it against full production reports.

---

## Usage

### FS Usage

`--config` and `--target` are always required, and `--target` accepts `portal` or `agol`.

```bash
# Simplest run — Portal, everything from the org config:
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal

# Simplest run — AGOL, everything from the org config:
python INVENTORY_FS_REPLICAS.py --config org2.config.toml --target agol

# More workers for a faster scan (either target):
python INVENTORY_FS_REPLICAS.py --config org2.config.toml --target agol --workers 96

# Override just the output directory:
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --output-dir "D:\reports"

# Test a single service's replicas (verify create/last-sync dates), either target:
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal \
    --test-service https://enterprise.com/server/rest/services/Hosted/Foo/FeatureServer
python INVENTORY_FS_REPLICAS.py --config org2.config.toml --target agol \
    --test-service https://services.arcgis.com/<org>/arcgis/rest/services/<name>/FeatureServer

# Use the OS certificate store instead of the public CA list (internal CA):
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --tls-mode os-trust-store

# Show help:
python INVENTORY_FS_REPLICAS.py --help
```

**Scenario: verifying a code change before a multi-hour Portal run.**
You've just adjusted `GUID_KEYS`/`DETAIL_ID_KEYS` after noticing blank dates in a previous run, and you don't want to wait 6-9 hours to find out if the fix worked. Pick one feature service you can also check in a browser and point `--test-service` at it — the run finishes in seconds and the log's `TEST RESULTS` block lets you compare each replica's dates directly against what the REST endpoint shows.

```bash
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal \
    --test-service https://enterprise.com/server/rest/services/Hosted/ROMO_TRANS_Trails_OfficialVisitorMap_ln/FeatureServer
```

**Scenario: full deployment-wide replica audit on Portal.**
Your Enterprise deployment has a hosting server plus several federated servers (east, west, waso), and you need one inventory covering all of them ahead of a geodatabase compress/maintenance window. Run without `--test-service` against `--target portal` — this walks every federated server's catalog and scans every discovered feature service.

```bash
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal
```

**Scenario: large AGOL organization, need it done faster.**
Your AGOL org has ~20,000 hosted feature services, and the default 64 workers still take longer than you'd like. Raise `--workers` to push more concurrency (watch the log for 429 throttling as you do).

```bash
python INVENTORY_FS_REPLICAS.py --config org2.config.toml --target agol --workers 96
```

**Scenario: routing output somewhere other than the configured default.**
You want this run's CSV and log written to a shared drive instead of the location baked into the org config, without changing that config for every other script that reads it. Override just `--output-dir` on the command line.

```bash
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --output-dir "D:\reports"
```

**Scenario: your Portal certificate is signed by an internal/organizational CA.**
Your Enterprise Portal's certificate is not from a public Certificate Authority, so `standard-ca` verification would fail even though the certificate is legitimately trusted at the OS level. Switch to `os-trust-store` so verification checks against the same trust store your operating system already uses.

```bash
python INVENTORY_FS_REPLICAS.py --config org1.config.toml --target portal --tls-mode os-trust-store
```

### SDE Usage

There is **no `--target` argument** — every run is against the `[portal]` section of the chosen `--config` org toml, unconditionally. The Data Store host is auto-detected on every run (HA-aware) unless `--db-host` is given.

```bash
# TEST run (first 50 replicas), everything from the org config [portal]:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml

# FULL inventory (all replicas):
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org2.config.toml --no-test

# FULL run, override the output directory for this run only:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test --output-dir "D:\reports"

# Force a specific Data Store host, bypassing live primary auto-detection:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test --db-host OVERRIDE.enterprise.com

# Custom domain suffix for FQDN construction:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --domain-suffix enterprise.com

# Use the OS certificate store for the discovery TLS check:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --tls-mode os-trust-store

# Show all options:
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --help
```

**Scenario: quick confidence check after a Data Store change.**
Your organization just went through a Data Store failover or a host migration, and you want to confirm connectivity and auth still work before running a full inventory. The default test mode scans only the first 50 replicas, so a broken connection, bad password, or discovery ambiguity surfaces in seconds — and because discovery always runs in full, this also confirms the live primary is being detected correctly.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml
```

**Scenario: full SDE inventory to pair with a REST report.**
You've just run `INVENTORY_FS_REPLICAS.py --target portal` and want a same-day SDE-side inventory to reconcile against it with `REPLICA_DELTA.ps1`. Run with `--no-test` immediately afterward so the two reports describe close to the same point in time.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test
```

**Scenario: troubleshooting a suspected bad failover, or a one-off connection to a specific machine.**
An administrator needs to force the scan against a specific Data Store machine — for example, testing connectivity to a standby, or working around a discovery failure while it's being investigated — without relying on auto-detection for this one run. `--db-host` completely bypasses live primary discovery.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test --db-host datastore-primary.example.com
```

**Scenario: custom domain suffix for FQDN construction.**
The hosting server's admin API reports Data Store machines by their short (non-FQDN) name, and the organization's domain differs from the built-in default (`enterprise.com`). Supplying `--domain-suffix` ensures the auto-detected short name resolves to a connectable host.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --domain-suffix example.com
```

**Scenario: routing output somewhere other than the configured default.**
Like the FS tool, you can override just the output directory for a single run without touching the shared org config.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test --output-dir "D:\reports"
```

**Scenario: replica count looks wrong (unexpectedly low or zero), and you suspect the wrong DB account.**
A run completed successfully with no errors, but the replica count is far lower than you know it should be, or is exactly zero. Check the completion email's "SDE connect as" line first — if it names the admin/monitor account (e.g. `hosting_datastore_db_admin`) instead of the managed schema owner (e.g. `hosting_datastore_schema_owner`), the org config is missing `db-user-mu`/`keyring-service-db-mu`. Add them to config, or override on the command line for a one-off run to confirm the fix before editing the shared config file.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --no-test --db-user-mu hosting_datastore_schema_owner --keyring-service-db-mu DATASTORE_MU
```

**Scenario: an internally-signed Portal certificate is blocking the discovery step.**
The Portal used for Data Store discovery has a certificate issued by an internal organizational CA. Switch the discovery TLS check to the OS trust store so it verifies correctly instead of failing.

```bash
python DATA_STORE_INVENTORY_SDE_REPLICAS.py --config org1.config.toml --tls-mode os-trust-store
```

### Correlate Usage

`--config` and `--target` are always required; `--target` accepts `portal` or `agol`. The tool discovers the organization's SQLite Geodatabase items itself, so no input file is supplied.

```bash
# Portal, everything else from the org config:
python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal

# AGOL:
python CORRELATE_SQLITE_REPLICAS.py --config org2.config.toml --target agol

# More workers for a faster run:
python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal --workers 96

# Use the OS certificate store (internal CA):
python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal --tls-mode os-trust-store

# Show help:
python CORRELATE_SQLITE_REPLICAS.py --help
```

**Scenario: finding dead offline-download items before a content cleanup.**
Your content has accumulated hundreds of "SQLite Geodatabase" items from months of Field Maps downloads, and you suspect many are leftovers whose replicas are long gone. Run the tool against your organization — it discovers those items itself and writes an ORPHANS CSV — each row tells you the dead item, its parent service, and (in `detail`) why it was flagged.

```bash
python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal
```

**Scenario: spot-checking a small organization.**
You want to confirm the tool's verdicts against what you can see in a browser. Run it against a small or non-production organization — it discovers the handful of SQLite items present and returns the answer (and the reasoning, in `detail`) in seconds.

```bash
python CORRELATE_SQLITE_REPLICAS.py --config org1.config.toml --target portal
```

### Delta Usage

All three paths are parameters; if omitted they are prompted for at runtime.

```powershell
# Provide all three paths (run from the script's own directory — note the .\):
powershell .\REPLICA_DELTA.ps1 -RestReport "C:\Temp\REPLICA\ORG1_PORTAL_ALL_FS_REPLICAS_YYYYMMDD_HHMM.csv" -SdeReport "C:\Temp\REPLICA\ORG1_PORTAL_DATA_STORE_SDE_REPLICAS_YYYYMMDD_HHMM.csv" -OutputDir "C:\Temp\REPLICA"

# No arguments — PowerShell prompts for each required path:
powershell .\REPLICA_DELTA.ps1

# If an execution-policy error occurs, use the -File form:
powershell -ExecutionPolicy Bypass -File .\REPLICA_DELTA.ps1 -RestReport "C:\Temp\REPLICA\ORG1_PORTAL_ALL_FS_REPLICAS_YYYYMMDD_HHMM.csv" -SdeReport "C:\Temp\REPLICA\ORG1_PORTAL_DATA_STORE_SDE_REPLICAS_YYYYMMDD_HHMM.csv" -OutputDir "C:\Temp\REPLICA"

# Full comment-based help:
Get-Help .\REPLICA_DELTA.ps1 -Full
```

**Scenario: routine post-scan reconciliation.**
You just finished running `INVENTORY_FS_REPLICAS.py --target portal` and `DATA_STORE_INVENTORY_SDE_REPLICAS.py --no-test` back-to-back and saved both CSVs to the same folder. Point the script at both files and the output directory to get the reconciliation CSV.

```powershell
powershell .\REPLICA_DELTA.ps1 -RestReport "C:\Temp\REPLICA\ORG1_PORTAL_ALL_FS_REPLICAS_202608301300.csv" -SdeReport "C:\Temp\REPLICA\ORG1_PORTAL_DATA_STORE_SDE_REPLICAS_202608301305.csv" -OutputDir "C:\Temp\REPLICA"
```

**Scenario: interactive use without remembering exact paths.**
You're running this ad hoc and don't want to type out full file paths from memory. Run with no arguments and let PowerShell prompt for each one.

```powershell
powershell .\REPLICA_DELTA.ps1
```

**Scenario: execution-policy blocks a direct run.**
Your machine's PowerShell execution policy prevents the script from running normally. Use the `-File` form with `-ExecutionPolicy Bypass` for this one invocation only.

```powershell
powershell -ExecutionPolicy Bypass -File .\REPLICA_DELTA.ps1 -RestReport "C:\Temp\REPLICA\ORG1_PORTAL_ALL_FS_REPLICAS_202608301300.csv" -SdeReport "C:\Temp\REPLICA\ORG1_PORTAL_DATA_STORE_SDE_REPLICAS_202608301305.csv" -OutputDir "C:\Temp\REPLICA"
```

**Scenario: only the output directory is known ahead of time.**
You always write delta reports to the same shared folder but the specific report filenames change every run. Supply just `-OutputDir` and let PowerShell prompt for the two report paths.

```powershell
powershell .\REPLICA_DELTA.ps1 -OutputDir "C:\Temp\REPLICA"
```

---

## Command-line options

### FS Command-line options

| Argument | Required | Description |
|---|---|---|
| `--config` | Yes | Path to the org-specific config TOML file (e.g. `org1.config.toml`). Selects which organization's connection/email/output settings and `org-name` are used. |
| `--target` | Yes | `portal` or `agol`; selects the config.toml section and which discovery path runs. |
| `--portal-url` | No | Overrides the config value. |
| `--username` | No | Overrides the config value. |
| `--keyring-service` | No | Overrides the config value. |
| `--output-dir` | No | Overrides the config value. |
| `--smtp-server` | No | Overrides the config value. |
| `--email-to` | No | Single address or comma-separated list; overrides config. |
| `--email-from` | No | Overrides the config value. |
| `--workers` | No | Concurrent worker threads (default **64**, same on both targets). |
| `--test-service` | No | TEST MODE. Full FeatureServer REST URL of a single hosted service (Portal or AGOL); only that service's replicas are scanned. Skips server enumeration/catalog walking (Portal) or bucketed search (AGOL). CSV is written and the report is emailed just like a full run. |
| `--tls-mode` | No | TLS verification strategy: `standard-ca` (default), `os-trust-store`, or `custom-ca-bundle` (requires `--ca-bundle-path`). |
| `--ca-bundle-path` | No | Path to a PEM CA bundle. Required when `--tls-mode` is `custom-ca-bundle`. |

There is no `--max-email-mb` flag — like every other `PORTAL_*` script, it is treated as a mail-server characteristic (not a per-run choice) and is resolved solely from the org's config TOML.

### SDE Command-line options

| Argument | Required | Description |
|---|---|---|
| `--config` | Yes | Path to the org-specific config TOML file (e.g. `org1.config.toml`). |
| `--hosting-url` | No | Hosting GIS Server admin URL, used only to discover the live Data Store topology. If omitted, taken from the `[portal]`/`[common]` section of config. |
| `--domain-suffix` | No | Domain suffix appended to a discovered short Data Store machine name to build a connectable FQDN. Default: `enterprise.com`. Ignored if the discovered name is already a full FQDN, or if `--db-host` is given. |
| `--tls-mode` | No | TLS verification strategy for the Portal/hosting-server discovery calls: `standard-ca` (default), `os-trust-store`, or `custom-ca-bundle` (requires `--ca-bundle-path`). |
| `--ca-bundle-path` | No | Path to a PEM CA bundle. Required when `--tls-mode` is `custom-ca-bundle`. |
| `--db-host` | No | Data Store host override. Supplying it also bypasses live primary auto-detection entirely — the value is used as-is for the connection instead of calling `discover_data_store()`. Normally omitted so auto-detection runs. |
| `--dbname` | No | Managed geodatabase name; overrides config. |
| `--db-port` | No | Data Store DB port; overrides config. |
| `--db-user` | No | Admin/monitor DB user (also the keyring username for `--keyring-service-db`); overrides config. Resolved but then overridden for the actual SDE connection by `--db-user-mu` whenever the latter is set. |
| `--keyring-service-db` | No | Keyring service name for the admin/monitor DB password; overrides config. |
| `--db-user-mu` | No | Managed-schema-owner DB user to actually connect as for the SDE/`ListReplicas` connection (e.g. the user that owns `SDE.GDB_REPLICAS`). If omitted, taken from the `[portal]`/`[common]` section of config (`db-user-mu`). Falls back to the admin/monitor `--db-user` account (with a warning) if unset anywhere. |
| `--keyring-service-db-mu` | No | Keyring service holding the managed-schema-owner's password (username = `--db-user-mu`). If omitted, taken from config (`keyring-service-db-mu`). |
| `--output-dir` | No | Overrides the config value. |
| `--smtp-server` | No | Overrides the config value. |
| `--email-to` | No | Single address or comma-separated list; overrides config. |
| `--email-from` | No | Overrides the config value. |
| `--portal-url` | No | Portal URL used for the discovery token request. If omitted, taken from config (required overall). |
| `--username` | No | Portal username used for the discovery token request. If omitted, taken from config (required overall). |
| `--keyring-service` | No | Keyring service holding the Portal account's password (username = `--username`). If omitted, taken from config (required overall). |
| `--test` / `--no-test` | No | Test mode is the default (first 50 replicas). Use `--no-test` for a full inventory. |

There is **no `--target` option** for this script.

### Correlate Command-line options

| Argument | Required | Description |
|---|---|---|
| `--config` | Yes | Path to the org-specific config TOML file. Selects which organization's connection/email/output settings and `org-name` are used. |
| `--target` | Yes | `portal` or `agol`; selects the config section and connection path. Also scopes the SQLite Geodatabase item discovery to that organization. |
| `--portal-url` | No | Overrides the config value. |
| `--username` | No | Overrides the config value. |
| `--keyring-service` | No | Overrides the config value. |
| `--output-dir` | No | Overrides the config value. |
| `--smtp-server` | No | Overrides the config value. |
| `--email-to` | No | Single address or comma-separated list; overrides config. |
| `--email-from` | No | Overrides the config value. |
| `--workers` | No | Concurrent worker threads (default **32**). |
| `--trusted-service-origin` | No | Repeatable exact HTTPS origin allowed to receive the Portal token for FeatureServer `/replicas` calls. Normally unnecessary because Portal federation metadata and AGOL `*.arcgis.com` domains are trusted automatically. Must be an origin only, with no path, query, or fragment. |
| `--tls-mode` | No | TLS verification strategy: `standard-ca` (default), `os-trust-store`, or `custom-ca-bundle` (requires `--ca-bundle-path`). |
| `--ca-bundle-path` | No | Path to a PEM CA bundle. Required when `--tls-mode` is `custom-ca-bundle`. |

Like the other Python tools, there is no `--max-email-mb` flag — it is resolved from the org config TOML. There is **no `--input-csv` flag** — the SQLite Geodatabase items are discovered by scanning the organization.

### Delta Command-line options

| Argument | Required | Description |
|---|---|---|
| `-RestReport` | Yes (prompted if omitted) | Full path to the REST report CSV from `INVENTORY_FS_REPLICAS.py --target portal` (must contain a `replica_id` column). |
| `-SdeReport` | Yes (prompted if omitted) | Full path to the SDE report CSV from `DATA_STORE_INVENTORY_SDE_REPLICAS.py` (must contain a `replica_id` column). |
| `-OutputDir` | Yes (prompted if omitted) | Existing directory where the timestamped output CSV is written. |

---

## Performance

### FS Performance

This scan can take several hours on a Portal deployment with thousands of services and tens of thousands of replicas, but only minutes on an AGOL organization with many more services but a flatter search path — see [Observed Runtime](#observed-runtime) for real numbers.

- A single pooled, retrying, TLS-verified `requests.Session` is reused for every HTTP call, with the connection pool sized to the worker count.
- **Portal:** server catalogs are walked **concurrently** (all servers at once, not sequentially), and folders within each server are also fetched in parallel.
- **AGOL:** service discovery uses the shared **bucketed search**, which partitions the catalog by creation date so it can enumerate well past the 10,000-result search cap that a single flat AGOL search would otherwise hit.
- Service-owner lookups are **batched** rather than one-per-item, on both targets.
- Pass 1 (replica lists) runs fully in parallel; a per-replica detail call in Pass 2 is issued **only** when the list response is missing owner/created/last-sync, and — thanks to `DETAIL_ID_KEYS` being pinned to a single candidate key — that's at most **one** detail call per replica needing it.
- Pass 2 schedules the services with the most detail work **first**, so long tails of slow services overlap with everything else instead of straggling at the end.

Throughput is tuned with `--workers` (default **64 on both targets**). This default is deliberately **not** raised automatically for AGOL even though an AGOL org can be much larger than a typical Portal deployment — raise `--workers` yourself based on what your Portal servers or AGOL org can actually sustain before you start seeing 429 throttling responses in the log. If more than ~80% of all replicas end up needing a Pass 2 detail call, the log emits an explicit warning explaining that this is expected on a "skinny" list endpoint, but suggests checking whether your deployment's/org's list endpoint actually does return owner/date fields under different key names that could be added to `OWNER_KEYS`/`CREATED_KEYS`/`LASTSYNC_KEYS` to skip Pass 2 for those replicas.

### SDE Performance

Performance here is dominated by three things: the one-time cost of live Data Store topology discovery (a single lightweight admin REST call), the cost of creating and validating the temporary SDE connection (`Describe`, logged and timed explicitly), and the single bulk SQL query against `GDB_REPLICAS` that resolves GUID/date detail for every replica in one round-trip rather than one query per replica. Because there is no per-replica REST call involved, this tool is materially faster than a full `INVENTORY_FS_REPLICAS.py --target portal` run at a comparable replica count — it is reading directly from the database rather than walking a server/service topology over HTTP.

### Correlate Performance

The tool is driven by how many SQLite Geodatabase items the organization contains, not by the overall size of the deployment, so its cost scales with how many such items the discovery step finds and — more importantly — how many **distinct parent services** those items reference.

- **Discovering the SQLite items** (Step 1) uses strict date-range bucketed search to enumerate the whole organization and then filters to the `SQLite Geodatabase` type, holding candidates in a temporary disk-backed store. This phase scales with the organization's total item count, but it is pure search paging (no per-item detail calls) and completes quickly even on large organizations.
- **Reading item records** (Step 2) is fast and fully parallel: hundreds of items resolve in a second or two.
- **Listing replicas on the referenced services** (Step 3) is the dominant phase. It contacts only the distinct services the discovered items point at (e.g. ~250 services for ~675 items), and for each live replica it does an extra detail lookup to retrieve the full GUID. This is what takes the run from seconds to a few minutes.
- The correlation itself (Step 4) is instant — it's an in-memory lookup.

Throughput is tuned with `--workers` (default **32**). Because the work is concentrated on a smaller set of services than a full deployment scan, a very high worker count yields diminishing returns and can provoke throttling on the busier services; raise it gradually and watch the log.

Crucially, this tool does **not** walk the entire deployment's service topology the way a full `INVENTORY_FS_REPLICAS.py --target portal` run does — beyond the one-time item-discovery search, it only ever contacts the services its discovered items actually came from. See [Observed Runtime](#observed-runtime).

### Delta Performance

This is a lightweight, in-memory CSV comparison — there are no network calls. Runtime scales with the combined row count of the two input CSVs and is dominated by `Import-Csv` parsing.

---

## Observed Runtime

A **full** FS scan (no `--test-service`) is roughly 6-9 hours against an Enterprise deployment with ~6,000 services and ~15,000 replicas, and roughly 20 minutes against an AGOL org with ~20,000 services and ~350,000 replicas. `--test-service` mode completes in **seconds** because it scans exactly one feature service. The SDE and Delta tools are both much faster since neither walks a REST catalog: SDE reads directly from the database, and Delta only compares two already-generated CSVs in memory. `CORRELATE_SQLITE_REPLICAS.py` is bounded by how many SQLite items the organization contains rather than the deployment's service topology, so it finishes in minutes.

| Tool | Run type | Observed duration | Number of Feature Services |
|------|----------|-------------------|------------------------------|
| FS   | Portal | ~6-9 hours | ~6,000 |
| FS   | AGOL  | ~20 minutes | ~20,000 |
| SDE  | Portal| ~5 minutes | ~6,000 |
| Correlate | Portal, ~675 discovered items referencing ~250 services | ~5 minutes | ~250 (only the referenced services) |
| Delta | Typical Portal reconciliation | a few seconds | ~15,000-20,000 replica IDs (combined) |
| Delta | Large Portal reconciliation | under a minute | ~30,000+ replica IDs (combined) |

> `CORRELATE_SQLITE_REPLICAS.py` is fast because, beyond the one-time organization item search, it only contacts the services its discovered items reference — not the whole deployment. Nearly all of its runtime is the per-replica GUID-detail lookups on just those services.

---

## Output file

### FS Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp, prefixed with the org name and platform label:

```
[ORG]_[PLATFORM]_ALL_FS_REPLICAS_<ts>.csv        # full run, either target
[ORG]_TEST_FS_REPLICAS_<ts>.csv                  # --test-service run (no platform in the name)
```

The run log is written to the same directory (`[org]_[platform_lowercase]_all_fs_replicas_<ts>.log`, or `[org]_test_fs_replicas_<ts>.log` in test mode) and emailed alongside the CSV.

### SDE Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp, always platform-prefixed `PORTAL`:

```
[ORG]_PORTAL_DATA_STORE_SDE_REPLICAS_<ts>.csv   # one row per replica
[ORG]_PORTAL_DATA_STORE_SDE_REPLICAS_<ts>.log   # always emailed
```

The platform prefix is always `PORTAL` — it is a fixed constant, not derived from any URL or configuration lookup. This script uses the shared `write_dict_csv()` helper, which — unlike `INVENTORY_FS_REPLICAS.py`'s own `write_csv()` — silently writes nothing if there are zero replica rows to write; check the log for a "Total replicas found: 0" message if the expected CSV doesn't appear.

### Correlate Output file

Written to the resolved output directory with a `YYYYMMDD_HHMM` timestamp, prefixed with the org name and platform label:

```
[ORG]_[PLATFORM]_SQLITE_REPLICA_CORRELATION_<ts>.csv          # full report: one row per discovered item
[ORG]_[PLATFORM]_SQLITE_REPLICA_CORRELATION_ORPHANS_<ts>.csv  # only the confirmed ORPHANED rows (written only if any exist)
```

The run log (`[org]_[platform_lowercase]_sqlite_replica_correlation_<ts>.log`) is written to the same directory and emailed alongside both CSVs. The orphans-only CSV deliberately contains **only** `ORPHANED` rows — `UNKNOWN_SERVICE` rows are excluded because they are not confirmed orphans. If there are no confirmed orphans, only the full CSV is written and a note is logged.

### Delta Output file

Written to `-OutputDir` with a timestamp prefix:

```
YYYYMMDDHHMM_REPLICA_DELTA.csv
```

---

## Exit codes

### FS Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — the run completed with no scan error and the CSV was written. |
| `1` | Failure — could not connect, TLS verification failed, the scan raised an error (e.g. AGOL org id could not be resolved, or Portal returned no federated servers), or the CSV could not be written. |

### SDE Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — the run completed and the CSV was written. |
| `1` | Failure — Data Store topology discovery failed or was ambiguous, TLS verification failed, or an exception occurred during connection, replica enumeration, detail lookup, or CSV writing. |
| `2` | Aborted before OTHER_ORGng any work — one or more required Data Store settings had no value on the command line or in config. |

### Correlate Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — the run completed with no fatal error and the full CSV was written. |
| `1` | Failure — could not connect, TLS verification failed, the organization id could not be resolved for the SQLite-item discovery, the correlation raised an error, or the CSV could not be written. |
| `2` | Invalid invocation — `--tls-mode custom-ca-bundle` was supplied without `--ca-bundle-path`. |

### Delta Exit codes

This script does not define explicit numeric exit codes. Validation failures (missing files, missing output directory, missing `replica_id` column) raise a terminating error via `throw`, which PowerShell surfaces as a non-zero exit when run non-interactively (e.g. via `-File`). A successful run prints the BOTH / REST_ONLY / SDE_ONLY summary and writes the output CSV.

---

## Notes and caveats

### FS Notes and caveats

- **Org-admin required on Portal.** If the federation endpoint returns no servers, confirm the signed-in account has org-admin rights. This requirement does not apply on AGOL, which uses ordinary item search instead of the federation endpoint.
- **AGOL org id resolution is mandatory.** If the organization id cannot be resolved on an AGOL run, the scan **aborts** rather than fall back to an unscoped search, which on AGOL would otherwise crawl the entire public ArcGIS Online catalog.
- **`service_owner` may be blank.** Services without a backing item (no `serviceItemId`) have no owner to resolve; those rows leave the column empty by design, on either target.
- **Date columns are UTC and day-precision only.** `date_created` and `last_sync_date` (and the derived `status`/`synced` columns) have no time component.
- **Read the phase timing summary.** The end-of-log `PHASE TIMING SUMMARY` block identifies the real bottleneck (usually service discovery vs. Pass 1 listing vs. Pass 2 detail) before you start tuning workers, on either target.
- **If most replicas need a detail call**, the log warns you at the ≥80% threshold: this usually just means the `/replicas` list response is intrinsically skinny (only `replicaID`/`replicaName`) and a detail call is unavoidable — but it's worth checking `OWNER_KEYS`/`CREATED_KEYS`/`LASTSYNC_KEYS` against your own deployment's/org's list JSON in case fields are simply under different key names there.
- **`--workers` default is not auto-tuned per target.** Even though an AGOL org can realistically be much larger than the Portal deployment this tool was originally built against, the default worker count is the same 64 on both targets — raise it explicitly with `--workers` for a large AGOL run, and watch the log for 429 throttling as you do.
- **Email attachment cap is configurable, project-wide.** This script resolves `max-email-mb` from the org config TOML and passes it into `gather_attachments()`, which decides whether to attach the CSV as-is, zip it first, or leave it on disk. If the CSV exceeds that cap it is first zipped and re-checked; only if the zip is still too large is it left on disk (with a note in the email body). If `max-email-mb` is unset in the org config, the built-in default of 20 MB applies. The log file is always attached regardless of size.
- **TLS verification is always on.** There is no flag to disable certificate checking. A failed handshake/verification against the target Portal or AGOL URL aborts the run immediately, before any scanning begins.
- **Throughput vs. throttling.** `--workers` raises parallelism, but too many concurrent calls can trigger server-side throttling (429/503) and net-slower runs due to retry backoff. Tune to your servers/org.
- **Multi-org output naming.** Every output file and email subject is prefixed with the org's `org-name` in addition to the platform label, so runs against different organizations are never ambiguous.

### SDE Notes and caveats

- **ArcPy required.** This tool depends on ArcPy (`ListReplicas`, `ArcSDESQLExecute`, `CreateDatabaseConnection`, `Describe`) and must run in an ArcGIS Python environment.
- **No `--target` flag, by design.** ArcGIS Online has no managed SDE Data Store for this tool to ever connect to, so a target selector would offer a choice that doesn't actually exist.
- **The Data Store host is never assumed from a static config value.** Live primary detection runs on every invocation unless `--db-host` is explicitly given; a discovery failure or ambiguous result is a hard abort, never a silent fallback.
- **`--db-host` disables HA-awareness for that run.** It is intended for deliberate troubleshooting or a one-off override, not routine use — a stale override left on a scheduled task could scan a decommissioned or standby machine after a real failover.
- **A minimal Portal token step is required.** Unlike a purely database-facing tool, this script needs a valid Portal account/password (via keyring or `PORTAL_PASSWORD`) and network access to the Portal token endpoint and the hosting server's admin API, purely to support live Data Store discovery. `portal-url`, `username`, and `keyring-service` are validated as **required** settings, not optional overrides.
- **The SDE connection uses the managed-schema-owner account, not the admin/monitor account.** `db-user`/`keyring-service-db` are the admin/monitor account, correct for catalog-style queries elsewhere in this project but insufficient for `ListReplicas` to see replica rows. This script resolves a separate `db-user-mu`/`keyring-service-db-mu` pair and substitutes those credentials for the actual SDE connection whenever they are configured; if they are not configured anywhere, the script warns loudly and falls back to the admin/monitor account rather than silently connecting as the wrong user.
- **Temporary connection only.** No permanent `.sde` file is used or left behind; the temporary connection is always cleaned up in a `finally` block.
- **Detail columns can be blank.** If the replica system table can't be read under any of the tried column layouts, `guid` / `date_created` / `lastReceive` / `lastSend` come out blank (with a warning in the log); the rest of the row is still written.
- **Test mode is the default.** Remember `--no-test` for a complete inventory. Data Store discovery itself always runs in full, regardless of test mode.
- **TLS verification is always on for the discovery step.** A failed handshake/verification against `--portal-url` aborts the run before any token is requested.
- **Connection failures are logged in detail.** Host/port/db/user, ArcPy tool messages, and the exception are all captured to make auth/network/dbname problems easy to diagnose. The `Describe` validation step's elapsed time is also logged and emailed — a slow value here is often the first sign of a marginal DB connection, even before anything actually fails.
- **Attachment cap.** Like `INVENTORY_FS_REPLICAS.py`, this script uses the shared `gather_attachments()` helper — but does not pass a `cap_mb`, so it still falls back to the built-in default rather than resolving `max-email-mb` from config. An oversized CSV is left on disk with its location noted in the email body; the log is always attached.

### Correlate Notes and caveats

- **The item list is discovered, not supplied.** The tool enumerates the organization's SQLite Geodatabase items itself (organization-scoped strict date-range bucketed search) and uses that inventory as its work list; there is no `--input-csv`. If the organization id cannot be resolved, the run aborts rather than fall back to an unscoped search.
- **The title is not the replica ID.** The code on the end of a SQLite item's title is **not** its replica GUID; matching on it will find nothing. The tool matches on the real replica GUID read from inside each item's own record. (This is the single most important thing to understand about how this tool works — see [Correlate How it works](#correlate-how-it-works).)
- **`ORPHANED` is a strong candidate signal, not an automatic delete list.** It confirms the originating replica is gone, not that the downloaded data is worthless — an orphan may still hold unsynced field edits. Always investigate before removing anything. The tool is read-only and never deletes anything itself.
- **`UNKNOWN_SERVICE` is not an orphan.** A service that couldn't be read (offline, 404, permissions, timeout) yields `UNKNOWN_SERVICE`, never `ORPHANED`, and is kept out of the orphans-only CSV. Re-run or re-check the service before drawing any conclusion about its items. Check the `service_reachable` column to confirm any orphan verdict rests on a successful read.
- **Trust depends on the replica list being complete.** An orphan verdict assumes the service returned its full replica list. If a service silently returned a partial list, a real replica could be mislabeled orphaned; the `service_live_replica_count` column is the sanity check (an implausibly low count next to many same-service orphans warrants a closer look).
- **It's a point-in-time snapshot.** The item discovery and the service checks happen within one run; in a busy system an item discovered and a service checked a few minutes apart can momentarily disagree. Run against a reasonably quiet system for the cleanest results.
- **Account visibility matters.** An account that cannot see certain services will produce `UNKNOWN_SERVICE` (not false orphans) for their items — but it also means those items can't be adjudicated until re-run with sufficient access. Likewise, items the account cannot see are not discovered in the first place.
- **Items need a readable record with a replica GUID.** Items whose record can't be read, or that lack a `properties.replicaID`, come back as `NO_GUID`/`NO_SERVICE` and are left for manual review rather than guessed at.
- **Every row explains itself.** The `detail` column gives a plain-language reason for each verdict, so a reviewer doesn't need to infer why an item was flagged.
- **Multi-org output naming.** Like the other tools, every output file and email subject is prefixed with the org's `org-name` plus the platform label.

### Delta Notes and caveats

- **Portal-only reconciliation.** There is no AGOL equivalent of the managed SDE Data Store, so this script only makes sense against a `--target portal` REST report.
- **Run the two upstream reports close together in time.** A replica created or removed between the two runs will appear as a false orphan; run `INVENTORY_FS_REPLICAS.py --target portal` and `DATA_STORE_INVENTORY_SDE_REPLICAS.py` back-to-back.
- **Only the hosting server is reconciled.** REST replicas from federated servers other than the hosting server are silently excluded before comparison. Reconciling a federated server would require changing the `$HostingServerUrl` constant.
- **Comparison is case-insensitive and whitespace-trimmed**, so GUID-based `replica_id` values line up between the two reports regardless of case or stray whitespace.
- **`REST_ONLY` and `SDE_ONLY` are your orphans; `BOTH` is healthy.** See [Delta Column descriptions](#delta-column-descriptions) for how to trace an orphan back to a workflow before deciding to unregister it.