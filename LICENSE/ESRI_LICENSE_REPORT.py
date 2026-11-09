#!/usr/bin/env python3
# ESRI LICENSE REPORT
# Analyze one or more exported Portal and/or ArcGIS Online (AGOL) user CSVs.
#
# SAMPLE COMMANDS (run from the folder containing this script):
#
# Compare one Portal report with one AGOL report:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv" --agol "C:\temp\agol_users.csv"
#
# Analyze just one Portal report:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv"
#
# Analyze just one AGOL report:
#   python ESRI_LICENSE_REPORT.py --agol "C:\temp\agol_users.csv"
#
# Compare multiple Portal reports (no AGOL reports):
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" "C:\temp\portal_2.csv"
#
# Compare multiple AGOL reports (no Portal reports):
#   python ESRI_LICENSE_REPORT.py --agol "C:\temp\agol_1.csv" "C:\temp\agol_2.csv"
#
# Compare multiple Portal AND multiple AGOL reports:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" "C:\temp\portal_2.csv" --agol "C:\temp\agol_1.csv" "C:\temp\agol_2.csv"
#
# Compare multiple Portal reports with one AGOL report:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" "C:\temp\portal_2.csv" --agol "C:\temp\agol_users.csv"
#
# Compare one Portal report with multiple AGOL reports:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv" --agol "C:\temp\agol_1.csv" "C:\temp\agol_2.csv"
#
# Automatically detect platforms, including CSVs containing both platforms:
#   python ESRI_LICENSE_REPORT.py --input "C:\temp\mixed_users.csv" "C:\temp\other_users.csv"
#
# Repeat input flags instead of listing files under a single flag:
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_1.csv" --portal "C:\temp\portal_2.csv" --agol "C:\temp\agol_users.csv"
#
# Choose an output folder (default: C:\temp):
#   python ESRI_LICENSE_REPORT.py --portal "C:\temp\portal_users.csv" --agol "C:\temp\agol_users.csv" --output-dir "C:\temp\license_reports"
#
# Show all command-line options:
#   python ESRI_LICENSE_REPORT.py --help
#
# INPUT AND OUTPUT NOTES:
# - Replace example paths with your actual exported user-report CSV paths.
# - Required: username, email, userLicenseTypeId or User Type, a last-login
#   column, and disabled or Member Account Status. Headers ignore case.
# - platform may be omitted with --agol/--portal; --input requires platform.
# - Last-login aliases: last_login, lastLogin, Last Login Date.
# - Excludes disabled accounts, missing/never logins, and logins before
#   2025-11-30. Cutoff date is inclusive. Epoch timestamps use UTC.
# - All output license values use standardized display names.
# - If both user-type columns are populated, their normalized values must agree.
# - Name and fullName are accepted for full name; duplicate aliases fail safely.
# - --portal and --agol validate every row against the specified platform;
#   use --input for automatic detection or mixed-platform CSVs.
# - Each input flag accepts multiple files and may be repeated. Supply each
#   input file only once. Empty reports require --portal or --agol.
# - Accounts match across sources by email AND username prefix before '@',
#   case-insensitively; ambiguous prefix matches are kept separate.
# - Multiple reports for a platform receive numbered output-column prefixes
#   such as PORTAL_1 / PORTAL_2 or AGOL_1 / AGOL_2. Source mappings are logged.
# - Output filename: ESRI_LICENSE_REPORT_YYYYMMDD_HHMM.csv.
#   Add --overwrite to replace an existing output with the same timestamp.
# - The UTILITIES package must be beside this script or in a parent folder.

import argparse
import csv
import datetime as dt
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path(r"C:\temp")

REQUIRED_COLUMNS = {"username", "email"}

LICENSE_RANK = {
    "Viewer": 1,
    "Contributor": 2,
    "Mobile Worker": 3,
    "Creator": 4,
    "Professional": 5,
    "Professional Plus": 6,
}

