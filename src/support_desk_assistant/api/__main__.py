"""uv run sda-api: serve the HTTP API (host and port from ``ApiSettings``)."""

import sys

import uvicorn

from support_desk_assistant.api.auth import AuthConfigError, check_auth_config
from support_desk_assistant.config import get_settings


def main() -> None:
    settings = get_settings()
    try:
        check_auth_config(settings)
    except AuthConfigError as exc:
        sys.exit(f"sda-api: {exc}")
    api = settings.api
    uvicorn.run(
        "support_desk_assistant.api.app:create_app", factory=True, host=api.host, port=api.port
    )


if __name__ == "__main__":
    main()
