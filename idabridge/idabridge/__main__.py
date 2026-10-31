"""Start the server: `python -m idabridge` (this is what the NSSM service runs)."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from . import config
from .api import create_app


def main() -> None:
    settings = config.load()
    if not settings.token and os.environ.get("IDABRIDGE_NO_AUTH") != "1":
        sys.exit("Set IDABRIDGE_TOKEN (any long random string), or IDABRIDGE_NO_AUTH=1 to run without one.")
    if settings.host not in ("127.0.0.1", "localhost") and not settings.host.startswith("100."):
        logging.warning("Listening on %s: make sure only your tailnet can reach it.", settings.host)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info",
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
