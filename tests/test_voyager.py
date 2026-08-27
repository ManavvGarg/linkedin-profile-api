"""Tests for the Voyager client's status-code semantics and session handling.

These matter disproportionately because Voyager's status codes do not mean what
they appear to mean. Getting `403` wrong sends an operator to re-copy a cookie
that was never the problem; following the `302` hides session death behind what
looks like a parsing bug. Both are pinned here.
"""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.errors import (
    EndpointRetired,
    ProfileNotFound,
    RateLimited,
    SchemaDrift,
    SessionChallenged,
    SessionExpired,
    SessionInvalid,
    SessionMalformed,
    SessionMissing,
    UpstreamBlocked,
)
from app.session import LinkedInSession, SessionState, mint_jsessionid
from app.voyager import VoyagerClient


def make_response(status: int, *, location: str = "", text: str = "", cookies=None):
    return SimpleNamespace(
        status_code=status,
        headers={"location": location} if location else {},
        text=text,
        cookies=cookies or {},
        json=lambda: {},
    )


@pytest.fixture
def session():
    return LinkedInSession(li_at="x" * 60, user_agent="test-agent")


@pytest.fixture
def client(session):
    return VoyagerClient(session, Settings(linkedin_li_at="x" * 60))


# --- status-code semantics -------------------------------------------------


def test_302_to_login_means_the_session_died(client, session):
    with pytest.raises(SessionExpired):
        client._classify(make_response(302, location="/uas/login?x=1"), endpoint="test")
    assert session.state is SessionState.EXPIRED


def test_302_to_challenge_is_distinct_from_expired(client, session):
    """The account is healthy; a human has to clear a checkpoint."""
    with pytest.raises(SessionChallenged):
        client._classify(
            make_response(302, location="/checkpoint/challenge/verify"), endpoint="test"
        )
    assert session.state is SessionState.CHALLENGED


def test_403_is_a_csrf_mismatch_not_a_dead_session(client, session):
    """The counter-intuitive one, and the most commonly misread."""
    with pytest.raises(SessionInvalid, match="CSRF"):
        client._classify(make_response(403, text="CSRF check failed."), endpoint="test")
    # Crucially, a CSRF mismatch must NOT be recorded as an expired session.
    assert session.state is not SessionState.EXPIRED


def test_410_is_reported_as_a_retired_endpoint(client):
    """The legacy profileView endpoint answers this since late 2025."""
    with pytest.raises(EndpointRetired):
        client._classify(make_response(410), endpoint="profileView")


def test_999_is_an_edge_block(client):
    with pytest.raises(UpstreamBlocked):
        client._classify(make_response(999), endpoint="test")


def test_429_is_rate_limiting(client):
    with pytest.raises(RateLimited):
        client._classify(make_response(429), endpoint="test")


def test_404_is_a_missing_profile(client):
    with pytest.raises(ProfileNotFound):
        client._classify(make_response(404), endpoint="test")


def test_unknown_status_is_drift_not_a_crash(client):
    with pytest.raises(SchemaDrift):
        client._classify(make_response(418), endpoint="test")


def test_200_passes_through(client):
    client._classify(make_response(200), endpoint="test")  # must not raise


def test_redirect_somewhere_unexpected_is_drift(client):
    with pytest.raises(SchemaDrift):
        client._classify(make_response(302, location="/somewhere/else"), endpoint="test")


# --- cookie handling -------------------------------------------------------


def test_csrf_token_header_equals_the_jsessionid_cookie(session):
    """LinkedIn only checks that these match — never the value itself."""
    headers = session.voyager_headers()
    cookies = session.cookies()
    assert headers["csrf-token"] == session.jsessionid
    assert cookies["JSESSIONID"] == f'"{session.jsessionid}"'
    assert cookies["JSESSIONID"].strip('"') == headers["csrf-token"]


def test_host_header_is_set_explicitly(session):
    """Voyager answers 400 invalid hostname without it, outside a browser."""
    assert session.voyager_headers()["host"] == "www.linkedin.com"


def test_minted_jsessionid_has_linkedin_shape():
    value = mint_jsessionid()
    assert value.startswith("ajax:")
    assert value.removeprefix("ajax:").isdigit()


def test_rotated_cookie_is_preferred_once_seen(session):
    original = session.active_li_at
    assert session.observe_rotation("y" * 60) is True
    assert session.active_li_at == "y" * 60
    assert session.active_li_at != original


def test_rotation_ignores_an_unchanged_or_junk_value(session):
    assert session.observe_rotation(session.li_at) is False
    assert session.observe_rotation("") is False
    assert session.observe_rotation("tooshort") is False


def test_cookies_use_the_rotated_value(session):
    session.observe_rotation("z" * 60)
    assert session.cookies()["li_at"] == "z" * 60


# --- session construction --------------------------------------------------


def test_missing_cookie_is_its_own_error():
    with pytest.raises(SessionMissing):
        LinkedInSession.from_settings(Settings(linkedin_li_at=""))


def test_malformed_cookie_is_distinguished_from_missing():
    with pytest.raises(SessionMalformed):
        LinkedInSession.from_settings(Settings(linkedin_li_at="short"))


def test_tolerates_pasting_the_whole_name_value_pair():
    """A very common paste mistake; worth accepting rather than rejecting."""
    session = LinkedInSession.from_settings(Settings(linkedin_li_at=f"li_at={'a' * 60}"))
    assert session.li_at == "a" * 60


def test_tolerates_surrounding_quotes():
    session = LinkedInSession.from_settings(Settings(linkedin_li_at=f'"{"a" * 60}"'))
    assert session.li_at == "a" * 60


def test_jsessionid_quotes_are_stripped_before_use():
    """LinkedIn stores it quoted sometimes and bare others; both must work."""
    session = LinkedInSession.from_settings(
        Settings(linkedin_li_at="a" * 60, linkedin_jsessionid='"ajax:12345"')
    )
    assert session.jsessionid == "ajax:12345"


# --- configuration guards --------------------------------------------------


def test_plain_socks5_proxy_is_rejected():
    """socks5:// resolves DNS locally; the failure looks exactly like a block."""
    with pytest.raises(ValueError, match="socks5h"):
        Settings(proxy_url="socks5://127.0.0.1:9050")


def test_socks5h_proxy_is_accepted():
    assert Settings(proxy_url="socks5h://127.0.0.1:9050").proxy_url.startswith("socks5h")


def test_auto_mode_resolves_by_whether_a_session_exists():
    assert Settings(fetch_mode="auto", linkedin_li_at="").effective_mode == "fixture"
    assert Settings(fetch_mode="auto", linkedin_li_at="a" * 60).effective_mode == "live"
