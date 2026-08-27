"""Which embedder this archive uses, and the key that opens it.

Layout and nothing else. The whole body is one component ``mailarc-ui``
exports — :func:`embedder_panel` — and this module only gives it a route, a
title and the paragraph that says what a reader is looking at.

Its own route rather than a card on ``/admin/insights``, and the reason is
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
from appkit_user.authentication.templates import authenticated_page

from mailarc_ui.embedder import EmbedderSettingsState, embedder_panel
from mailarc_ui.kit import PAGE_GAP, PAGE_PADDING, page_header
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_app

ROUTE = routes.EMBEDDER
"""Where this page lives; the sidebar reads the same constant."""


@authenticated_page(
    route=ROUTE,
    title="Embedder",
    description="Which model turns this archive into vectors, and what changing it costs",
    template=mailarc_app,
    # Admin-only, and this is the sharpest case of it in the application. The
    # other admin pages *read* every mailbox; this one decides which service
    # every message body is sent to the next time the embed job runs, and holds
    # the credential it is sent with. The decorator is the cosmetic half —
    # `EmbedderSettingsState._may_configure` is what actually refuses, on every
    # handler, because a Reflex event is addressable by name over the socket.
    admin_only=True,
    # ty cannot model reflex event-handler calls; suppress the false positive.
    on_load=[EmbedderSettingsState.load],  # ty: ignore[invalid-argument-type]
)
def embedder_page() -> rx.Component:
    return mn.stack(
        page_header(
            "Embedder",
            "Semantic search and the sixth topic signal need a model that "
            "turns a message into a vector. Nothing else does: with no "
            "embedder configured the import, the analyses and full-text "
            "search all work, and the two features that need vectors say "
            "so rather than answering with nothing. What is set here is "
            "laid over the configuration file, so an installation that "
            "never opens this page keeps behaving exactly as it did.",
        ),
        embedder_panel(),
        gap=PAGE_GAP,
        w="100%",
        maw=900,
        mx="auto",
        p=PAGE_PADDING,
    )