LICENSE_ID_TO_NAME = {
    "viewerUT": "Viewer",
    "editorUT": "Contributor",
    "fieldWorkerUT": "Mobile Worker",
    "creatorUT": "Creator",
    "GISProfessionalStdUT": "Professional",
    "GISProfessionalAdvUT": "Professional Plus",
}

LICENSE_NAMES = {name.casefold(): name for name in LICENSE_RANK}
LICENSE_NAMES.update({
    key.casefold(): name for key, name in LICENSE_ID_TO_NAME.items()
})

HEADER_NAMES = {
    "platform": "platform",
    "username": "username",
    "email": "email",
    "userlicensetypeid": "userLicenseTypeId",
    "user type": "User Type",
    "fullname": "fullName",
    "name": "fullName",
    "last_login": "lastLogin",
    "lastlogin": "lastLogin",
    "last login date": "lastLogin",
    "disabled": "disabled",
    "member account status": "Member Account Status",
}

LOGIN_CUTOFF = dt.date(2025, 11, 30)


class InputValidationError(ValueError):
    pass


def add_project_root_to_path():
    script_dir = Path(__file__).resolve().parent

    for candidate in (script_dir, *script_dir.parents):
        utilities_dir = candidate / "UTILITIES"

        if (
            utilities_dir.is_dir()
            and (utilities_dir / "__init__.py").is_file()
        ):
            root = str(candidate)

            if root not in sys.path:
                sys.path.insert(0, root)

            return candidate

    raise FileNotFoundError(
        "Could not locate UTILITIES\\__init__.py beside the script "
        f"or in any parent directory of: {script_dir}"
    )


def username_base(username):
    return username.strip().split("@", 1)[0].casefold()


def joined_values(values):
    return " | ".join(sorted(value for value in values if value))


def flatten_arguments(groups):
    return [
        path
        for group in (groups or [])
        for path in group
    ]


def normalize_header(value):
    stripped = (value or "").strip()
    return HEADER_NAMES.get(stripped.casefold(), stripped)


def detect_encoding(path):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            while handle.read(1024 * 1024):
                pass
        return "utf-8-sig"
    except UnicodeDecodeError:
        try:
            with path.open("r", encoding="cp1252", newline="") as handle:
                while handle.read(1024 * 1024):
                    pass
            return "cp1252"
        except UnicodeDecodeError as exc:
            raise InputValidationError(
                f"{path}: input is not readable as UTF-8 or Windows-1252"
            ) from exc


def detect_dialect(handle):
    sample = handle.read(65536)
    handle.seek(0)

    if not sample:
        return csv.excel

    try:
        return csv.Sniffer().sniff(
            sample,
            delimiters=",;\t",
        )
    except csv.Error:
        return csv.excel


def parse_last_login(value):
    """Parse login dates; return None for no recorded login.

    Epoch seconds/milliseconds, including scientific notation, use UTC.
    Aware ISO date-times use UTC; naive dates retain their recorded date.
    Slash dates use US month/day/year order, except year-first formats.
    """
    from decimal import Decimal, InvalidOperation

    value = (value or "").strip()
    if value.casefold() in {"", "never", "none", "null", "n/a", "na", "nat"}:
        return None

    try:
        number = Decimal(value)
    except InvalidOperation:
        number = None

    if number is not None:
        if not number.is_finite():
            raise ValueError("non-finite Unix timestamp")
        if number <= 0:
            return None
        seconds = number / 1000 if number >= 100000000000 else number
        try:
            return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc).date()
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("Unix timestamp out of range") from exc

    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.date()
    except ValueError:
        pass

    for fmt in (
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
        "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p",
        "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unsupported last-login value {value!r}")


def normalize_license(value, path, line, column):
    value = (value or "").strip()
    if not value:
        return ""
    result = LICENSE_NAMES.get(value.casefold())
    if result is None:
        raise InputValidationError(
            f"{path}, line {line}, column {column!r}: unknown user type {value!r}"
        )
    return result


