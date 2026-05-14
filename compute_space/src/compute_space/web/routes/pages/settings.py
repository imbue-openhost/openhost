from typing import Any

from litestar import Router
from litestar import get
from litestar.response import Template


@get("/settings")
async def settings_page(user: dict[str, Any]) -> Template:
    return Template(template_name="settings.html")


pages_settings_routes = Router(path="/", route_handlers=[settings_page])
