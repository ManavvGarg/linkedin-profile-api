"""Parser tests.

These lean on the specific ways Voyager payloads mislead a naive parser, since
those are the failures that produce plausible-looking wrong data rather than an
obvious crash.
"""

import json
from pathlib import Path

import pytest

from app.normalize import (
    build_index,
    normalize_profile,
    parse_date,
    parse_date_range,
    parse_image,
)

FIXTURE = Path(__file__).parent.parent / "app" / "fixtures" / "ada-lovelace-demo.json"


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def profile(payload):
    result, _ = normalize_profile(payload, public_id="ada-lovelace-demo")
    return result


# --- the ordering trap -----------------------------------------------------


def test_experience_follows_reference_order_not_included_order(payload, profile):
    """`included` is not in display order; the collection is authoritative.

    The fixture deliberately places a past position first in `included`. A parser
    that filters by `$type` and trusts that order produces a scrambled career
    with no error raised — data that looks fine and is wrong. This was a real
    bug: it survived a fixture that used plain reference lists and only surfaced
    against live LinkedIn data, which nests everything behind collections.
    """
    included_order = [
        e["title"]
        for e in payload["included"]
        if e.get("$type", "").endswith("profile.Position")
    ]
    assert included_order[0] != "Principal Engineer", "fixture should start out of order"

    assert [e.title for e in profile.experience] == [
        "Principal Engineer",
        "Senior Software Engineer",
        "Software Engineer",
    ]


def test_section_pointers_are_collection_urn_strings(payload):
    """Guards the fixture's fidelity to what LinkedIn actually sends.

    Live payloads point at sections with a CollectionResponse URN *string*, not
    an inline list. A resolver that only understands lists skips it silently and
    falls back to `included` order. If this assertion ever fails, the fixture has
    drifted back to an easier shape than reality and the ordering tests above
    stop proving anything.
    """
    profile_entity = next(
        e for e in payload["included"] if e.get("$type", "").endswith("profile.Profile")
    )
    for pointer in ("*profilePositionGroups", "*profileEducations", "*profileSkills"):
        ref = profile_entity[pointer]
        assert isinstance(ref, str), f"{pointer} should be a URN string, not {type(ref)}"
        assert ref.startswith("urn:li:collectionResponse:")


def test_positions_are_reached_through_their_position_group(payload, profile):
    """Experience is two collections deep, not one.

    Profile -> positionGroups collection -> PositionGroup ->
    `*profilePositionInPositionGroup` collection -> Position. The singular
    "Position" in that key is easy to get wrong, and guessing the plural yields
    None — which drops straight through to unordered output.
    """
    groups = [
        e for e in payload["included"] if e.get("$type", "").endswith("profile.PositionGroup")
    ]
    assert groups, "fixture should model position groups"
    assert all("*profilePositionInPositionGroup" in g for g in groups)

    # The second group holds two roles at one employer; both must be flattened out.
    northwind = [
        e for e in profile.experience if e.company and e.company.name == "Northwind Analytics"
    ]
    assert len(northwind) == 2
    assert [e.title for e in northwind] == ["Senior Software Engineer", "Software Engineer"]


def test_education_follows_reference_order(payload, profile):
    assert [e.school_name for e in profile.education] == [
        "Imperial College London",
        "University of Edinburgh",
    ]


def test_entities_keyed_by_dollar_id_are_indexed():
    """Not every entity carries `entityUrn`; some paging entities use `$id`.

    An index built on `entityUrn` alone drops them silently.
    """
    index = build_index(
        {
            "included": [
                {"$type": "com.example.Thing", "entityUrn": "urn:a"},
                {"$type": "com.example.Meta", "$id": "urn:b", "total": 3},
            ]
        }
    )
    assert "urn:a" in index
    assert "urn:b" in index


def test_collections_are_indexed_and_resolvable(payload):
    """The CollectionResponse entities the section pointers name must resolve."""
    index = build_index(payload)
    collections = [k for k in index if k.startswith("urn:li:collectionResponse:")]
    assert collections, "fixture should contain resolvable collections"
    for urn in collections:
        assert "*elements" in index[urn]


# --- the falsy-value trap --------------------------------------------------


def test_zero_endorsements_is_preserved_not_dropped(profile):
    """Zero is a real count. Treating falsy as absent silently loses it."""
    kubernetes = next(s for s in profile.skills if s.name == "Kubernetes")
    assert kubernetes.endorsement_count == 0


# --- dates -----------------------------------------------------------------


def test_current_role_has_no_end_date_and_is_flagged(profile):
    current = profile.experience[0]
    assert current.date_range.is_current is True
    assert current.date_range.end is None
    assert current.date_range.start.year == 2021
    assert current.date_range.start.month == 3


def test_past_role_is_not_flagged_current(profile):
    past = profile.experience[1]
    assert past.date_range.is_current is False
    assert past.date_range.end.year == 2021


def test_year_only_dates_are_kept_partial():
    """Education often carries a year with no month; don't invent January."""
    parsed = parse_date({"year": 2012})
    assert parsed.year == 2012
    assert parsed.month is None


def test_empty_date_becomes_none():
    assert parse_date({}) is None
    assert parse_date(None) is None
    assert parse_date_range({}) is None


# --- images ----------------------------------------------------------------


def test_vector_image_reconstructs_every_variant(profile):
    picture = profile.profile_picture
    assert picture is not None
    assert [v.width for v in picture.variants] == [100, 200, 400, 800]
    # `url` should be the largest, and rootUrl + path segment concatenated.
    assert picture.url.endswith("t=d800")
    assert picture.url.startswith("https://media.licdn.com/dms/image/v2/DEMO/")


def test_image_expiry_is_surfaced(profile):
    # These URLs are signed and short-lived; callers need to know that.
    assert profile.profile_picture.expires_at is not None


def test_image_without_artifacts_returns_none():
    assert parse_image({"rootUrl": "https://x/"}) is None
    assert parse_image(None) is None


# --- resilience ------------------------------------------------------------


def test_missing_profile_entity_warns_rather_than_raising():
    profile, warnings = normalize_profile({"included": []}, public_id="nobody")
    assert profile.public_id == "nobody"
    assert any("No Profile entity" in w for w in warnings)


def test_out_of_network_placeholder_is_called_out():
    """LinkedIn returns an anonymised stub for some out-of-network profiles."""
    _, warnings = normalize_profile({"included": []}, public_id="someone")
    assert any("out of network" in w for w in warnings)


def test_garbage_payload_does_not_raise():
    profile, _ = normalize_profile({"data": "nonsense", "included": "also nonsense"},
                                   public_id="x")
    assert profile.experience == []


# --- completeness ----------------------------------------------------------


def test_all_requested_sections_populate(profile):
    assert profile.full_name == "Ada Lovelace"
    assert profile.headline
    assert profile.summary
    assert profile.location.text == "London, England, United Kingdom"
    assert len(profile.experience) == 3
    assert len(profile.education) == 2
    assert len(profile.skills) == 4
    assert len(profile.certifications) == 1
    assert len(profile.languages) == 2
    assert profile.profile_picture is not None


def test_company_is_resolved_from_reference(profile):
    company = profile.experience[0].company
    assert company.name == "Analytical Systems"
    assert company.linkedin_url.endswith("/analytical-systems-demo")
    assert company.staff_count == 340


def test_language_proficiency_enum_survives(profile):
    english = next(x for x in profile.languages if x.name == "English")
    assert english.proficiency == "NATIVE_OR_BILINGUAL"
