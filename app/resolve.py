"""Turn whatever the caller passed in into a LinkedIn public id.

Accepts more shapes than the obvious one because callers paste what they have:
country subdomains, tracking query strings, Sales Navigator URLs, `/comm/in/`
links out of LinkedIn emails, or just the bare slug.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .errors import InvalidProfileURL

# Vanity slugs are percent-encoded when they contain non-Latin characters, so
# `%` has to survive into the id and be decoded afterwards.
_SLUG_RE = re.compile(r"^[A-Za-z0-9\-_%\.]+$")

_PROFILE_PREFIXES = (
    "/in/",
    "/comm/in/",
    "/pub/",
    "/profile/view",
)

_SALES_PREFIXES = (
    "/sales/people/",
    "/sales/profile/",
    "/sales/lead/",
)

_SEARCH_MARKERS = ("/search/results/", "/sales/search/")


def _strip_slug(raw: str) -> str:
    """Take the first path segment and drop any trailing junk."""
    slug = raw.split("/")[0].split("?")[0].split("#")[0].strip()
    return unquote(slug)


def extract_public_id(value: str) -> str:
    """Resolve a profile URL, or a bare public id, to the public id.

    Raises InvalidProfileURL when the input is not a member profile — notably
    for search URLs and company pages, which are common paste mistakes and
    deserve a clearer message than a downstream 404.
    """
    if not value or not value.strip():
        raise InvalidProfileURL("No profile URL supplied.")

    value = value.strip()

    # Bare slug, e.g. "williamhgates".
    if "/" not in value and "." not in value and _SLUG_RE.match(value):
        return unquote(value)

    candidate = value if "//" in value else f"https://{value}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "linkedin.com" not in host:
        raise InvalidProfileURL(f"Not a LinkedIn URL: {value!r}")

    if any(marker in path for marker in _SEARCH_MARKERS):
        raise InvalidProfileURL(
            "That is a LinkedIn search URL, not a profile URL. Supply a single "
            "member profile such as https://www.linkedin.com/in/<public-id>."
        )

    if path.startswith("/company/") or path.startswith("/school/"):
        raise InvalidProfileURL(
            "That is a company or school page. This API returns member profiles only."
        )

    # Sales Navigator: /sales/people/<id>,<auth-token>,<...>  ->  the leading id.
    for prefix in _SALES_PREFIXES:
        if path.startswith(prefix):
            tail = path[len(prefix) :]
            first = tail.split("/")[0].split(",")[0]
            if first:
                return unquote(first)
            raise InvalidProfileURL(f"Could not read a member id from {value!r}")

    for prefix in _PROFILE_PREFIXES:
        if path.startswith(prefix):
            tail = path[len(prefix) :]
            slug = _strip_slug(tail)
            if slug:
                return slug
            raise InvalidProfileURL(f"Could not read a public id from {value!r}")

    raise InvalidProfileURL(
        f"Unrecognised LinkedIn URL: {value!r}. Expected a member profile URL such "
        "as https://www.linkedin.com/in/<public-id>."
    )


def canonical_profile_url(public_id: str) -> str:
    return f"https://www.linkedin.com/in/{public_id}"
