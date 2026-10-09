"""Process-wide logging. `json` emits one structured line per record on stdout, which
Cloud Run / Cloud Logging parses (`severity`, `message`); `text` is for local use."""

import json
import logging
import sys
from datetime import UTC, datetime

from app.config import Settings

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        }
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(CloudLoggingFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    # Route uvicorn's own loggers through the root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
