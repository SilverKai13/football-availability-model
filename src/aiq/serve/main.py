"""Process entry point: ``aiq-api``."""

import uvicorn

from aiq.serve.api import configure_logging, create_app
from aiq.serve.config import Settings


def run() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
