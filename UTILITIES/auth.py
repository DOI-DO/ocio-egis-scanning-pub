"""
UTILITIES/auth.py
==============================================================================
Credential holding plus centralized text redaction.

TokenProvider keeps Portal/AGOL tokens out of accidental repr()/str() output.
redact_sensitive_data() is the output-boundary safety net for arbitrary text
that may contain credentials, including rendered exceptions and request URLs.
It recognizes the credential forms used by this project:

  * ArcGIS/OAuth token, access_token, refresh_token, and related parameters
  * Azure SAS signatures (sig=), SAS values, account keys, and connection
    strings
  * Authorization / Proxy-Authorization header values, including Bearer and
    Basic credentials
  * password/passwd/pwd, client secrets, API keys, and secret access keys
  * credentials embedded in URLs such as
        postgresql://user:password@database.example/db

redact_token() remains as a backward-compatible alias. Existing callers gain
the broader protection without source changes.

IMPORTANT BOUNDARY RULE
-----------------------
Logging configured by UTILITIES.logging_setup sanitizes the fully rendered
message, exception traceback, and stack text. Text written directly to email,
CSV, JSON, or another file does not pass through logging, so those call sites
must explicitly call redact_sensitive_data() first.

Public API
----------
    TokenProvider
    PortalGISTokenProvider
    AgolGISTokenProvider
    StaticTokenProvider
    provider_for_gis
    redact_sensitive_data(text) -> str
    redact_token(text) -> str      # backward-compatible alias
==============================================================================
"""

import abc
import re


# Provider repr()/str() intentionally use the historical lowercase spelling.
_REDACTED = "<redacted>"
_TEXT_REDACTED = "<REDACTED>"
_REDACTION_FAILURE = "<REDACTION-FAILED: ORIGINAL TEXT SUPPRESSED>"

# Exact key names and common separator variations. The pattern is inserted
# only into compiled expressions below; callers never supply regex fragments.
_SENSITIVE_KEY_PATTERN = (
    r"(?:"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"oauth[_-]?token|session[_-]?token|bearer[_-]?token|token|"
    r"sig|sas(?:[_-]?token)?|shared[_-]?access[_-]?signature|"
    r"password|passwd|pwd|client[_-]?secret|secret|"
    r"x[_-]?api[_-]?key|api[_-]?key|apikey|"
    r"account[_-]?key|storage[_-]?key|secret[_-]?access[_-]?key|"
    r"private[_-]?key|connection[_-]?string|database[_-]?url|db[_-]?url"
    r")"
)

# Generic URI user-info protection. This covers PostgreSQL, SQLAlchemy,
# SQL Server, Oracle, MySQL, MongoDB, HTTP basic-auth URLs, and similar forms.
# The username is retained for diagnostics; only the password is replaced.
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)"
    r"(?P<username>[^/\s:@]+):(?P<password>[^/\s@]*)@"
)

# Authorization values are always credentials, regardless of auth scheme.
# Quoted dict/JSON forms and ordinary header-line forms are both handled.
_AUTHORIZATION_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?
        (?<![A-Za-z0-9_])
        (?:proxy[-_])?authorization
        (?![A-Za-z0-9_])
        ["']?
        \s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        \{[^}]*\}
        |
        [^&\r\n,;\]\x7d]+
    )
    """
)

# Query strings, JSON/dict representations, DSNs, connection strings, and
# key=value / key:value text. The final alternative intentionally includes
# '<' and '>' so applying the redactor repeatedly is idempotent.
_KEY_VALUE_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        ["']?
        (?<![A-Za-z0-9_])
        (?P<key>""" + _SENSITIVE_KEY_PATTERN + r""")
        (?![A-Za-z0-9_])
        ["']?
        \s*[:=]\s*
    )
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        \{[^}]*\}
        |
        [^&\s,;)\]\x7d]+
    )
    """
)

