"""API key check for ``/ask`` and ``/tickets`` (docs/decisions.md #31).

One shared key, sent in the ``ApiSettings.key_header`` header. The API fails closed: it will not
start without a key unless ``allow_no_key`` is on and it is bound to a loopback address.
"""

import logging
from collections.abc import Callable
from secrets import compare_digest

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from support_desk_assistant.config import Settings

log = logging.getLogger(__name__)

NO_KEY_HELP = (
    "SDA_API_KEY is not set. Generate one with "
    "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and put it in .env "
    "(the variable name is in .env.example). For local development only, "
    "SDA_API__ALLOW_NO_KEY=true serves without a key on a loopback host."
)


class AuthConfigError(RuntimeError):
    """The API cannot start safely with this configuration."""


def _key(settings: Settings) -> str:
    return settings.api_key.get_secret_value() if settings.api_key else ""


def check_auth_config(settings: Settings) -> None:
    api = settings.api
    key = _key(settings)
    if key:
        if len(key) < api.min_key_chars:
            raise AuthConfigError(f"SDA_API_KEY must be at least {api.min_key_chars} characters")
        return
    if not api.allow_no_key:
        raise AuthConfigError(NO_KEY_HELP)
    if api.host not in api.loopback_hosts:
        raise AuthConfigError(
            f"serving without a key is only allowed on a loopback host, not {api.host}"
        )
    log.warning("API key check is off (SDA_API__ALLOW_NO_KEY): loopback only, development only")


def make_guard(settings: Settings) -> Callable[..., None]:
    """A FastAPI dependency that answers 401 unless the request carries the key."""
    check_auth_config(settings)
    key = _key(settings)
    expected = key.encode() if key else None
    header = APIKeyHeader(name=settings.api.key_header, auto_error=False)

    def guard(provided: str | None = Security(header)) -> None:
        if expected is None:
            return
        # Same answer for a missing and a wrong key; compare_digest takes constant time.
        if provided is None or not compare_digest(provided.encode(), expected):
            raise HTTPException(
                401, "missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"}
            )

    return guard