def read_report(path, expected_platform=None, logger=None):
    accounts_by_platform = defaultdict(dict)
    row_counts = Counter()
    exclusions = Counter()
    encoding = detect_encoding(path)

    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle, dialect=detect_dialect(handle))
        headers = [normalize_header(header) for header in (reader.fieldnames or [])]
        duplicates = [key for key, count in Counter(headers).items() if count > 1]
        if duplicates:
            raise InputValidationError(
                f"{path}: duplicate columns after normalization: {', '.join(duplicates)}"
            )
        missing = REQUIRED_COLUMNS - set(headers)
        if missing:
            raise InputValidationError(
                f"{path}: missing columns: {', '.join(sorted(missing))}"
            )
        license_columns = [
            key for key in ("userLicenseTypeId", "User Type") if key in headers
        ]
        if not license_columns:
            raise InputValidationError(
                f"{path}: expected userLicenseTypeId or User Type column"
            )
        if "lastLogin" not in headers:
            raise InputValidationError(
                f"{path}: expected last_login, lastLogin, or Last Login Date column"
            )
        if "platform" not in headers and expected_platform is None:
            raise InputValidationError(
                f"{path}: no platform column; specify --agol or --portal "
                "instead of --input"
            )
        if not any(key in headers for key in ("disabled", "Member Account Status")):
            raise InputValidationError(
                f"{path}: expected disabled or Member Account Status column; "
                "cannot verify account eligibility"
            )
        reader.fieldnames = headers

        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: row length differs from header"
                )
            # Infer platform only when its column is absent, never for a blank cell.
            platform = (
                row["platform"] if "platform" in headers else expected_platform
            ).strip().upper()
            if platform not in {"AGOL", "PORTAL"}:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: invalid platform {platform!r}"
                )
            if expected_platform is not None and platform != expected_platform:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: expected "
                    f"{expected_platform}, found {platform}"
                )
            accounts_by_platform[platform]
            row_counts[platform] += 1

            flag = row.get("disabled", "").strip().casefold()
            if flag not in {"", "true", "false", "1", "0", "yes", "no"}:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: invalid disabled flag {flag!r}"
                )
            disabled = (
                flag in {"true", "1", "yes"}
                or "disabled" in row.get("Member Account Status", "").casefold()
            )
            if disabled:
                exclusions["disabled"] += 1
                continue
            try:
                last_login = parse_last_login(row["lastLogin"])
            except ValueError as exc:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: {exc}"
                ) from exc
            if last_login is None:
                exclusions["no_recorded_login"] += 1
                continue
            if last_login < LOGIN_CUTOFF:
                exclusions["before_cutoff"] += 1
                continue

            username = row["username"].strip()
            email = row["email"].strip().casefold()
            base = username_base(username)
            if not email or not username or not base:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: blank email or username"
                )
            licenses = {
                normalize_license(row[key], path, reader.line_num, key)
                for key in license_columns if row[key].strip()
            }
            if len(licenses) > 1:
                raise InputValidationError(
                    f"{path}, line {reader.line_num}: conflicting user-type columns"
                )
            license_name = next(iter(licenses), "")
            account = accounts_by_platform[platform].setdefault(
                (email, username),
                {
                    "email": email, "username": username,
                    "username_base": base, "platform": platform,
                    "full_names": set(), "license_ids": set(),
                },
            )
            account["full_names"].add(row.get("fullName", "").strip())
            account["license_ids"].add(license_name)

    if not row_counts:
        if expected_platform is None:
            raise InputValidationError(
                f"{path}: empty report; specify --agol or --portal"
            )
        accounts_by_platform[expected_platform] = {}
        row_counts[expected_platform] = 0
    if logger is not None:
        logger.info(
            "Eligibility filter: cutoff=%s (inclusive); input rows=%d; "
            "excluded disabled=%d; excluded no recorded login=%d; "
            "excluded before cutoff=%d; eligible rows=%d; "
            "retained distinct accounts=%d",
            LOGIN_CUTOFF, sum(row_counts.values()), exclusions["disabled"],
            exclusions["no_recorded_login"], exclusions["before_cutoff"],
            sum(row_counts.values()) - sum(exclusions.values()),
            sum(len(accounts) for accounts in accounts_by_platform.values()),
        )
    return accounts_by_platform, row_counts, encoding


