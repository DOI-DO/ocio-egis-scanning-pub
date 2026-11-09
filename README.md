# Portal Transaction Evaluation and Registry Oversight for Data Anomaly Checking, Testing, and Yield Logging

## Table of Contents

- [Design conventions used throughout this project](#design-conventions-used-throughout-this-project)
- [Complementary Esri tooling](#complementary-esri-tooling)
- [Getting Started: Authentication and Configuration](#getting-started-authentication-and-configuration)
- [Example Commands](#example-commands)
- [Detailed descriptions](#detailed-descriptions)
- [ATTACHMENTS](#attachments)
- [CREDITS](#credits)
- [DEPCRECATED_CONTENT](#depcrecated_content)
- [DUPLICATE_DATA](#duplicate_data)
- [EMPTY_FEATURE_SERVICES_INVENTORY](#empty_feature_services_inventory)
- [FEATURE_SERVICES](#feature_services)
- [FIELD_NAMES](#field_names)
- [FEATURE_CLASS_NAMES](#feature_class_names)
- [GROUPS](#groups)
- [HOSTING_SVR_DATASTORE](#hosting_svr_datastore)
- [ITEMS](#items)
- [LICENSE](#license)
- [LOG_DUMP](#log_dump)
- [PORTAL_CLEAN_UP](#portal_clean_up)
- [PORTAL_ORPHAN_FEATURE_SERVICES](#portal_orphan_feature_services)
- [PUBLIC_EDIT_CONTROL](#public_edit_control)
- [RELATED_ITEMS](#related_items)
- [REPLICAS](#replicas)
- [ROLES](#roles)
- [SCHEDULED_TASKS](#scheduled_tasks)
- [SECURITY](#security)
- [USERS](#users)
- [UTILITIES](#utilities)
- [WEB_MAPS](#web_maps)
- [DEPENDENCIES](#dependencies)

---

## ⚠️ Every script in this project only *looks* — nothing is ever changed ⚠️

This is a **read-only inventory and reporting toolkit** for ArcGIS Online (AGOL) and ArcGIS Enterprise Portal. A privileged account is required so the scripts can *see* everything (users, groups, content, servers, and the underlying database), but no script in this project ever creates, edits, or deletes anything on the systems it scans, and no settings are ever changed. Everything each script finds gets written to a CSV report and emailed out, so an administrator can review it and decide what — if anything — to do about it. 

Think of this project as a smoke detector, not a fire extinguisher: it tells you where to look, it doesn't put anything out itself.

A collection of scanning and inventory scripts, all built around one shared toolbox of helper code called `UTILITIES`. Every script in this project pulls its connection, login, logging, email, and settings-handling logic from that one shared toolbox instead of re-inventing it in every script — so security behavior, retry logic, and email formatting stay consistent everywhere. Because settings live in a separate configuration file per organization, the same scripts can be pointed at a different agency's ArcGIS environment just by swapping which configuration file is used — no code changes required.

Scripts are grouped into folders by what they do (scanning, cleanup, inventory, licensing, and so on). This top-level readme summarizes **every file's purpose** in one place, grouped by folder.

## ⚠️ Python Environment ⚠️
This project was designed to work with the default Python environment installed with ArcGIS Pro, and intended to run on a server configured to perform automated scanning as scheduled tasks. Cloned ArcGIS Pro enviroments have been tested albeit heavily customized Python environments will likely result in many scans not functioning due to incompatible dependencies that are outside the scope of this project. 

## Design conventions used throughout this project

A handful of patterns repeat across almost every script in this project. Rather than explain them over and over in each folder's readme, they're explained once here.

### One settings file per organization

Every script requires a `--config` option pointing at a settings file (a `.toml` file, e.g. `ORG.config.toml`) for the organization being scanned, and most also require a `--target` option of either `portal` or `agol` to say which environment to connect to. Settings such as the web address, login name, output folder, and email addresses are looked up in a fixed order: anything typed on the command line wins first, then the organization's settings for that specific target, then a set of shared defaults, and finally a built-in fallback if nothing else is set. Every output file, log file, and email subject line is automatically labeled with that organization's short name, so results from different organizations can never be confused with each other, even if several scans are run side-by-side.  See the readme in the UTILITIES folder for more details on how to use config files for multi-org scanning. SCHEDULED_TASKS includes instructions on how to seed encrypted credentials to use with scheduled tasks and passwordless-service accounts. 

### "Test mode" is the default, not an afterthought

Most scanning scripts default to checking only a small sample (commonly 250–500 items or services) unless a "full scan" option is explicitly added to the command. This is an intentional safety rail: it's very easy to accidentally kick off a scan that would otherwise run for hours against an entire organization's content, and test mode lets someone confirm a scan is configured correctly on a small sample before committing to the long run.
#### ⚠️ Performance ⚠️
Use of test mode only from a local device while on the VPN is encouraged. Attempting to run a full scan from a local resource will result in abnormally long run times and some scans may in fact not complete. This project was designed to scan from assets located in cloud environments with access to high-speed WAN backbone. 

### Exceptions are allowed

Most scans support excluding one or more ArcGIS groups from being flagged. Real deployments always have a few sanctioned exceptions to a rule (a known duplicate kept intentionally, a demo service that's supposed to be empty, and so on), and this feature lets an administrator tell a scan "skip anything in this group" rather than getting the same false alarm every single run.

### Email reports never get too big to send

Every script's completion email uses the same three-step attachment strategy: attach the report file(s) as-is if they're small enough; if not, compress them into a single zip file and attach that instead; and if even the zipped version is too large for the mail server, attach just the log file and clearly explain in the email body exactly where the full report can still be found on disk. The size limit is configurable per organization.

### AGOL scans never accidentally sweep the entire public catalog 

ArcGIS Online is a single shared, multi-tenant system — a poorly-scoped search can return results from *other* organizations' public content, not just your own. Every script that searches AGOL's catalog restricts its search to the signed-in organization, and the more sensitive scans (for example, the related-items and empty-feature-service scans) will refuse to run at all if that restriction can't be confirmed, rather than risk scanning the wrong thing.

### One bad item never stops the whole scan

If a script runs into a problem with a single service, layer, or item partway through a scan of thousands, that one problem is recorded in the report and the scan keeps going. A single broken or inaccessible item is never allowed to abort an otherwise-successful multi-hour run.

### Security: how logins and network traffic are protected

Because these scripts connect to production ArcGIS systems using accounts with broad read access, security is treated as a first-class concern throughout the project, not an afterthought. Four protections work together:

- **Encrypted connections, verified every time.** Every network connection this project makes — whether it's the main login to Portal/AGOL or a script's own separate web requests — uses an encrypted (HTTPS/TLS) connection, and that connection's certificate is checked to confirm it's genuinely talking to the real server, not an impostor. There is no "just connect and don't check" mode anywhere in this project. For an internal Portal that uses an organization's own internal certificate authority, a script can be told to trust that internal certificate specifically (via an option called `--tls-mode`), but the checking itself is never turned off.

In most uses cases when scanning AGOL organizations with its default commercial certificate no additional scan parameters are required. Scanning on-prem ArcGIS Enterprise usually requires the use of --tls-mode os-trust-store to accommodate organization-issued certificates. Additional parameters are documented in the UTILITIES readme for other use cases. 

- **Login credentials are never handled as plain text.** Once a script logs in, it never keeps the raw login token (the temporary "you're allowed in" credential ArcGIS issues after signing in) sitting around as an ordinary piece of text that could accidentally get printed, logged, or emailed. Instead, the token is sealed inside a small wrapper object that only ever hands out the real value at the exact moment it's needed to make a request — and if that wrapper object itself is ever accidentally printed or logged by mistake, it displays as `<redacted>` instead of the real value.

- **A safety net catches tokens that slip through anyway.** Even with the precautions above, some error messages generated deep inside other software can accidentally include a login token as part of a web address in the error text. Every script scans text like this — before it's written to a log file, shown on screen, put in an email, or saved to a CSV — and automatically blanks out anything that looks like a token, replacing it with a placeholder. This check runs automatically on every single log line as a backstop, in addition to the careful handling described above.

- **Passwords are never stored in a settings file.** Organization settings files never contain a password. Instead, passwords are looked up from Windows' built-in secure password vault (Credential Manager) at the moment a script runs, using an account name and service label that are stored in the settings file (which is safe, since a service label is not a secret). This project includes companion scripts (in the `SCHEDULED_TASKS` folder) for safely storing a service account's password into that secure vault in the first place, and for confirming a password was stored successfully — without ever displaying the password itself.

## Complementary Esri tooling

This project intentionally stays in its lane: reviewing the **content, items, and services** stored in Portal/AGOL and reporting anything unusual. It's not meant to replace Esri's own diagnostic tools, which look at different parts of the system. For a fuller picture, the reports from this project are meant to be used *alongside* the following Esri tools, not instead of them:

| Tool | What it covers | How it complements this project |
|---|---|---|
| **[Egdbhealth](https://community.esri.com/en/discussion/886466/what-is-egdbhealth)** | Checks the health of the underlying enterprise database itself (Oracle, PostgreSQL, SQL Server). | This project's Data Store/database reconciliation scans compare what Portal *thinks* exists against what's actually in the database, but don't judge the database's own health (storage, performance, configuration). Egdbhealth is the right tool for that; this project's "orphaned" and "missing" findings are a good starting point for deciding which databases are worth a closer look with Egdbhealth. |
| **[SystemLogParser](https://github.com/Esri/SystemLogParser)** | Measures actual usage by reading server log files. | This project flags *candidate* content for cleanup based on what's in the catalog (no data, no matching Portal item, stale sync history), but a low "view count" in Portal is a weak stand-in for real usage. Before removing anything this project flags, check real request traffic for that service in SystemLogParser first, so nothing with active behind-the-scenes users gets accidentally retired. This project's log-collection script is a helpful first step here, since it gathers the raw log files that SystemLogParser then reads. |
| **[GIS Enterprise Reporter](https://github.com/EsriPS/gisenterprisereporter)** | A point-in-time snapshot of the whole deployment's architecture, configuration, machines, and certificates. | This project runs frequently and automatically to catch things that drift day-to-day (oversized attachments, empty services, stale groups, and so on); GIS Enterprise Reporter is better suited to an occasional (e.g. quarterly) deep documentation snapshot of the deployment as a whole, and it also checks certificate expiration dates, which this project doesn't do at all. |

**A simple way to think about it:** this project answers "what looks wrong right now," Egdbhealth answers "is the database itself healthy," SystemLogParser answers "is anyone actually using this," and GIS Enterprise Reporter answers "what does the whole deployment look like." None of the four tools alone gives the complete picture, and none of them are a substitute for an administrator's own judgment before removing or changing anything.

## Getting Started: Authentication and Configuration

This project uses encrypted password storage to prevent passwords from being displayed in logs or scan output. A basic understanding of [Credential Manager in Windows | Microsoft Support](https://support.microsoft.com/en-us/windows/security/credential-manager-in-windows) and [keyring · PyPI](https://pypi.org/project/keyring/) is recommended in order to successfully implement this project. 

Most of the complex configuration required for this project is getting the config.toml file correctly set up. Most of the configuration settings are explained with comments in the config file and are self-explanatory. This section describes additional detail on how to configure the authentication to the various systems being scanned.

### AGOL Authentication

#### 1. ArcGIS Online login

You will need an administrator-level login to AGOL using a built-in (headless) account. This is a username and password: this project has not been developed to support PIV or SAML authentication to either platform.

#### 2. AGOL Windows Credential Manager

In Windows Credential Manager click "Add a Generic Credential":

- **Internet or network address:** Can be any word; this is the "service" that will be used to retrieve the password. If you just have one portal, you can't go wrong with using "AGOL". 
- **User name:** This is the AGOL username from the previous step that has administrative access. 
- **Password:** This is the password associated with the username from the previous step. 

#### 3. AGOL configuration

In the `[your organization name]_config.toml` file in the Utilities folder, find the section `# [agol] - ArcGIS Online target (--target agol)`:

- Enter the Portal URL (web site address you use to access your ArcGIS Online) where you find:
  ```
  portal-url = "https://your-agol-url"
  ```
- Enter the username from step 1 where you find:
  ```
  username = "some-user-name"
  ```
- Enter the Internet or network address (service) from step 2 where you find:
  ```
  keyring-service = "AGOL"
  ```

### Portal Authentication

#### 1. Portal login

You will need an administrator-level login to Portal using a built-in (headless) account. This is a username and password: this project has not been developed to support PIV or SAML authentication to either platform.

#### 2. Portal Windows Credential Manager

In Windows Credential Manager click "Add a Generic Credential":

- **Internet or network address:** Can be any word; this is the "service" that will be used to retrieve the password. If you just have one portal, you can't go wrong with using "PORTAL". 
- **User name:** This is the Portal username from the previous step that has administrative access. 
- **Password:** This is the password associated with the username from the previous step. 

#### 3. Portal configuration

In the `[your organization name]_config.toml` file in the Utilities folder, find the section `# [portal] - ArcGIS Enterprise Portal target (--target portal)`:

- Enter the Portal URL (web site address you use to access your Enterprise GIS Portal) where you find:
  ```
  portal-url = "https://your-portal-url"
  ```
- Enter the username from step 1 where you find:
  ```
  username = "some-user-name"
  ```
- Enter the Internet or network address (service) from step 2 where you find:
  ```
  keyring-service = "PORTAL"
  ```

#### 4. Portal Hosting Server URL

The hosting server URL will be the same in a single or multi-machine environment. From the Portal Organization Setting blade, find Servers → Server role → Hosting Server Administration URL and enter the URL where you see:
```
hosting-url = "https://some-hosting-url"
```

#### 5. Portal Content Directory

From `https://your-portal-url/portal/portaladmin/system/directories/content` copy the content directory path to where you see:
```
content-items-dir = "\\\\some-server\\some-path\\content\\items"
```

#### 6. Hosting server / Data Store settings

You will need the Portal Data Store PostGRE admin and managed username and password. You will need to execute [listadminusers](https://doc.esri.com/en/arcgis-enterprise/latest/administer/dsu-listadminusers.html?pivots=os-windows), [listmanageduser](https://doc.esri.com/en/arcgis-enterprise/latest/administer/dsu-listmanageduser.html?pivots=os-windows), and [changepassword](https://doc.esri.com/en/arcgis-enterprise/latest/administer/dsu-changepassword.html?pivots=os-windows) to get this information. The authentication to the PostGRE database is required for the Portal-only datastore inventory scans.

**Database Port:** Will almost always be:
```
db-port = 9876
```

**Database Name:** Enter the output of [listmanageduser](https://doc.esri.com/en/arcgis-enterprise/latest/administer/dsu-listmanageduser.html?pivots=os-windows) in:
```
dbname = "some-database"
```

**Database User:** Enter the output of [listadminusers](https://doc.esri.com/en/arcgis-enterprise/latest/administer/dsu-listadminusers.html?pivots=os-windows) in:
```
db-user = "some-user"
```

#### 7. Datastore Admin Authentication

In Windows Credential Manager click "Add a Generic Credential":

- **Internet or network address:** Can be any word; this is the "service" that will be used to retrieve the password. If you just have one portal, you can't go wrong with using "DATASTORE". 
- **User name:** This is the datastore admin username from the previous step that has administrative access. 
- **Password:** This is the password associated with the username from the previous step.

Add the admin user name and Credential Service name in `db-user` and `keyring-service-db` in the toml file.

#### 8. Datastore Managed Schema Owner Authentication

In Windows Credential Manager click "Add a Generic Credential":

- **Internet or network address:** Can be any word; this is the "service" that will be used to retrieve the password. If you just have one portal, you can't go wrong with using "DATASTORE_MU". 
- **User name:** This is the datastore managed user username from the previous step that has user access. 
- **Password:** This is the password associated with the username from the previous step.

Add the managed user user name and Credential Service name in `db-user-mu` and `keyring-service-db-mu` in the toml file.

#### 9. ArcGIS Server Input Directory

Enter the UNC path (not drive letter) of the hosting server input directory in:
```
arcgisinput = "\\\\some-server\\some-path\\arcgissystem\\arcgisinput"
```

## Example Commands

The following samples are presented with the "test mode" flag (where available) so you can get comfortable with executing the scans before deploying them as a scheduled task to make sure you've got everything set up correctly.

### Attachments
```bash
python [folder]\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py --config [folder]\UTILITIES\ORG1.config.toml --target portal --threshold-mb 6

python [folder]\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --threshold-mb 6 --tls-mode os-trust-store 

python [folder]\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py --config [folder]\UTILITIES\ORG1.config.toml --target agol --threshold-mb 6

python [folder]\ATTACHMENTS\SCAN_ATTACHMENTS_SIZE.py --config [folder]\UTILITIES\ORG2.config.toml --target agol --threshold-mb 6
```

### Broken Feature Services
```bash
python [folder]\FEATURE_SERVICES\SCAN_BROKEN_HOSTED_SERVICES.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\FEATURE_SERVICES\SCAN_BROKEN_HOSTED_SERVICES.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\FEATURE_SERVICES\SCAN_BROKEN_HOSTED_SERVICES.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\FEATURE_SERVICES\SCAN_BROKEN_HOSTED_SERVICES.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Credits
```bash
python [folder]\CREDITS\AGOL_CREDIT_REPORT.py --config [folder]\UTILITIES\ORG1.config.toml --period week

python [folder]\CREDITS\AGOL_CREDIT_REPORT.py --config [folder]\UTILITIES\ORG1.config.toml --period month

python [folder]\CREDITS\AGOL_CREDIT_REPORT.py --config [folder]\UTILITIES\ORG2.config.toml --period month
```

### Deprecated Content
```bash
python [folder]\DEPCRECATED_CONTENT\DEPRECATED_CONTENT.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\DEPCRECATED_CONTENT\DEPRECATED_CONTENT.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\DEPCRECATED_CONTENT\DEPRECATED_CONTENT.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\DEPCRECATED_CONTENT\DEPRECATED_CONTENT.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Duplicate Layers
```bash
python [folder]\DUPLICATE_DATA\DUPLICATE_LAYERS.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\DUPLICATE_DATA\DUPLICATE_LAYERS.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\DUPLICATE_DATA\DUPLICATE_LAYERS.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\DUPLICATE_DATA\DUPLICATE_LAYERS.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Embedded Credentials
```bash
python [folder]\EMBEDDED_CREDENTIALS\PORTAL_SHARED_CREDENTIALS_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\EMBEDDED_CREDENTIALS\PORTAL_SHARED_CREDENTIALS_SCAN.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Empty Feature Services
```bash
python [folder]\EMPTY_FEATURE_SERVICES_INVENTORY\EMPTY_FEATURE_SERVICES.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\EMPTY_FEATURE_SERVICES_INVENTORY\EMPTY_FEATURE_SERVICES.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\EMPTY_FEATURE_SERVICES_INVENTORY\EMPTY_FEATURE_SERVICES.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\EMPTY_FEATURE_SERVICES_INVENTORY\EMPTY_FEATURE_SERVICES.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Feature Class Names
```bash
python [folder]\FEATURE_CLASS_NAMES\FEATURE_CLASS_NAME_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml

python [folder]\FEATURE_CLASS_NAMES\FEATURE_CLASS_NAME_SCAN.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --tls-mode os-trust-store 
```

### Field Names
```bash
python [folder]\FIELD_NAMES\FIELD_NAME_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\FIELD_NAMES\FIELD_NAME_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\FIELD_NAMES\FIELD_NAME_SCAN.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\FIELD_NAMES\FIELD_NAME_SCAN.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### FS Replicas
```bash
python [folder]\REPLICAS\INVENTORY_FS_REPLICAS.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\REPLICAS\INVENTORY_FS_REPLICAS.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\REPLICAS\INVENTORY_FS_REPLICAS.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store 

python [folder]\REPLICAS\INVENTORY_FS_REPLICAS.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Groups
```bash
python [folder]\GROUPS\GROUPS_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\GROUPS\GROUPS_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\GROUPS\GROUPS_SCAN.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store

python [folder]\GROUPS\GROUPS_SCAN.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Items
```bash
python [folder]\ITEMS\ITEM_INVENTORY.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\ITEMS\ITEM_INVENTORY.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\ITEMS\ITEM_INVENTORY.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store

python [folder]\ITEMS\ITEM_INVENTORY.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### License
```bash
python [folder]\LICENSE\NAMED_USER_LICENSE_INVENTORY.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\LICENSE\NAMED_USER_LICENSE_INVENTORY.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\LICENSE\NAMED_USER_LICENSE_INVENTORY.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store

python [folder]\LICENSE\NAMED_USER_LICENSE_INVENTORY.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Public Data Collection
```bash
python [folder]\PUBLIC_DATA_COLLECTION\PORTAL_PUBLIC_EDIT_SCAN.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\PUBLIC_DATA_COLLECTION\PORTAL_PUBLIC_EDIT_SCAN.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Users
```bash
python [folder]\USERS\USER_REPORT.py --config [folder]\UTILITIES\ORG1.config.toml --target portal --ad-check --dc AD.DOMAIN.CONTROLLER

python [folder]\USERS\USER_REPORT.py --config [folder]\UTILITIES\ORG1.config.toml --target agol --ad-check --dc AD.DOMAIN.CONTROLLER

python [folder]\USERS\USER_REPORT.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --ad-check --dc AD.DOMAIN.CONTROLLER --tls-mode os-trust-store

python [folder]\USERS\USER_REPORT.py --config [folder]\UTILITIES\ORG2.config.toml --target agol --ad-check --dc AD.DOMAIN.CONTROLLER
```

### Related Items
```bash
python [folder]\RELATED_ITEMS\GET_RELATED_ITEMS.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\RELATED_ITEMS\GET_RELATED_ITEMS.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store

python [folder]\RELATED_ITEMS\GET_RELATED_ITEMS.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\RELATED_ITEMS\GET_RELATED_ITEMS.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Web Maps
```bash
python [folder]\WEB_MAPS\SCAN_WEB_MAPS.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\WEB_MAPS\SCAN_WEB_MAPS.py --config [folder]\UTILITIES\ORG1.config.toml --target agol

python [folder]\WEB_MAPS\SCAN_WEB_MAPS.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --target portal --tls-mode os-trust-store

python [folder]\WEB_MAPS\SCAN_WEB_MAPS.py --config [folder]\UTILITIES\ORG2.config.toml --target agol
```

### Hosting Server Datastore
```bash
python [folder]\HOSTING_SVR_DATASTORE\PORTAL_HOSTING_SVR_DS_INVENTORY.py --config [folder]\UTILITIES\ORG1.config.toml

python [folder]\HOSTING_SVR_DATASTORE\PORTAL_HOSTING_SVR_DS_INVENTORY.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --tls-mode os-trust-store
```

### Portal Clean Up
```bash
python [folder]\PORTAL_CLEAN_UP\PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config [folder]\UTILITIES\ORG1.config.toml

python [folder]\PORTAL_CLEAN_UP\PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --tls-mode os-trust-store
```

### Portal Orphan Services
```bash
python [folder]\PORTAL_ORPHAN_FEATURE_SERVICES\PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config [folder]\UTILITIES\ORG1.config.toml

python [folder]\PORTAL_ORPHAN_FEATURE_SERVICES\PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --tls-mode os-trust-store
```

### Roles
```bash
python [folder]\ROLES\PORTAL_ROLES.py --config [folder]\UTILITIES\ORG2.config.toml --target agol

python [folder]\ROLES\PORTAL_ROLES.py --config [folder]\UTILITIES\ORG1.config.toml --target portal

python [folder]\ROLES\PORTAL_ROLES.py --config [folder]\UTILITIES\ORG1.config.toml --target agol
```

### SDE Replicas
```bash
python [folder]\REPLICAS\DATA_STORE_INVENTORY_SDE_REPLICAS.py --config [folder]\UTILITIES\ORG1.config.toml

python [folder]\REPLICAS\DATA_STORE_INVENTORY_SDE_REPLICAS.py --config [folder]\UTILITIES\SOME_PORTAL.config.toml --tls-mode os-trust-store
```

---

## Detailed descriptions

Each folder contains its own, more detailed readme with usage examples and configuration notes for the scripts in that folder.

---

## ATTACHMENTS

Finds unusually large file attachments stored on map layers.

| File | Purpose |
|---|---|
| **SCAN_ATTACHMENTS_SIZE.py** | Looks through every hosted map layer for attached files (photos, PDFs, and similar) at or above a size you choose. It first figures out which layers even have attachments, then measures every attachment on those layers and reports the ones that are too big — including the total size of everything oversized and how much space could be saved if every oversized file were shrunk down to 5 MB. Can skip an approved exception group, defaults to checking only 500 services at a time, and separately lists any services it couldn't read. Emails a report and a log. |

---

## CREDITS

Reports on ArcGIS Online credit usage.

| File | Purpose |
|---|---|
| **AGOL_CREDIT_REPORT.py** | Pulls a weekly or monthly credit-usage report from ArcGIS Online, cleans up Esri's raw export (which normally starts with a few header lines that aren't part of the actual data table), and produces three tidy reports: usage per person, usage totaled per application, and a usage summary per person. If a report for that time period was already generated, it automatically replaces it. AGOL only. Emails all three reports plus a log. |

---

## DEPCRECATED_CONTENT

Finds old, retired-style ArcGIS apps that are still in use.

| File | Purpose |
|---|---|
| **DEPRECATED_CONTENT.py** | Searches AGOL or Portal for items built with older, retired ArcGIS app templates that Esri no longer supports going forward. On AGOL it double-checks that the item is actually still wired up to a real map or group before flagging it, to avoid false alarms. Reports title, web address, view count, owner, and sharing details for everything it finds, then emails the report. Defaults to checking the first 250 items. |

---

## DUPLICATE_DATA

Finds map layers that appear to be duplicated across multiple services.

| File | Purpose |
|---|---|
| **DUPLICATE_LAYERS.py** | Compares every hosted map layer's name, shape type, and spatial reference to find layers that look like copies of each other across different services. It then narrows down false alarms — for example, skipping a case where one "duplicate" is actually just a legitimate view built from the other — and gives each remaining match a duplicate-likelihood score from 0–100 based on things like matching record counts, matching field structures, whether the layer has a generic name like "Layer1," and whether the whole service looks duplicated or just one layer in it. Every row in the report includes a plain-English sentence explaining exactly why it was flagged and how confident the tool is. Supports excluding an approved exception group and several ways to fine-tune what's checked. |

---

## EMPTY_FEATURE_SERVICES_INVENTORY

Finds map services that contain no data at all.

| File | Purpose |
|---|---|
| **EMPTY_FEATURE_SERVICES.py** | Checks every hosted map service to see whether *every single layer and table in it* has zero records. Anything with at least some data anywhere in it is left alone. Always restricted to your own organization's content (and refuses to run at all if that can't be confirmed on AGOL, to avoid accidentally scanning the entire public ArcGIS Online catalog). Reports the fully-empty services with their creation/edit dates and owner, then emails it. Defaults to checking the first 250 services; can skip an approved exception group. |

---

## FEATURE_SERVICES

Checks whether map services are actually working.

| File | Purpose |
|---|---|
| **SCAN_BROKEN_HOSTED_SERVICES.py** | Tests every map service it can find — whether it's hosted directly by Portal/AGOL, hosted on a connected (federated) server, or simply registered/referenced from elsewhere — by checking whether it responds correctly and whether each of its layers can be queried. Anything that errors out gets reported, along with a best guess at whether the failure looks like a permissions problem rather than something actually broken (though it's always included in the report either way, just labeled). Can restrict which categories of services get checked, and can skip an approved exception group. |

---

## FIELD_NAMES

Checks map layer column (field) names for naming problems.

| File | Purpose |
|---|---|
| **FIELD_NAME_SCAN.py** | Checks every field name in every hosted or connected map layer against a long list of words that are "reserved" by various database systems (Esri's own file databases, Microsoft SQL Server, PostgreSQL, Oracle, SQLite, and more) — a field named the same as one of these reserved words can cause confusing errors down the line. It also checks Esri's own naming rules (no illegal characters, not too long, must start with a letter), flags field names that expose internal database schema details, and flags duplicate ID/date/editor fields that suggest the underlying data was imported from somewhere that already had its own such fields. Produces a detailed report plus a second report summarizing which problem field names show up most often. |

---

## FEATURE_CLASS_NAMES

Checks the *real underlying database table names* behind map services for naming problems.

| File | Purpose |
|---|---|
| **FEATURE_CLASS_NAME_SCAN.py** | Portal only. Looks up each map layer's actual database table name (not just its display name in the map) and confirms that table really exists in the live database — automatically finding the current active database server even after a fail-over to a backup, so the check is always run against the right machine. Checks each table's name against Esri's naming rules and a list of forbidden name prefixes. By default only reports problems that are tied to an actual Portal item (so there's someone to follow up with); an option can additionally produce a complete, unfiltered listing of every table reference found. |

---

## GROUPS

Finds inactive ArcGIS groups.

| File | Purpose |
|---|---|
| **GROUPS_SCAN.py** | Finds groups older than 90 days that have no content in them, no members besides the owner, or both — while leaving administrative groups (ones marked so members can't leave) alone. Reports each stale group along with why it was flagged, then emails the report. |

---

## HOSTING_SVR_DATASTORE

Cross-checks what Portal thinks exists against what's actually stored in the underlying database. This tool, `PORTAL_CLEAN_UP`, and `PORTAL_ORPHAN_FEATURE_SERVICES` are designed to be used together as a set.

| File | Purpose |
|---|---|
| **PORTAL_HOSTING_SVR_DS_INVENTORY.py** | Portal only, read-only. Gathers every hosted map service's list of database tables (including behind-the-scenes attachment and companion tables), optionally checks a folder of service-definition files for additional table references, then connects directly to the underlying database to see what actually exists there. Cross-references the two lists and sorts every table into one of four categories: properly matched, referenced by a service but missing from the database, sitting in the database but referenced by no service (an orphan), or a likely support table for something else (like a versioned or attachment table). Remembers what it already checked so repeat runs are faster, unless a full refresh is requested. |

---

## ITEMS

A complete inventory of everything in the organization.

| File | Purpose |
|---|---|
| **ITEM_INVENTORY.py** | Inventories every single item (and, outside of test mode, every group too) in AGOL or Portal — title, owner, dates, description, tags, view count, sharing/access level, and more. Also separately identifies every hosted map service and classifies each one as hosted, connected from another server, or simply registered, and looks up when each one was last actually edited. Defaults to the first 250 items with groups skipped, for a quick check. |

---

## LICENSE

Tracks who's actually using their ArcGIS Pro license.

| File | Purpose |
|---|---|
| **NAMED_USER_LICENSE_INVENTORY.py** | For every user in the organization, reports their ArcGIS Pro license level (Basic/Standard/Advanced) and any add-on extensions they're assigned, whether they've checked their license out for offline/disconnected use, and — importantly — when they actually last *used* ArcGIS Pro (not just when they last logged into the website, which is a different thing). This "last used" date is looked up from the most reliable available source, with the specific source used noted on every row so nothing is left ambiguous. Can be limited to only users who actually hold a Pro license. Defaults to the first 50 users for a quick check. |

---

## LOG_DUMP

Collects diagnostic log files from every machine in the deployment.

| File | Purpose |
|---|---|
| **PORTAL_STACK_LOG_DUMP.py** | Automatically maps out every machine involved in a Portal deployment — the Portal server(s), the hosting server, the database server (including its backup, if any), and every connected server — then figures out exactly where each machine's log files live and copies the relevant ones, either to a local folder or up to Azure cloud storage. Can pull a one-time, date-filtered snapshot (e.g. "just the last 7 days," or a specific date range) or run as a standing nightly job that keeps a destination continuously up to date without ever deleting anything. Includes a "test mode" that shows exactly what *would* be copied, and checks the cloud storage connection, without actually moving any files. |

---

## PORTAL_CLEAN_UP

Compares Portal's item list against what's actually stored on disk. This tool, `HOSTING_SVR_DATASTORE`, and `PORTAL_ORPHAN_FEATURE_SERVICES` are designed to be used together as a set.

| File | Purpose |
|---|---|
| **PORTAL_ITEM_ID_COMPARE_TO_CONTENT_DIR.py** | Portal only. Compares the list of item IDs Portal knows about against the actual folders sitting on disk in Portal's content storage area, measuring how much disk space each folder takes up. Anything found on disk with no matching Portal item is double-checked before being called an orphan (in case it was simply missed by an earlier filter), and Esri's own built-in system content is automatically excluded from the report so it doesn't create noise. |

---

## PORTAL_ORPHAN_FEATURE_SERVICES

Finds map services on the server that Portal doesn't know about. This tool, `HOSTING_SVR_DATASTORE`, and `PORTAL_CLEAN_UP` are designed to be used together as a set.

| File | Purpose |
|---|---|
| **PORTAL_INVENTORY_ORPHAN_HOSTED_SERVICES.py** | Portal only. Walks the entire hosting server to list every map service that exists on it, then compares that list against Portal's own catalog to find services that exist on the server but were never registered as — or are no longer linked to — a Portal item. For each one found, it checks whether any other item or map still quietly references it, and tests whether it still contains real data, so the report can tell the difference between "genuinely abandoned" and "still secretly in use." Always runs a complete scan and always emails a report. |

---

## PUBLIC_EDIT_CONTROL

Identifies public sharing risks with interactive editing.

| File | Purpose |
|---|---|
| **PORTAL_PUBLIC_EDIT_SCAN.py** | AGOL-specific scanner that finds hosted feature services shared to the public (access="public") with editing capabilities enabled. For each flagged service, classifies exactly what the public can do: submit new edits, view existing edits, update edits, or delete edits. The report focuses on services with actual editing risk (Create/Update/Delete enabled); read-only public views are excluded. Always restricted to the scanned organization's own content (refuses to run without this restriction confirmed, to avoid scanning AGOL's public catalog). Defaults to checking only the first 25 services in test mode. Emails a detailed CSV report with editing capabilities and guidance. |

---

## RELATED_ITEMS

Maps out which apps, maps, and services were built from which raw data files.

| File | Purpose |
|---|---|
| **GET_RELATED_ITEMS.py** | For each spreadsheet, CSV, KML, shapefile, or file geodatabase uploaded to the organization, finds every map, app, survey, or service that was built using it. This is particularly useful for spotting cases where someone uploaded a backup copy of a map layer's source data and then forgot to delete it after downloading what they needed. Always restricted to the organization's own content on AGOL — it refuses to run without that restriction confirmed, to avoid a scan of the entire public ArcGIS Online catalog. A fast "test mode" samples 20 items of each data type so every type is represented even in a quick check; a full mode checks everything. |

---

## REPLICAS

Finds and evaluates offline/sync replicas of map data.

| File | Purpose |
|---|---|
| **INVENTORY_FS_REPLICAS.py** | Inventories every "sync replica" (the technical mechanism behind offline data collection and syncing) across the whole deployment, or across an AGOL organization's hosted services. Labels each replica as actively syncing or stale (untouched for more than 90 days) and shows how recently it was last synced in plain language (e.g. "14 days ago"). Includes a single-service debug mode for spot-checking one service at a time. |
| **DATA_STORE_INVENTORY_SDE_REPLICAS.py** | Portal only. Looks at replicas from the *database's own point of view* rather than through the website/REST API — a complementary, deeper check that can catch replicas the service-level view in `INVENTORY_FS_REPLICAS.py` might not see, such as ones orphaned at the database level. Defaults to checking the first 50 replicas for a quick check. |

---

## ROLES

Generates a privilege matrix for Portal and AGOL roles.

| File | Purpose |
|---|---|
| **PORTAL_ROLES.py** | Fetches every custom role (and optionally built-in roles) from Portal or AGOL, along with each role's complete privilege list, then populates an Excel template with a permission matrix. Columns list each role, rows list each privilege, and cells contain a checkmark if the role has that privilege. The template's privilege labels (column C) are left unchanged; only the role columns (starting D) and checkmarks are written. Useful for documentation, auditing, and designing new custom roles. Supports explicit TLS verification mode (`--tls-mode`) for the script's own direct REST calls to fetch roles, separate from the arcgis SDK's own connection. |

---

## SCHEDULED_TASKS

Helpers for running these scans automatically and managing stored passwords safely.

| File | Purpose |
|---|---|
| **CREATE_SCH_TASK.ps1** | Sets up a Windows Scheduled Task that runs one of these scripts automatically every day at a set time, using a dedicated service account rather than a person's own login. |
| **SEED_CRED_MANAGER.ps1** | A one-time helper that safely stores a password into the dedicated service account's secure Windows password vault, so scheduled scripts can log in without a password ever being typed into a script or settings file. The password is never left sitting in a saved task afterward. |
| **CHECK_CRED_MANAGER.ps1** | Lets an administrator confirm which login entries are stored in the service account's password vault, without ever revealing what any of the actual passwords are. |

---

## SECURITY

Identifies risky credential-handling patterns.

| File | Purpose |
|---|---|
| **PORTAL_SHARED_CREDENTIALS_SCAN.py** | Scans AGOL or Portal for service items that embed shared (stored) credentials to a remote hosted service — either direct credentials stored on a Feature/Map/Image/Geometry service item, or API keys stored on an API Key item. For each service flagged, reports the exact nature of the exposure (stored username/password, API key with privileges, proxy URL pattern, etc.). Helps administrators identify high-risk credential exposure before it becomes an operational problem. Items are checked concurrently for speed. The report includes direct links to each flagged item in Portal/AGOL. Defaults to checking only the first 25 candidate services in test mode; supports full org scan with `--no-test`. |

---

## USERS

A full roster of every user account, with an optional Active Directory cross-check.

| File | Purpose |
|---|---|
| **USER_REPORT.py** | Inventories every user account in the organization — name, contact info, role, license type, whether their email is verified, and how many items/groups they own — while excluding Esri's own built-in system accounts. An optional add-on cross-references every account against the organization's Active Directory to check whether it's active, disabled, locked out, or expired, using only the credentials of the person already logged into the computer running the script (a password is never asked for or transmitted for this check). Defaults to the first 250 users for a quick check. |

---

## UTILITIES

The shared toolbox every other script in this project relies on. See `UTILITIES\readme.md` for full technical detail; summarized here by file.

| File | Purpose |
|---|---|
| **\_\_init\_\_.py** | The front door to the shared toolbox — lets every other script pull in the helper functions it needs with one simple import line, without needing to know which specific file inside the toolbox each function actually lives in. |
| **gis_connect.py** | Handles logging into Portal or AGOL. Looks up the password from Windows' secure password vault (or a fallback environment variable), signs in, and always double-checks that the connection is genuinely encrypted and verified — this checking cannot be turned off. |
| **emailer.py** | Builds and sends every completion email in this project, including the smart three-step attachment strategy (attach as-is, then try a zip, then fall back to just the log with an explanation) and CSV-writing helpers. |
| **logging_setup.py** | Sets up each script's activity log — always shown on screen, and also saved to a file once the output folder is known. Every log line is automatically screened for anything that looks like a login token before it's written anywhere, as a safety-net on top of the login-handling protections described above. |
| **exclusions.py** | Powers the "skip this approved exception group" feature used by most scanning scripts. |
| **path_discovery.py** | Figures out exactly where each server's log files and install folders live on disk, with no guessing — if a path can't be confirmed, that one copy is skipped and reported rather than assumed. |
| **robocopy_utils.py** | Handles the actual file-copying mechanics (and date-range filtering) for the log-collection tool. |
| **stack_discovery.py** | Automatically maps out every machine in a Portal deployment — the Portal server(s), the hosting server, every connected server, and the database server(s) — so other scripts don't need this information hard-coded anywhere. |
| **bucketing.py** | Works around ArcGIS's built-in 10,000-result search limit by splitting a large search into smaller date-range chunks and combining the results, so scripts can inventory organizations with far more than 10,000 items. |
| **restutils.py** | A large collection of shared web-request helpers: reusable connection pooling, paged searching, and small utilities like generating consistent timestamps and telling AGOL and Portal apart automatically from a web address. |
| **arcgis_rest.py** | Fast, direct web-request helpers for the highest-traffic scanning operations (attachment and layer scanning), avoiding heavier, slower techniques where a lighter approach will do the job just as well. |
| **datastore_config.py** | Resolves the specific database-connection settings (host, port, database name, credentials) used only by scans that talk directly to the underlying database. |
| **azcopy_utils.py** | Handles uploading and syncing collected log files to Azure cloud storage, including safely retrieving the cloud storage access key from Windows' secure password vault rather than storing it in a settings file. |
| **cli.py** | Defines the shared command-line options (like `--config` and `--target`) that every script uses, and the logic that resolves final settings from the command line, the settings file, and built-in defaults. |
| **config.py** | Reads and caches each organization's settings file. Deliberately has no default/fallback settings file, so a script can never accidentally read the wrong organization's settings by mistake. |
| **secure_session.py** | Builds the encrypted, certificate-checked web connections that scripts use for their own direct web requests (separate from the main ArcGIS login), with an explicit, clearly-logged escape hatch for temporary troubleshooting only — one that never silences the warning that verification is off. |
| **auth.py** | Wraps every login token in a protective wrapper the moment it's received, so the raw token value is never handled as a plain, easily-leaked string, and provides the automatic text-scanning safety net that blanks out any token-shaped text before it reaches a log, email, or report. |
| **checkpoint.py** | Lets a long-running scan remember which services it already finished, so an interrupted multi-hour scan can pick back up where it left off instead of starting over from scratch. |
| **ORG.config.toml** | The settings file for the ORG organization: output folder, email settings, Portal/AGOL web addresses, login service names, and database connection details. |

---

## WEB_MAPS

Finds maps with broken or unreachable layers.

| File | Purpose |
|---|---|
| **SCAN_WEB_MAPS.py** | Checks every web map in the organization for layers that no longer respond correctly — timed out, errored, or came back with an unexpected response. To keep this fast even across thousands of maps, it reads every map's contents first, then tests each *unique* layer web address only once (since many maps often reuse the same layers), and finally builds the per-map report from those results. Restricted to the organization's own content by default. |

---

## DEPENDENCIES

**⚠️ Dependencies do NOT auto-install. You must install them manually before running any scripts.**

This project runs inside the ArcGIS Pro Python environment. Many required packages are already present, but several additional packages must be installed once into that environment before the scripts will work.

### Installation

```bash
pip install -r requirements.txt
```

Or manually install the packages listed below. **Python 3.10+ is required** (Python 3.11+ is recommended).

### Required packages

| Package | Purpose |
|---|---|
| **arcgis** (≥2.1.0) | Portal/AGOL login and REST API (usually pre-installed with ArcGIS Pro) |
| **requests** (≥2.25.0) | HTTP requests for REST calls |
| **keyring** (≥20.0.0) | Retrieve passwords from Windows Credential Manager (**required** for login) |
| **urllib3** (≥1.26.0) | Connection pooling and retry logic (usually auto-installed with requests) |

### Conditional packages

| Package | When needed |
|---|---|
| **truststore** (≥0.7.0) | Only if using `--tls-mode os-trust-store` for TLS verification |
| **tomli** (≥1.1.0) | Only for Python 3.10 or earlier (Python 3.11+ includes `tomllib` built-in) |

### Windows-specific packages

| Package | Purpose |
|---|---|
| **pywin32** (≥300) | Enhanced Windows API access (optional; needed only for advanced Windows features) |

### Non-Python tools required

These are separate Windows programs/services that must already exist on the machine:

- **robocopy.exe** — Windows built-in file copy utility (log collection)
- **azcopy.exe** — Microsoft Azure Blob upload tool (optional; only for `--destination blob`)
- **Remote Registry service** — Must be enabled on remote ArcGIS servers (log-path discovery)
- **Admin share (D$)** — Network file sharing permission needed on remote servers (log collection)

### Quick start

```bash
# Install all requirements at once
pip install -r requirements.txt

# OR manually install the core packages
pip install arcgis requests keyring

# OPTIONAL: If using OS-trust-store TLS mode
pip install truststore

# OPTIONAL: If running on Python 3.10 or earlier
pip install tomli
```

After installation, scripts will run without further setup (credentials are retrieved from Windows Credential Manager, not from settings files or environment variables). 