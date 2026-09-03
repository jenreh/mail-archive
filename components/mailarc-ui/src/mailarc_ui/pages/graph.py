"""One corner of the archive, drawn — and the one page that has to own a height.

Layout and nothing else, like every module in this package. The whole body is
:func:`~mailarc_ui.graph.explorer_panel`; what is here is a route, a title and
the sentence that says what a reader is looking at.

The height is the exception and it is not a preference. This page is three
columns divided by a splitter, and a splitter divides *a definite height* —
given none it collapses to the height of its tallest child, which for a canvas
that measures its own container is nought. So it takes the search page's
template, ``mailarc_full_app``, which is the one that sizes ``main`` to the
window rather than letting it grow downwards.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.graph import GraphExplorerState, explorer_panel
from mailarc_ui.kit import PAGE_INSET
from mailarc_ui.shell import routes
from mailarc_ui.shell.templates import mailarc_full_app, public_page

ROUTE = routes.GRAPH
"""Where this page lives; the rail reads the same constant."""


@public_page(
    route=ROUTE,
    title="Graph",
    description="The archive as a picture — topics, circles, people and their mail",
    template=mailarc_full_app,
    on_load=[GraphExplorerState.load],
)
def graph_page() -> rx.Component:
    return mn.box(
        explorer_panel(),
        w="100%",
        h="100%",
        p=PAGE_INSET,
        style={"minHeight": 0},
    )