def load_sources(input_specs, logger):
    sources = []

    for path, expected_platform in input_specs:
        logger.info("Reading input CSV: %s", path)

        platform_accounts, row_counts, encoding = read_report(
            path,
            expected_platform,
            logger=logger,
        )

        logger.info(
            "Input encoding: %s; AGOL rows=%d; PORTAL rows=%d",
            encoding,
            row_counts["AGOL"],
            row_counts["PORTAL"],
        )

        for platform in ("AGOL", "PORTAL"):
            if platform not in platform_accounts:
                continue

            sources.append(
                {
                    "path": path,
                    "platform": platform,
                    "accounts": list(
                        platform_accounts[platform].values()
                    ),
                    "input_rows": row_counts[platform],
                    "encoding": encoding,
                }
            )

    platform_counts = Counter(
        source["platform"]
        for source in sources
    )
    platform_numbers = Counter()

    logger.info("Input CSV to output-column mapping:")

    for source in sources:
        platform = source["platform"]
        platform_numbers[platform] += 1
        number = platform_numbers[platform]

        prefix = (
            f"{platform}_{number}"
            if platform_counts[platform] > 1
            else platform
        )

        source["number"] = number
        source["prefix"] = prefix

        for account in source["accounts"]:
            account["source_prefix"] = prefix

        logger.info(
            "%s -> %s [platform=%s; instance=%d; "
            "input rows=%d; distinct accounts=%d]",
            prefix,
            source["path"],
            platform,
            number,
            source["input_rows"],
            len(source["accounts"]),
        )

        logger.info(
            "%s output columns: %s_username, %s_fullName, "
            "%s_userLicenseTypeId, %s_account_count_for_email",
            prefix,
            prefix,
            prefix,
            prefix,
            prefix,
        )

    return sources


def build_fieldnames(sources):
    fields = [
        "email",
        "username_base",
        "platform",
        "source_instances",
    ]

    for source in sources:
        prefix = source["prefix"]

        fields.extend(
            [
                f"{prefix}_username",
                f"{prefix}_fullName",
                f"{prefix}_userLicenseTypeId",
            ]
        )

    fields.extend(
        [
            "license_reported",
            "AGOL_account_count_for_email",
            "PORTAL_account_count_for_email",
        ]
    )

    for source in sources:
        fields.append(
            f"{source['prefix']}_account_count_for_email"
        )

    fields.extend(
        [
            "multiple_accounts_same_platform",
            "multiple_accounts_same_instance",
            "cross_platform_match",
        ]
    )

    return list(dict.fromkeys(fields))


def select_reported_license(accounts):
    """Always return the highest-ranked proper display name, never an ID."""
    candidates = set()
    unknown = set()
    for account in accounts:
        for raw_value in account["license_ids"]:
            value = (raw_value or "").strip()
            if not value:
                continue
            proper_name = LICENSE_NAMES.get(value.casefold())
            if proper_name is None:
                unknown.add(value)
            else:
                candidates.add(proper_name)
    if unknown:
        first = accounts[0]
        raise InputValidationError(
            f"Unrecognized license type(s) for email "
            f"{first['email']!r}, username prefix "
            f"{first['username_base']!r}: "
            f"{', '.join(sorted(unknown))}"
        )
    if not candidates:
        return ""
    return max(candidates, key=LICENSE_RANK.__getitem__)


