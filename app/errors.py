"""Typed errors.

The session-failure taxonomy here is deliberately five-way rather than a single
"auth failed". The five states need genuinely different responses from an
operator, and collapsing them — particularly EXPIRED and CHALLENGED — is the
most common integration bug in this space:

  MISSING     no cookie configured                -> set LINKEDIN_LI_AT
  MALFORMED   cookie present but not cookie-shaped -> re-copy it, paste error
  EXPIRED     cookie was valid, no longer is       -> log in again, re-copy
  CHALLENGED  cookie is fine, LinkedIn wants a PIN -> open LinkedIn, verify, re-copy
  INVALID     cookie rejected outright             -> wrong account or revoked

CHALLENGED in particular is not a credential problem at all: the account is
healthy and the cookie is real, but LinkedIn has interposed a verification step.
Telling an operator "expired" there sends them down the wrong path.
"""

from __future__ import annotations

from typing import Any


class ProfileAPIError(Exception):
    """Base for every error this service raises deliberately."""

    status_code: int = 500
    code: str = "internal_error"
    remediation: str | None = None

    def __init__(self, message: str, *, detail: Any = None, remediation: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if remediation is not None:
            self.remediation = remediation

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.remediation:
            body["error"]["remediation"] = self.remediation
        if self.detail is not None:
            body["error"]["detail"] = self.detail
        return body


# --- Input -----------------------------------------------------------------


class BatchTooLarge(ProfileAPIError):
    status_code = 413
    code = "batch_too_large"
    remediation = (
        "Split the request into smaller batches. The cap exists because lookups are "
        "sequential and rate-limited upstream, so a large batch would exceed typical "
        "HTTP timeouts long before it finished. Raise MAX_BATCH_SIZE if your "
        "deployment tolerates longer requests."
    )


class InvalidProfileURL(ProfileAPIError):
    status_code = 400
    code = "invalid_profile_url"
    remediation = (
        "Pass a LinkedIn member profile URL such as "
        "https://www.linkedin.com/in/williamhgates, or the bare public id."
    )


# --- Session ---------------------------------------------------------------


class SessionError(ProfileAPIError):
    status_code = 503
    code = "session_error"


class SessionMissing(SessionError):
    code = "session_missing"
    remediation = (
        "No LinkedIn session is available for this request. Either supply your own "
        "`session_cookie` in the POST body, configure LINKEDIN_LI_AT on the server, "
        "or run with FETCH_MODE=fixture to serve bundled sample profiles."
    )


class SessionMalformed(SessionError):
    code = "session_malformed"
    remediation = (
        "LINKEDIN_LI_AT does not look like a li_at cookie. Copy the cookie value "
        "only — not the whole `name=value` pair, and no surrounding quotes."
    )


class SessionExpired(SessionError):
    code = "session_expired"
    remediation = (
        "The session cookie is no longer accepted. Log in to LinkedIn again and "
        "copy a fresh li_at value."
    )


class SessionChallenged(SessionError):
    code = "session_challenged"
    remediation = (
        "LinkedIn is asking this account to verify itself (usually an emailed PIN). "
        "The cookie is fine — open LinkedIn in a browser, complete the check, then "
        "copy the li_at value again. This often follows a burst of automated traffic."
    )


class SessionInvalid(SessionError):
    code = "session_invalid"
    remediation = "The session cookie was rejected outright. It may have been revoked."


# --- Upstream --------------------------------------------------------------


class ProfileNotFound(ProfileAPIError):
    status_code = 404
    code = "profile_not_found"
    remediation = (
        "LinkedIn returned no profile for this identifier. Note that LinkedIn "
        "answers 999 for blocked and non-existent profiles alike, so a genuinely "
        "missing profile and a rate-limited request can be indistinguishable."
    )


class RateLimited(ProfileAPIError):
    status_code = 429
    code = "rate_limited"
    remediation = (
        "LinkedIn is refusing requests from this IP or session. Backing off does "
        "not clear it — the guest-path block is IP-scoped and lasts hours. Reduce "
        "request volume, and route through a different egress IP to recover."
    )


class UpstreamBlocked(ProfileAPIError):
    status_code = 502
    code = "upstream_blocked"
    remediation = (
        "LinkedIn returned its 999 block response. From a datacenter IP the "
        "www host blocks unconditionally; a residential egress or proxy is needed."
    )


class SchemaDrift(ProfileAPIError):
    """LinkedIn answered, but not in a shape we recognise.

    Raised rather than returning a half-empty profile, so drift surfaces as a
    loud failure instead of silently degraded data.
    """

    status_code = 502
    code = "upstream_schema_drift"
    remediation = (
        "LinkedIn's internal API changed shape. This is expected periodically — "
        "it is a private API with no compatibility guarantee. See README "
        "'Handling schema drift'."
    )


class EndpointRetired(SchemaDrift):
    code = "upstream_endpoint_retired"
    remediation = (
        "LinkedIn returned 410 Gone for this endpoint. The legacy profileView "
        "endpoint was retired around September 2025; the app should be using the "
        "dash profiles endpoint instead."
    )


class FixtureNotFound(ProfileAPIError):
    status_code = 404
    code = "fixture_not_found"
    remediation = (
        "Running in fixture mode, which serves only bundled sample profiles. "
        "Call GET /v1/fixtures to list them, or configure LINKEDIN_LI_AT for live "
        "lookups."
    )
