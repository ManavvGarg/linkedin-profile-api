"""Orchestration: cache, then Voyager, then optionally the guest page."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cache import TTLCache
from .config import Settings
from .errors import (
    FixtureNotFound,
    ProfileAPIError,
    SessionMissing,
)
from .guest import fetch_guest_profile
from .models import (
    BatchItem,
    BatchResponse,
    Profile,
    ProfileResponse,
    RequestMeta,
    SectionProvenance,
)
from .normalize import merge_section, normalize_profile
from .resolve import extract_public_id
from .session import LinkedInSession, SessionState
from .voyager import VoyagerClient

logger = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Sections the contract promises, mapped to the Profile field they populate.
# These are fetched individually ONLY when the main profile payload did not
# already resolve them — every entry is an extra throttled round trip against
# the scarcest resource in the system, so a request that would duplicate data we
# already hold is not worth making.
EXTRA_SECTIONS = {
    "profileSkills": "skills",
    "profileCertifications": "certifications",
    "profileLanguages": "languages",
}


def _profile_urn(payload: dict[str, Any]) -> str | None:
    """Find the member URN, which the section endpoints key off."""
    for entity in payload.get("included") or []:
        if isinstance(entity, dict) and str(entity.get("$type", "")).endswith(".Profile"):
            if urn := entity.get("entityUrn"):
                return str(urn)
    data = payload.get("data")
    if isinstance(data, dict):
        elements = data.get("*elements") or data.get("elements")
        if isinstance(elements, list) and elements and isinstance(elements[0], str):
            return elements[0]
    return None

# Sections whose provenance is reported. Keys match Profile field names.
_SECTION_KEYS = (
    "identity",
    "headline",
    "summary",
    "location",
    "profile_picture",
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
    "counts",
)

# What the logged-out page genuinely cannot supply. Kept explicit so the guest
# path reports these as redacted rather than as legitimately empty.
_GUEST_REDACTED = frozenset(
    {"summary", "skills", "certifications", "languages"}
)
_GUEST_PARTIAL = frozenset({"experience", "education", "headline", "counts"})


class ProfileService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._cache: TTLCache[ProfileResponse] = TTLCache(
            ttl_seconds=settings.cache_ttl_seconds, max_entries=settings.cache_max_entries
        )
        self._session: LinkedInSession | None = None
        self._client: VoyagerClient | None = None

        if settings.effective_mode == "live":
            try:
                self._session = LinkedInSession.from_settings(settings)
                self._client = VoyagerClient(self._session, settings)
            except ProfileAPIError as exc:
                # Surface at request time with a useful message rather than
                # refusing to boot — /health should stay reachable.
                logger.error("Session unavailable: %s", exc.message)

    # --- introspection ------------------------------------------------------

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def session_state(self) -> SessionState:
        return self._session.state if self._session else SessionState.UNKNOWN

    def available_fixtures(self) -> list[str]:
        return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))

    def check_session(self) -> SessionState:
        if self._client is None:
            return SessionState.UNKNOWN
        return self._client.check_session()

    # --- session selection --------------------------------------------------

    def _client_for(self, session_cookie: str | None) -> tuple[VoyagerClient, str]:
        """Pick the Voyager client to use, and the cache namespace it owns.

        A caller may supply their own `li_at` per request. That makes the cache
        namespace load-bearing rather than cosmetic: what a session can see
        depends on who it is — connection degree changes which fields LinkedIn
        returns — so serving one account's cached profile to a request made with
        a different cookie would be both wrong and a privacy leak. Each
        credential therefore gets its own namespace, keyed by a hash so the
        cookie itself is never used as a key or written to a log.
        """
        if not session_cookie:
            if self._client is None:
                raise SessionMissing(
                    "No LinkedIn session is configured. Set LINKEDIN_LI_AT, or supply "
                    "session_cookie in the request body."
                )
            return self._client, "env"

        # Build an ephemeral session; never mutate the shared one.
        overridden = self._settings.model_copy(update={"linkedin_li_at": session_cookie})
        session = LinkedInSession.from_settings(overridden)
        fingerprint = hashlib.sha256(session.li_at.encode()).hexdigest()[:12]
        return VoyagerClient(session, overridden), f"req-{fingerprint}"

    # --- main entry point ---------------------------------------------------

    def get_profile(
        self,
        url_or_id: str,
        *,
        refresh: bool = False,
        allow_guest: bool | None = None,
        session_cookie: str | None = None,
    ) -> ProfileResponse:
        started = time.perf_counter()
        public_id = extract_public_id(url_or_id)

        mode = self._settings.effective_mode
        # Fixture mode never touches a session, so it shares one namespace.
        namespace = "fixture" if mode == "fixture" else None
        client: VoyagerClient | None = None
        if namespace is None:
            client, namespace = self._client_for(session_cookie)
        cache_key = f"{namespace}:{public_id}"

        if not refresh:
            if entry := self._cache.get(cache_key):
                cached = entry.value.model_copy(deep=True)
                cached.request.cached = True
                cached.request.cache_age_seconds = entry.age_seconds
                cached.request.requested_url = url_or_id
                cached.request.duration_ms = int((time.perf_counter() - started) * 1000)
                return cached

        if mode == "fixture":
            response = self._from_fixture(public_id, url_or_id)
        else:
            response = self._from_live(
                public_id,
                url_or_id,
                client=client,  # type: ignore[arg-type]
                allow_guest=(
                    self._settings.enable_guest_fallback if allow_guest is None else allow_guest
                ),
            )

        response.request.duration_ms = int((time.perf_counter() - started) * 1000)
        self._cache.set(cache_key, response)
        return response

    def get_profiles(
        self,
        urls: list[str],
        *,
        refresh: bool = False,
        allow_guest: bool | None = None,
        session_cookie: str | None = None,
    ) -> BatchResponse:
        """Resolve several profiles in one call.

        Deliberately sequential. Concurrency here would multiply the request
        rate against the one resource that is genuinely scarce, and the throttle
        between upstream calls exists precisely to avoid the burst pattern that
        gets sessions restricted. Batching is a convenience for the caller, not
        a way to go faster.

        Duplicate URLs are resolved once — the second occurrence is a cache hit,
        which is why the cache is what makes this endpoint safe to expose.
        """
        started = time.perf_counter()
        results: list[BatchItem] = []

        for url in urls:
            try:
                response = self.get_profile(
                    url,
                    refresh=refresh,
                    allow_guest=allow_guest,
                    session_cookie=session_cookie,
                )
                results.append(BatchItem(url=url, status="ok", result=response))
            except ProfileAPIError as exc:
                results.append(BatchItem(url=url, status="error", error=exc.to_payload()["error"]))
            except Exception as exc:  # never let one bad URL discard the batch
                logger.exception("Unexpected failure for %s", url)
                results.append(
                    BatchItem(
                        url=url,
                        status="error",
                        error={"code": "internal_error", "message": str(exc)},
                    )
                )

        succeeded = sum(1 for r in results if r.status == "ok")
        return BatchResponse(
            requested=len(urls),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            duration_ms=int((time.perf_counter() - started) * 1000),
            results=results,
        )

    # --- sources ------------------------------------------------------------

    def _from_live(
        self,
        public_id: str,
        requested_url: str,
        *,
        client: VoyagerClient,
        allow_guest: bool,
    ) -> ProfileResponse:
        try:
            voyager = client.fetch_profile(public_id)
        except ProfileAPIError as exc:
            if not allow_guest:
                raise
            logger.warning(
                "Voyager failed (%s); falling back to the logged-out page.", exc.code
            )
            profile, warnings = fetch_guest_profile(public_id, self._settings)
            warnings.insert(0, f"Authenticated lookup failed: {exc.code} — {exc.message}")
            return self._build(
                profile, public_id, requested_url, source="guest_page", warnings=warnings
            )

        profile, warnings = normalize_profile(voyager.payload, public_id=public_id)

        # Backfill only what the main payload did not already carry.
        gaps = [s for s, field in EXTRA_SECTIONS.items() if not getattr(profile, field)]
        if gaps and self._settings.backfill_empty_sections:
            urn = _profile_urn(voyager.payload)
            if urn:
                for section in gaps:
                    payload = client.fetch_section(urn, section, public_id=public_id)
                    if payload is None:
                        warnings.append(f"Could not retrieve the {section} section.")
                    elif warning := merge_section(profile, section, payload):
                        warnings.append(warning)
            else:
                warnings.append(
                    "No profile URN was present, so empty sections could not be backfilled."
                )

        return self._build(
            profile,
            public_id,
            requested_url,
            source="voyager",
            warnings=warnings,
            endpoint=voyager.endpoint,
        )

    def _from_fixture(self, public_id: str, requested_url: str) -> ProfileResponse:
        path = FIXTURE_DIR / f"{public_id}.json"
        if not path.exists():
            raise FixtureNotFound(
                f"No bundled fixture for {public_id!r}.",
                detail={"available": self.available_fixtures()},
            )

        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        profile, warnings = normalize_profile(raw, public_id=public_id)
        warnings.insert(
            0,
            "Served from a bundled fixture, not from LinkedIn. Set LINKEDIN_LI_AT and "
            "FETCH_MODE=live for real lookups.",
        )
        return self._build(
            profile, public_id, requested_url, source="fixture", warnings=warnings
        )

    # --- assembly -----------------------------------------------------------

    def _build(
        self,
        profile: Profile,
        public_id: str,
        requested_url: str,
        *,
        source: str,
        warnings: list[str],
        endpoint: str | None = None,
    ) -> ProfileResponse:
        return ProfileResponse(
            request=RequestMeta(
                requested_url=requested_url,
                resolved_public_id=public_id,
                retrieved_at=datetime.now(UTC),
                cached=False,
            ),
            source=source,  # type: ignore[arg-type]
            provenance=self._provenance(profile, source, endpoint),
            warnings=warnings,
            profile=profile,
        )

    def _provenance(
        self, profile: Profile, source: str, endpoint: str | None
    ) -> dict[str, SectionProvenance]:
        """Describe each section so callers can tell empty from withheld.

        The distinction matters: a genuinely empty skills list and one that was
        redacted behind the login wall are different facts, and a consumer that
        cannot tell them apart will treat withheld data as absent data.
        """
        note = f"Voyager endpoint: {endpoint}" if endpoint else None
        out: dict[str, SectionProvenance] = {}

        for key in _SECTION_KEYS:
            if source == "guest_page":
                redacted = key in _GUEST_REDACTED
                partial = key in _GUEST_PARTIAL
                out[key] = SectionProvenance(
                    source="guest_page",
                    complete=not (redacted or partial),
                    redacted=redacted,
                    note=(
                        "LinkedIn withholds this from logged-out clients."
                        if redacted
                        else "Partially available without authentication."
                        if partial
                        else None
                    ),
                )
            else:
                out[key] = SectionProvenance(
                    source=source,  # type: ignore[arg-type]
                    complete=True,
                    redacted=False,
                    note=note,
                )

        # An empty section from an authenticated call is worth flagging: it is
        # usually genuine, but it is also how upstream drift first shows up.
        if source in ("voyager", "fixture"):
            for key in ("experience", "education", "skills", "certifications", "languages"):
                if not getattr(profile, key, None):
                    out[key].complete = False
                    out[key].note = (
                        "Empty. Either the member has no entries, or LinkedIn changed "
                        "this section's shape."
                    )

        return out
