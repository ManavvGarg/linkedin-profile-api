"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FetchMode = Literal["live", "fixture", "auto"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LinkedIn session
    linkedin_li_at: str = ""
    linkedin_jsessionid: str = ""
    linkedin_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # Fetch behaviour
    fetch_mode: FetchMode = "auto"
    impersonate: str = "chrome131"
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
