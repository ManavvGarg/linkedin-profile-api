"""Turn a Voyager payload into the public schema.

Voyager answers in Rest.li's normalized form: `data` holds mostly URN
*references*, and `included` is a flat, denormalized bag of every entity
involved — positions, schools, dates, images, paging metadata — each tagged with
a `$type` and keyed by `entityUrn`. Reassembling a profile means walking that
graph rather than reading a tree.

Three traps this module is written around:

**`included` is not in display order.** Filtering it by `$type` alone returns
the right entities in the wrong sequence, with no error raised — jobs end up
under the wrong employer and the output looks plausible. Order has to come from
the URN sequence in the referring `*field`, so that is tried first and the
`included` order is only a fallback.

**Not every entity is keyed by `entityUrn`.** Paging metadata uses `$id`, so an
index built solely on `entityUrn` silently loses entries.

**`$type` strings drift.** Matching is on the trailing segment
(`...profile.Position` -> `Position`) rather than the fully-qualified name, so a
namespace reshuffle degrades one section instead of emptying the whole profile.

Everything here is defensive: a section that fails to parse produces a warning
and an empty list, never an exception. A caller would rather have nine sections
and a warning than a 500.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .models import (
    Certification,
    Company,
    Counts,
    Course,
    DateRange,
    Education,
    Experience,
    HonorAward,
    Image,
    ImageVariant,
    Language,
    Location,
    PartialDate,
    Profile,
    Project,
    Publication,
    Skill,
    VolunteerExperience,
)

logger = logging.getLogger(__name__)


# --- graph plumbing --------------------------------------------------------


def _type_leaf(entity: Any) -> str:
    """`com.linkedin.voyager.dash.identity.profile.Position` -> `Position`."""
    if not isinstance(entity, dict):
        return ""
    return str(entity.get("$type", "")).rsplit(".", 1)[-1]


def build_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index every included entity by whichever key it carries."""
    index: dict[str, dict[str, Any]] = {}
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        # `$id` is not a synonym for entityUrn — paging metadata only has the
        # former, so both are indexed.
        for key in ("entityUrn", "$id"):
            if value := entity.get(key):
                index[str(value)] = entity
    return index


def entities_of_type(
    index: dict[str, dict[str, Any]], *type_leaves: str
) -> list[dict[str, Any]]:
    wanted = set(type_leaves)
    return [e for e in index.values() if _type_leaf(e) in wanted]


def _deref(index: dict[str, dict[str, Any]], ref: Any) -> dict[str, Any] | None:
    if isinstance(ref, str):
        return index.get(ref)
    if isinstance(ref, dict):
        return ref
    return None


def _collection_urns(index: dict[str, dict[str, Any]], ref: Any) -> list[str]:
    """Expand a section reference into an ordered list of entity URNs.

    A section pointer is rarely a plain list. In live payloads it is usually a
    *string* URN naming a CollectionResponse, whose `*elements` holds the real
    URNs in display order. A resolver that only understands lists silently skips
    it and falls back to `included` order — which is not display order, so the
    output looks plausible and is wrong.
    """
    if isinstance(ref, str):
        entity = index.get(ref)
        if entity is None:
            return []
        elements = entity.get("*elements") or entity.get("elements")
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, str)]
        return [ref]  # a direct entity reference, not a collection
    if isinstance(ref, dict):
        elements = ref.get("*elements") or ref.get("elements")
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, str)]
    if isinstance(ref, list):
        return [e for e in ref if isinstance(e, str)]
    return []


# A PositionGroup bundles the roles held at one company. The roles themselves
# hang off this key — note the singular "Position": guessing the plural form
# yields None and costs you the ordering.
_GROUP_POSITIONS_KEY = "*profilePositionInPositionGroup"


