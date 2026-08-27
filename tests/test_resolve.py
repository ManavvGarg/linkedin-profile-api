import pytest

from app.errors import InvalidProfileURL
from app.resolve import extract_public_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.linkedin.com/in/williamhgates", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
        ("http://www.linkedin.com/in/williamhgates", "williamhgates"),
        ("www.linkedin.com/in/williamhgates", "williamhgates"),
        ("linkedin.com/in/williamhgates", "williamhgates"),
        # Country subdomains — LinkedIn serves the same profile from every one.
        ("https://in.linkedin.com/in/williamhgates", "williamhgates"),
        ("https://uk.linkedin.com/in/williamhgates", "williamhgates"),
        # Tracking parameters and fragments are noise on a pasted URL.
        ("https://www.linkedin.com/in/williamhgates?trk=public_profile", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/#experience", "williamhgates"),
        ("https://www.linkedin.com/in/williamhgates/detail/contact-info/", "williamhgates"),
        # Links out of LinkedIn's own emails.
        ("https://www.linkedin.com/comm/in/williamhgates", "williamhgates"),
        ("https://www.linkedin.com/pub/williamhgates", "williamhgates"),
        # Bare slug.
        ("williamhgates", "williamhgates"),
        ("satya-nadella-1a2b3c", "satya-nadella-1a2b3c"),
        # Whitespace from a sloppy paste.
        ("  https://www.linkedin.com/in/williamhgates  ", "williamhgates"),
    ],
)
def test_accepts_common_url_shapes(value, expected):
    assert extract_public_id(value) == expected


def test_sales_navigator_url_reduces_to_member_id():
    url = "https://www.linkedin.com/sales/people/ACwAAABw,NAME_SEARCH,abcd"
    assert extract_public_id(url) == "ACwAAABw"


def test_percent_encoded_slug_is_decoded():
    # Non-Latin vanity slugs arrive percent-encoded and must survive intact.
    assert extract_public_id("https://www.linkedin.com/in/%E5%B1%B1%E7%94%B0") == "山田"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://example.com/in/someone",
        "https://twitter.com/someone",
    ],
)
def test_rejects_non_linkedin_input(value):
    with pytest.raises(InvalidProfileURL):
        extract_public_id(value)


def test_search_url_gets_a_specific_message():
    # A common paste mistake; a generic 404 downstream would be unhelpful.
    with pytest.raises(InvalidProfileURL, match="search URL"):
        extract_public_id("https://www.linkedin.com/search/results/people/?keywords=x")


def test_company_url_gets_a_specific_message():
    with pytest.raises(InvalidProfileURL, match="company or school"):
        extract_public_id("https://www.linkedin.com/company/microsoft")
