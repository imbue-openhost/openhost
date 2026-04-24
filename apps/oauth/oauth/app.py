from pathlib import Path

from litestar import Litestar
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.template import TemplateConfig

from oauth.db import init_db
from oauth.routes.api.dashboard import router as dashboard_router
from oauth.routes.api.service import router as service_router
from oauth.routes.api.testing import router as testing_router
from oauth.routes.pages.oauth import router as pages_router


def _on_startup(_app: Litestar) -> None:
    init_db()


app = Litestar(
    route_handlers=[service_router, pages_router, dashboard_router, testing_router],
    on_startup=[_on_startup],
    template_config=TemplateConfig(
        directory=Path(__file__).parent / "templates",
        engine=JinjaTemplateEngine,
    ),
)
