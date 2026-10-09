"""Production entrypoint: `python -m app.serve` (the container CMD).

Binds `BUGTRACE_HOST` and `PORT` (Cloud Run) or `BUGTRACE_PORT`. Local development
keeps using `make dev` (uvicorn --reload on 127.0.0.1)."""

from typing import Any

import uvicorn

from app.config import Settings, get_settings
from app.logs import configure_logging

# Cloud Run sends SIGTERM and kills the instance 10s later; finish shutdown before that
# so in-flight runs are marked FAILED instead of being cut off mid-write.
GRACEFUL_SHUTDOWN_SECONDS = 8


def server_options(settings: Settings) -> dict[str, Any]:
    return {
        "host": settings.host,
        "port": settings.port,
        "log_config": None,  # keep configure_logging's handlers
        "access_log": True,
        "proxy_headers": True,  # Cloud Run terminates TLS in front of the container
        "forwarded_allow_ips": "*",
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
    }


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run("app.main:app", **server_options(settings))


if __name__ == "__main__":
    main()
