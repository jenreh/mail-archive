"""The two layouts every page goes through, and the decorator `/` needs.

appkit gives four page decorators and three of them hard-wire their layout
into ``_render_layout`` — a ``mn.flex`` with the navbar as the left column,
inside the library, unstylable without patching it. ``authenticated_page`` is
the fourth and takes a ``template`` callable instead, which is what these two
layouts are.

What it cannot do is serve a page without a sign-in: it builds every
``on_load`` with ``LoginState.check_auth`` first, and that handler redirects
to ``/login``. The archive's dashboard is public, so :func:`public_page` is
the same construction without that one handler.

Two things ``rx.page`` alone loses, and only one of them is wanted back:

* ``theme_wrapper`` — appkit's ``rx.theme(...)``, bound to ``ThemeState`` and
  to the wait cursor ``LoadingState`` drives. Without it a public page renders
  unthemed, which looks like a broken stylesheet rather than like a missing
  wrapper. Re-applied explicitly below.
* ``session_monitor()`` — the hidden pair of buttons and the interval that
  re-checks and prolongs a session. Deliberately absent: there is no session
  on a public page, and polling ``check_auth`` from one would log a visitor
  out of nothing every thirty seconds.
"""

from collections.abc import Callable
from typing import Any

import appkit_mantine as mn
import reflex as rx
from appkit_ui.global_states import LoadingState
from appkit_user.authentication.templates import default_meta, theme_wrapper

from mailarc_ui.shell.navigation import NAVBAR_WIDTH, app_sidebar

Template = Callable[[rx.Component], rx.Component]
"""What a page decorator's ``template`` argument is: content in, layout out."""

PageContent = Callable[[], rx.Component]
"""A page: no arguments, one component."""


def mailarc_app(body: rx.Component) -> rx.Component:
    """The shell every page sits in: sidebar left, page content right."""
    return mn.app_shell(
        app_sidebar(),
        mn.app_shell.main(body),
        navbar={"width": NAVBAR_WIDTH, "breakpoint": "sm"},
        padding="md",
        class_name="ma-shell",
    )


def mailarc_full_app(body: rx.Component) -> rx.Component:
    """The same shell with ``main`` sized to the viewport.

    For ``/admin/review``, which is a two-column reader: the message list
    scrolls on the left and the message scrolls on the right, and both need a
    parent with a height rather than one that grows to fit them. It used to
    get that from ``navbar_layout``'s hard-coded ``100vh`` column, so without
    this the reader would silently turn into one very long page.
    """
    return mn.app_shell(
        app_sidebar(),
        mn.app_shell.main(body, class_name="ma-main-viewport"),
        navbar={"width": NAVBAR_WIDTH, "breakpoint": "sm"},
        padding="md",
        class_name="ma-shell",
    )


def _public_handlers(on_load: Any) -> list[Any]:
    """``on_load`` for a page with no sign-in.

    The same shape appkit builds — the caller's handlers, then the loading
    reset last — minus ``LoginState.check_auth``. The reset is not optional:
    ``theme_wrapper`` renders a wait cursor while ``LoadingState.is_loading``
    is true, and a page that never clears it keeps that cursor for as long as
    the visitor stays.
    """
    handlers: list[Any] = []
    if isinstance(on_load, list):
        handlers.extend(on_load)
    elif on_load is not None:
        handlers.append(on_load)
    handlers.append(LoadingState.set_is_loading(False))
    return handlers


def public_page(
    route: str,
    title: str,
    *,
    description: str | None = None,
    template: Template = mailarc_app,
    on_load: Any = None,
    meta: list[dict] | None = None,
) -> Callable[[PageContent], PageContent]:
    """Register a page anyone may open, under the archive's shell.

    There is no ``admin_only`` argument and there is not going to be one: a
    decorator that could gate a page is a decorator somebody will reach for
    when they want a gated page, and then a page's protection would depend on
    an argument rather than on which decorator was used. Anything that needs
    a sign-in uses ``authenticated_page``.

    The return type says what actually comes back — the page function, which
    ``rx.page`` registers and hands straight back. appkit's own decorators
    annotate the same thing as ``rx.Component``, which is why every test that
    calls one of their pages needs a suppression to do it.
    """
    handlers = _public_handlers(on_load)

    def decorator(page_content: PageContent) -> PageContent:
        all_meta = [*default_meta, *(meta or [])]

        @rx.page(
            route=route,
            title=title,
            description=description,
            meta=all_meta,
            on_load=handlers,
        )
        def theme_wrap() -> rx.Component:
            return theme_wrapper(template(page_content()))

        return theme_wrap

    return decorator
