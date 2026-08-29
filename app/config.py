"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FetchMode = Literal["live", "fixture", "auto"]


def _chrome_major(text: str) -> int | None:
    """Pull a Chrome major version out of a UA string or an impersonate target."""
    match = re.search(r"[Cc]hrome[/_]?(\d+)", text or "")
    return int(match.group(1)) if match else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LinkedIn session
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    # MUST match the browser the li_at cookie was copied from. LinkedIn binds a
    # session to its browser fingerprint: replaying a cookie under a stale or
    # mismatched User-Agent reads as session hijacking, and LinkedIn responds by
    # invalidating the cookie outright — a 302 carrying `li_at=delete me` and
    # `clear-site-data`. Observed directly with a Chrome/131 UA in 2026.
    linkedin_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )

    # Fetch behaviour
    fetch_mode: FetchMode = "auto"
    # Keep the major version aligned with linkedin_user_agent; see the validator
    # below, which refuses a wide mismatch rather than letting it burn a session.
    impersonate: str = "chrome150"
    proxy_url: str = ""
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    request_timeout_seconds: int = 30

    # Cache
    cache_ttl_seconds: int = 86_400
    cache_max_entries: int = 1_000

    # This API's own auth
    api_keys: str = ""
    cors_origins: str = "*"

    # Guest fallback. Off by default: the guest path returns asterisk-masked
    # data and burns an IP-wide, multi-hour block after ~5 requests, so it is
    # only worth enabling when partial data genuinely beats no data.
    enable_guest_fallback: bool = Field(default=False)

    @field_validator("proxy_url")
    @classmethod
    def _warn_on_plain_socks5(cls, v: str) -> str:
        # socks5:// resolves DNS locally, which can hand back an address the
        # proxy cannot route. The failure is indistinguishable from a block, so
        # it is worth rejecting loudly rather than debugging it later.
        if v.startswith("socks5://"):
            raise ValueError(
                "Use socks5h:// rather than socks5:// so DNS resolves at the proxy. "
                "With socks5:// a resolution failure looks exactly like being blocked."
            )
        return v

    @model_validator(mode="after")
    def _check_fingerprint_alignment(self) -> Settings:
        """Refuse a User-Agent and TLS fingerprint that disagree badly.

        These two travel together as one identity. If the UA claims Chrome 150
        while the TLS handshake is Chrome 131's, the mismatch is visible to
        LinkedIn and gets the session cookie invalidated on the spot — a failure
        that surfaces as "logged out" and looks nothing like its cause.

        Only a wide gap is rejected, since curl_cffi ships a coarse set of
        targets and an exact match is often impossible.
        """
        ua_major = _chrome_major(self.linkedin_user_agent)
        imp_major = _chrome_major(self.impersonate)
        if ua_major and imp_major and abs(ua_major - imp_major) > 6:
            raise ValueError(
                f"LINKEDIN_USER_AGENT reports Chrome {ua_major} but IMPERSONATE is "
                f"'{self.impersonate}' (Chrome {imp_major}). LinkedIn treats that "
                "mismatch as session hijacking and will invalidate li_at. Pick an "
                "IMPERSONATE target close to your browser's real version."
            )
        return self

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    @property
    def has_session(self) -> bool:
        return bool(self.linkedin_li_at.strip())

    @property
    def effective_mode(self) -> Literal["live", "fixture"]:
        """Resolve `auto` against whether a session is actually configured."""
        if self.fetch_mode == "auto":
            return "live" if self.has_session else "fixture"
        return self.fetch_mode  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
