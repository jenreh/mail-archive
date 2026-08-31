"""The two layouts every page goes through, and the decorator that registers it.

appkit gave four page decorators and every one of them starts an ``on_load``
chain with ``LoginState.check_auth``, which redirects to a sign-in page. The
archive is a desktop application and has no sign-in at all, so
:func:`public_page` is the only decorator left: ``rx.page`` plus the two things
that would otherwise be lost with appkit's wrapper.

* :func:`theme_wrapper` — the ``rx.theme`` every page needs. Without it a page
  renders unthemed, which looks like a broken stylesheet rather than like a
  missing wrapper. It is four lines and they are here rather than imported,
  because the version that used to be imported is bound to ``appkit_user``'s
  own ``ThemeState`` and this application no longer depends on that package.
  ``LoadingState`` stays: it is ``appkit_ui``'s, and it is what drives the wait
  cursor between a click and the page it opens.
* ``session_monitor()`` — the hidden pair of buttons and the interval that
  re-checked and prolonged a session. Gone with the sessions.
"""

from collections.abc import Callable
from typing import Any

import appkit_mantine as mn
import reflex as rx
from appkit_ui.global_states import LoadingState

from mailarc_ui.shell.navigation import NAVBAR_WIDTH, app_sidebar

DEFAULT_META: list[dict[str, str]] = [
    {
        "name": "viewport",
        "content": "width=device-width, shrink-to-fit=no, initial-scale=1",
    },
]
"""What every page declares before whatever it adds of its own.

One tag, and it is the one that cannot be left out: without it a mobile
browser lays the page out at 980px and scales the result down, so the rail and
the reading pane arrive as a photograph of a desktop.
"""

Template = Callable[[rx.Component], rx.Component]
"""What a page decorator's ``template`` argument is: content in, layout out."""

PageContent = Callable[[], rx.Component]
"""A page: no arguments, one component."""


def theme_wrapper(content: rx.Component) -> rx.Component:
    """The theme every page is drawn inside, plus the wait cursor.

    ``appearance="inherit"`` is what lets the colour scheme come from one
    place: Reflex's colour mode drives both this and — through
    ``appkit_mantine``'s ``force_color_scheme`` — Mantine's, so the rail's
    toggle moves the whole design rather than half of it.
    """
    return rx.theme(
        content,
        has_background=True,
        radius="large",
        appearance="inherit",
        class_name=rx.cond(LoadingState.is_loading, "cursor-wait", ""),
    )


def mailarc_app(body: rx.Component) -> rx.Component:
    """The shell every page sits in: the icon rail left, page content right."""
    return mn.app_shell(
        app_sidebar(),
        mn.app_shell.main(body),
        navbar={"width": NAVBAR_WIDTH, "breakpoint": 0},
        padding="md",
        class_name="ma-shell",
    )


def mailarc_full_app(body: rx.Component) -> rx.Component:
    """The same shell with ``main`` sized to the viewport.

    For the two-column readers — search and ``/admin/review`` — where the list
    scrolls on the left and the message scrolls on the right, and both need a
    parent with a height rather than one that grows to fit them. Without it a
    reader silently turns into one very long page.
    """
    return mn.app_shell(
        app_sidebar(),
        mn.app_shell.main(body, class_name="ma-main-viewport"),
        navbar={"width": NAVBAR_WIDTH, "breakpoint": 0},
        padding="md",
        class_name="ma-shell",
    )


def _public_handlers(on_load: Any) -> list[Any]:
    """``on_load`` for a page: the caller's handlers, then the loading reset.

    The reset is not optional: :func:`theme_wrapper` renders a wait cursor
    while ``LoadingState.is_loading`` is true, and a page that never clears it
    keeps that cursor for as long as the visitor stays.
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
    """Register a page under the archive's shell.

    There is no ``admin_only`` argument and there is not going to be one. The
    application holds one person's mail on one person's machine; a gate here
    would be a permission nothing can check, and a decorator that could take
    one is a decorator somebody will pass it to.

    The return type says what actually comes back — the page function, which
    ``rx.page`` registers and hands straight back. appkit's own decorators
    annotate the same thing as ``rx.Component``, which is why every test that
    called one of them needed a suppression to do it.
    """
    handlers = _public_handlers(on_load)

    def decorator(page_content: PageContent) -> PageContent:
        all_meta = [*DEFAULT_META, *(meta or [])]

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
