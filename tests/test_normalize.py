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
    """`included` is not in display order; the reference list is authoritative.

    The fixture deliberately places the oldest position first in `included`. A
    parser that filters by `$type` and trusts that order produces a reversed
    career with no error raised — data that looks fine and is wrong.
    """
    included_order = [
        e["title"]
        for e in payload["included"]
        if e.get("$type", "").endswith("profile.Position")
    ]
    assert included_order[0] == "Software Engineer", "fixture should start out of order"

    assert [e.title for e in profile.experience] == [
        "Principal Engineer",
        "Senior Software Engineer",
        "Software Engineer",
    ]


def test_education_follows_reference_order(payload, profile):
    assert [e.school_name for e in profile.education] == [
        "Imperial College London",
        "University of Edinburgh",
    ]


def test_collection_metadata_is_indexed_despite_lacking_entity_urn(payload):
    """Paging entities key off `$id`, so an entityUrn-only index loses them."""
    index = build_index(payload)
    assert "urn:li:fsd_collectionMetadata:demo1" in index


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
    assert [v.width for v in picture.variants] == [100, 400, 800]
    # `url` should be the largest, and rootUrl + path segment concatenated.
    assert picture.url.endswith("t=demo800")
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
