"""uv run sda-api: serve the HTTP API (host and port from ``ApiSettings``)."""

import uvicorn

from support_desk_assistant.config import get_settings


def main() -> None:
    api = get_settings().api
    uvicorn.run(
        "support_desk_assistant.api.app:create_app", factory=True, host=api.host, port=api.port
    )


if __name__ == "__main__":
    main()
