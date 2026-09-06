"""Which embedder this archive uses, and the key that opens it.

Layout and nothing else. The whole body is one component ``mailarc-ui``
exports — :func:`embedder_panel` — and this module only gives it a route, a
title and the paragraph that says what a reader is looking at.

Its own route rather than a card on ``/insights``, and the reason is
worth stating where the route is declared: this page must work when nothing
else does. Configuring an embedder is what somebody does *before* semantic
search answers, so the page cannot depend on a graph, on a rebuilt derived
layer or on an embedder existing — while the insights page fires five graph
reads in its ``on_load``. It is also the only page in the application that
writes configuration, which is a different kind of act from reporting on an
archive and deserves a different place to stand.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.embedder import (
    EmbedderSettingsState,
    InterpreterSettingsState,
    embedder_panel,
    interpreter_panel,
)
from mailarc_ui.kit import PAGE_GAP, PAGE_INSET
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app, public_page

ROUTE = routes.EMBEDDER
"""Where this page lives; the rail reads the same constant."""


@public_page(
    route=ROUTE,
    title="Models",
    description="The two models this archive can use, and what changing either costs",
    template=mailarc_app,
    on_load=[EmbedderSettingsState.load, InterpreterSettingsState.load],
)
def embedder_page() -> rx.Component:
    """Both models, stacked, the embedder first.

    The embedder is first because it is the one whose settings cost something
    to get wrong: a changed model strands every stored vector, and its half of
    the page carries the warnings and the rebuild that answer for that. The
    interpreter is second and simpler — nothing it produces is stored.

    The **route stays `/admin/embedder`** even though the page is now called
    Models. It is quoted in every remedy sentence semantic search shows, pinned
    against ``SETTINGS_PAGE`` by ``test_ui_pages.py``, and in anybody's
    bookmarks; renaming it would buy a tidier URL and break all three.
    """
    return mn.stack(
        embedder_panel(),
        interpreter_panel(),
        gap=PAGE_GAP,
        w="100%",
        p=PAGE_INSET,
    )
