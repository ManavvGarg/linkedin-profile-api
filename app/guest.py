"""Logged-out fallback. Deliberately limited, and off by default.

What this can actually recover, measured directly against live profiles:
name, headline (truncated), location, profile photo, follower count, the first
employer, and the first school with its year range.

What it cannot recover, ever: job titles, position dates, the full About text,
skills, certifications, and languages. LinkedIn redacts these *server-side* for
logged-out clients — the values are not in the HTML at all. They arrive as runs
of asterisks that preserve the original character count and word boundaries:

    "jobTitle": ["******** *** ***", "****** ***** ** ********"]
    <h4><p class="blur" aria-hidden="true">********</p></h4>

("Chairman and CEO" -> 8/3/3 characters, exactly.) So no amount of parsing
skill recovers them; the information was removed before it was sent.

The rate ceiling is the harder limit. Measured on a clean residential IP: around
five to ten profile fetches, then HTTP 999 for **more than two hours**, scoped
to the IP rather than the host or the session. Backing off does not clear it,
and every country subdomain blocks simultaneously. That makes this path
unusable as the primary source for a hosted API — a handful of requests would
take the whole deployment down for hours.

It is kept because partial data with honest provenance sometimes beats an error,
but it must be opted into via ENABLE_GUEST_FALLBACK.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from curl_cffi import requests as crequests

from .config import Settings
from .errors import ProfileNotFound, UpstreamBlocked
from .models import (
    Company,
    Counts,
    DateRange,
    Education,
    Experience,
    Image,
    ImageVariant,
    Location,
    PartialDate,
    Profile,
)
from .session import browser_headers

logger = logging.getLogger(__name__)

_LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_MASK_RE = re.compile(r"^[\s*]*$")


def is_masked(value: Any) -> bool:
    """True when LinkedIn replaced the text with asterisks."""
    return isinstance(value, str) and bool(value) and bool(_MASK_RE.match(value))


def _clean(value: Any) -> str | None:
    """Return real text, or None when the value is a redaction placeholder."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or is_masked(value):
        return None
    return value


def fetch_guest_profile(public_id: str, settings: Settings) -> tuple[Profile, list[str]]:
    """Fetch and parse the logged-out profile page."""
    warnings: list[str] = []

    # Country subdomains sit behind a different CDN than `www` and apply looser
    # filtering, so they answer where `www` will not — particularly from
    # datacenter IPs, where `www` refuses unconditionally.
    hosts = ("www.linkedin.com", "in.linkedin.com", "uk.linkedin.com")
    html: str | None = None
    last_status: int | None = None

    for host in hosts:
        url = f"https://{host}/in/{public_id}"
        kwargs: dict[str, Any] = {
            "headers": browser_headers(settings.linkedin_user_agent),
            "timeout": settings.request_timeout_seconds,
            "impersonate": settings.impersonate,
            "allow_redirects": True,
        }
        if settings.proxy_url:
            kwargs["proxies"] = {"http": settings.proxy_url, "https": settings.proxy_url}

        try:
            response = crequests.get(url, **kwargs)
        except Exception as exc:
            logger.warning("Guest fetch failed for %s: %s", host, exc)
            continue

        last_status = response.status_code
        if response.status_code == 200 and response.text:
            html = response.text
            if host != "www.linkedin.com":
                warnings.append(f"Recovered via {host}; www.linkedin.com refused the request.")
            break

    if html is None:
        raise UpstreamBlocked(
            "Every logged-out host refused the request "
            f"(last status {last_status}). LinkedIn blocks an IP for hours after "
            "roughly five guest profile fetches, and backing off does not clear it.",
            detail={"last_status": last_status},
        )

    person = _extract_person(html)
    if person is None:
        raise ProfileNotFound(
            "The logged-out page carried no Person structured data.",
            detail={"public_id": public_id},
        )

    warnings.append(
        "Served from the logged-out profile page. LinkedIn redacts job titles, "
        "position dates, the full About text, skills, certifications and languages "
        "for anonymous clients — those fields are absent rather than empty."
    )

    return _person_to_profile(person, public_id, html), warnings


