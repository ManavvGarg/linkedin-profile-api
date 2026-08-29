"""FastAPI application."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .errors import BatchTooLarge, InvalidProfileURL, ProfileAPIError
from .models import BatchResponse, HealthResponse, ProfileResponse
from .service import ProfileService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

VERSION = "1.0.0"

DESCRIPTION = """
Turns a LinkedIn profile URL into structured JSON.

Data comes from LinkedIn's internal **Voyager** API — the private JSON API
linkedin.com's own frontend calls — authenticated with a session cookie. That is
the same approach the established commercial scrapers take, and it is the only
one that can satisfy this contract: LinkedIn redacts job titles, dates, skills
and certifications *server-side* for logged-out clients, so those fields cannot
be scraped anonymously at any level of effort.

**Every response carries provenance.** The `provenance` map says, per section,
where the data came from and whether it is complete or was withheld — so an
empty `skills` array is distinguishable from a redacted one.

Voyager is undocumented and unversioned. Endpoint shapes change without notice;
this service reports that as `upstream_schema_drift` rather than quietly
returning a half-empty profile.
"""

_service: ProfileService | None = None


def get_service(settings: Annotated[Settings, Depends(get_settings)]) -> ProfileService:
    global _service
    if _service is None:
        _service = ProfileService(settings)
    return _service


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Enforce the API's own auth, when any keys are configured."""
    keys = settings.api_key_set
    if not keys:
        return  # open by design when unconfigured; fine locally, not in public
    if x_api_key not in keys:
        raise ProfileAPIError(
            "Missing or invalid API key.",
            remediation="Send a valid key in the X-API-Key header.",
        ) from None


def _checked_session_cookie(value: str | None, settings: Settings) -> str | None:
    """Gate the per-request credential on the deployment allowing it."""
    if not value:
        return None
    if not settings.allow_session_override:
        raise ProfileAPIError(
            "This deployment does not accept a per-request session cookie.",
            remediation=(
                "Omit `session_cookie` to use the server's configured session, or "
                "set ALLOW_SESSION_OVERRIDE=true."
            ),
        )
    return value


