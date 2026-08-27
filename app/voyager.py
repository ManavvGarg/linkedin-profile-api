"""Client for LinkedIn's internal Voyager API.

Voyager is the private JSON API linkedin.com's own frontend calls. It is not
documented, not versioned for third parties, and carries no compatibility
promise — so this client is built to fail loudly and specifically rather than
to pretend stability it does not have.

Two design points worth calling out:

Everything here is a plain HTTP request against a JSON endpoint. There is no
browser, no rendering engine and no JavaScript execution anywhere in this
client — `curl_cffi` is an HTTP library that reproduces Chrome's TLS handshake
fingerprint, which is a property of the socket, not a browser.

**Endpoint versions self-heal.** Voyager's `decorationId` and GraphQL `queryId`
carry version suffixes that LinkedIn rotates (`FullProfileWithEntities-91` and
`-93` were both live in different codebases simultaneously). Hardcoding one is
why scrapers break on a Tuesday. Known versions are tried against the API first,
so the normal path is a single JSON call; only when all of them fail does the
client read the current version out of a profile page and retry.

**Status codes do not mean what they look like.** Mapped explicitly in
`_classify`, because the intuitive reading is wrong in three of the five cases.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from curl_cffi import requests as crequests

from .config import Settings
from .errors import (
    EndpointRetired,
    ProfileNotFound,
    RateLimited,
    SchemaDrift,
    SessionChallenged,
    SessionExpired,
    SessionInvalid,
    UpstreamBlocked,
)
from .session import LinkedInSession, SessionState, Throttle, browser_headers

logger = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
VOYAGER = f"{BASE}/voyager/api"

# Tried in order against the JSON API before anything else. LinkedIn rotates
# this version suffix, so these WILL go stale — which is why the client can
# discover the current one and self-heal. Newest first.
_KNOWN_DECORATIONS = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-91",
)

_DECORATION_RE = re.compile(
    r"com\.linkedin\.voyager\.dash\.deco\.identity\.profile\.FullProfileWithEntities-\d+"
)
_QUERY_ID_RE = re.compile(r"voyagerIdentityDashProfiles\.[0-9a-f]{16,}")


@dataclass
class VoyagerResponse:
    payload: dict[str, Any]
    endpoint: str
    status_code: int


class VoyagerClient:
    def __init__(self, session: LinkedInSession, settings: Settings):
        self._session = session
        self._settings = settings
        self._throttle = Throttle(settings.min_delay_seconds, settings.max_delay_seconds)
        self._decoration: str | None = None
        self._query_id: str | None = None

    # --- transport ----------------------------------------------------------

    def _request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        throttle: bool = True,
    ) -> crequests.Response:
        if throttle:
            self._throttle.wait()

        kwargs: dict[str, Any] = {
            "headers": headers,
            "cookies": self._session.cookies(),
            "timeout": self._settings.request_timeout_seconds,
            "impersonate": self._settings.impersonate,
            # Critical: a dead session is a 302 to /uas/login. Following it
            # yields a 200 login page and the auth failure becomes invisible.
            "allow_redirects": False,
        }
        if self._settings.proxy_url:
            kwargs["proxies"] = {
                "http": self._settings.proxy_url,
                "https": self._settings.proxy_url,
            }

        response = crequests.get(url, **kwargs)
        self._capture_rotated_cookie(response)
        return response

    def _capture_rotated_cookie(self, response: crequests.Response) -> None:
        """Persist a rotated li_at so the session outlives the pasted cookie."""
        try:
            new_value = response.cookies.get("li_at")
        except Exception:  # pragma: no cover - cookie jar shapes vary
            new_value = None
        if new_value and self._session.observe_rotation(new_value):
            logger.info("LinkedIn rotated li_at; now using the refreshed value.")

    def _classify(self, response: crequests.Response, *, endpoint: str) -> None:
        """Raise the right error for a non-200. Order matters."""
        status = response.status_code

        if status == 200:
            return

        # A redirect to the login wall is the ONLY reliable signal that the
        # session itself is dead.
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if "/uas/login" in location or "/checkpoint/" in location:
                if "/checkpoint/challenge" in location:
                    self._session.mark(SessionState.CHALLENGED, location)
                    raise SessionChallenged(
                        "LinkedIn redirected to a verification challenge.",
                        detail={"location": location},
                    )
                self._session.mark(SessionState.EXPIRED, location)
                raise SessionExpired(
                    "LinkedIn redirected to the login wall — the session cookie is dead.",
                    detail={"location": location},
                )
            raise SchemaDrift(
                f"Unexpected redirect from {endpoint}.", detail={"location": location}
            )

        if status == 999:
            raise UpstreamBlocked(
                "LinkedIn returned 999 (blocked).",
                detail={"endpoint": endpoint},
            )

        if status == 429:
            raise RateLimited("LinkedIn rate-limited this request.", detail={"endpoint": endpoint})

        if status == 410:
            raise EndpointRetired(
                f"LinkedIn retired {endpoint} (410 Gone).",
                detail={"endpoint": endpoint},
            )

        if status == 403:
            # Counter-intuitive but consistent: 403 is a CSRF mismatch, not an
            # expired session. Treating it as expired sends operators to
            # re-copy a cookie that was never the problem.
            body = (response.text or "")[:200]
            raise SessionInvalid(
                "LinkedIn rejected the CSRF token. The csrf-token header must equal "
                "the JSESSIONID cookie.",
                detail={"endpoint": endpoint, "body": body},
            )

        if status == 401:
            self._session.mark(SessionState.INVALID)
            raise SessionInvalid(
                "LinkedIn rejected the session.", detail={"endpoint": endpoint}
            )

        if status == 404:
            raise ProfileNotFound("LinkedIn has no such profile.", detail={"endpoint": endpoint})

        raise SchemaDrift(
            f"Unexpected status {status} from {endpoint}.",
            detail={"endpoint": endpoint, "body": (response.text or "")[:200]},
        )

    def _get_json(self, url: str, *, endpoint: str, referer: str | None = None) -> dict[str, Any]:
        response = self._request(
            url, headers=self._session.voyager_headers(referer_public_id=referer)
        )
        self._classify(response, endpoint=endpoint)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SchemaDrift(
                f"{endpoint} returned a 200 that is not JSON.",
                detail={"body": (response.text or "")[:200]},
            ) from exc
        if not isinstance(payload, dict):
            raise SchemaDrift(f"{endpoint} returned {type(payload).__name__}, expected object.")

        # LinkedIn returns 410 as a 200-with-body on some paths.
        if payload.get("data", {}).get("status") == 410 or payload.get("status") == 410:
            raise EndpointRetired(f"{endpoint} answered 410 Gone in the response body.")

        return payload

    # --- profile endpoints --------------------------------------------------

    def _try_dash_profiles(self, public_id: str, decoration: str) -> VoyagerResponse:
        encoded = quote(public_id, safe="")
        url = (
            f"{VOYAGER}/identity/dash/profiles"
            f"?q=memberIdentity&memberIdentity={encoded}&decorationId={decoration}"
        )
        payload = self._get_json(url, endpoint="dash/profiles", referer=public_id)
        self._session.mark(SessionState.VALID)
        return VoyagerResponse(
            payload=payload, endpoint=f"dash/profiles[{decoration}]", status_code=200
        )

    def fetch_profile(self, public_id: str) -> VoyagerResponse:
        """Fetch the main profile blob.

        Order matters. Known `decorationId` versions are tried against the JSON
        API first, so the normal path is API calls only — no page fetch, and one
        request rather than two. Only when every known version has failed in a
        drift-shaped way does the client fall back to reading the current
        version out of a profile page, then retry the API with it.

        That keeps the common case pure API while retaining the ability to
        self-heal when LinkedIn rotates the version, which it does.

        The legacy `profileView` endpoint is deliberately absent: it was retired
        around September 2025 and now answers 410. Calling it would only waste a
        round trip against the rate budget.
        """
        errors: list[str] = []

        # 1 — the normal path: known versions, JSON API only.
        for decoration in _KNOWN_DECORATIONS:
            try:
                return self._try_dash_profiles(public_id, decoration)
            except (EndpointRetired, SchemaDrift, ProfileNotFound) as exc:
                # Only version-shaped failures are worth another candidate;
                # auth and rate-limit errors propagate immediately.
                errors.append(f"{decoration}: {exc}")

        # 2 — self-heal: every known version failed, so find the current one.
        logger.warning(
            "All %d known decorationId versions failed; attempting version discovery.",
            len(_KNOWN_DECORATIONS),
        )
        decoration, query_id = self.discover_versions(public_id)

        if decoration and decoration not in _KNOWN_DECORATIONS:
            try:
                response = self._try_dash_profiles(public_id, decoration)
                logger.info(
                    "Recovered using discovered decorationId %s. Consider adding it to "
                    "_KNOWN_DECORATIONS to avoid the discovery round trip.",
                    decoration,
                )
                return response
            except (EndpointRetired, SchemaDrift, ProfileNotFound) as exc:
                errors.append(f"{decoration} (discovered): {exc}")

        if query_id:
            encoded = quote(public_id, safe="")
            url = (
                f"{VOYAGER}/graphql?includeWebMetadata=true"
                f"&variables=(vanityName:{encoded})&queryId={query_id}"
            )
            try:
                payload = self._get_json(url, endpoint="graphql", referer=public_id)
                self._session.mark(SessionState.VALID)
                return VoyagerResponse(
                    payload=payload, endpoint=f"graphql[{query_id}]", status_code=200
                )
            except (EndpointRetired, SchemaDrift, ProfileNotFound) as exc:
                errors.append(f"graphql: {exc}")

        raise SchemaDrift(
            "Every known profile endpoint failed, and version discovery did not "
            "recover. LinkedIn's internal API has most likely changed shape.",
            detail={"attempts": errors},
        )

    # --- version discovery (fallback only) ----------------------------------

    def discover_versions(self, public_id: str) -> tuple[str | None, str | None]:
        """Read the current decorationId and GraphQL queryId off a profile page.

        Used only after every known version has failed. LinkedIn's own
        JavaScript necessarily names the versions its frontend is calling, which
        makes the page the most reliable source for them.

        This is a plain HTTP GET parsed with a regex — no browser, no rendering,
        no JavaScript execution. Result is cached for the client's lifetime so
        the cost is paid at most once.
        """
        if self._decoration or self._query_id:
            return self._decoration, self._query_id

        url = f"{BASE}/in/{quote(public_id)}/"
        try:
            response = self._request(url, headers=browser_headers(self._session.user_agent))
        except Exception as exc:  # discovery is best-effort by design
            logger.warning("Version discovery request failed: %s", exc)
            return None, None

        if response.status_code != 200:
            logger.warning("Version discovery got HTTP %s.", response.status_code)
            return None, None

        html = response.text or ""
        if match := _DECORATION_RE.search(html):
            self._decoration = match.group(0)
            logger.info("Discovered decorationId %s", self._decoration)
        if match := _QUERY_ID_RE.search(html):
            self._query_id = match.group(0)
            logger.info("Discovered queryId %s", self._query_id)

        return self._decoration, self._query_id

    def fetch_skills(self, public_id: str) -> dict[str, Any] | None:
        """Skills, which the main blob often truncates. Best-effort."""
        url = f"{VOYAGER}/identity/profiles/{quote(public_id, safe='')}/skills?count=100"
        try:
            return self._get_json(url, endpoint="profiles/skills", referer=public_id)
        except (SchemaDrift, EndpointRetired, ProfileNotFound) as exc:
            logger.info("Skills endpoint unavailable: %s", exc)
            return None

    def fetch_network_info(self, public_id: str) -> dict[str, Any] | None:
        """Connection and follower counts. Best-effort."""
        url = f"{VOYAGER}/identity/profiles/{quote(public_id, safe='')}/networkinfo"
        try:
            return self._get_json(url, endpoint="profiles/networkinfo", referer=public_id)
        except (SchemaDrift, EndpointRetired, ProfileNotFound) as exc:
            logger.info("Network info endpoint unavailable: %s", exc)
            return None

    def check_session(self) -> SessionState:
        """Probe `/voyager/api/me` to classify the session.

        Worth doing before concluding anything from a 302/401/403 on a data
        endpoint, since those have causes unrelated to session health.
        """
        try:
            self._get_json(f"{VOYAGER}/me", endpoint="me")
        except (SessionExpired, SessionChallenged, SessionInvalid):
            return self._session.state
        except Exception:
            return SessionState.UNKNOWN
        self._session.mark(SessionState.VALID)
        return SessionState.VALID
