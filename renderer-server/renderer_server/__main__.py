from __future__ import annotations

import sys

import uvicorn

from .app import create_app
from .config import ConfigError, Settings


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    ssl_kwargs = {}
    if not settings.dev_http:
        ssl_kwargs = {
            "ssl_certfile": str(settings.tls_cert),
            "ssl_keyfile": str(settings.tls_key),
        }

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        **ssl_kwargs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