# Command-line forms such as '--password value' and '-api-key value'. Forms
# using '=' or ':' are already handled by _KEY_VALUE_RE.
_CLI_SECRET_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![A-Za-z0-9_])
        --?""" + _SENSITIVE_KEY_PATTERN + r"""
        (?![A-Za-z0-9_])
        \s+
    )
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        \{[^}]*\}
        |
        \S+
    )
    """
)


def _replacement_for_value(value):
    """Preserve matching quote/braces around a redacted value."""
    if len(value) >= 2:
        pairs = {'"': '"', "'": "'", "{": "}"}
        closing = pairs.get(value[0])
        if closing and value[-1] == closing:
            return f"{value[0]}{_TEXT_REDACTED}{closing}"
    return _TEXT_REDACTED


def _replace_named_value(match):
    return match.group("prefix") + _replacement_for_value(match.group("value"))


def _replace_url_userinfo(match):
    return (
        match.group("scheme")
        + match.group("username")
        + ":"
        + _TEXT_REDACTED
        + "@"
    )


def redact_sensitive_data(text):
    """
    Redact recognized credentials from arbitrary text.

    The operation is case-insensitive, idempotent, and safe to call on text
    that contains no credential. Non-string values are returned unchanged for
    backward compatibility. An unexpected internal redaction failure fails
    closed by suppressing the original string instead of returning it.

    This is intended for rendered output: exception text, log messages, email
    bodies, CSV error cells, command output, and diagnostic URLs. It does not
    replace the primary control of avoiding credential-bearing strings in the
    first place.
    """
    if not isinstance(text, str) or not text:
        return text

    try:
        redacted = _AUTHORIZATION_RE.sub(_replace_named_value, text)
        redacted = _URL_USERINFO_RE.sub(_replace_url_userinfo, redacted)
        redacted = _KEY_VALUE_RE.sub(_replace_named_value, redacted)
        redacted = _CLI_SECRET_RE.sub(_replace_named_value, redacted)
        return redacted
    except Exception:
        # Security boundary: never return the original string when the
        # sanitizer itself encounters an unexpected failure.
        return _REDACTION_FAILURE


def redact_token(text):
    """
    Backward-compatible alias for redact_sensitive_data().

    Historically this function removed only token=<value>. It now applies the
    complete credential redaction policy so every existing call site also
    protects SAS signatures, OAuth tokens, Authorization headers, passwords,
    API/client secrets, account keys, and credential-bearing URLs.
    """
    return redact_sensitive_data(text)


class TokenProvider(abc.ABC):
    """
    Abstract base. Holds a token value privately; exposes it ONLY via
    get()/auth_params(). repr()/str() NEVER include the value, so logging,
    debugging, or dumping this object by accident cannot leak the token.
    """

    def __init__(self):
        self._token = None

    # -- the ONLY way to get the actual value out ------------------------
    def get(self):
        """
        Return the current raw token string. Callers should call this as
        LATE as possible -- immediately before building the outgoing
        request -- and must never pass the return value to a logger, an
        email body, or a CSV cell.
        """
        if not self._token:
            self.refresh()
        return self._token

    def auth_params(self):
        """
        Convenience for REST call sites: {"token": <value>} ready to merge
        into a params/data dict, e.g.:
            merged = {"f": "json", **provider.auth_params()}
        Exists so call sites read `provider.auth_params()` instead of
        `{"token": provider.get()}` repeated everywhere -- purely for
        readability; it still calls get() under the hood.
        """
        return {"token": self.get()}

    def is_present(self):
        """True if a token value is currently held (without exposing it).
        Safe to log: `logger.info("token present: %s", provider.is_present())`."""
        return bool(self._token)

    @abc.abstractmethod
    def refresh(self):
        """
        Subclasses implement how to (re)acquire the token. Must set
        self._token. Called automatically by get() when no token is held
        yet; callers may also call it explicitly after a 403/token-expired
        response to force re-acquisition.
        """
        raise NotImplementedError

    # -- safety net: NEVER show the token, even by accident ---------------
    def __repr__(self):
        return f"{self.__class__.__name__}(token={_REDACTED}, present={self.is_present()})"

    def __str__(self):
        return self.__repr__()

    def __format__(self, spec):
        # Blocks f"{provider}" / "{}".format(provider) from ever rendering
        # the token even if a caller supplies a format spec.
        return self.__repr__()


class PortalGISTokenProvider(TokenProvider):
    """
    Wraps an authenticated arcgis.gis.GIS connected to an Enterprise Portal.
    The SDK already manages Portal token renewal internally (via gis._con);
    refresh() simply re-reads its CURRENT value rather than this class
    caching one snapshot forever, so a mid-run SDK-driven renewal is picked
    up on the next get() after a refresh() call.
    """

    def __init__(self, gis):
        super().__init__()
        self._gis = gis
        self.refresh()

    def refresh(self):
        self._token = self._gis._con.token


class AgolGISTokenProvider(TokenProvider):
    """
    Wraps an authenticated arcgis.gis.GIS connected to ArcGIS Online. Kept
    as a DISTINCT subclass from PortalGISTokenProvider (rather than one
    shared "GISTokenProvider") because AGOL and Enterprise Portal acquire
    and refresh tokens via different underlying REST flows; today both
    simply proxy the SDK's own token, but this gives each platform its own
    place to grow platform-specific refresh/retry/backoff behavior later
    without branching on platform inside a single shared class.
    """

    def __init__(self, gis):
        super().__init__()
        self._gis = gis
        self.refresh()

    def refresh(self):
        self._token = self._gis._con.token


class StaticTokenProvider(TokenProvider):
    """
    Wraps a bare token string obtained some other way (e.g. a script that
    only has a token already, not a `gis` object to read one from). This
    exists so there is ALWAYS a TokenProvider-shaped way to hand a token to
    the rest of the code -- callers should never be tempted to bypass the
    wrapper "just this once" and pass a raw string into a function that
    expects a provider. refresh() is a no-op: a static token cannot renew
    itself; callers needing renewal should use a GIS-backed provider instead.
    """

    def __init__(self, token_str, label="static"):
        super().__init__()
        self._token = token_str
        self._label = label

    def refresh(self):
        # Nothing to do -- a static token has no renewal mechanism. If it
        # has expired, get() will keep returning the same (now-invalid)
        # value; the caller is responsible for constructing a new provider.
        return

    def __repr__(self):
        return (f"{self.__class__.__name__}(label={self._label!r}, "
               f"token={_REDACTED}, present={self.is_present()})")


def provider_for_gis(gis, platform_label):
    """
    Convenience factory: build the right TokenProvider subclass for an
    already-connected `gis`, given a PLATFORM label string ("PORTAL" or
    "AGOL", e.g. from restutils.get_platform()). Falls back to
    PortalGISTokenProvider for any unrecognized label, since Enterprise
    Portal is the more common non-AGOL case in this project.
    """
    if str(platform_label).strip().upper() == "AGOL":
        return AgolGISTokenProvider(gis)
    return PortalGISTokenProvider(gis)