app = FastAPI(
    title="LinkedIn Profile API",
    description=DESCRIPTION,
    version=VERSION,
    docs_url="/",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(ProfileAPIError)
async def handle_api_error(_: Request, exc: ProfileAPIError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("%s: %s", exc.code, exc.message)
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


SESSION_COOKIE_DOC = (
    "Optional `li_at` cookie to use for this request instead of the server's "
    "configured session. Sent over HTTPS only — this value is a full LinkedIn "
    "login, not a scoped API key, so anyone holding it can act as that account. "
    "It is never logged, never echoed back, and cached results are namespaced "
    "per credential so one caller's data is never served to another."
)


class ProfileRequest(BaseModel):
    url: str = Field(
        description="A LinkedIn profile URL, or a bare public id.",
        examples=["https://www.linkedin.com/in/williamhgates"],
    )
    refresh: bool = Field(default=False, description="Bypass the cache.")
    allow_guest_fallback: bool | None = Field(
        default=None,
        description=(
            "Fall back to the logged-out page if the authenticated lookup fails. "
            "Returns heavily reduced data and risks an hours-long IP block; "
            "defaults to the ENABLE_GUEST_FALLBACK setting."
        ),
    )
    session_cookie: str | None = Field(default=None, description=SESSION_COOKIE_DOC)


class BatchRequest(BaseModel):
    """Batch input.

    Accepts `urls` as a list, or `url` as a single string, so the same endpoint
    serves both shapes rather than forcing callers to special-case one profile.
    """

    urls: list[str] | None = Field(
        default=None,
        description="LinkedIn profile URLs or public ids.",
        examples=[["https://www.linkedin.com/in/williamhgates", "andrewyng"]],
    )
    url: str | None = Field(
        default=None, description="A single profile URL. Merged with `urls` if both are given."
    )
    refresh: bool = Field(default=False, description="Bypass the cache.")
    allow_guest_fallback: bool | None = None
    session_cookie: str | None = Field(default=None, description=SESSION_COOKIE_DOC)

    def resolved_urls(self, limit: int) -> list[str]:
        """Merge the two input shapes, de-duplicate, and enforce the cap.

        De-duplication happens here rather than downstream because a repeated
        URL in one batch is almost always a caller mistake, and silently paying
        for it twice would spend a rate budget the caller cannot see.
        """
        merged = list(self.urls or [])
        if self.url:
            merged.append(self.url)

        seen: set[str] = set()
        unique = [u for u in merged if u.strip() and not (u in seen or seen.add(u))]

        if not unique:
            raise InvalidProfileURL(
                "Supply at least one profile URL, in `urls` (a list) or `url` (a string)."
            )
        if len(unique) > limit:
            raise BatchTooLarge(
                f"{len(unique)} URLs requested but the limit is {limit}.",
                detail={"requested": len(unique), "limit": limit},
            )
        return unique


@app.get(
    "/v1/profile",
    response_model=ProfileResponse,
    response_model_exclude_none=False,  # explicit nulls are part of the contract
    summary="Fetch a profile by URL",
    dependencies=[Depends(require_api_key)],
    tags=["profile"],
)
def get_profile(
    service: Annotated[ProfileService, Depends(get_service)],
    url: Annotated[str, Query(description="LinkedIn profile URL or public id.")],
    refresh: Annotated[bool, Query(description="Bypass the cache.")] = False,
    allow_guest_fallback: Annotated[bool | None, Query()] = None,
) -> ProfileResponse:
    """Fetch one profile.

    No `session_cookie` parameter here on purpose: query strings land in server
    logs, proxy logs and browser history, which is the wrong place for a
    credential. Use POST if you need to supply your own session.
    """
    return service.get_profile(url, refresh=refresh, allow_guest=allow_guest_fallback)


@app.post(
    "/v1/profile",
    response_model=ProfileResponse,
    response_model_exclude_none=False,
    summary="Fetch a profile by URL (JSON body)",
    dependencies=[Depends(require_api_key)],
    tags=["profile"],
)
def post_profile(
    body: ProfileRequest,
    service: Annotated[ProfileService, Depends(get_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProfileResponse:
    return service.get_profile(
        body.url,
        refresh=body.refresh,
        allow_guest=body.allow_guest_fallback,
        session_cookie=_checked_session_cookie(body.session_cookie, settings),
    )


@app.post(
    "/v1/profiles",
    response_model=BatchResponse,
    response_model_exclude_none=False,
    summary="Fetch several profiles in one call",
    dependencies=[Depends(require_api_key)],
    tags=["profile"],
)
def post_profiles(
    body: BatchRequest,
    service: Annotated[ProfileService, Depends(get_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchResponse:
    """Resolve a batch of profiles.

    Synchronous and sequential, capped at MAX_BATCH_SIZE. Every URL is reported
    independently, so one failure does not discard the rest of the batch —
    check `status` per item rather than assuming the whole call succeeded.

    Expect roughly ten seconds per uncached profile: lookups are throttled on
    purpose, because bursts are what get a LinkedIn session restricted. Repeats
    within a batch are served from cache.
    """
    urls = body.resolved_urls(settings.max_batch_size)
    return service.get_profiles(
        urls,
        refresh=body.refresh,
        allow_guest=body.allow_guest_fallback,
        session_cookie=_checked_session_cookie(body.session_cookie, settings),
    )


@app.get("/v1/health", response_model=HealthResponse, tags=["ops"])
def health(
    service: Annotated[ProfileService, Depends(get_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """Liveness plus session state.

    Deliberately does not call LinkedIn — a health check that spends rate budget
    is a health check that eventually causes the outage it is meant to detect.
    Use /v1/session/check to probe the session on purpose.
    """
    mode = settings.effective_mode
    state = service.session_state.value
    degraded = mode == "fixture" or state in ("expired", "challenged", "invalid")
    return HealthResponse(
        status="degraded" if degraded else "ok",
        mode=mode,
        session_configured=settings.has_session,
        session_state=state,
        cache_entries=service.cache_size,
        version=VERSION,
    )


@app.post("/v1/session/check", tags=["ops"], dependencies=[Depends(require_api_key)])
def session_check(service: Annotated[ProfileService, Depends(get_service)]) -> dict[str, str]:
    """Probe the session against LinkedIn. Costs one upstream request."""
    return {"session_state": service.check_session().value}


@app.get("/v1/fixtures", tags=["ops"])
def list_fixtures(service: Annotated[ProfileService, Depends(get_service)]) -> dict[str, list[str]]:
    """Sample profiles served in fixture mode."""
    return {"fixtures": service.available_fixtures()}
