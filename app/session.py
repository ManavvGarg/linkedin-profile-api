"""LinkedIn session: cookies, headers, and the state machine around them.

Three things here are easy to get wrong and expensive to debug:

**The CSRF token is not a security control.** LinkedIn only checks that the
`csrf-token` header equals the `JSESSIONID` cookie — it never validates the
value. So a client can mint its own. That is why this module happily generates a
JSESSIONID when none is configured, and why a 403 means "your header and cookie
disagree", not "your session died".

**Redirects must not be followed.** Session death shows up as a 302 to
`/uas/login`. Follow it and you get a 200 with a login page, so the failure
looks like a parsing problem rather than an auth problem. One team reportedly
chased this into building a whole stealth browser when their cookies had simply
expired.

**The User-Agent is half the credential.** LinkedIn ties the session to the
browser fingerprint it was created in; replaying a cookie under a different UA
is a documented way to shorten its life.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum

from .config import Settings
from .errors import SessionMalformed, SessionMissing


class SessionState(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    EXPIRED = "expired"
    CHALLENGED = "challenged"
    INVALID = "invalid"


def mint_jsessionid() -> str:
    """Generate a JSESSIONID in LinkedIn's `ajax:<digits>` shape."""
    return f"ajax:{random.randrange(10**18, 10**19)}"


@dataclass
class LinkedInSession:
    """Holds the credential and the freshest cookie LinkedIn has handed back.

    Cookie rotation matters more than it looks: LinkedIn rotates `li_at`
    server-side, so a long-lived deployment that keeps replaying the originally
    pasted value will eventually be using a stale one. Reading the rotated
    cookie back off each response and preferring it is what lets a session
    outlive the cookie the operator actually pasted.
    """

    li_at: str
    user_agent: str
    jsessionid: str = field(default_factory=mint_jsessionid)

    state: SessionState = SessionState.UNKNOWN
    rotated_li_at: str | None = None
    last_checked_at: float | None = None
    last_error: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> LinkedInSession:
        raw = settings.linkedin_li_at.strip()
        if not raw:
            raise SessionMissing("No LinkedIn session cookie is configured.")

        # Tolerate the two most common paste mistakes rather than failing on them.
        if raw.lower().startswith("li_at="):
            raw = raw.split("=", 1)[1]
        raw = raw.strip().strip('"').strip("'")

        if len(raw) < 20 or " " in raw:
            raise SessionMalformed(
                f"LINKEDIN_LI_AT does not look like a session cookie "
                f"(length {len(raw)}, whitespace={' ' in raw})."
            )

        jsession = settings.linkedin_jsessionid.strip() or mint_jsessionid()
        # LinkedIn sometimes stores JSESSIONID wrapped in quotes and sometimes not,
        # so strip defensively rather than assuming either form.
        jsession = jsession.strip('"')

        return cls(li_at=raw, user_agent=settings.linkedin_user_agent, jsessionid=jsession)

    # --- credential access --------------------------------------------------

    @property
    def active_li_at(self) -> str:
        """The freshest cookie we hold: a rotated one if LinkedIn sent one."""
        with self._lock:
            return self.rotated_li_at or self.li_at

    def observe_rotation(self, new_value: str) -> bool:
        """Record a rotated li_at seen on a response. Returns True if it changed."""
        new_value = (new_value or "").strip().strip('"')
        if not new_value or len(new_value) < 20:
            return False
        with self._lock:
            if new_value == (self.rotated_li_at or self.li_at):
                return False
            self.rotated_li_at = new_value
            return True

    def mark(self, state: SessionState, error: str | None = None) -> None:
        with self._lock:
            self.state = state
            self.last_checked_at = time.time()
            self.last_error = error

    # --- wire format --------------------------------------------------------

    def cookies(self) -> dict[str, str]:
        return {"li_at": self.active_li_at, "JSESSIONID": f'"{self.jsessionid}"'}

    def voyager_headers(self, *, referer_public_id: str | None = None) -> dict[str, str]:
        """Headers for an authenticated Voyager call.

        `Host` is set explicitly because Voyager answers `400 invalid hostname`
        without it when the caller is not a browser — an easy hour to lose.
        """
        headers = {
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "accept-language": "en-US,en;q=0.9",
            "csrf-token": self.jsessionid,  # must equal the JSESSIONID cookie
            "host": "www.linkedin.com",
            "user-agent": self.user_agent,
            "x-restli-protocol-version": "2.0.0",
            "x-li-lang": "en_US",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if referer_public_id:
            headers["referer"] = f"https://www.linkedin.com/in/{referer_public_id}/"
        return headers


def browser_headers(user_agent: str) -> dict[str, str]:
    """Headers for a full-page (non-API) fetch.

    Completeness here is what decides whether LinkedIn answers at all. Measured
    directly: a bare User-Agent gets 999, while this set gets 200 from the same
    IP moments later. The `sec-fetch-*` and `sec-ch-ua` families are the ones
    doing the work — a plausible UA alone is not enough.
    """
    return {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": user_agent,
    }


class Throttle:
    """Jittered minimum spacing between upstream calls.

    Fixed delays are themselves a fingerprint, so the interval is randomised.
    This bounds burst rate only; it is not a substitute for the cache, which is
    what actually keeps request volume down.
    """

    def __init__(self, min_seconds: float, max_seconds: float):
        self._min = max(0.0, min_seconds)
        self._max = max(self._min, max_seconds)
        self._last: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            target = random.uniform(self._min, self._max)
            elapsed = time.time() - self._last
            if elapsed < target:
                time.sleep(target - elapsed)
            self._last = time.time()
