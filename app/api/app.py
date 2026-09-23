from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from app.api.errors import rab_error_handler, unexpected_error_handler
from app.api.routes import router
from app.bootstrap import AppServices, create_services
from app.domain.errors import RABError


logger = logging.getLogger(__name__)


def create_app(
    state_dir: Path | None = None,
    services: AppServices | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_services = services or create_services(state_dir)
        application.state.services = active_services
        try:
            yield
        finally:
            result = active_services.runtime_manager.shutdown()
            application.state.runtime_shutdown_result = result
            if result.completed:
                logger.info("runtime manager shutdown completed")
            else:
                logger.error(
                    "runtime manager shutdown incomplete",
                    extra={"runtime_shutdown_completed": False},
                )

    application = FastAPI(title="Remote AI Bridge API", lifespan=lifespan)
    application.add_exception_handler(RABError, rab_error_handler)
    application.add_exception_handler(Exception, unexpected_error_handler)
    application.include_router(router)
    return application


# Importing this module creates only the ASGI application object. Service and
# RuntimeManager construction remains deferred to the lifespan startup hook.
app = create_app()
