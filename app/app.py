import logging

import reflex as rx
from appkit_commons.middleware import ForceHTTPSMiddleware
from appkit_user.authentication.pages import (  # noqa: F401
    azure_oauth_callback_page,
    github_oauth_callback_page,
)
from appkit_user.user_management.pages import (
    create_login_page,
    create_password_reset_confirm_page,
    create_password_reset_request_page,
    create_profile_page,
)
from starlette.types import ASGIApp

from app.components.navbar import app_navbar
from app.composition import graph_server_lifespan
from app.pages.home import home_page  # noqa: F401
from app.pages.users import users_page  # noqa: F401
from app.styles import base_style, base_stylesheets

logging.basicConfig(level=logging.DEBUG)
create_login_page()
create_profile_page(
    app_navbar(),
    class_name="w-full gap-6 max-w-[800px]",
    padding="2rem",
)
create_password_reset_request_page()
create_password_reset_confirm_page()


# Middleware transformer for HTTPS redirect
def add_https_middleware(asgi_app: ASGIApp) -> ASGIApp:
    """Wrap the ASGI app with HTTPS redirect middleware."""
    return ForceHTTPSMiddleware(asgi_app)


app = rx.App(
    stylesheets=base_stylesheets,
    style=base_style,  # ty: ignore[invalid-argument-type]  # reflex dynamic styling
    api_transformer=[add_https_middleware],
)

# Own the FalkorDB process for exactly as long as the app runs. In local mode
# this starts the vendored server on boot and stops it on shutdown; in remote
# mode both ends are no-ops.
app.register_lifespan_task(graph_server_lifespan)
