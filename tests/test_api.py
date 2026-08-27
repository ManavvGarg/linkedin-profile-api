"""End-to-end tests through the HTTP layer, in fixture mode."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ["FETCH_MODE"] = "fixture"
os.environ["API_KEYS"] = ""

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

DEMO = "https://www.linkedin.com/in/ada-lovelace-demo"


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    import app.main as main_module

    main_module._service = None
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_health_reports_fixture_mode(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "fixture"
    assert body["status"] == "degraded"  # fixture mode is not a healthy prod state
    assert body["session_configured"] is False


def test_fetch_profile_returns_the_full_schema(client):
    response = client.get("/v1/profile", params={"url": DEMO})
    assert response.status_code == 200
    body = response.json()

    assert body["schema_version"] == "1.0"
    assert body["source"] == "fixture"
    assert body["request"]["resolved_public_id"] == "ada-lovelace-demo"

    profile = body["profile"]
    assert profile["full_name"] == "Ada Lovelace"
    assert profile["headline"]
    assert profile["summary"]
    assert profile["location"]["text"] == "London, England, United Kingdom"
    assert len(profile["experience"]) == 3
    assert len(profile["education"]) == 2
    assert len(profile["skills"]) == 4
    assert len(profile["certifications"]) == 1
    assert len(profile["languages"]) == 2
    assert profile["profile_picture"]["url"]


def test_every_challenge_field_is_present(client):
    """The brief names specific fields; assert each one is reachable."""
    profile = client.get("/v1/profile", params={"url": DEMO}).json()["profile"]
    for field in (
        "full_name",
        "headline",
        "location",
        "summary",
        "experience",
        "education",
        "skills",
        "certifications",
        "languages",
        "profile_picture",
    ):
        assert field in profile, f"{field} missing from the response"
        assert profile[field] not in (None, [], {}), f"{field} is empty"


def test_nulls_are_explicit_not_omitted(client):
    """Absent keys make column sets ragged; null says 'LinkedIn had no value'."""
    profile = client.get("/v1/profile", params={"url": DEMO}).json()["profile"]
    assert "pronouns" in profile
    assert "background_picture" in profile
    education = profile["education"][0]
    assert "description" in education  # null in the fixture, still present


def test_dates_are_structured_not_strings(client):
    """A consumer should never have to parse 'Jan 2021 - Present'."""
    experience = client.get("/v1/profile", params={"url": DEMO}).json()["profile"]["experience"]
    date_range = experience[0]["date_range"]
    assert date_range["start"]["year"] == 2021
    assert date_range["start"]["month"] == 3
    assert date_range["end"] is None
    assert date_range["is_current"] is True


def test_provenance_is_reported_per_section(client):
    body = client.get("/v1/profile", params={"url": DEMO}).json()
    assert body["provenance"]["experience"]["source"] == "fixture"
    assert body["provenance"]["skills"]["complete"] is True


def test_post_accepts_a_json_body(client):
    response = client.post("/v1/profile", json={"url": DEMO})
    assert response.status_code == 200
    assert response.json()["profile"]["full_name"] == "Ada Lovelace"


def test_bare_public_id_is_accepted(client):
    response = client.get("/v1/profile", params={"url": "ada-lovelace-demo"})
    assert response.status_code == 200


def test_second_request_is_served_from_cache(client):
    client.get("/v1/profile", params={"url": DEMO})
    body = client.get("/v1/profile", params={"url": DEMO}).json()
    assert body["request"]["cached"] is True
    assert body["request"]["cache_age_seconds"] is not None


def test_refresh_bypasses_the_cache(client):
    client.get("/v1/profile", params={"url": DEMO})
    body = client.get("/v1/profile", params={"url": DEMO, "refresh": "true"}).json()
    assert body["request"]["cached"] is False


# --- errors ----------------------------------------------------------------


def test_invalid_url_is_a_400_with_remediation(client):
    response = client.get("/v1/profile", params={"url": "https://example.com/x"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_profile_url"
    assert "remediation" in error


def test_search_url_is_rejected_specifically(client):
    response = client.get(
        "/v1/profile",
        params={"url": "https://www.linkedin.com/search/results/people/?keywords=x"},
    )
    assert response.status_code == 400
    assert "search URL" in response.json()["error"]["message"]


def test_unknown_fixture_lists_what_is_available(client):
    response = client.get("/v1/profile", params={"url": "nobody-here"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "fixture_not_found"
    assert "ada-lovelace-demo" in error["detail"]["available"]


def test_fixtures_endpoint_lists_samples(client):
    response = client.get("/v1/fixtures")
    assert response.status_code == 200
    assert "ada-lovelace-demo" in response.json()["fixtures"]


def test_openapi_schema_is_served(client):
    assert client.get("/openapi.json").status_code == 200
