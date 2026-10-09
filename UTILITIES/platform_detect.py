"""Map a portal URL to a PLATFORM label used across output/logging/CSV."""

# Known portal URLs -> PLATFORM label.
PLATFORM_PORTAL_URL = "https://enterprise.com/portal"
PLATFORM_AGOL_URL = "https://org.maps.arcgis.com/home"

_PLATFORM_BY_URL = {
    PLATFORM_PORTAL_URL: "PORTAL",
    PLATFORM_AGOL_URL: "AGOL",
}


def get_platform(portal_url):
    """
    Return the PLATFORM label ('PORTAL' or 'AGOL') for a given portal URL.

    Matching ignores a trailing slash and surrounding whitespace. If the URL is
    not one of the two known values, 'UNKNOWN' is returned so downstream output
    still has a value in the PLATFORM column.
    """
    normalized = (portal_url or "").strip().rstrip("/")
    for url, label in _PLATFORM_BY_URL.items():
        if normalized == url.rstrip("/"):
            return label
    return "UNKNOWN"