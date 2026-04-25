from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import ClientException
from litestar.exceptions import MethodNotAllowedException
from litestar.template import TemplateConfig

from oauth.db import init_db
from oauth.routes.api.dashboard import router as dashboard_router
from oauth.routes.api.service import router as service_router
from oauth.routes.api.testing import router as testing_router
from oauth.routes.pages.oauth import router as pages_router


def _on_startup(_app: Litestar) -> None:
    init_db()


def _client_error_handler(_request: Request[Any, Any, Any], exc: ClientException) -> Response[Any]:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if isinstance(exc, MethodNotAllowedException):
        allow = "POST"
        if exc.headers:
            allow = exc.headers.get("allow", allow)
        return Response(
            content={"error": "method_not_allowed", "message": detail},
            status_code=405,
            headers={"Allow": allow},
        )
    return Response(content={"error": "validation_error", "message": detail}, status_code=400)


app = Litestar(
    route_handlers=[service_router, pages_router, dashboard_router, testing_router],
    on_startup=[_on_startup],
    template_config=TemplateConfig(
        directory=Path(__file__).parent / "templates",
        engine=JinjaTemplateEngine,
    ),
    exception_handlers={ClientException: _client_error_handler},
)
