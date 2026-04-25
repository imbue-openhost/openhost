from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import ClientException
from litestar.template import TemplateConfig

from oauth.db import init_db
from oauth.routes.api.dashboard import router as api_dashboard_router
from oauth.routes.api.device import router as api_device_router
from oauth.routes.api.testing import router as testing_router
from oauth.routes.pages.dashboard import router as pages_dashboard_router
from oauth.routes.pages.oauth import router as pages_oauth_router
from oauth.routes.service.oauth import router as service_router


def _on_startup(_app: Litestar) -> None:
    init_db()


def _client_error_handler(_request: Request[Any, Any, Any], exc: ClientException) -> Response[Any]:
    """Reformat Litestar's client errors (validation, malformed JSON) to match the spec's Error schema."""
    headers = dict(exc.headers) if exc.headers else None
    return Response(
        content={"error": "validation_error", "message": str(exc.detail)},
        status_code=exc.status_code,
        headers=headers,
    )


app = Litestar(
    route_handlers=[
        service_router,
        pages_oauth_router,
        pages_dashboard_router,
        api_dashboard_router,
        api_device_router,
        testing_router,
    ],
    on_startup=[_on_startup],
    template_config=TemplateConfig(
        directory=Path(__file__).parent / "templates",
        engine=JinjaTemplateEngine,
    ),
    exception_handlers={ClientException: _client_error_handler},
)
