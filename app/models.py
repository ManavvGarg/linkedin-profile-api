"""The public response schema.

Four decisions here are direct reactions to how the incumbent scrapers get this
wrong (see README "Schema design"):

1. Nested, not flattened. PhantomBuster's CSV keeps only the current and one
   previous role, so a ten-job career silently becomes two. Flattening is an
   export concern; it does not belong in the API contract.
2. Structured dates. `{"year": 2021, "month": 1}` rather than
   `"Jan 2021 - Present"`, so every consumer does not rewrite the same brittle
   parser. LinkedIn's own data is already structured — stringifying it destroys
   information we were handed for free.
3. Explicit nulls, never absent keys. PhantomBuster deletes falsy values, which
   makes column sets vary row to row and drops legitimate zeroes. Absent should
   mean "LinkedIn had no value", not "the serialiser ate it".
4. Per-response provenance. A field that came from the authenticated API and one
   recovered from a masked guest page are not the same fact, and the caller
   deserves to know which they hold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --- Primitives ------------------------------------------------------------


class PartialDate(BaseModel):
    """A date LinkedIn may only know partially.

    Profiles routinely carry a year with no month (education especially), so
    every component is independently optional rather than forcing a synthetic
    January.
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None

    @property
    def is_empty(self) -> bool:
        return self.year is None and self.month is None and self.day is None


class DateRange(BaseModel):
    start: PartialDate | None = None
    end: PartialDate | None = None
    is_current: bool = Field(
        default=False,
        description="True when LinkedIn records a start but no end date.",
    )


class ImageVariant(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None
    expires_at: datetime | None = None


class Image(BaseModel):
    """A LinkedIn media asset in every size LinkedIn offered.

    These URLs are signed and short-lived — LinkedIn stamps an expiry into the
    query string. Treat them as fetch-now-or-lose-it: mirror the bytes if you
    need them to survive, never persist the URL as though it were stable.
    """

    url: str | None = Field(default=None, description="Largest available variant.")
    variants: list[ImageVariant] = Field(default_factory=list)
    expires_at: datetime | None = None


# --- Profile sections ------------------------------------------------------


class Company(BaseModel):
    name: str | None = None
    urn: str | None = None
    linkedin_url: str | None = None
    logo: Image | None = None
    industry: str | None = None
    staff_count: int | None = None


class Experience(BaseModel):
    title: str | None = None
    company: Company | None = None
    employment_type: str | None = Field(
        default=None, description="e.g. FULL_TIME, PART_TIME, CONTRACT, INTERNSHIP."
    )
    location: str | None = None
    description: str | None = None
    date_range: DateRange | None = None
    duration_months: int | None = None
    skills: list[str] = Field(default_factory=list)


class Education(BaseModel):
    school_name: str | None = None
    school_urn: str | None = None
    school_linkedin_url: str | None = None
    school_logo: Image | None = None
    degree_name: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Skill(BaseModel):
    name: str
    endorsement_count: int | None = None


class Certification(BaseModel):
    name: str | None = None
    authority: str | None = None
    license_number: str | None = None
    url: str | None = None
    company: Company | None = None
    date_range: DateRange | None = None


class Language(BaseModel):
    name: str
    proficiency: str | None = Field(
        default=None,
        description=(
            "LinkedIn's enum: ELEMENTARY, LIMITED_WORKING, PROFESSIONAL_WORKING, "
            "FULL_PROFESSIONAL, NATIVE_OR_BILINGUAL."
        ),
    )


class Project(BaseModel):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    date_range: DateRange | None = None


class Publication(BaseModel):
    name: str | None = None
    publisher: str | None = None
    description: str | None = None
    url: str | None = None
    published_on: PartialDate | None = None


class HonorAward(BaseModel):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    issued_on: PartialDate | None = None


class VolunteerExperience(BaseModel):
    role: str | None = None
    organization: str | None = None
    cause: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Course(BaseModel):
    name: str | None = None
    number: str | None = None


class Location(BaseModel):
    text: str | None = Field(default=None, description="As displayed on the profile.")
    country_code: str | None = None
    postal_code: str | None = None


class Counts(BaseModel):
    connections: int | None = None
    followers: int | None = None
    connections_capped: bool = Field(
        default=False,
        description=(
            "True when LinkedIn reports the '500+' bucket rather than an exact "
            "figure. The connections value is then a floor, not a count."
        ),
    )


# --- Envelope --------------------------------------------------------------

Source = Literal["voyager", "guest_page", "fixture"]


class SectionProvenance(BaseModel):
    """Where each section came from, and whether it is trustworthy.

    Borrowed from the one commercial scraper that publishes per-record
    provenance. Without it, a caller cannot tell an empty section that is
    genuinely empty from one that was gated, redacted, or failed to parse — and
    those demand completely different handling.
    """

    source: Source
    complete: bool = Field(
        default=True,
        description="False when the section was gated, truncated, or partially parsed.",
    )
    redacted: bool = Field(
        default=False,
        description=(
            "True when LinkedIn returned placeholder text instead of values. The "
            "logged-out profile page does this: it ships asterisk runs in place of "
            "job titles, preserving character counts but not content."
        ),
    )
    note: str | None = None


class Profile(BaseModel):
    # Identity
    public_id: str | None = Field(default=None, description="The /in/<slug> segment.")
    urn: str | None = Field(default=None, description="Stable member URN.")
    profile_url: str | None = None

    # Top card
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    summary: str | None = Field(default=None, description="The About section.")
    location: Location | None = None
    industry: str | None = None
    pronouns: str | None = None

    # Media
    profile_picture: Image | None = None
    background_picture: Image | None = None

    # Badges
    is_open_to_work: bool | None = None
    is_hiring: bool | None = None
    is_premium: bool | None = None
    is_influencer: bool | None = None
    is_verified: bool | None = None

    counts: Counts = Field(default_factory=Counts)

    # Sections
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    honors: list[HonorAward] = Field(default_factory=list)
    volunteer: list[VolunteerExperience] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)


class RequestMeta(BaseModel):
    requested_url: str = Field(description="Exactly what the caller supplied.")
    resolved_public_id: str | None = None
    retrieved_at: datetime
    cached: bool = False
    cache_age_seconds: int | None = None
    duration_ms: int | None = None


class ProfileResponse(BaseModel):
    """Top-level response for GET/POST /v1/profile."""

    schema_version: str = Field(
        default="1.0",
        description="Bumped on breaking changes to this contract, never on drift upstream.",
    )
    request: RequestMeta
    source: Source
    provenance: dict[str, SectionProvenance] = Field(
        default_factory=dict,
        description="Per-section provenance, keyed by the Profile field name.",
    )
    warnings: list[str] = Field(default_factory=list)
    profile: Profile


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    mode: Literal["live", "fixture"]
    session_configured: bool
    session_state: str = Field(
        description="unknown until a live call runs; then valid / expired / challenged."
    )
    cache_entries: int
    version: str
