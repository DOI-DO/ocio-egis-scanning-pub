#!/usr/bin/env python3
r"""
FIELD_NAME_SCAN.py
==============================================================================
Read-only scan of Portal or AGOL Feature/Map Service field names for the same
reserved-keyword, Esri naming-rule, SDE/DBO prefix, Survey123, and duplicate
system-field conditions as the original script. 

This hardened edition preserves the original rule tables, rule evaluation,
report columns, target-specific rule bundles, test/full/single-service modes,
exclusions, TLS choices, output naming pattern, and notification purpose. It
adds strict catalog/error accounting, trusted token destinations, token
refresh-and-retry, bounded concurrency, per-thread HTTP sessions, and explicit
COMPLETE/INCOMPLETE/ABORTED process status.

The token is centrally held by a TokenProvider and exposed only at the request
boundary. Automatically discovered services outside the authenticated
Portal/federation boundary are not contacted unless their exact HTTPS origin is
explicitly authorized for anonymous access or as a token destination. A URL
supplied directly with --service-url may be queried anonymously.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import tempfile
import math
import os
import re
import sys

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from threading import Lock, local
from typing import Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import UTILITIES as _UTILITIES  # noqa: E402

_EXPECTED_UTILITIES_DIR = os.path.realpath(os.path.join(_PROJECT_ROOT, "UTILITIES"))
_ACTUAL_UTILITIES_DIR = os.path.realpath(
    os.path.dirname(getattr(_UTILITIES, "__file__", ""))
)
if os.path.normcase(_ACTUAL_UTILITIES_DIR) != os.path.normcase(_EXPECTED_UTILITIES_DIR):
    raise ImportError(
        "Refusing to import UTILITIES from an unexpected location: "
        f"{_ACTUAL_UTILITIES_DIR!r}; expected {_EXPECTED_UTILITIES_DIR!r}."
    )

from UTILITIES import (  # noqa: E402
    DEFAULT_CAP,
    DEFAULT_TIMEOUT,
    VerifyMode,
    add_common_portal_args,
    add_config_arg,
    add_email_args,
    add_exclude_group_arg,
    add_output_dir_arg,
    add_target_arg,
    add_test_flag,
    build_secure_session,
    build_timestamp,
    connect_gis,
    ensure_output_dir,
    finalize_config,
    gather_attachments,
    get_max_email_mb,
    get_org_id,
    get_platform,
    provider_for_gis,
    redact_token,
    send_email,
    setup_logging,
    start_timer,
    subject_line,
    timing_block,
    tune_gis_session,
    verify_tls_handshake,
    write_dict_csv,
)

LOGGER_NAME = "field_name_scan"
SUBJECT_PREFIX = "Feature Service Field Name Reserved-Keyword / Rule Scan"

DEFAULT_WORKERS = 64

TEST_LIMIT = 250

MAX_FIELD_NAME_LEN = 128

_ESRI_SYSTEM_OWNERS = (
    "esri", "esri_nav", "esri_livingatlas", "esri_boundaries",
    "esri_demographics", "esri_apps", "esri_en",
)

_TLS_MODE_CHOICES = {
    "standard": VerifyMode.STANDARD_CA,
    "os-trust-store": VerifyMode.OS_TRUST_STORE,
    "custom-ca-bundle": VerifyMode.CUSTOM_CA_BUNDLE,
}


_ESRI_FGDB = frozenset({
    "ALTER", "AND", "BETWEEN", "BY", "COLUMN", "CREATE", "DELETE", "DROP", "EXISTS", "FOR",
    "FROM", "GROUP", "IN", "INSERT", "INTO", "IS", "LIKE", "NOT", "NULL", "OR", "ORDER",
    "SELECT", "SET", "TABLE", "UPDATE", "VALUES", "WHERE",
})

_ESRI_PRO = frozenset({
    "DAY", "MONTH", "TABLE", "TEXT", "USER", "WHEN", "WHERE", "YEAR",
})

_MS_SQL = frozenset({
    "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUTHORIZATION", "BACKUP", "BEGIN",
    "BETWEEN", "BREAK", "BROWSE", "BULK", "BY", "CASCADE", "CASE", "CHECK", "CHECKPOINT",
    "CLOSE", "CLUSTERED", "COALESCE", "COLLATE", "COLUMN", "COMMIT", "COMPUTE",
    "CONSTRAINT", "CONTAINS", "CONTAINSTABLE", "CONTINUE", "CONVERT", "CREATE", "CROSS",
    "CURRENT", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_USER",
    "CURSOR", "DATABASE", "DBCC", "DEALLOCATE", "DECLARE", "DEFAULT", "DELETE", "DENY",
    "DESC", "DISK", "DISTINCT", "DISTRIBUTED", "DOUBLE", "DROP", "DUMP", "ELSE", "END",
    "ERRLVL", "ESCAPE", "EXCEPT", "EXEC", "EXECUTE", "EXISTS", "EXIT", "EXTERNAL", "FETCH",
    "FILE", "FILLFACTOR", "FOR", "FOREIGN", "FREETEXT", "FREETEXTTABLE", "FROM", "FULL",
    "FUNCTION", "GOTO", "GRANT", "GROUP", "HAVING", "HOLDLOCK", "IDENTITY", "IDENTITYCOL",
    "IDENTITY_INSERT", "IF", "IN", "INDEX", "INDEXED", "INITIALLY", "INNER", "INSERT", "INSTEAD", "INTERSECT", "INTO", "IS",
    "JOIN", "KEY", "KILL", "LEFT", "LIKE", "LINENO", "LOAD", "MERGE", "NATIONAL",
    "NOCHECK", "NONCLUSTERED", "NOT", "NULL", "NULLIF", "OF", "OFF", "OFFSETS", "ON",
    "OPEN", "OPENDATASOURCE", "OPENQUERY", "OPENROWSET", "OPENXML", "OPTION", "OR",
    "ORDER", "OUTER", "OVER", "PERCENT", "PIVOT", "PLAN", "PRECISION", "PRIMARY", "PRINT",
    "PROC", "PROCEDURE", "PUBLIC", "RAISERROR", "READ", "READTEXT", "RECONFIGURE",
    "REFERENCES", "REPLICATION", "RESTORE", "RESTRICT", "RETURN", "REVERT", "REVOKE",
    "RIGHT", "ROLLBACK", "ROWCOUNT", "ROWGUIDCOL", "RULE", "SAVE", "SCHEMA",
    "SECURITYAUDIT", "SELECT", "SEMANTICKEYPHRASETABLE", "SEMANTICSIMILARITYDETAILSTABLE",
    "SEMANTICSIMILARITYTABLE", "SESSION_USER", "SET", "SETUSER", "SHUTDOWN", "SOME",
    "STATISTICS", "SYSTEM_USER", "TABLE", "TABLESAMPLE", "TEXTSIZE", "THEN", "TO", "TOP",
    "TRAN", "TRANSACTION", "TRIGGER", "TRUNCATE", "TRY_CONVERT", "TSEQUAL", "UNION",
    "UNIQUE", "UNPIVOT", "UPDATE", "UPDATETEXT", "USE", "USER", "VALUES", "VARYING",
    "VIEW", "WAITFOR", "WHEN", "WHERE", "WHILE", "WITH", "WITHIN GROUP", "WRITETEXT",
})

_ORACLE = frozenset({
    "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUDIT", "BETWEEN", "BY", "CHAR",
    "CHECK", "CLUSTER", "COLUMN", "COLUMN_VALUE", "COMMENT", "COMPRESS", "CONNECT",
    "CREATE", "CURRENT", "DATE", "DECIMAL", "DEFAULT", "DELETE", "DESC", "DISTINCT",
    "DROP", "ELSE", "EXCLUSIVE", "EXISTS", "FILE", "FLOAT", "FOR", "FROM", "GRANT",
    "GROUP", "HAVING", "IDENTIFIED", "IMMEDIATE", "IN", "INCREMENT", "INDEX", "INITIAL",
    "INSERT", "INTEGER", "INTERSECT", "INTO", "IS", "LEVEL", "LIKE", "LOCK", "LONG",
    "MAXEXTENTS", "MINUS", "MLSLABEL", "MODE", "MODIFY", "NESTED_TABLE_ID", "NOAUDIT",
    "NOCOMPRESS", "NOT", "NOWAIT", "NULL", "NUMBER", "OF", "OFFLINE", "ON", "ONLINE",
    "OPTION", "OR", "ORDER", "PCTFREE", "PRIOR", "PUBLIC", "RAW", "RENAME", "RESOURCE",
    "REVOKE", "ROW", "ROWID", "ROWNUM", "ROWS", "SELECT", "SESSION", "SET", "SHARE",
    "SIZE", "SMALLINT", "START", "SUCCESSFUL", "SYNONYM", "SYSDATE", "TABLE", "THEN", "TO",
    "TRIGGER", "UID", "UNION", "UNIQUE", "UPDATE", "USER", "VALIDATE", "VALUES", "VARCHAR",
    "VARCHAR2", "VIEW", "WHENEVER", "WHERE", "WITH",
})

_POSTGRE = frozenset({
    "ALL", "ANALYSE", "ANALYZE", "AND", "ANY", "ARRAY", "AS", "ASC", "ASYMMETRIC",
    "BOTH", "CASE", "CAST", "CHECK", "COLLATE", "COLUMN", "CONSTRAINT", "CREATE",
    "CURRENT_CATALOG", "CURRENT_DATE", "CURRENT_ROLE", "CURRENT_TIME",
    "CURRENT_TIMESTAMP", "CURRENT_USER", "DEFAULT", "DEFERRABLE", "DESC", "DISTINCT",
    "DO", "ELSE", "END", "EXCEPT", "FALSE", "FETCH", "FOR", "FOREIGN", "FROM", "GRANT",
    "GROUP", "HAVING", "IN", "INITIALLY", "INTERSECT", "INTO", "LATERAL", "LEADING",
    "LIMIT", "LOCALTIME", "LOCALTIMESTAMP", "NOT", "NULL", "OFFSET", "ON", "ONLY", "OR",
    "ORDER", "PLACING", "PRIMARY", "REFERENCES", "RETURNING", "SELECT", "SESSION_USER",
    "SOME", "SYMMETRIC", "TABLE", "THEN", "TO", "TRAILING", "TRUE", "UNION", "UNIQUE",
    "USER", "USING", "VARIADIC", "WHEN", "WHERE", "WINDOW", "WITH",
})

_SQLITE = frozenset({
    "ACTION", "ADD", "AFTER", "ALL", "ALTER", "ALWAYS", "ANALYZE", "AND", "AS", "ASC",
    "ATTACH", "AUTOINCREMENT", "BEFORE", "BEGIN", "BETWEEN", "BY", "CASCADE", "CASE",
    "CAST", "CHECK", "COLLATE", "COLUMN", "COMMIT", "CONFLICT", "CONSTRAINT", "CREATE",
    "CROSS", "CURRENT", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "DATABASE",
    "DEFAULT", "DEFERRABLE", "DEFERRED", "DELETE", "DESC", "DETACH", "DISTINCT", "DO",
    "DROP", "EACH", "ELSE", "END", "ESCAPE", "EXCEPT", "EXCLUDE", "EXCLUSIVE", "EXISTS",
    "EXPLAIN", "FAIL", "FILTER", "FIRST", "FOLLOWING", "FOR", "FOREIGN", "FROM", "FULL",
    "GENERATED", "GLOB", "GROUP", "GROUPS", "HAVING", "IF", "IGNORE", "IMMEDIATE", "IN",
    "INDEX", "INDEXED", "INITIALLY", "INNER", "INSERT", "INSTEAD", "INTERSECT", "INTO",
    "IS", "ISNULL", "JOIN", "KEY", "LAST", "LEFT", "LIKE", "LIMIT", "MATCH",
    "MATERIALIZED", "NATURAL", "NO", "NOT", "NOTHING", "NOTNULL", "NULL", "NULLS", "OF",
    "OFFSET", "ON", "OR", "ORDER", "OTHERS", "OUTER", "OVER", "PARTITION", "PLAN",
    "PRAGMA", "PRECEDING", "PRIMARY", "QUERY", "RAISE", "RANGE", "RECURSIVE", "REFERENCES",
    "REGEXP", "REINDEX", "RELEASE", "RENAME", "REPLACE", "RESTRICT", "RETURNING", "RIGHT",
    "ROLLBACK", "ROW", "ROWS", "SAVEPOINT", "SELECT", "SET", "TABLE", "TEMP", "TEMPORARY",
    "THEN", "TIES", "TO", "TRANSACTION", "TRIGGER", "UNBOUNDED", "UNION", "UNIQUE",
})

_ESRI_FQ = frozenset({
    "AREA", "EMAXX", "EMAXY", "EMAXZ", "EMINX", "EMINY", "EMINZ", "ENTITY", "LEN",
    "MAX_MEASURE", "MIN_MEASURE", "NUMOFPTS", "POINTS",
})

_DATABRICKS = frozenset({
    "CROSS", "CURRENT_", "DEFAULT", "EXCEPT", "FALSE", "FULL", "INNER", "INTERSECT",
    "JOIN", "LATERAL", "LEFT", "MASK", "MINUS", "NATURAL", "NULL", "ON", "RIGHT", "SEMI",
    "TRUE", "UNION", "USING",
})

_VENDOR_RULES = (
    (_ESRI_FGDB, "Field name contains ESRI File Geodatabase Reserved Keyword"),
    (_ESRI_PRO, "Field name contains ESRI ArcGIS Pro Reserved Keyword"),
    (_MS_SQL, "Field name contains Microsoft SQL Server Reserved Keyword "
              "(could cause problem with Microsoft SQL data connections)"),
    (_POSTGRE, "Field name contains Postgre SQL Server Reserved Keyword "
               "(could cause problem with Portal and AGOL)"),
    (_ORACLE, "Field name contains Oracle SQL Server Reserved Keyword"),
    (_SQLITE, "Field name contains SQLite SQL Server Reserved Keyword "
              "(could cause problem with mobile workflows)"),
    (_ESRI_FQ, "Field name contains ESRI Fully Qualified Keyword "
               "(results in field name with lots of dots) when used with enterprise DB"),
    (_DATABRICKS, "Field name contains Databricks Reserved Keyword"),
)

_AGOL_RULE_SETS = frozenset({_ESRI_FGDB, _ESRI_PRO, _POSTGRE, _SQLITE, _DATABRICKS})
_VENDOR_RULES_AGOL = tuple(
    (kw_set, problem) for (kw_set, problem) in _VENDOR_RULES
    if kw_set in _AGOL_RULE_SETS
)

_PORTAL_VENDOR_SETS = tuple(kw for (kw, _p) in _VENDOR_RULES)
_AGOL_VENDOR_SETS = tuple(kw for (kw, _p) in _VENDOR_RULES_AGOL)

_RULES_BY_TARGET = {
    "portal": {
        "rules": _VENDOR_RULES,
        "all_sets": _PORTAL_VENDOR_SETS,
        "any": frozenset().union(*_PORTAL_VENDOR_SETS),
    },
    "agol": {
        "rules": _VENDOR_RULES_AGOL,
        "all_sets": _AGOL_VENDOR_SETS,
        "any": frozenset().union(*_AGOL_VENDOR_SETS),
    },
}

PROBLEM_ALL_VENDORS = ("Field name contains a Reserved Keyword listed by all software "
                       "and database vendors and is definitely causing a problem")

PROBLEM_ESRI_RULES = "Field name violates ESRI field name rules"

PROBLEM_S123 = ("ESRI BUG-000183727 (S123 adds underscore to field name). If you "
                "experience problems with this layer, this could be why")

PROBLEM_DUP_GLOBALID = ("Layer has a duplicate/imported GlobalID field ({field}) "
                        "alongside the system GlobalID, indicating source data "
                        "that already contained a GlobalID")

PROBLEM_DUP_OBJECTID = ("Layer has a duplicate/imported ObjectID field ({field}) "
                        "alongside the system ObjectID, indicating source data "
                        "that already contained an ObjectID")

PROBLEM_DUP_EDITOR = ("Layer has a duplicate/imported editor-tracking field "
                      "({field}) alongside a base '{base}' field, indicating "
                      "source data that already contained that column")

_DUP_GLOBALID_RE = re.compile(r"^GLOBALID_(\d+)$", re.IGNORECASE)
_DUP_OBJECTID_RE = re.compile(r"^OBJECTID_(\d+)$", re.IGNORECASE)

_DUP_EDITOR_BASES = (
    "edit_date", "editdate",
    "create_date", "createdate",
    "edit_by", "editby",
    "create_by", "createby",
    "edited_by", "editedby",
    "created_by", "createdby",
)
_DUP_EDITOR_RES = {
    base: re.compile(rf"^{re.escape(base)}_(\d+)$", re.IGNORECASE)
    for base in _DUP_EDITOR_BASES
}

_ESRI_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

_ESRI_RULE_EXCEPTIONS = frozenset({
    "SHAPE.STLENGTH()",
    "SHAPE.STAREA()",
    "DIRTYAREA.STAREA()",
    "DIRTYAREA.STLENGTH()",
    "ST_AREA(SHAPE)",
    "ST_LENGTH(SHAPE)",
})

_SCHEMA_PREFIX_RE = re.compile(r"^(DBO|SDE)\.", re.IGNORECASE)

PROBLEM_SCHEMA_EXPOSURE = "Exposes SDE schema: security violation"


def _keyword_problems(upper_name, bundle):
    if all(upper_name in s for s in bundle["all_sets"]):
        return [PROBLEM_ALL_VENDORS]

    problems = []
    for kw_set, problem in bundle["rules"]:
        if upper_name in kw_set and problem not in problems:
            problems.append(problem)
    return problems


def _character_problems(name, is_survey):
    problems = []

    if name.upper() in _ESRI_RULE_EXCEPTIONS:
        return problems

    if is_survey and name.startswith("_"):
        problems.append(PROBLEM_S123)
        remainder = name[1:]
        if remainder and _ESRI_NAME_RE.match(remainder) and len(name) <= MAX_FIELD_NAME_LEN:
            return problems

    if not _ESRI_NAME_RE.match(name) or len(name) > MAX_FIELD_NAME_LEN:
        if PROBLEM_ESRI_RULES not in problems:
            problems.append(PROBLEM_ESRI_RULES)
    return problems


def _schema_exposure_problem(name):
    if name and _SCHEMA_PREFIX_RE.match(name):
        return [PROBLEM_SCHEMA_EXPOSURE]
    return []


def evaluate_field_name(name, is_survey, bundle):
    if not name:
        return ""

    upper = name.upper()
    is_exception = upper in _ESRI_RULE_EXCEPTIONS
    is_reserved = upper in bundle["any"]
    is_legal = (is_exception
                or (bool(_ESRI_NAME_RE.match(name))
                    and len(name) <= MAX_FIELD_NAME_LEN))
    has_schema_prefix = bool(_SCHEMA_PREFIX_RE.match(name))

    if (not is_reserved and is_legal
            and not (is_survey and name.startswith("_"))
            and not has_schema_prefix):
        return ""

    problems = _keyword_problems(upper, bundle) if is_reserved else []
    problems.extend(_character_problems(name, is_survey))
    problems.extend(_schema_exposure_problem(name))
    return ";".join(problems)


MAX_WORKERS = 64
MAX_FULL_SERVICES = 2_000_000
SEARCH_PAGE_SIZE = 100
MAX_REDIRECTS = 3
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ERROR_SAMPLES = 50
AGOL_TRUSTED_SUFFIX = ".arcgis.com"
_SERVICE_PATH_RE = re.compile(
    r"(?i)^(?P<root>.+/(?:FeatureServer|MapServer))(?:/(?P<layer>\d+))?/?$"
)
_ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_AUTH_ERROR_RE = re.compile(
    r"(?i)(?:\b(?:498|499)\b|invalid\s+token|token\s+(?:is\s+)?expired|"
    r"token\s+required|authentication\s+token\s+required|\bHTTP\s+401\b)"
)
_MAPSERVER_NOT_FOUND_RE = re.compile(
    r"(?i)\b(?:REST\s+error|HTTP)\s+404\b"
)


class ScanAbort(RuntimeError):
    """A controlled failure that invalidates the complete scan."""


class RestCallError(RuntimeError):
    """A sanitized direct-REST failure with classification metadata."""

    def __init__(self, message: str, *, tls_error: bool = False,
                 auth_error: bool = False):
        super().__init__(redact_token(str(message)))
        self.tls_error = bool(tls_error)
        self.auth_error = bool(auth_error)


@dataclass(frozen=True)
class ParsedServiceUrl:
    normalized_url: str
    service_root: str
    layer_id: int | None
    origin: tuple[str, str, int]
    path: str


@dataclass
class ServiceOutcome:
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tls_errors: int = 0
    survey_errors: int = 0
    layers_examined: int = 0

    def add_error(self, message: str, *, tls_error: bool = False,
                  survey_error: bool = False) -> None:
        self.errors.append(redact_token(str(message)))
        if tls_error:
            self.tls_errors += 1
        if survey_error:
            self.survey_errors += 1


@dataclass
class ScanResult:
    rows: list[dict] = field(default_factory=list)
    services_discovered: int = 0
    services_scanned: int = 0
    services_with_errors: int = 0
    item_errors: int = 0
    tls_errors: int = 0
    survey_errors: int = 0
    layers_examined: int = 0
    error_samples: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return self.item_errors > 0


class CandidateStore:
    """Disk-backed, insertion-ordered service store for large full scans."""

    def __init__(self, logger):
        fd, path = tempfile.mkstemp(prefix="field_name_scan_", suffix=".sqlite")
        os.close(fd)
        self.path = path
        self.logger = logger
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            self._connection = sqlite3.connect(path)
            self._connection.execute(
                "CREATE TABLE seen_items (item_id TEXT PRIMARY KEY)"
            )
            self._connection.execute(
                "CREATE TABLE seen_urls (url_key TEXT PRIMARY KEY)"
            )
            self._connection.execute(
                "CREATE TABLE services ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "item_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
            )
            self._count = 0
            self._pending_writes = 0
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        logger.debug("Using temporary candidate store: %s", path)

    def mark_item_seen(self, item_id: str) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO seen_items(item_id) VALUES (?)", (item_id,)
        )
        self._pending_writes += 1
        self._commit_if_needed()
        return cursor.rowcount == 1

    def mark_url_seen(self, url_key: str) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO seen_urls(url_key) VALUES (?)", (url_key,)
        )
        self._pending_writes += 1
        self._commit_if_needed()
        return cursor.rowcount == 1

    def add(self, item_id: str, service: dict) -> None:
        payload = json.dumps(service, ensure_ascii=False, separators=(",", ":"))
        self._connection.execute(
            "INSERT INTO services(item_id, payload) VALUES (?, ?)",
            (item_id, payload),
        )
        self._count += 1
        self._pending_writes += 1
        self._commit_if_needed()

    def _commit_if_needed(self) -> None:
        if self._pending_writes >= 1000:
            self._connection.commit()
            self._pending_writes = 0

    def __len__(self) -> int:
        return self._count

    def flush(self) -> None:
        self._connection.commit()
        self._pending_writes = 0

    def __iter__(self):
        self._connection.commit()
        self._pending_writes = 0
        cursor = self._connection.execute(
            "SELECT payload FROM services ORDER BY seq"
        )
        try:
            for (payload,) in cursor:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise ScanAbort("Candidate store contained malformed data.")
                yield value
        finally:
            cursor.close()

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.logger.warning(
                    "Could not remove temporary candidate store %s: %s",
                    self.path, exc,
                )


class WorkerSessionPool:
    """Give each thread its own Session while retaining one TLS policy."""

    def __init__(self, mode: VerifyMode, ca_bundle_path: str | None):
        self._mode = mode
        self._ca_bundle_path = ca_bundle_path
        self._local = local()
        self._create_lock = Lock()
        self._sessions: list[requests.Session] = []

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session
        # truststore injection is process-global, so serialize first-use session
        # construction instead of racing it in many worker threads.
        with self._create_lock:
            session = build_secure_session(
                self._mode,
                ca_bundle_path=self._ca_bundle_path,
                pool_connections=2,
                pool_maxsize=4,
                retry_total=2,
                logger=None,
            )
            self._sessions.append(session)
        self._local.session = session
        return session

    def close(self) -> None:
        for session in self._sessions:
            try:
                session.close()
            except Exception:
                pass
        self._sessions.clear()


class FixedSessionPool:
    """Adapter used by the backward-compatible scan_one_service() API."""

    def __init__(self, session: requests.Session):
        self._session = session

    def get(self) -> requests.Session:
        return self._session


class TrustedUrlPolicy:
    """Validate Portal endpoints and decide where the Portal token may go."""

    def __init__(self, portal_base: str, target: str,
                 explicit_origins: Iterable[str], logger,
                 anonymous_origins: Iterable[str] = ()):
        self.portal_base = _normalize_portal_base(portal_base)
        self.target = target
        self.logger = logger
        self.portal_origin = _origin_tuple(self.portal_base)
        if self.portal_origin is None:
            raise ScanAbort("Could not determine the authenticated Portal origin.")
        portal_parts = urlsplit(self.portal_base)
        self.portal_path = (portal_parts.path or "").rstrip("/")
        self.explicit_origins: set[tuple[str, str, int]] = set()
        self.anonymous_origins: set[tuple[str, str, int]] = set()
        self.federation_bases: list[tuple[tuple[str, str, int], str]] = []

        for raw in explicit_origins:
            parts = _split_https_url(raw)
            if (parts.path not in ("", "/") or parts.query or parts.fragment):
                raise ScanAbort(
                    "--trusted-service-origin must be an exact HTTPS origin "
                    f"without a path, query, fragment, or user-info: {raw!r}"
                )
            origin = _origin_tuple_from_parts(parts)
            assert origin is not None
            self.explicit_origins.add(origin)

        for raw in anonymous_origins:
            parts = _split_https_url(raw)
            if (parts.path not in ("", "/") or parts.query or parts.fragment):
                raise ScanAbort(
                    "--anonymous-service-origin must be an exact HTTPS origin "
                    f"without a path, query, fragment, or user-info: {raw!r}"
                )
            origin = _origin_tuple_from_parts(parts)
            assert origin is not None
            self.anonymous_origins.add(origin)

    def add_federation_base(self, raw: object) -> None:
        if not raw:
            return
        parts = _split_https_url(str(raw))
        if parts.query or parts.fragment:
            raise ScanAbort(
                f"Federation URL contains a query or fragment: {raw!r}"
            )
        origin = _origin_tuple_from_parts(parts)
        assert origin is not None
        base_path = _normalize_federation_base_path(parts.path or "")
        entry = (origin, base_path)
        if entry not in self.federation_bases:
            self.federation_bases.append(entry)

    def validate_portal_url(self, raw: str) -> str:
        try:
            parts = _split_https_url(raw)
        except ScanAbort as exc:
            raise RestCallError(str(exc)) from exc
        origin = _origin_tuple_from_parts(parts)
        if origin != self.portal_origin:
            raise RestCallError(
                "Portal REST request attempted to leave the authenticated "
                "Portal origin"
            )
        if parts.query or parts.fragment:
            raise RestCallError(
                "Portal REST URL must not contain a query string or fragment"
            )
        encoded_path = parts.path or ""
        if _path_is_unsafe(encoded_path):
            raise RestCallError(
                "Portal REST URL contains unsafe path encoding or traversal"
            )
        path = encoded_path.rstrip("/")
        path_lower = path.lower()
        base = self.portal_path.lower()
        boundaries = (
            f"{base}/sharing" if base else "/sharing",
            f"{base}/portaladmin" if base else "/portaladmin",
        )
        if not any(
                path_lower == boundary
                or path_lower.startswith(boundary + "/")
                for boundary in boundaries):
            raise RestCallError(
                f"Portal REST path is outside the expected sharing/portaladmin "
                f"boundary: {path!r}"
            )
        return _rebuild_url(parts)

    def parse_service_url(self, raw: str) -> ParsedServiceUrl:
        try:
            parts = _split_https_url(raw)
        except ScanAbort as exc:
            raise RestCallError(str(exc)) from exc
        if parts.query or parts.fragment:
            raise RestCallError(
                "Service URL must not contain a query string or fragment"
            )
        encoded_path = parts.path or ""
        if _path_is_unsafe(encoded_path):
            raise RestCallError(
                "Service URL contains unsafe path encoding or traversal"
            )
        path = encoded_path.rstrip("/")
        if "/rest/services/" not in path.lower():
            raise RestCallError(
                "Service URL is not under an ArcGIS /rest/services/ path"
            )
        match = _SERVICE_PATH_RE.match(path)
        if match is None:
            raise RestCallError(
                "Service URL must end in FeatureServer, MapServer, or a numeric "
                "sub-layer id"
            )
        root_path = match.group("root")
        layer_text = match.group("layer")
        normalized = _rebuild_url(parts._replace(path=path, query="", fragment=""))
        root = _rebuild_url(parts._replace(path=root_path, query="", fragment=""))
        origin = _origin_tuple_from_parts(parts)
        assert origin is not None
        return ParsedServiceUrl(
            normalized_url=normalized,
            service_root=root,
            layer_id=int(layer_text) if layer_text is not None else None,
            origin=origin,
            path=path,
        )

    def _matches_federation_base(self, parsed: ParsedServiceUrl) -> bool:
        service_path = parsed.path.lower()
        for origin, base_path in self.federation_bases:
            if parsed.origin != origin:
                continue
            if not base_path or service_path.startswith(base_path + "/"):
                return True
        return False

    def is_portal_proxy_service(self, parsed: ParsedServiceUrl) -> bool:
        if parsed.origin != self.portal_origin:
            return False
        return bool(re.search(
            r"(?i)/sharing/servers/[0-9a-f]{32}/rest/services/",
            parsed.path,
        ))

    def is_registered_federated_service(self, raw: str) -> bool:
        try:
            parsed = self.parse_service_url(raw)
        except (RestCallError, ScanAbort):
            return False
        return self._matches_federation_base(parsed) or self.is_portal_proxy_service(parsed)

    def may_send_token_to_service(self, parsed: ParsedServiceUrl) -> bool:
        if parsed.origin == self.portal_origin:
            return True
        if parsed.origin in self.explicit_origins:
            return True
        if self.target == "agol":
            _scheme, host, port = parsed.origin
            if port == 443 and host.endswith(AGOL_TRUSTED_SUFFIX):
                return True
        return self._matches_federation_base(parsed)

    def may_contact_anonymously(self, parsed: ParsedServiceUrl) -> bool:
        return parsed.origin in self.anonymous_origins

    def render_anonymous_destinations(self) -> str:
        if not self.anonymous_origins:
            return "(none)"
        return ", ".join(
            sorted(_origin_text(origin) for origin in self.anonymous_origins)
        )

    def render_trusted_destinations(self) -> str:
        values = {_origin_text(self.portal_origin)}
        values.update(_origin_text(value) for value in self.explicit_origins)
        values.update(_origin_text(origin) for origin, _path in self.federation_bases)
        if self.target == "agol":
            values.add(f"*{AGOL_TRUSTED_SUFFIX}:443")
        return ", ".join(sorted(values))


class ArcGISRestClient:
    """Strict JSON GET client with URL policy and one token-refresh retry."""

    def __init__(self, session_pool: WorkerSessionPool, token_provider,
                 policy: TrustedUrlPolicy, logger):
        self.session_pool = session_pool
        self.token_provider = token_provider
        self.policy = policy
        self.logger = logger
        self._refresh_lock = Lock()
        self._warning_lock = Lock()
        self._anonymous_origins_warned: set[tuple[str, str, int]] = set()

    def _refresh_if_stale(self, failed_token: str) -> str:
        with self._refresh_lock:
            try:
                current = self.token_provider.get()
                if current == failed_token:
                    self.token_provider.refresh()
                refreshed = self.token_provider.get()
            except Exception as exc:
                raise RestCallError(
                    f"token refresh failed: {exc}", auth_error=True
                ) from exc
            if not refreshed:
                raise RestCallError(
                    "token refresh returned no token", auth_error=True
                )
            return refreshed

    def _warn_anonymous(self, parsed: ParsedServiceUrl) -> None:
        with self._warning_lock:
            if parsed.origin in self._anonymous_origins_warned:
                return
            self._anonymous_origins_warned.add(parsed.origin)
        self.logger.warning(
            "Withholding the organization token from untrusted service origin "
            "%s; requests there will be anonymous. Use "
            "--trusted-service-origin only after independently verifying the "
            "destination.", _origin_text(parsed.origin)
        )

    def get_portal_json(self, url: str, params: dict | None = None,
                        timeout: int = DEFAULT_TIMEOUT) -> dict:
        checked = self.policy.validate_portal_url(url)
        return self._get_json(checked, params, timeout, kind="portal",
                              send_token=True)

    def get_service_json(self, url: str, params: dict | None = None,
                         timeout: int = DEFAULT_TIMEOUT) -> dict:
        parsed = self.policy.parse_service_url(url)
        send_token = self.policy.may_send_token_to_service(parsed)
        if not send_token:
            self._warn_anonymous(parsed)
        return self._get_json(parsed.normalized_url, params, timeout,
                              kind="service", send_token=send_token)

    def _validate_redirect(self, url: str, kind: str, send_token: bool,
                           initial_service_origin=None) -> str:
        if kind == "portal":
            return self.policy.validate_portal_url(url)
        parsed = self.policy.parse_service_url(url)
        if (initial_service_origin is not None
                and parsed.origin != initial_service_origin):
            raise RestCallError(
                "Service request redirected to a different origin"
            )
        if send_token and not self.policy.may_send_token_to_service(parsed):
            raise RestCallError(
                "Token-bearing service request redirected outside the trusted "
                "Portal/federation boundary"
            )
        return parsed.normalized_url

    def _get_json(self, url: str, params: dict | None, timeout: int,
                  *, kind: str, send_token: bool) -> dict:
        token = self.token_provider.get() if send_token else ""
        try:
            return self._get_json_once(url, params, timeout, kind=kind,
                                       send_token=send_token, token=token)
        except RestCallError as exc:
            if not send_token or not exc.auth_error:
                raise
        refreshed = self._refresh_if_stale(token)
        return self._get_json_once(url, params, timeout, kind=kind,
                                   send_token=True, token=refreshed)

    def _get_json_once(self, url: str, params: dict | None, timeout: int,
                       *, kind: str, send_token: bool, token: str) -> dict:
        current_url = url
        initial_service_origin = None
        if kind == "service":
            initial_service_origin = self.policy.parse_service_url(url).origin
        merged = {"f": "json"}
        if params:
            merged.update(params)
        # The caller cannot smuggle or replace a token through params.
        merged.pop("token", None)
        if send_token:
            merged["token"] = token

        for redirect_count in range(MAX_REDIRECTS + 1):
            checked_url = self._validate_redirect(
                current_url, kind, send_token, initial_service_origin
            )
            try:
                response = self.session_pool.get().get(
                    checked_url,
                    params=merged,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.exceptions.Timeout as exc:
                raise RestCallError(f"timeout after {timeout}s") from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(f"request failed: {exc}") from exc

            if 300 <= response.status_code < 400:
                try:
                    location = response.headers.get("Location")
                finally:
                    response.close()
                if not location:
                    raise RestCallError(
                        f"HTTP {response.status_code} redirect had no Location header"
                    )
                if redirect_count >= MAX_REDIRECTS:
                    raise RestCallError(
                        f"too many redirects (more than {MAX_REDIRECTS})"
                    )
                redirected = urljoin(checked_url, location)
                redirect_parts = urlsplit(redirected)
                if redirect_parts.query or redirect_parts.fragment:
                    raise RestCallError(
                        "Redirect destination contained a query string or fragment"
                    )
                current_url = redirected
                continue

            if response.status_code in (401, 498, 499):
                status = response.status_code
                reason = response.reason
                response.close()
                raise RestCallError(
                    f"HTTP {status} {reason}", auth_error=True
                )

            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError) as exc:
                    response.close()
                    raise RestCallError(
                        f"invalid Content-Length header: {raw_length!r}"
                    ) from exc
                if declared_length < 0 or declared_length > MAX_JSON_BYTES:
                    response.close()
                    raise RestCallError(
                        f"JSON response exceeded the "
                        f"{MAX_JSON_BYTES // (1024 * 1024)} MB limit"
                    )

            chunks = []
            total_bytes = 0
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > MAX_JSON_BYTES:
                        raise RestCallError(
                            f"JSON response exceeded the "
                            f"{MAX_JSON_BYTES // (1024 * 1024)} MB limit"
                        )
                    chunks.append(chunk)
            except RestCallError:
                raise
            except requests.exceptions.Timeout as exc:
                raise RestCallError(
                    f"timeout while reading response after {timeout}s"
                ) from exc
            except requests.exceptions.SSLError as exc:
                raise RestCallError(
                    f"TLS/CERTIFICATE VERIFICATION FAILED while reading: {exc}",
                    tls_error=True,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise RestCallError(
                    f"request failed while reading response: {exc}"
                ) from exc
            finally:
                response.close()
            content = b"".join(chunks)

            if response.status_code >= 400 and not content:
                raise RestCallError(
                    f"HTTP {response.status_code} {response.reason}",
                    auth_error=response.status_code in (401, 403, 498, 499),
                )
            try:
                data = json.loads(content.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RestCallError(
                    f"non-JSON response (HTTP {response.status_code})",
                    auth_error=response.status_code in (401, 403, 498, 499),
                ) from exc
            if not isinstance(data, dict):
                raise RestCallError("endpoint returned a non-object JSON response")

            rest_error = _format_rest_error(data)
            if rest_error is not None:
                code = str((data.get("error") or {}).get("code", ""))
                raise RestCallError(
                    rest_error,
                    auth_error=code in {"498", "499"}
                    or bool(_AUTH_ERROR_RE.search(rest_error)),
                )
            if response.status_code >= 400:
                raise RestCallError(
                    f"HTTP {response.status_code} {response.reason}",
                    auth_error=response.status_code in (401, 403, 498, 499),
                )
            if str(data.get("status", "")).lower() == "error":
                messages = data.get("messages")
                detail = messages if messages else "no error messages supplied"
                raise RestCallError(
                    f"endpoint returned status=error: {detail}"
                )
            return data

        raise RestCallError("redirect processing failed")



def _bounded_int(label: str, minimum: int, maximum: int):
    def _parse(value: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer between {minimum} and {maximum}."
            ) from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}."
            )
        return parsed
    return _parse


def _path_is_unsafe(encoded_path: str) -> bool:
    current = encoded_path
    for _ in range(2):
        if "\\" in current or any(ord(char) < 32 for char in current):
            return True
        if any(segment in (".", "..") for segment in current.split("/")):
            return True
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return ("\\" in current
            or any(ord(char) < 32 for char in current)
            or any(segment in (".", "..") for segment in current.split("/")))


def _normalize_portal_base(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    if base.lower().endswith("/home"):
        base = base[:-len("/home")]
    return base


def _split_https_url(raw: object):
    candidate = str(raw or "").strip()
    try:
        parts = urlsplit(candidate)
        _ = parts.port
    except (TypeError, ValueError) as exc:
        raise ScanAbort(f"Could not parse HTTPS URL {candidate!r}: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise ScanAbort(f"URL must use HTTPS: {candidate!r}")
    if not parts.hostname:
        raise ScanAbort(f"URL has no hostname: {candidate!r}")
    if parts.username is not None or parts.password is not None:
        raise ScanAbort(f"URL contains user-info credentials: {candidate!r}")
    return parts


def _origin_tuple(raw: str) -> tuple[str, str, int] | None:
    try:
        return _origin_tuple_from_parts(_split_https_url(raw))
    except ScanAbort:
        return None


def _origin_tuple_from_parts(parts) -> tuple[str, str, int] | None:
    if not parts.hostname:
        return None
    return (parts.scheme.lower(), parts.hostname.lower().rstrip("."),
            parts.port or 443)


def _origin_text(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{rendered_host}" + (f":{port}" if port != 443 else "")


def _rebuild_url(parts) -> str:
    host = (parts.hostname or "").lower().rstrip(".")
    rendered_host = f"[{host}]" if ":" in host else host
    port = parts.port
    netloc = rendered_host + (f":{port}" if port and port != 443 else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "",
                       parts.query or "", parts.fragment or ""))


def _format_rest_error(data: object) -> str | None:
    if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
        return None
    err = data["error"]
    code = err.get("code", "?")
    message_code = err.get("messageCode")
    message = err.get("message") or "unknown REST error"
    details_raw = err.get("details") or []
    if isinstance(details_raw, (list, tuple)):
        details = "; ".join(str(value) for value in details_raw)
    else:
        details = str(details_raw)
    pieces = [f"REST error {code}"]
    if message_code:
        pieces.append(f"messageCode={message_code}")
    pieces.append(str(message))
    if details:
        pieces.append(details)
    return redact_token(": ".join(pieces))


def _validate_target_platform(target: str, platform: str) -> None:
    expected = "AGOL" if target == "agol" else "PORTAL"
    if platform == "UNKNOWN":
        raise ScanAbort("The configured Portal URL could not be classified.")
    if platform != expected:
        raise ScanAbort(
            f"--target {target!r} conflicts with the configured URL, which was "
            f"classified as {platform}."
        )


def _validate_email_cap(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ScanAbort(f"max-email-mb is not numeric: {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ScanAbort("max-email-mb must be a positive finite number.")
    return parsed


def _reserve_run_stamp(output_dir: str, org_prefix: str, target: str,
                       timestamp: str) -> tuple[str, str]:
    for sequence in range(1000):
        run_stamp = timestamp if sequence == 0 else f"{timestamp}_{sequence:02d}"
        log_path = os.path.join(
            output_dir,
            f"{org_prefix}_field_name_scan_{target}_{run_stamp}.log",
        )
        try:
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return run_stamp, log_path
    raise ScanAbort("Could not reserve a unique run filename after 1,000 attempts.")


def _portal_base_url(gis) -> str:
    value = _normalize_portal_base(getattr(gis, "url", None))
    if value:
        return value
    portal = getattr(gis, "_portal", None)
    value = _normalize_portal_base(getattr(portal, "url", None))
    if not value:
        raise ScanAbort("The authenticated GIS object exposed no Portal base URL.")
    return value


def _coerce_mapping(value: object) -> dict | None:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "items"):
        try:
            return dict(value.items())
        except Exception:
            return None
    return None


def _normalize_federation_base_path(path: str) -> str:
    normalized = (path or "").rstrip("/").lower()
    for suffix in ("/admin", "/rest"):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    return normalized


def _federation_server_dicts(gis, client: ArcGISRestClient) -> list[dict]:
    sdk_error = None
    try:
        raw = gis.admin.federation.servers
        if callable(raw):
            raw = raw()
        raw_mapping = _coerce_mapping(raw)
        if raw_mapping is not None:
            servers = raw_mapping.get("servers", [])
        elif isinstance(raw, (list, tuple)):
            servers = list(raw)
        else:
            raise TypeError("federation servers value had an unexpected shape")
        if not isinstance(servers, (list, tuple)):
            raise TypeError("federation servers value was not a list")
        converted = []
        for entry in servers:
            mapping = _coerce_mapping(entry)
            if mapping is None:
                raise TypeError("federation server entry was not a mapping")
            converted.append(mapping)
        return converted
    except Exception as exc:
        sdk_error = redact_token(str(exc))

    try:
        data = client.get_portal_json(
            f"{client.policy.portal_base}/portaladmin/federation/servers"
        )
        servers = data.get("servers") or []
        if not isinstance(servers, list):
            raise ScanAbort("Federation REST response field 'servers' was not a list.")
        converted = []
        for entry in servers:
            mapping = _coerce_mapping(entry)
            if mapping is None:
                raise ScanAbort("Federation REST response contained a malformed entry.")
            converted.append(mapping)
        return converted
    except Exception as exc:
        raise ScanAbort(
            "Could not read Portal federation metadata through either the SDK "
            f"({sdk_error}) or Portal REST ({redact_token(str(exc))})."
        ) from exc


def _configure_federation_policy(gis, client: ArcGISRestClient, logger) -> list[dict]:
    servers = _federation_server_dicts(gis, client)
    for index, server in enumerate(servers):
        values = []
        for key in (
            "url", "serverUrl", "publicUrl", "webAdaptorUrl", "servicesUrl",
            "adminUrl",
        ):
            value = server.get(key)
            if value:
                values.append(value)
        if not values:
            raise ScanAbort(
                f"Federation server record {index} exposed no public/admin URL."
            )
        accepted = 0
        failures = []
        for value in values:
            try:
                client.policy.add_federation_base(value)
                accepted += 1
            except ScanAbort as exc:
                failures.append(f"{value!r}: {exc}")
        if not accepted:
            raise ScanAbort(
                f"Federation server record {index} exposed no safe HTTPS URL: "
                + "; ".join(failures)
            )
        for failure in failures:
            logger.warning("Ignoring unsafe federation URL: %s", failure)
    logger.info("Resolved %d federation server record(s).", len(servers))
    logger.info("Trusted token destination(s): %s",
                client.policy.render_trusted_destinations())
    return servers


def _resolve_excluded_item_ids_strict(gis, raw_group_ids: str | None,
                                      logger) -> set[str]:
    group_ids = list(dict.fromkeys(
        value.strip() for value in str(raw_group_ids or "").split(",")
        if value.strip()
    ))
    excluded: set[str] = set()
    for group_id in group_ids:
        if not _ITEM_ID_RE.fullmatch(group_id):
            raise ScanAbort(
                f"Exclusion group id must be a 32-character hexadecimal id: "
                f"{group_id!r}"
            )
        try:
            group = gis.groups.get(group_id)
        except Exception as exc:
            raise ScanAbort(
                f"Could not resolve exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        if group is None:
            raise ScanAbort(f"Exclusion group {group_id!r} was not found.")
        try:
            members = group.content()
        except Exception as exc:
            raise ScanAbort(
                f"Could not read exclusion group {group_id!r}: "
                f"{redact_token(str(exc))}"
            ) from exc
        member_ids = {
            str(getattr(member, "id", "")).strip()
            for member in (members or []) if getattr(member, "id", None)
        }
        excluded.update(member_ids)
        logger.info(
            "Exclusion group %s (%s) contributes %d item id(s).",
            group_id, getattr(group, "title", "?"), len(member_ids),
        )
    return excluded


def _bounded_future_results(executor, items: Iterable[dict], worker,
                            max_in_flight: int):
    iterator = iter(items)
    pending = {}

    def submit_next() -> bool:
        try:
            item = next(iterator)
        except StopIteration:
            return False
        pending[executor.submit(worker, item)] = item
        return True

    for _ in range(max_in_flight):
        if not submit_next():
            break

    while pending:
        completed, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
        for future in completed:
            item = pending.pop(future)
            yield item, future
            submit_next()

def _duplicate_system_field_problems(fields):
    names_lower = set()
    for f in (fields or []):
        if isinstance(f, dict) and f.get("name"):
            names_lower.add(f["name"].lower())

    problems = {}

    for base_lower, base_re, sys_type, template in (
        ("globalid", _DUP_GLOBALID_RE, "esriFieldTypeGlobalID",
         PROBLEM_DUP_GLOBALID),
        ("objectid", _DUP_OBJECTID_RE, "esriFieldTypeOID",
         PROBLEM_DUP_OBJECTID),
    ):
        base_present = base_lower in names_lower
        for f in (fields or []):
            if not isinstance(f, dict):
                continue
            fname = f.get("name")
            if not fname or not base_re.match(fname):
                continue
            if base_present or f.get("type") == sys_type:
                problems[fname] = template.format(field=fname)

    for base_lower, base_re in _DUP_EDITOR_RES.items():
        if base_lower not in names_lower:
            continue
        for f in (fields or []):
            if not isinstance(f, dict):
                continue
            fname = f.get("name")
            if not fname or not base_re.match(fname):
                continue
            problems[fname] = PROBLEM_DUP_EDITOR.format(
                field=fname, base=base_lower)

    return problems


def _rows_for_fields(fields, is_survey, platform, service_name, service_url,
                     owner, layer_id, layer_name, bundle):
    dup_problems = _duplicate_system_field_problems(fields)
    rows = []
    for fld in (fields or []):
        if not isinstance(fld, dict):
            continue
        fname = fld.get("name")
        if not fname:
            continue
        problem = evaluate_field_name(fname, is_survey, bundle)
        dup = dup_problems.get(fname)
        if dup:
            problem = f"{problem};{dup}" if problem else dup
        if problem:
            rows.append({
                "platform": platform,
                "title": service_name,
                "service_url": service_url,
                "owner": owner,
                "layer_id": layer_id,
                "layer_name": layer_name,
                "field_name": fname,
                "field_alias": fld.get("alias", ""),
                "is_survey123": "Y" if is_survey else "N",
                "problem": problem,
            })
    return rows



def _type_keywords(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(entry) for entry in value]
    return [str(value)]


def _resolve_survey_flag(
        client: ArcGISRestClient,
        item_id: str,
        type_keywords: object,
        timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, list[tuple[str, bool]]]:
    """Resolve Survey123 status while preserving indeterminate REST failures."""
    keywords = _type_keywords(type_keywords)
    if keywords:
        for keyword in keywords:
            lowered = keyword.lower()
            if ("survey123" in lowered or lowered == "survey123 hub"
                    or "survey2service" in lowered):
                return True, []
        # Preserve the original shortcut: non-empty typeKeywords with no survey
        # marker are treated as non-Survey123 without a relatedItems call.
        return False, []

    if not item_id:
        return False, []
    if not _ITEM_ID_RE.fullmatch(str(item_id)):
        return False, [(
            f"invalid Portal item id for Survey123 lookup: {item_id!r}", False
        )]

    endpoint = (
        f"{client.policy.portal_base}/sharing/rest/content/items/"
        f"{quote(str(item_id), safe='')}/relatedItems"
    )
    errors: list[tuple[str, bool]] = []
    successful_directions = 0
    for direction in ("reverse", "forward"):
        try:
            data = client.get_portal_json(
                endpoint,
                {
                    "relationshipType": "Survey2Service",
                    "direction": direction,
                },
                timeout,
            )
        except RestCallError as exc:
            errors.append((
                f"Survey2Service {direction} lookup failed: {exc}",
                exc.tls_error,
            ))
            continue
        related = data.get("relatedItems")
        if not isinstance(related, list):
            errors.append((
                f"Survey2Service {direction} response had no relatedItems list",
                False,
            ))
            continue
        successful_directions += 1
        if related:
            # A relationship in either direction proves the positive result;
            # an earlier failure in the other direction no longer matters.
            return True, []

    if successful_directions < 2 and errors:
        return False, errors
    return False, []


def _is_map_service(parsed: ParsedServiceUrl) -> bool:
    return parsed.service_root.lower().endswith("/mapserver")


def _is_expected_map_service_not_found(
        parsed: ParsedServiceUrl, error: RestCallError) -> bool:
    """Treat a missing MapServer as outside this field-name scan's scope.

    Portal catalogs can retain Map Service items after the backing map service
    has been removed. This scanner is not a service-health audit, so a 404 from
    a MapServer is a clean skip. FeatureServer 404s and every non-404 failure
    remain scan errors because they can hide field-bearing data.
    """
    return (_is_map_service(parsed)
            and bool(_MAPSERVER_NOT_FOUND_RE.search(str(error))))


def _layer_descriptors(
        defn: dict, *, map_service: bool = False
        ) -> tuple[list[tuple[int, str, object]], list[str]]:
    descriptors = []
    errors = []
    for key in ("layers", "tables"):
        container = defn.get(key, [])
        if container is None:
            container = []
        if not isinstance(container, list):
            errors.append(f"service definition field {key!r} was not a list")
            continue
        for index, layer in enumerate(container):
            if not isinstance(layer, dict):
                errors.append(f"service definition {key}[{index}] was not an object")
                continue
            layer_id = layer.get("id")
            if isinstance(layer_id, bool) or layer_id is None:
                errors.append(f"service definition {key}[{index}] had no valid id")
                continue
            try:
                normalized_id = int(layer_id)
            except (TypeError, ValueError):
                errors.append(
                    f"service definition {key}[{index}] had invalid id {layer_id!r}"
                )
                continue
            if normalized_id < 0:
                errors.append(
                    f"service definition {key}[{index}] had negative id {normalized_id}"
                )
                continue
            fields = layer.get("fields")
            if fields is not None and not isinstance(fields, list):
                # MapServer roots can describe group, raster, tile, or other
                # non-feature layers with a non-list/placeholder fields value.
                # Those layers have no field names to evaluate and are a clean
                # skip, not a malformed-response warning. FeatureServer keeps
                # the strict validation because every listed feature/table
                # layer is expected to expose a real fields array.
                if not map_service:
                    errors.append(
                        f"service definition {key}[{index}] field list was malformed"
                    )
                fields = None
            descriptors.append((
                normalized_id,
                layer.get("name") or f"Layer {normalized_id}",
                fields,
            ))
    return descriptors, errors


def _scan_one_service_outcome(
        client: ArcGISRestClient,
        service_url: str,
        platform: str,
        bundle: dict,
        service_name: str = "",
        owner: str = "",
        is_survey: bool | None = None,
        item_id: str = "",
        type_keywords: object = None,
        timeout: int = DEFAULT_TIMEOUT) -> ServiceOutcome:
    outcome = ServiceOutcome()
    try:
        parsed = client.policy.parse_service_url(service_url)
    except (RestCallError, ScanAbort) as exc:
        outcome.add_error(f"invalid service URL {service_url!r}: {exc}")
        return outcome

    map_service = _is_map_service(parsed)
    root_defn = None
    try:
        root_defn = client.get_service_json(parsed.service_root, timeout=timeout)
    except RestCallError as exc:
        # A catalog can retain a Map Service item after its backing service is
        # removed. A 404 MapServer has no fields to inspect, so skip it without
        # turning this field-name report into a service-health warning.
        if _is_expected_map_service_not_found(parsed, exc):
            if parsed.layer_id is None:
                return outcome
            # Preserve the direct-sublayer fallback: a proxy can occasionally
            # expose the layer endpoint even when its parent root returns 404.
            root_defn = {}
        else:
            # Preserve the original single-layer fallback: a layer endpoint may
            # still be readable even when its parent definition is unavailable.
            if parsed.layer_id is None:
                outcome.add_error(
                    f"could not read service definition {parsed.service_root}: {exc}",
                    tls_error=exc.tls_error,
                )
                return outcome
            outcome.add_error(
                f"parent service definition could not be read; direct layer "
                f"fallback used: {exc}", tls_error=exc.tls_error,
            )
            root_defn = {}

    if not isinstance(root_defn, dict):
        outcome.add_error("service definition was not a JSON object")
        return outcome

    if is_survey is None:
        resolved_item_id = item_id or root_defn.get("serviceItemId") or ""
        survey, survey_errors = _resolve_survey_flag(
            client, str(resolved_item_id), type_keywords, timeout
        )
        is_survey = survey
        for message, tls_error in survey_errors:
            outcome.add_error(message, tls_error=tls_error,
                              survey_error=True)

    svc_name = service_name or root_defn.get("serviceDescription") \
        or parsed.service_root
    descriptors, descriptor_errors = _layer_descriptors(
        root_defn, map_service=map_service
    )
    for message in descriptor_errors:
        outcome.add_error(message)

    if parsed.layer_id is not None:
        matching = [entry for entry in descriptors if entry[0] == parsed.layer_id]
        layer_name = f"Layer {parsed.layer_id}"
        fields = None
        if matching:
            _lid, layer_name, fields = matching[0]
        if fields is None:
            try:
                data = client.get_service_json(parsed.normalized_url,
                                               timeout=timeout)
            except RestCallError as exc:
                if _is_expected_map_service_not_found(parsed, exc):
                    return outcome
                outcome.add_error(
                    f"layer {parsed.normalized_url} could not be read: {exc}",
                    tls_error=exc.tls_error,
                )
                return outcome
            if "fields" in data and not isinstance(data.get("fields"), list):
                if map_service:
                    # Group/raster/tile MapServer layers frequently expose no
                    # usable fields array. They are outside a field-name scan.
                    outcome.layers_examined += 1
                    return outcome
                outcome.add_error(
                    f"layer {parsed.normalized_url} returned a malformed fields list"
                )
                return outcome
            fields = data.get("fields")
            layer_name = data.get("name") or layer_name
            if not item_id and data.get("serviceItemId") and is_survey is False \
                    and not _type_keywords(type_keywords):
                # The direct layer may expose the item id when the parent did not.
                survey, survey_errors = _resolve_survey_flag(
                    client, str(data.get("serviceItemId")), type_keywords, timeout
                )
                if survey:
                    is_survey = True
                for message, tls_error in survey_errors:
                    outcome.add_error(message, tls_error=tls_error,
                                      survey_error=True)
        outcome.layers_examined += 1
        # A valid group/raster layer can legitimately have no fields. Preserve
        # the original clean-skip behavior in that case.
        if fields is None:
            return outcome
        outcome.rows.extend(_rows_for_fields(
            fields, bool(is_survey), platform, svc_name,
            parsed.normalized_url, owner, parsed.layer_id, layer_name, bundle,
        ))
        return outcome

    for layer_id, layer_name, fields in descriptors:
        layer_url = f"{parsed.service_root}/{layer_id}"
        if fields is None:
            try:
                data = client.get_service_json(layer_url, timeout=timeout)
            except RestCallError as exc:
                if _is_expected_map_service_not_found(parsed, exc):
                    outcome.layers_examined += 1
                    continue
                outcome.add_error(
                    f"layer {layer_url} could not be read: {exc}",
                    tls_error=exc.tls_error,
                )
                continue
            if "fields" in data and not isinstance(data.get("fields"), list):
                if map_service:
                    # Non-feature MapServer layers legitimately have no fields
                    # list; count them as examined and skip without a warning.
                    outcome.layers_examined += 1
                    continue
                outcome.add_error(f"layer {layer_url} returned a malformed fields list")
                continue
            fields = data.get("fields")
            layer_name = data.get("name") or layer_name
        outcome.layers_examined += 1
        if fields is None:
            continue
        outcome.rows.extend(_rows_for_fields(
            fields, bool(is_survey), platform, svc_name,
            parsed.service_root, owner, layer_id, layer_name, bundle,
        ))
    return outcome


def scan_one_service(session, service_url, token_provider, platform,
                     portal_url, bundle, service_name="", owner="",
                     is_survey=None, item_id="", type_keywords=None,
                     timeout=DEFAULT_TIMEOUT, logger=None,
                     trusted_service_origins=()):
    """Backward-compatible single-service entry point used by older callers.

    Cross-origin services are queried anonymously unless their exact HTTPS
    origin is supplied through trusted_service_origins.
    """
    effective_logger = logger or logging.getLogger(f"{LOGGER_NAME}.compat")
    target = "agol" if str(platform).strip().upper() == "AGOL" else "portal"
    policy = TrustedUrlPolicy(
        portal_url, target, trusted_service_origins, effective_logger
    )
    client = ArcGISRestClient(
        FixedSessionPool(session), token_provider, policy, effective_logger
    )
    outcome = _scan_one_service_outcome(
        client,
        service_url,
        platform,
        bundle,
        service_name=service_name,
        owner=owner,
        is_survey=is_survey,
        item_id=item_id,
        type_keywords=type_keywords,
        timeout=timeout,
    )
    if logger is not None:
        for message in outcome.errors:
            logger.warning("Service %r: %s", service_url, message)
    return outcome.rows


def _esri_owner_exclusion():
    return "NOT (" + " OR ".join(f"owner:{owner}" for owner in _ESRI_SYSTEM_OWNERS) + ")"


@dataclass(frozen=True)
class SearchBucket:
    hit_cap: bool
    stopped_early: bool = False


def _strict_search_bucket(client: ArcGISRestClient, query: str,
                          consumer=None,
                          cap: int = DEFAULT_CAP) -> SearchBucket:
    """Strictly page one date bucket; consume only after proving it is under cap."""
    endpoint = f"{client.policy.portal_base}/sharing/rest/search"
    start = 1
    received = 0
    declared_total = None

    while received < cap:
        page_size = min(SEARCH_PAGE_SIZE, cap - received)
        if start + page_size - 1 > cap:
            break
        data = client.get_portal_json(
            endpoint,
            {
                "q": query,
                "sortField": "modified",
                "sortOrder": "desc",
                "start": start,
                "num": page_size,
            },
        )
        page = data.get("results")
        if page is None:
            page = data.get("items")
        if page is None:
            page = []
        if (not isinstance(page, list)
                or any(not isinstance(item, dict) for item in page)):
            raise ScanAbort("Search response contained a malformed results list.")
        if "total" not in data:
            raise ScanAbort("Search response omitted its total value.")
        try:
            page_total = int(data.get("total"))
        except (TypeError, ValueError) as exc:
            raise ScanAbort("Search response contained an invalid total value.") from exc
        if page_total < 0:
            raise ScanAbort("Search response contained a negative total value.")
        if declared_total is None:
            declared_total = page_total
            # A capped bucket must be subdivided before any result is retained;
            # otherwise parent-bucket rows would be duplicated by child buckets.
            if declared_total >= cap:
                return SearchBucket(hit_cap=True)
        elif page_total != declared_total:
            raise ScanAbort(
                "Search total changed during pagination; catalog completeness "
                "cannot be guaranteed."
            )

        if not page:
            if declared_total > received:
                raise ScanAbort(
                    "Search returned an empty page before its declared total "
                    "was reached."
                )
            break

        for item in page:
            received += 1
            if consumer is not None and consumer(item):
                return SearchBucket(hit_cap=False, stopped_early=True)
        if received >= declared_total:
            break

        previous_start = start
        next_start = data.get("nextStart")
        if isinstance(next_start, int) and next_start > 0:
            start = next_start
        else:
            start += len(page)
        if start <= previous_start:
            raise ScanAbort("Search pagination made no forward progress.")

    if declared_total is not None and received < declared_total:
        raise ScanAbort(
            "Search pagination ended before its declared total was reached."
        )
    return SearchBucket(hit_cap=False)


def _enumerate_services_strict(
        gis,
        target: str,
        client: ArcGISRestClient,
        logger,
        excluded_ids: set[str],
        max_items: int | None) -> CandidateStore:
    """Strictly enumerate candidates into a bounded-memory disk store."""
    store = CandidateStore(logger)
    try:
        owner_clause = _esri_owner_exclusion()
        if target == "agol":
            org_id = get_org_id(gis, logger=logger)
            if not org_id:
                raise ScanAbort(
                    "Could not resolve the organization id; refusing an unscoped "
                    "ArcGIS Online search."
                )
            extra_clause = (
                f'orgid:{org_id} AND type:"Feature Service" AND {owner_clause}'
            )
            logger.info("Scoping AGOL scan to organization id: %s", org_id)
        else:
            org_id = None
            extra_clause = (
                '(type:"Feature Service" OR type:"Map Service") '
                f'AND {owner_clause}'
            )

        logger.info("Enumerating services on %s with strict date bucketing.",
                    target.upper())
        span_start = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        span_end = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=365)
        )
        dropped_registered = 0
        dropped_excluded = 0
        query_count = 0

        def retain(item: dict) -> bool:
            nonlocal dropped_registered, dropped_excluded
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                raise ScanAbort("Search returned a service item without an id.")
            if not store.mark_item_seen(item_id):
                return False
            if org_id is not None:
                reported_org = item.get("orgId")
                if reported_org not in (None, "", org_id):
                    raise ScanAbort(
                        f"Search returned out-of-organization item {item_id!r} "
                        f"(orgId={reported_org!r}, expected {org_id!r})."
                    )

            raw_url = str(item.get("url") or "").strip()
            safe_url = str(redact_token(raw_url) or "").rstrip("/")
            keywords = _type_keywords(item.get("typeKeywords"))
            if target == "portal":
                hosted = any(
                    keyword.lower() == "hosted service" for keyword in keywords
                )
                federated = bool(
                    safe_url
                    and client.policy.is_registered_federated_service(safe_url)
                )
                if not hosted and not federated:
                    dropped_registered += 1
                    return False

            # Preserve the original URL-level de-duplication order. An excluded
            # first item still claims the URL, exactly as filter-after-enumerate
            # did in the original implementation.
            url_key = safe_url.lower() if safe_url else f"<missing>:{item_id}"
            if not store.mark_url_seen(url_key):
                return False
            if item_id in excluded_ids:
                dropped_excluded += 1
                return False
            if max_items is None and len(store) >= MAX_FULL_SERVICES:
                raise ScanAbort(
                    f"The full scan exceeded its {MAX_FULL_SERVICES:,}-service "
                    "safety cap; catalog completeness cannot be guaranteed."
                )

            store.add(item_id, {
                "url": safe_url,
                "name": item.get("title") or "",
                "item_id": item_id,
                "owner": item.get("owner") or "",
                "type_keywords": keywords,
            })
            return max_items is not None and len(store) >= max_items

        def search_range(lo_ms: int, hi_ms: int) -> bool:
            nonlocal query_count
            query_count += 1
            query = f"{extra_clause} AND created:[{lo_ms} TO {hi_ms}]"
            bucket = _strict_search_bucket(client, query, consumer=retain)
            if bucket.hit_cap and hi_ms > lo_ms:
                midpoint = lo_ms + (hi_ms - lo_ms) // 2
                if midpoint <= lo_ms:
                    midpoint = lo_ms + 1
                if search_range(lo_ms, midpoint):
                    return True
                return search_range(midpoint + 1, hi_ms)
            if bucket.hit_cap:
                raise ScanAbort(
                    f"A one-millisecond search bucket [{lo_ms} TO {hi_ms}] "
                    f"reached the {DEFAULT_CAP}-item cap; catalog completeness "
                    "cannot be guaranteed."
                )
            if bucket.stopped_early:
                return True
            if query_count % 20 == 0:
                logger.info(
                    "  %d bucket queries run, %d service(s) retained.",
                    query_count, len(store),
                )
            return False

        cursor = span_start
        stop = False
        while cursor < span_end and not stop:
            if cursor.month == 12:
                next_month = cursor.replace(year=cursor.year + 1, month=1)
            else:
                next_month = cursor.replace(month=cursor.month + 1)
            stop = search_range(
                int(cursor.timestamp() * 1000),
                int(next_month.timestamp() * 1000) - 1,
            )
            cursor = next_month

        store.flush()
        if target == "portal":
            logger.info(
                "Excluded %d registered/referenced (non-hosted, "
                "non-federated) service(s).", dropped_registered,
            )
        if dropped_excluded:
            logger.info(
                "Excluded %d service(s) belonging to requested exclusion "
                "group(s).", dropped_excluded,
            )
        logger.info(
            "Service discovery complete: %d bucket query/queries, %d distinct "
            "service URL(s).", query_count, len(store),
        )
        return store
    except Exception:
        store.close()
        raise


def _scan_services(
        services: Iterable[dict],
        client: ArcGISRestClient,
        platform: str,
        target: str,
        workers: int,
        logger) -> ScanResult:
    result = ScanResult(services_discovered=len(services))
    bundle = _RULES_BY_TARGET[target]
    total = len(services)
    logger.info(
        "Scanning field names across %d service(s) with %d worker(s).",
        total, workers,
    )

    def worker(service: dict) -> ServiceOutcome:
        raw_url = service.get("url", "")
        try:
            parsed = client.policy.parse_service_url(raw_url)
        except RestCallError as exc:
            outcome = ServiceOutcome()
            outcome.add_error(f"invalid service URL {raw_url!r}: {exc}")
            return outcome
        if (not client.policy.may_send_token_to_service(parsed)
                and not client.policy.may_contact_anonymously(parsed)):
            outcome = ServiceOutcome()
            outcome.add_error(
                "refusing to contact an unregistered discovered service origin "
                f"{_origin_text(parsed.origin)}; authorize it explicitly with "
                "--anonymous-service-origin or --trusted-service-origin"
            )
            return outcome

        survey, survey_errors = _resolve_survey_flag(
            client, service.get("item_id", ""), service.get("type_keywords")
        )
        outcome = _scan_one_service_outcome(
            client,
            service.get("url", ""),
            platform,
            bundle,
            service_name=service.get("name", ""),
            owner=service.get("owner", ""),
            is_survey=survey,
            item_id=service.get("item_id", ""),
            type_keywords=service.get("type_keywords"),
        )
        for message, tls_error in survey_errors:
            outcome.add_error(message, tls_error=tls_error,
                              survey_error=True)
        return outcome

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for service, future in _bounded_future_results(
                executor, services, worker, max(workers, workers * 2)):
            result.services_scanned += 1
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = ServiceOutcome()
                outcome.add_error(
                    f"unexpected worker failure for {service.get('url', '')}: {exc}"
                )
            result.rows.extend(outcome.rows)
            result.layers_examined += outcome.layers_examined
            result.tls_errors += outcome.tls_errors
            result.survey_errors += outcome.survey_errors
            if outcome.errors:
                result.services_with_errors += 1
                result.item_errors += len(outcome.errors)
                for message in outcome.errors:
                    logger.warning("Service %r: %s", service.get("url", ""), message)
                    if len(result.error_samples) < MAX_ERROR_SAMPLES:
                        result.error_samples.append(
                            f"{service.get('item_id') or '?'}: {message}"
                        )
            if result.services_scanned % 500 == 0 or result.services_scanned == total:
                logger.info(
                    "Progress: %d / %d service(s), %d field problem(s), "
                    "%d error(s).", result.services_scanned, total,
                    len(result.rows), result.item_errors,
                )
    return result


def _run_scan_hardened(gis, target, client: ArcGISRestClient, platform,
                       test_mode, service_url, workers, logger,
                       excluded_ids) -> ScanResult:
    bundle = _RULES_BY_TARGET[target]
    if service_url:
        logger.info("Single-service mode: scanning only %r", service_url)
        outcome = _scan_one_service_outcome(
            client, service_url, platform, bundle
        )
        result = ScanResult(
            rows=outcome.rows,
            services_discovered=1,
            services_scanned=1,
            services_with_errors=1 if outcome.errors else 0,
            item_errors=len(outcome.errors),
            tls_errors=outcome.tls_errors,
            survey_errors=outcome.survey_errors,
            layers_examined=outcome.layers_examined,
            error_samples=outcome.errors[:MAX_ERROR_SAMPLES],
        )
        for message in outcome.errors:
            logger.warning("Single service: %s", message)
        return result

    services = _enumerate_services_strict(
        gis,
        target,
        client,
        logger,
        excluded_ids,
        TEST_LIMIT if test_mode else None,
    )
    try:
        if test_mode:
            logger.info(
                "TEST MODE: scanning the first %d post-exclusion service(s). "
                "Pass --no-test for a full scan.", len(services),
            )
        return _scan_services(
            services, client, platform, target, workers, logger
        )
    finally:
        services.close()

def run_scan(gis, target, session, token_provider, platform, test_mode,
             service_url, workers, logger, excluded_ids):
    """Backward-compatible orchestration API; returns the original row list."""
    try:
        safe_workers = int(workers)
    except (TypeError, ValueError) as exc:
        raise ValueError("workers must be an integer") from exc
    if not 1 <= safe_workers <= MAX_WORKERS:
        raise ValueError(f"workers must be between 1 and {MAX_WORKERS}")

    policy = TrustedUrlPolicy(
        _portal_base_url(gis), target, (), logger, anonymous_origins=()
    )
    client = ArcGISRestClient(
        FixedSessionPool(session), token_provider, policy, logger
    )
    if target == "portal":
        _configure_federation_policy(gis, client, logger)
    result = _run_scan_hardened(
        gis, target, client, platform, test_mode, service_url, safe_workers,
        logger, excluded_ids,
    )
    if result.incomplete:
        logger.warning(
            "Backward-compatible run_scan() completed INCOMPLETE with %d "
            "check error(s).", result.item_errors,
        )
    return result.rows


def _field_name_word_counts(rows):
    counter = Counter(
        r.get("field_name", "") for r in rows if r.get("field_name")
    )
    return [
        {"field_name": name, "count": count}
        for name, count in sorted(
            counter.items(), key=lambda kv: (-kv[1], kv[0].lower())
        )
    ]

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan Portal/AGOL feature-service field names for reserved "
                    "keywords (per DB vendor) and ESRI field-name rule "
                    "violations. Test mode (first %d services) is the default. "
                    "TLS verification is explicit for direct REST calls and "
                    "the ArcGIS SDK connection." % TEST_LIMIT
    )
    add_config_arg(parser)
    add_target_arg(parser)
    add_common_portal_args(parser)
    add_email_args(parser)
    add_output_dir_arg(parser)
    add_exclude_group_arg(parser)
    add_test_flag(
        parser,
        help_text=(f"Disable test mode. By DEFAULT the scan is limited to the "
                   f"first {TEST_LIMIT} services; pass --no-test to scan the "
                   f"entire deployment."))
    parser.add_argument(
        "--service-url", default=None,
        help="Optional. Scan ONLY this single service. Provide the REST "
             "endpoint of the service root (…/FeatureServer or …/MapServer) or "
             "a single sub-layer (…/FeatureServer/3). Test mode, full "
             "enumeration, and --exclude-group-id are ignored when this is set.")
    parser.add_argument(
        "--trusted-service-origin",
        action="append",
        default=[],
        metavar="HTTPS_ORIGIN",
        help="Optional additional exact HTTPS origin authorized to receive the "
             "Portal token in single-service or unusual reverse-proxy "
             "topologies. May be repeated. Normal hosted/federated origins are "
             "learned automatically.")
    parser.add_argument(
        "--anonymous-service-origin",
        action="append",
        default=[],
        metavar="HTTPS_ORIGIN",
        help="Optional exact HTTPS origin that discovered service items may be "
             "queried anonymously. May be repeated. Unregistered origins are "
             "otherwise not contacted automatically.")
    parser.add_argument(
        "--workers", type=_bounded_int("--workers", 1, MAX_WORKERS),
        default=DEFAULT_WORKERS,
        help=f"Concurrent workers for per-service reads (default "
             f"{DEFAULT_WORKERS}; maximum {MAX_WORKERS}).")

    tls_group = parser.add_argument_group(
        "TLS verification (explicit, required choice)")
    tls_group.add_argument(
        "--tls-mode",
        choices=sorted(_TLS_MODE_CHOICES.keys()),
        default="standard",
        help="How direct REST calls and the ArcGIS SDK connection verify TLS. "
             "'standard' uses the public CA set; 'os-trust-store' uses the "
             "operating-system trust store; 'custom-ca-bundle' uses the PEM "
             "file supplied by --ca-bundle-path.")
    tls_group.add_argument(
        "--ca-bundle-path",
        default=None,
        help="Path to a PEM CA bundle. Required only for "
             "--tls-mode custom-ca-bundle.")
    return parser.parse_args()


def _send_abort_email(subject: str, reason: str, log_path: str,
                      args, start_wallclock, start_perf, logger) -> None:
    safe_reason = redact_token(str(reason))
    body = (
        f"Feature service field-name scan ABORTED.\n\n"
        f"Organization: {args.org_name}\n"
        f"Target: {args.target.upper()}\n"
        f"Reason: {safe_reason}\n\n"
        f"{timing_block(start_wallclock, start_perf)}"
    )
    send_email(subject, body, [log_path], args.smtp_server, args.email_to,
               args.email_from, logger)


def _run(args, run_stamp: str, log_path: str, logger,
         start_wallclock, start_perf) -> int:
    platform = get_platform(args.portal_url)
    _validate_target_platform(args.target, platform)
    max_email_mb = _validate_email_cap(
        get_max_email_mb(args.target, args.config)
    )
    subject = subject_line(SUBJECT_PREFIX, run_stamp, platform,
                           org_name=args.org_name)

    logger.info("=" * 70)
    logger.info("Feature Service Field Name Reserved-Keyword / Rule Scan - run started")
    logger.info("Organization      : %s", args.org_name)
    logger.info("Target            : %s", args.target.upper())
    logger.info("Platform          : %s", platform)
    logger.info("Mode              : %s", "SINGLE SERVICE" if args.service_url
                else ("TEST (first %d)" % TEST_LIMIT if args.test else "FULL"))
    logger.info("TLS mode          : %s", args.tls_mode)
    if args.tls_mode == "custom-ca-bundle":
        logger.info("CA bundle path    : %s", args.ca_bundle_path)
    logger.info("Max email MB      : %s", max_email_mb)
    logger.info("Worker threads    : %d", args.workers)
    logger.info("Exclude group ID  : %r", args.exclude_group_id or "(none)")
    logger.info("=" * 70)

    mode = _TLS_MODE_CHOICES[args.tls_mode]
    ca_path = args.ca_bundle_path if mode is VerifyMode.CUSTOM_CA_BUNDLE else None
    base_session = None
    worker_sessions = None
    try:
        try:
            base_session = build_secure_session(
                mode,
                ca_bundle_path=ca_path,
                pool_connections=max(8, args.workers),
                pool_maxsize=max(8, args.workers),
                retry_total=2,
                logger=logger,
            )
            handshake_ok, handshake_detail = verify_tls_handshake(
                args.portal_url, base_session, logger=logger
            )
        except Exception as exc:
            raise ScanAbort(
                f"Could not establish TLS verification strategy "
                f"{args.tls_mode!r}: {redact_token(str(exc))}"
            ) from exc
        if not handshake_ok:
            raise ScanAbort(
                f"TLS handshake verification to {args.portal_url} failed: "
                f"{redact_token(str(handshake_detail.get('error')))}"
            )

        try:
            gis = connect_gis(
                args.portal_url,
                args.username,
                args.keyring_service,
                logger,
                verify_mode=mode,
                ca_bundle_path=ca_path,
            )
        except Exception as exc:
            raise ScanAbort(
                f"Could not connect to Portal/AGOL: {redact_token(str(exc))}"
            ) from exc

        token_provider = provider_for_gis(gis, platform)
        logger.info("Token acquired via %s (token present: %s)",
                    type(token_provider).__name__, token_provider.is_present())
        tune_gis_session(gis, args.workers)

        portal_base = _portal_base_url(gis)
        policy = TrustedUrlPolicy(
            portal_base,
            args.target,
            args.trusted_service_origin,
            logger,
            anonymous_origins=args.anonymous_service_origin,
        )
        worker_sessions = WorkerSessionPool(mode, ca_path)
        client = ArcGISRestClient(worker_sessions, token_provider, policy, logger)

        if args.target == "portal":
            _configure_federation_policy(gis, client, logger)
        else:
            logger.info("Trusted token destination(s): %s",
                        policy.render_trusted_destinations())
        logger.info("Explicit anonymous service origin(s): %s",
                    policy.render_anonymous_destinations())

        if args.service_url:
            if args.exclude_group_id:
                logger.info("--exclude-group-id is ignored in single-service mode.")
            excluded_ids = set()
        else:
            excluded_ids = _resolve_excluded_item_ids_strict(
                gis, args.exclude_group_id, logger
            )

        scan_result = _run_scan_hardened(
            gis,
            args.target,
            client,
            platform,
            test_mode=args.test,
            service_url=args.service_url,
            workers=args.workers,
            logger=logger,
            excluded_ids=excluded_ids,
        )

        platform_prefix = str(platform).upper()
        csv_path = os.path.join(
            args.output_dir,
            f"{args.org_name_safe}_{platform_prefix}_FIELD_NAME_SCAN_"
            f"{args.target.upper()}_{run_stamp}.csv",
        )
        fieldnames = [
            "platform", "title", "service_url", "owner", "layer_id",
            "layer_name", "field_name", "field_alias", "is_survey123",
            "problem",
        ]
        written = write_dict_csv(
            scan_result.rows,
            csv_path,
            fieldnames=fieldnames,
            logger=logger,
            label="field problem",
            force_values={"platform": platform},
        )

        word_counts = _field_name_word_counts(scan_result.rows)
        word_count_csv_path = os.path.join(
            args.output_dir,
            f"{args.org_name_safe}_{platform_prefix}_FIELD_NAME_SCAN_"
            f"{args.target.upper()}_{run_stamp}_WORD_COUNTS.csv",
        )
        word_count_written = write_dict_csv(
            word_counts,
            word_count_csv_path,
            fieldnames=["field_name", "count"],
            logger=logger,
            label="field-name word count",
        )

        status = "INCOMPLETE" if scan_result.incomplete else "COMPLETE"
        logger.info("=" * 70)
        logger.info("RUN SUMMARY - %s", status)
        logger.info("Services discovered : %d", scan_result.services_discovered)
        logger.info("Services scanned    : %d", scan_result.services_scanned)
        logger.info("Layers examined     : %d", scan_result.layers_examined)
        logger.info("Field problems      : %d", len(scan_result.rows))
        logger.info("Distinct field names: %d", len(word_counts))
        logger.info("Services with errors: %d", scan_result.services_with_errors)
        logger.info("Item/check errors   : %d", scan_result.item_errors)
        logger.info("Survey lookup errors: %d", scan_result.survey_errors)
        logger.info("TLS errors          : %d", scan_result.tls_errors)
        logger.info("=" * 70)

        csv_paths = [value for value in (written, word_count_written) if value]
        attachments, note = gather_attachments(
            log_path, csv_paths, args.output_dir, logger,
            cap_mb=max_email_mb, label="CSV",
        )

        completion_subject = subject
        if scan_result.incomplete:
            completion_subject = subject_line(
                SUBJECT_PREFIX + " (INCOMPLETE)", run_stamp, platform,
                org_name=args.org_name,
            )
        no_findings_text = (
            "(none confirmed; run incomplete -- absence is not proof of compliance)"
            if scan_result.incomplete else "(none - no problems found)"
        )
        body = (
            f"Feature service field-name scan {status}.\n\n"
            f"Organization        : {args.org_name}\n"
            f"Target              : {args.target.upper()}\n"
            f"Platform            : {platform}\n"
            f"TLS mode            : {args.tls_mode}\n"
            f"Mode                : "
            f"{'SINGLE SERVICE' if args.service_url else ('TEST (first %d)' % TEST_LIMIT if args.test else 'FULL')}\n"
            f"Excluded group(s)   : {args.exclude_group_id or '(none)'}\n"
            f"Services discovered : {scan_result.services_discovered}\n"
            f"Services scanned    : {scan_result.services_scanned}\n"
            f"Layers examined     : {scan_result.layers_examined}\n"
            f"Field problems      : {len(scan_result.rows)}\n"
            f"Distinct field names: {len(word_counts)}\n"
            f"Services with errors: {scan_result.services_with_errors}\n"
            f"Item/check errors   : {scan_result.item_errors}\n"
            f"Survey lookup errors: {scan_result.survey_errors}\n"
            f"TLS errors          : {scan_result.tls_errors}\n"
            f"Detail CSV          : {csv_path if written else no_findings_text}\n"
            f"Word-count CSV      : {word_count_csv_path if word_count_written else no_findings_text}\n\n"
        )
        if scan_result.incomplete:
            body += (
                "WARNING: One or more services/layers could not be fully read. "
                "Confirmed findings are retained, but missing rows must not be "
                "interpreted as proof that those services comply. See the log.\n\n"
            )
            if scan_result.error_samples:
                body += "First error sample(s):\n" + "\n".join(
                    f"  {sample}" for sample in scan_result.error_samples[:10]
                ) + "\n\n"
        body += timing_block(start_wallclock, start_perf) + note
        send_email(
            completion_subject, body, attachments, args.smtp_server,
            args.email_to, args.email_from, logger,
        )

        if scan_result.incomplete:
            logger.warning(
                "FIELD_NAME_SCAN completed with %d item/check error(s); "
                "returning exit status 0 because the scan ran to completion. "
                "The report remains marked INCOMPLETE because those services "
                "could not be fully classified.",
                scan_result.item_errors,
            )
        else:
            logger.info("FIELD_NAME_SCAN finished COMPLETE.")
        return 0
    finally:
        if worker_sessions is not None:
            worker_sessions.close()
        if base_session is not None:
            try:
                base_session.close()
            except Exception:
                pass


def main() -> int:
    args = parse_args()
    if args.tls_mode == "custom-ca-bundle" and not args.ca_bundle_path:
        print("--ca-bundle-path is required when --tls-mode is "
              "custom-ca-bundle.", file=sys.stderr)
        return 2

    finalize_config(
        args,
        required=(
            "portal-url", "username", "keyring-service", "output-dir",
            "smtp-server", "email-to", "email-from",
        ),
    )
    ensure_output_dir(args.output_dir)
    timestamp = build_timestamp()
    try:
        run_stamp, log_path = _reserve_run_stamp(
            args.output_dir, args.org_name_safe, args.target, timestamp
        )
    except Exception as exc:
        print(f"Run ABORTED. Could not reserve output paths: {exc}",
              file=sys.stderr)
        return 1

    try:
        logger = setup_logging(log_path, LOGGER_NAME)
    except Exception as exc:
        try:
            os.remove(log_path)
        except OSError:
            pass
        print(
            f"Run ABORTED. Could not configure logging at {log_path!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    start_wallclock, start_perf = start_timer()
    provisional_platform = get_platform(args.portal_url)
    provisional_subject = subject_line(
        SUBJECT_PREFIX + " (ABORTED)", run_stamp, provisional_platform,
        org_name=args.org_name,
    )
    try:
        return _run(
            args, run_stamp, log_path, logger, start_wallclock, start_perf
        )
    except ScanAbort as exc:
        safe = redact_token(str(exc))
        logger.error("Run ABORTED: %s", safe)
        _send_abort_email(
            provisional_subject, safe, log_path, args,
            start_wallclock, start_perf, logger,
        )
        return 1
    except Exception as exc:
        safe = redact_token(str(exc))
        logger.exception("Unexpected fatal error: %s", safe)
        _send_abort_email(
            provisional_subject,
            f"Unexpected fatal error: {safe}",
            log_path,
            args,
            start_wallclock,
            start_perf,
            logger,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
