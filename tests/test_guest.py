"""Tests for the logged-out fallback.

The important behaviour here is recognising redaction. LinkedIn does not omit
withheld fields — it substitutes asterisk runs that preserve the original
character count. A parser that does not detect this will happily emit
`title: "********"` as though it were a job title.
"""

from app.guest import _extract_person, _person_to_profile, count_redactions, is_masked

# Trimmed from a real logged-out response. The masking is exactly as served:
# server-side, on the wire, with word boundaries and character counts intact.
GUEST_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"http://schema.org","@graph":[
 {"@type":"WebPage","url":"https://www.linkedin.com/in/demo-person"},
 {"@type":"Person",
  "name":"Demo Person",
  "description":"Building data infrastructure. Previously at two analytics companies\\u2026",
  "disambiguatingDescription":"Creator, Top Voice",
  "address":{"@type":"PostalAddress","addressCountry":"GB","addressLocality":"London, England"},
  "image":{"@type":"ImageObject","contentUrl":"https://media.licdn.com/dms/image/v2/X/photo_200_200"},
  "jobTitle":["******** *** ***","****** ***** ** ********"],
  "awards":[],"knowsLanguage":[],"memberOf":[],
  "alumniOf":[{"@type":"EducationalOrganization","name":"Imperial College London",
               "url":"https://www.linkedin.com/school/imperial/",
               "member":{"@type":"OrganizationRole","startDate":2008,"endDate":2012}},
              {"@type":"EducationalOrganization","name":"********** ** *********",
               "member":{"@type":"OrganizationRole","startDate":2012,"endDate":2013}}],
  "worksFor":[{"@type":"Organization","name":"Analytical Systems",
               "url":"https://www.linkedin.com/company/analytical-systems",
               "location":"London","member":{"@type":"OrganizationRole"}},
              {"@type":"Organization","name":"********* *********",
               "member":{"@type":"OrganizationRole"}}],
  "interactionStatistic":{"@type":"InteractionCounter",
    "interactionType":"https://schema.org/FollowAction","userInteractionCount":8421},
  "sameAs":"https://www.linkedin.com/in/demo-person",
  "url":"https://www.linkedin.com/in/demo-person"}]}
</script></head>
<body>
<div class="blurred-overlay"><h4><p class="blur" aria-hidden="true">********</p></h4></div>
</body></html>
"""


def test_detects_asterisk_masking():
    assert is_masked("********")
    assert is_masked("******** *** ***")
    assert is_masked("   ****   ")


def test_does_not_flag_real_text_as_masked():
    assert not is_masked("Principal Engineer")
    assert not is_masked("")
    assert not is_masked(None)
    # An asterisk inside real text is not a redaction.
    assert not is_masked("C** Developer")


def test_extracts_the_person_node():
    person = _extract_person(GUEST_HTML)
    assert person is not None
    assert person["name"] == "Demo Person"


def test_recovers_the_fields_that_survive_redaction():
    person = _extract_person(GUEST_HTML)
    profile = _person_to_profile(person, "demo-person", GUEST_HTML)

    assert profile.full_name == "Demo Person"
    assert profile.first_name == "Demo"
    assert profile.location.text == "London, England"
    assert profile.location.country_code == "GB"
    assert profile.profile_picture.url.endswith("photo_200_200")
    assert profile.counts.followers == 8421
    # `description` is truncated but not masked — the richest anonymous field.
    assert "data infrastructure" in profile.headline


def test_masked_values_become_null_not_asterisks():
    """The whole point: never emit a redaction placeholder as though it were data."""
    person = _extract_person(GUEST_HTML)
    profile = _person_to_profile(person, "demo-person", GUEST_HTML)

    for experience in profile.experience:
        assert experience.title is None  # always withheld logged-out
        if experience.company.name is not None:
            assert "*" not in experience.company.name

    for education in profile.education:
        if education.school_name is not None:
            assert "*" not in education.school_name


def test_position_count_survives_even_when_names_are_masked():
    """Masked entries still prove a role existed; dropping them understates history."""
    person = _extract_person(GUEST_HTML)
    profile = _person_to_profile(person, "demo-person", GUEST_HTML)

    assert len(profile.experience) == 2
    assert profile.experience[0].company.name == "Analytical Systems"
    assert profile.experience[1].company.name is None  # masked -> null, entry kept


def test_education_keeps_year_ranges():
    """Unlike positions, schools retain their dates on the logged-out page."""
    person = _extract_person(GUEST_HTML)
    profile = _person_to_profile(person, "demo-person", GUEST_HTML)

    first = profile.education[0]
    assert first.school_name == "Imperial College London"
    assert first.date_range.start.year == 2008
    assert first.date_range.end.year == 2012


def test_summary_is_never_claimed_from_the_guest_page():
    """About is behind the login wall entirely."""
    person = _extract_person(GUEST_HTML)
    profile = _person_to_profile(person, "demo-person", GUEST_HTML)
    assert profile.summary is None


def test_counts_redactions_for_diagnostics():
    assert count_redactions(GUEST_HTML) >= 1
