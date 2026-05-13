from typing import Any

from litestar import get
from litestar.response import Template


@get("/settings", sync_to_thread=False)
def settings_page(user: dict[str, Any]) -> Template:
    return Template(template_name="settings.html")


pages_settings_routes = [settings_page]
