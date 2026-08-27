"""FastAPI application."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .errors import ProfileAPIError
from .models import HealthResponse, ProfileResponse
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
) -> ProfileResponse:
    return service.get_profile(
        body.url, refresh=body.refresh, allow_guest=body.allow_guest_fallback
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