def make_output_row(
    email,
    base,
    accounts,
    sources,
    fields,
    platform_email_counts,
    instance_email_counts,
    match_status,
):
    row = dict.fromkeys(fields, "")
    represented_prefixes = {
        account["source_prefix"]
        for account in accounts
    }
    represented_platforms = {
        account["platform"]
        for account in accounts
    }

    row["email"] = email
    row["username_base"] = base

    row["platform"] = " | ".join(
        platform
        for platform in ("AGOL", "PORTAL")
        if platform in represented_platforms
    )

    row["source_instances"] = " | ".join(
        source["prefix"]
        for source in sources
        if source["prefix"] in represented_prefixes
    )

    for platform in ("AGOL", "PORTAL"):
        row[f"{platform}_account_count_for_email"] = (
            platform_email_counts[(email, platform)]
        )

    for source in sources:
        prefix = source["prefix"]

        row[f"{prefix}_account_count_for_email"] = (
            instance_email_counts[(email, prefix)]
        )

    multiple_platforms = [
        platform
        for platform in ("AGOL", "PORTAL")
        if platform_email_counts[(email, platform)] > 1
    ]

    multiple_instances = [
        source["prefix"]
        for source in sources
        if instance_email_counts[
            (email, source["prefix"])
        ] > 1
    ]

    row["multiple_accounts_same_platform"] = (
        " | ".join(multiple_platforms)
        if multiple_platforms
        else "NONE"
    )

    row["multiple_accounts_same_instance"] = (
        " | ".join(multiple_instances)
        if multiple_instances
        else "NONE"
    )

    row["cross_platform_match"] = match_status

    for account in accounts:
        prefix = account["source_prefix"]

        row[f"{prefix}_username"] = account["username"]
        row[f"{prefix}_fullName"] = joined_values(
            account["full_names"]
        )
        row[f"{prefix}_userLicenseTypeId"] = joined_values(
            account["license_ids"]
        )

    row["license_reported"] = select_reported_license(accounts)

    return row