def _expand_position_groups(
    entities: list[dict[str, Any]], index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Flatten PositionGroups into their constituent Positions, order preserved.

    LinkedIn groups consecutive roles at the same employer. The group carries
    the company and the outer date range; the individual roles carry titles and
    descriptions, so the Positions are what we want — reached through the group
    to inherit its ordering.
    """
    out: list[dict[str, Any]] = []
    for entity in entities:
        if _type_leaf(entity) != "PositionGroup":
            out.append(entity)
            continue
        for urn in _collection_urns(index, entity.get(_GROUP_POSITIONS_KEY)):
            if position := index.get(urn):
                out.append(position)
    return out


def ordered_refs(
    owner: dict[str, Any] | None,
    index: dict[str, dict[str, Any]],
    *field_names: str,
) -> list[dict[str, Any]]:
    """Resolve a section reference, preserving LinkedIn's own ordering."""
    if not owner:
        return []
    for name in field_names:
        for key in (f"*{name}", name):
            if key not in owner:
                continue
            urns = _collection_urns(index, owner[key])
            resolved = [index.get(u) for u in urns]
            if found := [r for r in resolved if r]:
                return _expand_position_groups(found, index)
    return []


def collect_section(
    owner: dict[str, Any] | None,
    index: dict[str, dict[str, Any]],
    field_names: tuple[str, ...],
    type_leaves: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Ordered references when available, else every entity of the right type."""
    if ordered := ordered_refs(owner, index, *field_names):
        return ordered
    return entities_of_type(index, *type_leaves)


# --- value coercion --------------------------------------------------------


def parse_date(raw: Any) -> PartialDate | None:
    if not isinstance(raw, dict):
        return None
    date = PartialDate(
        year=_as_int(raw.get("year")),
        month=_as_int(raw.get("month")),
        day=_as_int(raw.get("day")),
    )
    return None if date.is_empty else date


def parse_date_range(raw: Any) -> DateRange | None:
    if not isinstance(raw, dict):
        return None
    start = parse_date(raw.get("start"))
    end = parse_date(raw.get("end"))
    if start is None and end is None:
        return None
    # LinkedIn signals "current" by omitting the end date rather than with a flag.
    return DateRange(start=start, end=end, is_current=start is not None and end is None)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_str(value: Any) -> str | None:
    """Unwrap LinkedIn's several string shapes into a plain string.

    Text arrives as a bare string, as `{"text": ...}`, or — on newer dash
    entities — as `{"multiLocaleFirstName": {"en_US": ...}}`.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"].strip() or None
        for locale in ("en_US", "en"):
            if isinstance(value.get(locale), str):
                return value[locale].strip() or None
        if len(value) == 1:
            (only,) = value.values()
            if isinstance(only, str):
                return only.strip() or None
    return None


def parse_image(raw: Any, index: dict[str, dict[str, Any]] | None = None) -> Image | None:
    """Rebuild a usable URL from LinkedIn's VectorImage.

    A VectorImage is a `rootUrl` plus N `artifacts`, each contributing a path
    segment for one rendered size. The full URL is the concatenation — neither
    half is usable alone. Each artifact carries its own expiry, typically on the
    order of minutes.
    """
    if isinstance(raw, str) and index:
        raw = index.get(raw)
    if not isinstance(raw, dict):
        return None

    # Unwrap the several containers a picture can hide behind.
    for key in ("displayImageReference", "vectorImage", "image", "displayImage"):
        if key in raw and isinstance(raw[key], (dict, str)):
            inner = raw[key]
            if isinstance(inner, str) and index:
                inner = index.get(inner, {})
            if isinstance(inner, dict) and ("rootUrl" in inner or "artifacts" in inner):
                raw = inner
                break
            if isinstance(inner, dict) and "vectorImage" in inner:
                raw = inner["vectorImage"]
                break

    root = raw.get("rootUrl")
    artifacts = raw.get("artifacts")
    if not root or not isinstance(artifacts, list):
        # Some payloads carry a ready-made absolute URL instead.
        for key in ("url", "contentUrl"):
            if isinstance(raw.get(key), str) and raw[key].startswith("http"):
                return Image(url=raw[key], variants=[ImageVariant(url=raw[key])])
        return None

    variants: list[ImageVariant] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        segment = artifact.get("fileIdentifyingUrlPathSegment")
        if not isinstance(segment, str):
            continue
        variants.append(
            ImageVariant(
                url=f"{root}{segment}",
                width=_as_int(artifact.get("width")),
                height=_as_int(artifact.get("height")),
                expires_at=_epoch_ms_to_dt(artifact.get("expiresAt")),
            )
        )

    if not variants:
        return None

    variants.sort(key=lambda v: v.width or 0)
    largest = variants[-1]
    return Image(url=largest.url, variants=variants, expires_at=largest.expires_at)


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    ms = _as_int(value)
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _company_from(raw: Any, index: dict[str, dict[str, Any]]) -> Company | None:
    entity = _deref(index, raw)
    if not entity:
        return None
    name = _as_str(entity.get("name")) or _as_str(entity.get("companyName"))
    urn = entity.get("entityUrn")
    universal = _as_str(entity.get("universalName"))
    return Company(
        name=name,
        urn=urn,
        linkedin_url=f"https://www.linkedin.com/company/{universal}" if universal else None,
        logo=parse_image(entity.get("logo") or entity.get("logoResolutionResult"), index),
        industry=_as_str(entity.get("industry")),
        staff_count=_as_int(entity.get("staffCount")),
    )


# --- sections --------------------------------------------------------------


def _find_profile_entity(
    payload: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Locate the Profile entity among everything else in `included`."""
    candidates = entities_of_type(index, "Profile")
    if candidates:
        # Prefer the richest candidate: mini-profile stubs also type as Profile.
        return max(candidates, key=lambda e: len(e.keys()))

    # `elements[0]` is the dash endpoint's shape when not fully normalized.
    data = payload.get("data")
    if isinstance(data, dict):
        elements = data.get("elements")
        if isinstance(elements, list) and elements and isinstance(elements[0], dict):
            return elements[0]
        nested = data.get("data")
        if isinstance(nested, dict):
            for value in nested.values():
                if isinstance(value, dict):
                    refs = value.get("*elements") or value.get("elements")
                    if isinstance(refs, list) and refs:
                        if resolved := _deref(index, refs[0]):
                            return resolved
    return None


def _experience(
    entities: list[dict[str, Any]], index: dict[str, dict[str, Any]]
) -> list[Experience]:
    out: list[Experience] = []
    for e in entities:
        company = _company_from(
            e.get("*company") or e.get("company") or e.get("companyResolutionResult"), index
        )
        if company is None and (name := _as_str(e.get("companyName"))):
            company = Company(name=name, urn=e.get("companyUrn"))
        out.append(
            Experience(
                title=_as_str(e.get("title")),
                company=company,
                employment_type=_as_str(e.get("employmentType"))
                or _as_str(e.get("employmentTypeUrn")),
                location=_as_str(e.get("locationName")) or _as_str(e.get("geoLocationName")),
                description=_as_str(e.get("description")),
                date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
                duration_months=_as_int(e.get("durationInMonths")),
                skills=[s for s in (_as_str(x) for x in e.get("skills") or []) if s],
            )
        )
    return out


def _education(entities: list[dict[str, Any]], index: dict[str, dict[str, Any]]) -> list[Education]:
    out: list[Education] = []
    for e in entities:
        school = _deref(index, e.get("*school") or e.get("school"))
        universal = _as_str(school.get("universalName")) if school else None
        out.append(
            Education(
                school_name=_as_str(e.get("schoolName"))
                or (_as_str(school.get("name")) if school else None),
                school_urn=e.get("schoolUrn") or (school.get("entityUrn") if school else None),
                school_linkedin_url=(
                    f"https://www.linkedin.com/school/{universal}" if universal else None
                ),
                school_logo=parse_image(school.get("logo"), index) if school else None,
                degree_name=_as_str(e.get("degreeName")),
                field_of_study=_as_str(e.get("fieldOfStudy")),
                grade=_as_str(e.get("grade")),
                activities=_as_str(e.get("activities")),
                description=_as_str(e.get("description")),
                date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
            )
        )
    return out


def _skills(entities: list[dict[str, Any]]) -> list[Skill]:
    out: list[Skill] = []
    seen: set[str] = set()
    for e in entities:
        name = _as_str(e.get("name")) or _as_str(e.get("skill"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        # Endorsement counts legitimately include 0, so this must not be
        # collapsed into "falsy means absent".
        count = _as_int(e.get("endorsementCount"))
        if count is None:
            count = _as_int(e.get("numEndorsements"))
        out.append(Skill(name=name, endorsement_count=count))
    return out


def _certifications(
    entities: list[dict[str, Any]], index: dict[str, dict[str, Any]]
) -> list[Certification]:
    return [
        Certification(
            name=_as_str(e.get("name")),
            authority=_as_str(e.get("authority")),
            license_number=_as_str(e.get("licenseNumber")),
            url=_as_str(e.get("url")),
            company=_company_from(e.get("*company") or e.get("company"), index),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
        )
        for e in entities
    ]


def _languages(entities: list[dict[str, Any]]) -> list[Language]:
    out: list[Language] = []
    for e in entities:
        if name := _as_str(e.get("name")):
            out.append(Language(name=name, proficiency=_as_str(e.get("proficiency"))))
    return out


def _projects(entities: list[dict[str, Any]]) -> list[Project]:
    return [
        Project(
            name=_as_str(e.get("title")) or _as_str(e.get("name")),
            description=_as_str(e.get("description")),
            url=_as_str(e.get("url")),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
        )
        for e in entities
    ]


def _publications(entities: list[dict[str, Any]]) -> list[Publication]:
    return [
        Publication(
            name=_as_str(e.get("name")) or _as_str(e.get("title")),
            publisher=_as_str(e.get("publisher")),
            description=_as_str(e.get("description")),
            url=_as_str(e.get("url")),
            published_on=parse_date(e.get("publishedOn") or e.get("date")),
        )
        for e in entities
    ]


def _honors(entities: list[dict[str, Any]]) -> list[HonorAward]:
    return [
        HonorAward(
            title=_as_str(e.get("title")) or _as_str(e.get("name")),
            issuer=_as_str(e.get("issuer")),
            description=_as_str(e.get("description")),
            issued_on=parse_date(e.get("issuedOn") or e.get("issueDate")),
        )
        for e in entities
    ]


def _volunteer(entities: list[dict[str, Any]]) -> list[VolunteerExperience]:
    return [
        VolunteerExperience(
            role=_as_str(e.get("role")) or _as_str(e.get("title")),
            organization=_as_str(e.get("companyName")) or _as_str(e.get("organizationName")),
            cause=_as_str(e.get("cause")),
            description=_as_str(e.get("description")),
            date_range=parse_date_range(e.get("dateRange") or e.get("timePeriod")),
        )
        for e in entities
    ]


def _courses(entities: list[dict[str, Any]]) -> list[Course]:
    return [
        Course(name=_as_str(e.get("name")), number=_as_str(e.get("number"))) for e in entities
    ]


def _resolve_location(root: dict[str, Any], index: dict[str, dict[str, Any]]) -> Location | None:
    """Rebuild the display location from Voyager's nested geo entities.

    The shape is two hops, not one: `geoLocation` is a wrapper carrying a `*geo`
    reference, and the Geo entity it points at carries a `*country` reference to
    a second Geo. Dereferencing `geoLocation` directly yields the wrapper, whose
    only useful content is the pointer — which is how this silently returned
    null against real data.

    City and country also live on different entities, so "Seattle, Washington"
    and "United States" have to be recombined into the string LinkedIn displays.
    """
    text = _as_str(root.get("locationName")) or _as_str(root.get("geoLocationName"))
    country_code = None
    country_name = None

    # `location` is a sibling of `geoLocation` and is where the country code lives.
    loc_obj = root.get("location")
    if isinstance(loc_obj, dict):
        country_code = _as_str(loc_obj.get("countryCode"))

    geo_wrapper = root.get("geoLocation")
    geo_ref = geo_wrapper.get("*geo") or geo_wrapper.get("geo") if isinstance(
        geo_wrapper, dict
    ) else (root.get("*geoLocation") or root.get("geoLocation"))
    geo = _deref(index, geo_ref)

    if geo:
        city = _as_str(geo.get("defaultLocalizedNameWithoutCountryName")) or _as_str(
            geo.get("defaultLocalizedName")
        )
        country = _deref(index, geo.get("*country") or geo.get("country"))
        if country:
            country_name = _as_str(country.get("defaultLocalizedName"))
        if not text:
            text = ", ".join(p for p in (city, country_name) if p) or None

    if not (text or country_code):
        return None
    return Location(
        text=text,
        country_code=country_code or _as_str(root.get("countryCode")),
        postal_code=_as_str(root.get("postalCode")),
    )


def _resolve_industry(root: dict[str, Any], index: dict[str, dict[str, Any]]) -> str | None:
    """Resolve the industry reference.

    Industry entities key their label as `name`, which the generic string
    coercion does not look for — it handles `text` and locale maps. Worth
    resolving explicitly rather than widening that helper, since `name` is
    ambiguous across entity types.
    """
    if direct := _as_str(root.get("industryName")):
        return direct
    entity = _deref(index, root.get("*industry") or root.get("industryUrn"))
    return _as_str(entity.get("name")) if entity else None


# --- entry point -----------------------------------------------------------


# Section name as LinkedIn spells it -> (Profile field, entity types, builder,
# whether the builder needs the entity index). Used to merge separately-fetched
# section collections, since the main profile decoration resolves only positions
# and educations and leaves the rest as unexpanded pointers.
SECTION_BUILDERS: dict[str, tuple[str, tuple[str, ...], Any, bool]] = {
    "profileSkills": ("skills", ("Skill", "ProfileSkill"), _skills, False),
    "profileCertifications": (
        "certifications",
        ("Certification", "ProfileCertification"),
        _certifications,
        True,
    ),
    "profileLanguages": ("languages", ("Language", "ProfileLanguage"), _languages, False),
    "profileProjects": ("projects", ("Project", "ProfileProject"), _projects, False),
    "profileHonors": ("honors", ("Honor", "ProfileHonor"), _honors, False),
    "profilePublications": (
        "publications",
        ("Publication", "ProfilePublication"),
        _publications,
        False,
    ),
    "profileVolunteerExperiences": (
        "volunteer",
        ("VolunteerExperience", "ProfileVolunteerExperience"),
        _volunteer,
        False,
    ),
    "profileCourses": ("courses", ("Course", "ProfileCourse"), _courses, False),
}


def merge_section(
    profile: Profile, section: str, payload: dict[str, Any] | None
) -> str | None:
    """Merge a separately-fetched section collection into a Profile.

    Returns a warning string if the section could not be parsed, else None.
    An empty collection is not an error: LinkedIn genuinely returns
    `paging.total = 0` for sections a member has not filled in.
    """
    spec = SECTION_BUILDERS.get(section)
    if spec is None or not payload:
        return None

    field, type_leaves, builder, needs_index = spec
    try:
        index = build_index(payload)
        entities = entities_of_type(index, *type_leaves)
        if not entities:
            return None
        built = builder(entities, index) if needs_index else builder(entities)
        existing = getattr(profile, field)
        # Keep whatever the main payload already resolved; append only new items.
        seen = {str(getattr(x, "name", None) or getattr(x, "title", None)) for x in existing}
        existing.extend(
            x
            for x in built
            if str(getattr(x, "name", None) or getattr(x, "title", None)) not in seen
        )
    except Exception as exc:
        logger.exception("Failed to merge section %s", section)
        return f"Could not parse the {field} section: {exc}"
    return None


def normalize_profile(
    payload: dict[str, Any],
    *,
    public_id: str,
    sections: dict[str, dict[str, Any] | None] | None = None,
) -> tuple[Profile, list[str]]:
    """Build a Profile, plus warnings for anything that did not parse cleanly."""
    warnings: list[str] = []
    index = build_index(payload)
    root = _find_profile_entity(payload, index)

    if root is None:
        warnings.append(
            "No Profile entity found in the Voyager payload; only top-level fields recovered."
        )
        root = {}

    def section(
        label: str,
        field_names: tuple[str, ...],
        type_leaves: tuple[str, ...],
        builder: Any,
        needs_index: bool = False,
    ) -> Any:
        try:
            entities = collect_section(root, index, field_names, type_leaves)
            return builder(entities, index) if needs_index else builder(entities)
        except Exception as exc:  # one bad section must not lose the profile
            logger.exception("Failed to parse %s", label)
            warnings.append(f"Could not parse {label}: {exc}")
            return []

    first = _as_str(root.get("firstName")) or _as_str(root.get("multiLocaleFirstName"))
    last = _as_str(root.get("lastName")) or _as_str(root.get("multiLocaleLastName"))
    full = " ".join(p for p in (first, last) if p) or None

    location = _resolve_location(root, index)
    industry = _resolve_industry(root, index)

    profile = Profile(
        public_id=_as_str(root.get("publicIdentifier")) or public_id,
        urn=root.get("entityUrn"),
        profile_url=f"https://www.linkedin.com/in/{public_id}",
        first_name=first,
        last_name=last,
        full_name=full,
        headline=_as_str(root.get("headline")) or _as_str(root.get("multiLocaleHeadline")),
        summary=_as_str(root.get("summary")) or _as_str(root.get("about")),
        location=location,
        industry=industry,
        pronouns=_as_str(root.get("pronoun")),
        profile_picture=parse_image(root.get("profilePicture"), index),
        background_picture=parse_image(root.get("backgroundPicture"), index),
        is_premium=root.get("premium") if isinstance(root.get("premium"), bool) else None,
        is_influencer=root.get("influencer") if isinstance(root.get("influencer"), bool) else None,
        is_verified=root.get("verified") if isinstance(root.get("verified"), bool) else None,
        experience=section(
            "experience",
            ("profilePositionGroups", "profilePositions", "positionGroupView", "positionView"),
            ("Position", "ProfilePosition"),
            _experience,
            needs_index=True,
        ),
        education=section(
            "education",
            ("profileEducations", "educationView"),
            ("Education", "ProfileEducation"),
            _education,
            needs_index=True,
        ),
        skills=section(
            "skills", ("profileSkills", "skillView"), ("Skill", "ProfileSkill"), _skills
        ),
        certifications=section(
            "certifications",
            ("profileCertifications", "certificationView"),
            ("Certification", "ProfileCertification"),
            _certifications,
            needs_index=True,
        ),
        languages=section(
            "languages",
            ("profileLanguages", "languageView"),
            ("Language", "ProfileLanguage"),
            _languages,
        ),
        projects=section(
            "projects", ("profileProjects", "projectView"), ("Project", "ProfileProject"), _projects
        ),
        publications=section(
            "publications",
            ("profilePublications", "publicationView"),
            ("Publication", "ProfilePublication"),
            _publications,
        ),
        honors=section(
            "honors", ("profileHonors", "honorView"), ("Honor", "ProfileHonor"), _honors
        ),
        volunteer=section(
            "volunteer",
            ("profileVolunteerExperiences", "volunteerExperienceView"),
            ("VolunteerExperience", "ProfileVolunteerExperience"),
            _volunteer,
        ),
        courses=section(
            "courses", ("profileCourses", "courseView"), ("Course", "ProfileCourse"), _courses
        ),
    )

    for name, section_payload in (sections or {}).items():
        if warning := merge_section(profile, name, section_payload):
            warnings.append(warning)

    profile.counts = _counts(None, root)

    if not profile.full_name:
        warnings.append(
            "No name was recovered. This usually means the profile is out of network "
            "and LinkedIn returned an anonymised placeholder rather than the member."
        )

    return profile, warnings


def _counts(network_payload: dict[str, Any] | None, root: dict[str, Any]) -> Counts:
    connections = followers = None
    capped = False

    if network_payload:
        data = network_payload.get("data")
        source = data if isinstance(data, dict) else network_payload
        connections = _as_int(source.get("connectionsCount"))
        followers = _as_int(source.get("followersCount"))
        # LinkedIn reports the 500+ bucket rather than an exact number above the
        # threshold, so the value is a floor and must be labelled as one.
        if source.get("connectionsCountCapped") is True or connections == 500:
            capped = True

    if followers is None:
        followers = _as_int(root.get("followersCount"))

    return Counts(connections=connections, followers=followers, connections_capped=capped)
