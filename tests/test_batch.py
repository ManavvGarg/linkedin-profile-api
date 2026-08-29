"""Batch endpoint and per-request session override."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.errors import SessionMissing
from app.main import app
from app.service import ProfileService

DEMO = "https://www.linkedin.com/in/ada-lovelace-demo"


@pytest.fixture
def client():
    return TestClient(app)


# --- batch input shapes ----------------------------------------------------


def test_batch_accepts_a_urls_array(client):
    response = client.post("/v1/profiles", json={"urls": [DEMO, "ada-lovelace-demo"]})
    assert response.status_code == 200
    body = response.json()
    # Two spellings of the same profile stay two results: the caller asked about
    # both and deserves a result keyed to each input. The cost is deduplicated
    # rather than the response — the second is a cache hit, not a second fetch.
    assert body["requested"] == 2
    assert body["succeeded"] == 2
    assert [item["url"] for item in body["results"]] == [DEMO, "ada-lovelace-demo"]
    assert body["results"][0]["result"]["request"]["cached"] is False
    assert body["results"][1]["result"]["request"]["cached"] is True


def test_exact_duplicate_urls_are_collapsed(client):
    """A repeated identical URL is a caller mistake, not a request for two results."""
    body = client.post("/v1/profiles", json={"urls": [DEMO, DEMO, DEMO]}).json()
    assert body["requested"] == 1


def test_batch_accepts_distinct_urls(client):
    response = client.post("/v1/profiles", json={"urls": [DEMO, "nobody-here"]})
    assert response.status_code == 200
    body = response.json()
    assert body["requested"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1


def test_batch_accepts_a_single_url_string(client):
    """Same endpoint serves one profile without special-casing."""
    response = client.post("/v1/profiles", json={"url": DEMO})
    assert response.status_code == 200
    assert response.json()["succeeded"] == 1


def test_batch_merges_url_and_urls(client):
    response = client.post("/v1/profiles", json={"urls": ["nobody-here"], "url": DEMO})
    assert response.json()["requested"] == 2


def test_empty_batch_is_rejected(client):
    response = client.post("/v1/profiles", json={"urls": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_profile_url"


def test_oversized_batch_is_rejected_with_the_limit(client):
    response = client.post("/v1/profiles", json={"urls": [f"person-{i}" for i in range(50)]})
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "batch_too_large"
    assert error["detail"]["limit"] == 10
    assert error["detail"]["requested"] == 50


# --- per-item isolation ----------------------------------------------------


def test_one_failure_does_not_discard_the_batch(client):
    """The whole point of per-item status: partial success is still useful."""
    body = client.post(
        "/v1/profiles", json={"urls": ["nobody-here", DEMO, "https://example.com/x"]}
    ).json()

    by_status = {item["status"] for item in body["results"]}
    assert by_status == {"ok", "error"}

    ok = next(i for i in body["results"] if i["status"] == "ok")
    assert ok["result"]["profile"]["full_name"] == "Ada Lovelace"
    assert ok["error"] is None

    bad_url = next(i for i in body["results"] if i["url"] == "https://example.com/x")
    assert bad_url["error"]["code"] == "invalid_profile_url"
    assert bad_url["result"] is None


def test_batch_reports_its_own_timing(client):
    body = client.post("/v1/profiles", json={"urls": [DEMO]}).json()
    assert body["duration_ms"] >= 0
    assert body["schema_version"] == "1.0"


# --- session override ------------------------------------------------------


def test_cache_is_namespaced_per_credential():
    """A per-request cookie must not read another credential's cached data.

    What a session can see depends on who it is — connection degree changes
    which fields LinkedIn returns — so sharing one cache across credentials
    would serve wrong data and leak between callers.
    """
    service = ProfileService(Settings(_env_file=None, fetch_mode="live", linkedin_li_at="a" * 60))

    _, env_ns = service._client_for(None)
    _, req_ns = service._client_for("b" * 60)
    _, same_again = service._client_for("b" * 60)
    _, different = service._client_for("c" * 60)

    assert env_ns == "env"
    assert req_ns != env_ns, "a supplied cookie must not share the server's namespace"
    assert req_ns == same_again, "the same cookie must map to a stable namespace"
    assert req_ns != different, "different cookies must not share a namespace"


def test_namespace_does_not_contain_the_raw_cookie():
    """The namespace becomes a cache key; it must not carry the credential."""
    cookie = "supersecretcookievalue" + "z" * 40
    service = ProfileService(Settings(_env_file=None, fetch_mode="live", linkedin_li_at="a" * 60))
    _, namespace = service._client_for(cookie)
    assert cookie not in namespace
    assert namespace.startswith("req-")


def test_session_override_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("ALLOW_SESSION_OVERRIDE", "false")
    get_settings.cache_clear()
    import app.main as main_module

    main_module._service = None

    response = client.post("/v1/profile", json={"url": DEMO, "session_cookie": "x" * 60})
    assert response.status_code == 500
    assert "does not accept" in response.json()["error"]["message"]

    get_settings.cache_clear()
    main_module._service = None


def test_get_endpoint_has_no_session_cookie_parameter():
    """Credentials must not travel in a query string, where logs capture them."""
    spec = app.openapi()
    params = spec["paths"]["/v1/profile"]["get"].get("parameters", [])
    assert "session_cookie" not in {p["name"] for p in params}


def test_session_cookie_is_not_echoed_back(client):
    body = client.post("/v1/profiles", json={"url": DEMO, "session_cookie": "x" * 60}).json()
    assert "x" * 60 not in str(body)


# --- credential precedence -------------------------------------------------
#
# The contract: use the caller's cookie when they supply one, otherwise fall
# back to the server's. These pin all four combinations, including the one that
# was broken — a server holding no credential of its own.


def _service(env_cookie: str = "", fetch_mode: str = "auto") -> ProfileService:
    return ProfileService(
        Settings(_env_file=None, fetch_mode=fetch_mode, linkedin_li_at=env_cookie)
    )


def test_falls_back_to_the_env_cookie_when_caller_supplies_none():
    service = _service(env_cookie="e" * 60, fetch_mode="live")
    client, namespace = service._client_for(None)
    assert namespace == "env"
    assert client._session.active_li_at == "e" * 60


def test_callers_cookie_takes_precedence_over_the_env_one():
    service = _service(env_cookie="e" * 60, fetch_mode="live")
    client, namespace = service._client_for("u" * 60)
    assert client._session.active_li_at == "u" * 60, "caller's credential must win"
    assert namespace != "env"


def test_supplied_cookie_enables_live_lookups_with_no_server_credential():
    """The regression: a server holding no cookie must still honour the caller's.

    With FETCH_MODE=auto and no LINKEDIN_LI_AT, mode resolution used to fall to
    fixture and silently discard the credential the caller had just supplied.
    """
    service = _service(env_cookie="", fetch_mode="auto")
    assert service._resolve_mode(None) == "fixture", "no credential anywhere -> fixtures"
    assert service._resolve_mode("u" * 60) == "live", "caller's credential -> live"


def test_explicit_fixture_mode_is_not_overridden_by_a_request_cookie():
    """FETCH_MODE=fixture is a deliberate offline choice; a body cannot undo it."""
    service = _service(env_cookie="", fetch_mode="fixture")
    assert service._resolve_mode("u" * 60) == "fixture"


def test_no_credential_anywhere_reports_a_useful_error():
    service = _service(env_cookie="", fetch_mode="live")
    with pytest.raises(SessionMissing) as excinfo:
        service._client_for(None)
    # The message should name both ways out, not just the env var.
    assert "session_cookie" in str(excinfo.value.remediation)