def analyze_sources(sources):
    fields = build_fieldnames(sources)

    accounts = [
        account
        for source in sources
        for account in source["accounts"]
    ]

    platform_email_counts = Counter(
        (account["email"], account["platform"])
        for account in accounts
    )

    instance_email_counts = Counter(
        (account["email"], account["source_prefix"])
        for account in accounts
    )

    groups = defaultdict(lambda: defaultdict(list))

    for account in accounts:
        key = (
            account["email"],
            account["username_base"],
        )
        groups[key][account["source_prefix"]].append(account)

    supplied_platforms = {
        source["platform"]
        for source in sources
    }

    source_order = {
        source["prefix"]: index
        for index, source in enumerate(sources)
    }

    rows = []

    for (email, base), instance_groups in sorted(groups.items()):
        grouped_accounts = sorted(
            [
                account
                for instance_accounts in instance_groups.values()
                for account in instance_accounts
            ],
            key=lambda account: (
                source_order[account["source_prefix"]],
                account["username"],
            ),
        )

        ambiguous = (
            len(instance_groups) > 1
            and any(
                len(instance_accounts) > 1
                for instance_accounts in instance_groups.values()
            )
        )

        if ambiguous:
            for account in grouped_accounts:
                rows.append(
                    make_output_row(
                        email,
                        base,
                        [account],
                        sources,
                        fields,
                        platform_email_counts,
                        instance_email_counts,
                        "AMBIGUOUS_PREFIX_NOT_MERGED",
                    )
                )

            continue

        if len(instance_groups) > 1:
            group_platforms = {
                account["platform"]
                for account in grouped_accounts
            }

            match_status = (
                "EMAIL_AND_USERNAME_PREFIX"
                if len(group_platforms) > 1
                else "EMAIL_AND_USERNAME_PREFIX_SAME_PLATFORM"
            )

            rows.append(
                make_output_row(
                    email,
                    base,
                    grouped_accounts,
                    sources,
                    fields,
                    platform_email_counts,
                    instance_email_counts,
                    match_status,
                )
            )

            continue

        if len(sources) == 1:
            match_status = "OTHER_PLATFORM_NOT_SUPPLIED"
        elif len(supplied_platforms) == 1:
            match_status = "NO_MATCHING_ACCOUNT_ON_OTHER_INSTANCE"
        else:
            match_status = "NO_MATCHING_ACCOUNT_ON_OTHER_PLATFORM"

        for account in grouped_accounts:
            rows.append(
                make_output_row(
                    email,
                    base,
                    [account],
                    sources,
                    fields,
                    platform_email_counts,
                    instance_email_counts,
                    match_status,
                )
            )

    represented = Counter(
        (
            row["email"],
            source["prefix"],
            row[f"{source['prefix']}_username"],
        )
        for row in rows
        for source in sources
        if row[f"{source['prefix']}_username"]
    )

    expected = Counter(
        (
            account["email"],
            account["source_prefix"],
            account["username"],
        )
        for account in accounts
    )

    if represented != expected:
        raise ValueError(
            "Account reconciliation failed: each input account "
            "must be represented exactly once in the output"
        )

    stats = {
        "accounts": len(accounts),
        "unique_emails": len(
            {account["email"] for account in accounts}
        ),
        "ambiguous_rows": sum(
            row["cross_platform_match"]
            == "AMBIGUOUS_PREFIX_NOT_MERGED"
            for row in rows
        ),
        "blank_license_accounts": sum(
            "" in account["license_ids"]
            for account in accounts
        ),
        "cross_platform_matches": sum(
            row["cross_platform_match"]
            == "EMAIL_AND_USERNAME_PREFIX"
            for row in rows
        ),
        "same_platform_instance_matches": sum(
            row["cross_platform_match"]
            == "EMAIL_AND_USERNAME_PREFIX_SAME_PLATFORM"
            for row in rows
        ),
    }

    return rows, fields, stats


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze one or more AGOL/PORTAL user-report CSVs. "
            "Requires username, email, a user-type column, a last-login column, "
            "and a disabled/status column. Platform may be inferred from "
            "--agol/--portal. Multiple reports per platform "
            "receive numbered output-column prefixes."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--agol",
        type=Path,
        nargs="+",
        action="append",
        default=None,
        metavar="CSV",
        help=(
            "One or more AGOL CSVs. May be repeated. "
            "Every data row must identify AGOL."
        ),
    )

    parser.add_argument(
        "--portal",
        type=Path,
        nargs="+",
        action="append",
        default=None,
        metavar="CSV",
        help=(
            "One or more PORTAL CSVs. May be repeated. "
            "Every data row must identify PORTAL."
        ),
    )

    parser.add_argument(
        "--input",
        type=Path,
        nargs="+",
        action="append",
        default=None,
        metavar="CSV",
        help=(
            "One or more CSVs with platform detected from data. "
            "May be repeated. Mixed AGOL/PORTAL files are accepted."
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Destination folder for the automatically named CSV",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing report with the same timestamp",
    )

    return parser


def collect_input_specs(args):
    specs = []

    for groups, expected_platform in (
        (args.agol, "AGOL"),
        (args.portal, "PORTAL"),
        (args.input, None),
    ):
        for path in flatten_arguments(groups):
            specs.append(
                (
                    path.expanduser().resolve(),
                    expected_platform,
                )
            )

    return specs


def log_summary(logger, sources, rows, stats):
    logger.info(
        "Analysis complete: source instances=%d; "
        "distinct source accounts=%d; unique emails=%d; "
        "output rows=%d",
        len(sources),
        stats["accounts"],
        stats["unique_emails"],
        len(rows),
    )

    for source in sources:
        prefix = source["prefix"]

        logger.info(
            "%s summary: file=%s; input rows=%d; "
            "distinct accounts=%d; represented output accounts=%d",
            prefix,
            source["path"],
            source["input_rows"],
            len(source["accounts"]),
            sum(
                bool(row[f"{prefix}_username"])
                for row in rows
            ),
        )

    logger.info(
        "Cross-platform matched rows: %d",
        stats["cross_platform_matches"],
    )

    logger.info(
        "Same-platform cross-instance matched rows: %d",
        stats["same_platform_instance_matches"],
    )

    if stats["ambiguous_rows"]:
        logger.warning(
            "%d output rows have ambiguous username-prefix "
            "matches and were kept separate",
            stats["ambiguous_rows"],
        )

    if stats["blank_license_accounts"]:
        logger.warning(
            "%d accounts contain blank license IDs; "
            "blank values were not inferred",
            stats["blank_license_accounts"],
        )

    reported_counts = Counter(
        row["license_reported"]
        for row in rows
    )

    for license_id in LICENSE_RANK:
        logger.info(
            "license_reported %s: %d",
            license_id,
            reported_counts[license_id],
        )

    if reported_counts[""]:
        logger.warning(
            "Rows with blank license_reported: %d",
            reported_counts[""],
        )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not any((args.agol, args.portal, args.input)):
        parser.error(
            "Supply at least one CSV through "
            "--agol, --portal, or --input"
        )

    try:
        project_root = add_project_root_to_path()
    except FileNotFoundError as exc:
        parser.error(str(exc))

    try:
        from UTILITIES import (
            ensure_output_dir,
            setup_logging,
            write_dict_csv,
        )
    except ImportError as exc:
        parser.error(
            f"Found UTILITIES at {project_root / 'UTILITIES'}, "
            f"but importing it failed: {exc}"
        )

    logger = setup_logging(None, "ESRI_LICENSE_REPORT")

    temporary_path = None
    output = None
    reserved_output = False

    try:
        input_specs = collect_input_specs(args)
        output_dir = args.output_dir.expanduser().resolve()

        date_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
        output = output_dir / f"ESRI_LICENSE_REPORT_{date_stamp}.csv"

        logger.info("Project root: %s", project_root)
        logger.info("Supplied input files: %d", len(input_specs))
        logger.info("Output CSV: %s", output)

        seen_paths = set()

        for path, expected_platform in input_specs:
            if not path.is_file():
                raise InputValidationError(
                    f"Input file does not exist or is not a file: {path}"
                )

            if path in seen_paths:
                raise InputValidationError(
                    f"The same input file was supplied more than once: "
                    f"{path}"
                )

            seen_paths.add(path)

            if path == output:
                raise InputValidationError(
                    "Output must not replace an input report"
                )

        if output_dir.exists() and not output_dir.is_dir():
            raise ValueError(
                f"Output directory path is not a folder: {output_dir}"
            )

        if output.is_dir():
            raise ValueError(
                f"The generated output path is a directory: {output}"
            )

        if output.exists() and not args.overwrite:
            raise FileExistsError(
                f"Output already exists: {output}. "
                "Use --overwrite to replace it, or run again "
                "after the timestamp changes."
            )

        sources = load_sources(input_specs, logger)
        rows, fields, stats = analyze_sources(sources)

        ensure_output_dir(str(output_dir))

        if not args.overwrite:
            with output.open("x", encoding="utf-8"):
                pass
            reserved_output = True

        fd, temporary_name = tempfile.mkstemp(
            prefix=".esri_license_report_",
            suffix=".csv",
            dir=str(output_dir),
        )
        temporary_path = Path(temporary_name)
        os.close(fd)

        if rows:
            written = write_dict_csv(
                rows,
                str(temporary_path),
                fieldnames=fields,
            )

            if not written:
                raise OSError(
                    "The shared CSV helper did not write the output"
                )
        else:
            with temporary_path.open(
                "w",
                encoding="utf-8",
                newline="",
            ) as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=fields,
                )
                writer.writeheader()

        os.replace(str(temporary_path), str(output))
        temporary_path = None
        reserved_output = False

        log_summary(logger, sources, rows, stats)
        logger.info("Wrote report: %s", output)

        return 0

    except InputValidationError as exc:
        logger.warning(
            "Input validation failed. Run aborted; "
            "no new report was written. %s",
            exc,
        )
        return 1

    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        logger.error("Analysis failed: %s", exc)
        return 1

    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not remove temporary file %s: %s",
                    temporary_path,
                    exc,
                )

        if reserved_output and output is not None:
            try:
                output.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not remove reserved output file %s: %s",
                    output,
                    exc,
                )


if __name__ == "__main__":
    sys.exit(main())