def _extract_person(html: str) -> dict[str, Any] | None:
    """Pull the `Person` node out of the page's single JSON-LD block."""
    for block in _LD_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Person":
                return node
    return None


def _person_to_profile(person: dict[str, Any], public_id: str, html: str) -> Profile:
    name = _clean(person.get("name"))
    first = last = None
    if name:
        parts = name.split(None, 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else None

    address = person.get("address") if isinstance(person.get("address"), dict) else {}
    image = person.get("image") if isinstance(person.get("image"), dict) else {}
    photo_url = _clean(image.get("contentUrl"))

    followers = None
    stat = person.get("interactionStatistic")
    if isinstance(stat, dict):
        count = stat.get("userInteractionCount")
        if isinstance(count, int):
            followers = count

    # `description` is truncated with an ellipsis but is NOT masked, which makes
    # it the richest single field available anonymously.
    headline = _clean(person.get("description")) or _clean(
        person.get("disambiguatingDescription")
    )

    return Profile(
        public_id=public_id,
        urn=None,
        profile_url=f"https://www.linkedin.com/in/{public_id}",
        first_name=first,
        last_name=last,
        full_name=name,
        headline=headline,
        summary=None,  # gated behind the login wall
        location=Location(
            text=_clean(address.get("addressLocality")),
            country_code=_clean(address.get("addressCountry")),
        )
        if address
        else None,
        profile_picture=Image(url=photo_url, variants=[ImageVariant(url=photo_url)])
        if photo_url
        else None,
        counts=Counts(followers=followers),
        experience=_guest_experience(person),
        education=_guest_education(person),
    )


def _guest_experience(person: dict[str, Any]) -> list[Experience]:
    """Employers, with titles dropped because they are always redacted.

    Position order is preserved even where names are masked, so an entry with a
    null company still carries the fact that a role existed — which is more
    honest than silently shortening the list.
    """
    out: list[Experience] = []
    works_for = person.get("worksFor")
    if not isinstance(works_for, list):
        return out

    for org in works_for:
        if not isinstance(org, dict):
            continue
        company_name = _clean(org.get("name"))
        member = org.get("member") if isinstance(org.get("member"), dict) else {}
        out.append(
            Experience(
                title=None,  # always masked on the logged-out page
                company=Company(
                    name=company_name,
                    linkedin_url=_clean(org.get("url")),
                ),
                location=_clean(org.get("location")),
                description=_clean(member.get("description")),
                date_range=_guest_date_range(member),
            )
        )
    return out


def _guest_education(person: dict[str, Any]) -> list[Education]:
    """Schools. Unlike positions, these keep their year ranges."""
    out: list[Education] = []
    alumni_of = person.get("alumniOf")
    if not isinstance(alumni_of, list):
        return out

    for school in alumni_of:
        if not isinstance(school, dict):
            continue
        member = school.get("member") if isinstance(school.get("member"), dict) else {}
        out.append(
            Education(
                school_name=_clean(school.get("name")),
                school_linkedin_url=_clean(school.get("url")),
                description=_clean(member.get("description")),
                date_range=_guest_date_range(member),
            )
        )
    return out


def _guest_date_range(member: dict[str, Any]) -> DateRange | None:
    """Read JSON-LD dates, which are bare years or `YYYY-MM` strings."""

    def parse(value: Any) -> PartialDate | None:
        if isinstance(value, int):
            return PartialDate(year=value)
        if isinstance(value, str) and value.strip():
            parts = value.strip().split("-")
            try:
                year = int(parts[0])
            except ValueError:
                return None
            month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            return PartialDate(year=year, month=month)
        return None

    start = parse(member.get("startDate"))
    end = parse(member.get("endDate"))
    if start is None and end is None:
        return None
    return DateRange(start=start, end=end, is_current=start is not None and end is None)


def count_redactions(html: str) -> int:
    """How many redacted text nodes the page carried — useful for diagnostics."""
    return len(re.findall(r'class="blur"', html)) + len(
        re.findall(r">\s*\*{3,}\s*<", html)
    )
