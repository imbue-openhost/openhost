from typing import Any

from litestar import Request
from litestar import Router
from litestar import get
from litestar.response import Template


@get("/")
async def index(request: Request[Any, Any, Any]) -> Template:
    """Dashboard page for managing stored OAuth tokens."""
    return Template(template_name="index.html")


router = Router(path="", route_handlers=[index])
