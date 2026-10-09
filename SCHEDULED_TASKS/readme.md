# Credentials, Keyring, and Scheduled Task Provisioning

This document covers two related things: (1) how this toolkit's Python scripts retrieve credentials via `keyring` and the Windows Credential Manager, and (2) the three PowerShell helper scripts used to provision, seed, and verify those credentials for the scheduled tasks that run under a gMSA (Group Managed Service Account).

The first section is a deep background on using Keyring and Windows Credential Manager if you want to DIY your own credential managment for using these scripts as scheduled tasks. PowerShell scripts (at the bottom) presents a find-and-replace push-button way to accomplish the same secure, no-exposed password method of accomlishing the same. 

This description assumes you are using a gMSA. Those with regular domain service accounts can use this process with some, albeit easier, modification of the ps scripts included simply by logging into the server using the service account and configuring the Credential Manager and creating the task as the service account. A description of that process is deliberately not included here: it is well documented elsewhere and use of gMSA is a more secure method. 

## Table of contents

- [Authentication](#authentication)
  - [Keyring and the Windows Credential Manager](#keyring-and-the-windows-credential-manager)
    - [How keyring is used in the toolkit](#how-keyring-is-used-in-the-toolkit)
    - [Adding passwords to the Windows Credential Manager](#adding-passwords-to-the-windows-credential-manager)
    - [Programmatically provisioning credentials that persist across reboots and work under a service account](#programmatically-provisioning-credentials-that-persist-across-reboots-and-work-under-a-service-account)
    - [Important considerations for scheduled / service accounts](#important-considerations-for-scheduled--service-accounts)
- [PowerShell scripts](#powershell-scripts)
  - [SEED_CRED_MANAGER.ps1](#seed_cred_managerps1)
  - [CHECK_CRED_MANAGER.ps1](#check_cred_managerps1)
  - [CREATE_SCH_TASK.ps1](#create_sch_taskps1)
  - [How the three scripts fit together](#how-the-three-scripts-fit-together)

# Authentication

## Keyring and the Windows Credential Manager

The toolkit never stores passwords in `config.toml`, in source code, or on the command line. Instead every credential is retrieved at run time from the operating system's secure credential store by way of the [`keyring`](https://pypi.org/project/keyring/) Python library. On Windows, `keyring` reads and writes the **Windows Credential Manager** (specifically the "Generic Credentials" area under Windows Vault). This keeps secrets out of plaintext files, out of process listings, and out of any log or email the tools produce.

### How keyring is used in the toolkit

`keyring` identifies each stored secret by a `(service, username)` pair. The scripts follow a consistent convention:

- The **service name** is the `keyring-service` value (e.g. `PORTAL`, `AGOL`, or `DATASTORE`).
- The **username** is the login the tool authenticates as (e.g. `PORTAL_ADMIN`, `AGOL_ADMIN`, `hosting_datastore_db_admin`).

### Adding passwords to the Windows Credential Manager

Because the tools look up secrets by an exact `(service, username)` pair, the entries you create must match the `keyring-service` and `username` values the tool will resolve for that `--target`. The most reliable way to create entries that `keyring` can read is to use `keyring` itself, so that the credential is written in the format the library expects.

#### Method 1 — Python one-liners (recommended)

Run these once, in the same ArcGIS Python environment the scripts run in, replacing the placeholder passwords:

```bat
:: Portal login (matches [portal]: keyring-service = PORTAL, username = PORTAL_ADMIN)
python -c "import keyring; keyring.set_password('PORTAL','PORTAL_ADMIN','<portal-password>')"

:: AGOL login (matches [agol]: keyring-service = AGOL, username = AGOL_ADMIN)
python -c "import keyring; keyring.set_password('AGOL','AGOL_ADMIN','<agol-password>')"

:: Data Store admin (matches keyring-service-db = DATASTORE, db-user = hosting_datastore_db_admin)
python -c "import keyring; keyring.set_password('DATASTORE','hosting_datastore_db_admin','<db-password>')"
```

Each of these creates a Generic Credential in the Windows Credential Manager that `keyring.get_password(service, user)` will later find.

#### Method 2 — The `keyring` command-line tool

If the `keyring` package is installed it also provides a CLI. It prompts for the password so it does not appear in your command history:

```bat
python -m keyring set PORTAL PORTAL_ADMIN
python -m keyring set AGOL AGOL_ADMIN
python -m keyring set DATASTORE hosting_datastore_db_admin
```

#### Verifying an entry

To confirm a credential is stored and readable (this prints the password, so run it only where that is acceptable):

```bat
python -c "import keyring; print(keyring.get_password('PORTAL','PORTAL_ADMIN'))"
```

Or with the CLI:

```bat
python -m keyring get PORTAL PORTAL_ADMIN
```

#### Deleting or rotating an entry

To rotate a password, simply `set` it again with the new value (it overwrites). To remove it:

```bat
python -c "import keyring; keyring.delete_password('PORTAL','PORTAL_ADMIN')"
```

### Programmatically provisioning credentials that persist across reboots and work under a service account

The one-liners above are fine for interactive first-time setup, but production deployments usually need credentials **provisioned programmatically** — as part of a build/deploy step or a setup script — in a way that (a) survives reboots and (b) is readable by the unattended **service account** that actually runs the scheduled tasks. This section covers how to do that reliably.

#### Persistence across reboots is about *scope*, not *time*

A common misconception is that Credential Manager entries expire or are lost on reboot. They are not. When `keyring` writes a Generic Credential on Windows it uses the `CRED_PERSIST_LOCAL_MACHINE` persistence scope, which means the credential is stored durably in the user's Windows Vault and **remains available across logoffs and reboots** until it is explicitly deleted or overwritten. There is no TTL to configure.

What actually determines whether a credential is available is **which Windows user profile it was written under**. Generic Credentials are stored **per profile**, so a secret written under your interactive profile is invisible to any other account. The reboot question is therefore really a *profile* question: the credential persists fine, but only for the profile that owns it.

#### The core rule for service accounts

**Write the credential under the exact Windows account that will run the script.** A password stored under your interactive login will not be found by a scheduled task running as `svc_gis` (or `LocalSystem`, or any other principal). There are three practical ways to satisfy this rule; pick whichever fits your environment.

##### Option A — Provision interactively as the service account

If the service account can be logged into (or you can launch a shell as it), simply run the same `keyring.set_password(...)` calls **in that account's session**. The simplest launcher is `runas`, which starts a process under the target account using its profile:

```bat
:: Opens a shell running as the service account; run the set_password lines inside it
runas /user:DOMAIN\svc_gis /profile "cmd"
```

Inside that shell, run the Method 1 one-liners. Because the writes now happen under `svc_gis`'s profile, the scheduled task running as `svc_gis` will find them after any reboot. The `/profile` switch is important: it forces the account's user profile to load, which is where the Vault lives.

##### Option B — A reusable provisioning script

For repeatable deploys, wrap the writes in a small, idempotent provisioning script that reads the plaintext secrets from a transient source (an environment variable injected by your deployment tool, a prompt, or a secrets manager) and writes them to keyring. Run this script **as the service account** (via `runas`, a "run once as this user" scheduled task, or your configuration-management tool's run-as facility).

```python
# provision_credentials.py — run once, AS THE SERVICE ACCOUNT.
# Reads secrets from environment variables so nothing is hard-coded or
# left in shell history, then writes them to the Windows Credential Manager
# under the (service, username) pairs the toolkit expects.
import os
import sys
import keyring

# (keyring_service, username, env_var_holding_the_secret)
ENTRIES = [
    ("PORTAL",    "PORTAL_ADMIN", "PROV_PORTAL_PW"),
    ("AGOL",      "AGOL_ADMIN",   "PROV_AGOL_PW"),
    ("DATASTORE", "hosting_datastore_db_admin",       "PROV_DB_PW"),
]

def main():
    missing = []
    for service, user, env_var in ENTRIES:
        secret = os.environ.get(env_var)
        if not secret:
            missing.append(env_var)
            continue
        keyring.set_password(service, user, secret)
        # Read it straight back to confirm the write landed in THIS profile.
        if keyring.get_password(service, user) != secret:
            print(f"ERROR: verification failed for {service}/{user}")
            sys.exit(1)
        print(f"OK: stored and verified {service}/{user}")

    if missing:
        print("WARNING: no secret provided for: " + ", ".join(missing))

if __name__ == "__main__":
    main()
```

Invoke it with the secrets supplied only for the lifetime of the call, for example:

```bat
:: Run as the service account; the variables exist only for this process tree
set "PROV_PORTAL_PW=<portal-password>"
set "PROV_AGOL_PW=<agol-password>"
set "PROV_DB_PW=<db-password>"
python provision_credentials.py
:: Clear them again immediately
set "PROV_PORTAL_PW="
set "PROV_AGOL_PW="
set "PROV_DB_PW="
```

Because the script reads it back immediately, it also doubles as a persistence check: a successful "stored and verified" line confirms the entry is present in the running account's Vault and will survive a reboot.

##### Option C — `LocalSystem` and other non-interactive principals

If a task runs as `LocalSystem`, `LocalService`, or `NetworkService`, you generally cannot log in interactively to seed the Vault, and the per-profile isolation still applies. The two supported approaches are:

- **Run the provisioning step as that principal.** Tools such as PsExec can launch a process as `LocalSystem` (`psexec -s -i python provision_credentials.py`), letting the write happen under the correct profile. Use this only where running as `LocalSystem` is an accepted practice.
- **Prefer a real, dedicated service account instead.** A domain or local service account with "Log on as a batch job" rights is easier to provision (Option A/B), easier to audit, and avoids the profile-loading pitfalls of the built-in machine principals. This is the recommended pattern for the toolkit.

#### Making sure the service account's profile actually loads

For a credential written under a service account to be readable at task time, that account's **profile must load when the task runs**. Two settings matter:

- In Task Scheduler, configure the task to **"Run whether user is logged on or not"** and, if your policy allows, **"Do not store password"** using a Group Managed Service Account (gMSA) or an account with the appropriate batch-logon right. The account still needs its profile to load so the Vault is mounted.
- If you see a task that succeeds interactively but fails unattended with a "no password found in keyring" error, the usual cause is that the credential lives in a different profile than the one the task runs under, or the runtime account's profile is not being loaded. Re-provision under the exact task account (Option A/B) to resolve it.

#### Verifying persistence and account visibility

After provisioning, confirm the entry is visible **from the service account's context**, not just yours. The most direct check is to run the verify one-liner as that account:

```bat
runas /user:DOMAIN\svc_gis /profile "cmd /k python -c \"import keyring; print(bool(keyring.get_password('PORTAL','PORTAL_ADMIN')))\""
```

A printed `True` confirms the credential exists in that account's Vault and — because `keyring` uses machine-local persistence — will remain there across reboots. To prove reboot-persistence explicitly, run the same check again after restarting the machine; the entry should still resolve without re-provisioning.

#### Security notes for programmatic provisioning

- **Never bake plaintext secrets into the provisioning script or into `config.toml`.** Pull them from environment variables, an interactive prompt, or an enterprise secrets manager, and clear them from the environment immediately after the write.
- **Keep provisioning out of source control history.** Command lines that include `set "...PW=..."` should not be committed; prefer prompts or injected variables.
- **Provision on the machine that will run the task.** Because the Vault is machine-local per profile, credentials do not roam between servers on their own. In an HA deployment, provision on **every node** that could run the scheduled task (see the Paths section for the related HA considerations).
- **Rotate by overwriting.** Re-running the provisioning step with a new secret overwrites the existing entry in place; no delete is required first.

### Important considerations for scheduled / service accounts

The Windows Credential Manager stores Generic Credentials **per Windows user profile**. This has two practical consequences for these tools, which are often run as scheduled tasks:

- The credential must be created **while logged in as (or running as) the same Windows account** that will execute the script. A password stored under your interactive profile will not be visible to a scheduled task running as a different service account.
- For a scheduled task, either run the one-liner interactively as that service account, or use a "Run only when user is logged on" configuration, or supply the fallback environment variable in the task's environment. When in doubt, create the entry under the exact account the scheduler uses.

Finally, note that the service and username you store **must** match what `--target` resolves to. If you point a run at a non-default section — for example overriding `--keyring-service` or `--username` on the command line, or editing `config.toml` — you must create a matching Credential Manager entry for that new pair, or the lookup will fail and the run will abort.

# PowerShell scripts

The three scripts below implement the "Option A / gMSA" pattern described above using Windows Task Scheduler instead of `runas`, which is the correct approach when the service account is a **gMSA** (Group Managed Service Account) — gMSAs have no user-settable password and generally cannot be used with `runas`, but they can be used as the principal of a scheduled task.

## SEED_CRED_MANAGER.ps1

**Purpose:** Write (seed) a credential into the Windows Credential Manager **under the gMSA's own profile**, so that later scheduled tasks running as that same gMSA can find it via `keyring`.

**How it works:**

1. Defines a temporary Task Scheduler folder/task (`\PTERODACTYL\Seed-Password`).
2. Builds a scheduled task action that runs `cmdkey /generic:PORTAL /user:test_user /pass:$PortalPw`, which creates a Generic Credential in Windows Credential Manager.
3. Sets the task's principal to the gMSA account (`ORG\IORG_gSMA_ACCOUNT`) with `LogonType Password` — this is what causes the write to land in the gMSA's own credential vault/profile rather than the interactive user's.
4. Registers the task, starts it immediately, waits 10 seconds for it to complete, then **unregisters (deletes) the task** so it doesn't persist.
5. A comment notes that the plaintext password is not retained anywhere afterward, unless PowerShell script-block logging is enabled as a local policy — in which case that log should be deleted.

**Key variables to edit before running:**
- `$PortalPw` — the actual password to store.
- The `cmdkey /generic:...` target/user values should match whatever `keyring-service` / `username` pair `config.toml` expects for that entry (the script's default of `PORTAL` / `test_user` is a placeholder and should be changed to match, e.g., `PORTAL` / `PORTAL_ADMIN`).

**Why this pattern instead of `runas`:** Because gMSAs don't have an interactively-usable password, you can't `runas /user:gmsa$`. Running the credential-writing command as a **scheduled task whose principal is the gMSA** is the supported way to make Windows execute a command "as" that account, which achieves the same profile-scoped write that Option A describes for regular service accounts.

## CHECK_CRED_MANAGER.ps1

**Purpose:** Verify, after the fact, that the expected credential(s) actually exist in the gMSA's Credential Manager vault — **without ever exposing the password**.

**How it works:**

1. Registers a scheduled task (`\pterodactyl\List-GMSA-Creds`) whose action runs `cmdkey /list`, redirecting the output to `c:\temp\gmsa_creds.txt`.
2. The task's principal is the same gMSA (`ORG\IORG_gSMA_ACCOUNT`), so `cmdkey /list` reports the credentials visible from **that account's** vault — the exact scope that matters for troubleshooting "credential not found" errors.
3. Unlike the seed script, this task is registered as a **persistent** task (it is not unregistered at the end), so it can be re-run on demand any time you want to re-check the vault contents.
4. `cmdkey /list` only shows the target names and usernames stored (e.g. `PORTAL` / `PORTAL_ADMIN`) — it does not print passwords — so this is safe to run and review repeatedly.

**Usage note:** After running (or re-running) this task via `Start-ScheduledTask`, check `c:\temp\gmsa_creds.txt` for an entry matching the `(service, username)` pair your `config.toml` expects. If it's missing, re-run `SEED_CRED_MANAGER.ps1` (or the equivalent `keyring.set_password` call executed as the gMSA) for that pair.

## CREATE_SCH_TASK.ps1

**Purpose:** Register the actual **production scheduled task** that runs one of the toolkit's Python reporting scripts on a recurring schedule, under the gMSA.

**How it works:**

1. Creates a Task Scheduler folder (`\PTERODACTYL\`) and names the task `ORG_PORTAL_ScanAttachmentsSize`.
2. Points the task action at the ArcGIS Python interpreter (`D:\User\TomPy\python.exe`), with a working directory of `D:\User\PTERODACTYL_ORG`.
3. Builds the argument list to invoke `ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py` with:
   - `--config` pointing at `UTILITIES\ORG.config.toml`
   - `--target portal`
   - `--threshold-mb 6`
   - `--no-test` (so it runs the full scan, not the capped test mode)
4. Sets a **daily trigger at 06:00**.
5. Sets the principal to the same gMSA (`ORG\IORG_gSMA_ACCOUNT`) with `LogonType Password` and `RunLevel Highest` — this is the account whose Credential Manager vault must already contain the `PORTAL` / `PORTAL_ADMIN` entry (seeded via `SEED_CRED_MANAGER.ps1`) for the Python script's `connect_gis()` call to succeed.
6. Applies settings so the task starts when available, tolerates battery power, and doesn't require network availability to start (the Python script itself will fail cleanly if it can't reach Portal).

**Dependency on the other two scripts:** This task will only succeed if the gMSA it runs as (`ORG\IORG_gSMA_ACCOUNT`) already has the matching credential seeded (`SEED_CRED_MANAGER.ps1`) — which can be confirmed with `CHECK_CRED_MANAGER.ps1` — since the Python script authenticates via `keyring.get_password('PORTAL', 'PORTAL_ADMIN')` exactly as described in [How keyring is used in the toolkit](#how-keyring-is-used-in-the-toolkit).

## How the three scripts fit together

These scripts implement, end-to-end, the gMSA variant of the "provision → verify → run" workflow described earlier in this document:

1. **`SEED_CRED_MANAGER.ps1`** — one-time (or per-rotation) step. Writes the credential into the gMSA's own Credential Manager vault by running `cmdkey` as a transient scheduled task whose principal is the gMSA, then deletes that transient task.
2. **`CHECK_CRED_MANAGER.ps1`** — verification step, usable any time. Confirms (without exposing the password) that the expected `(service, username)` entries exist in that same gMSA's vault, by running `cmdkey /list` as a persistent, on-demand scheduled task.
3. **`CREATE_SCH_TASK.ps1`** — the production task. Registers the recurring scheduled task that actually runs a toolkit Python script (here, the attachment-size scan) as the gMSA — the same account whose vault was seeded and checked in steps 1 and 2.

In short: seed the credential under the gMSA → confirm it's really there → register the recurring job that depends on it.