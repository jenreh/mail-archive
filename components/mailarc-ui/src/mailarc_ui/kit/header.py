"""What a page says about itself before it shows anything.

The two numbers a page frame is made of live here as well, because they are the
same decision as the header: how far the content sits from the shell, and how
far the title sits from the first thing under it.
"""

import appkit_mantine as mn
import reflex as rx

PAGE_PADDING = "24px"
"""How far a page's content sits from the shell's edge.

The design's spacing rhythm, and it is inside ``mn.app_shell(padding="md")``,
so the content is inset 24 + 16 = 40px from the window. ``2rem`` used to sit
here and made that 48, which read as a narrower page rather than as a roomier
one.
"""

PAGE_GAP = "24px"
"""How far the title sits above the first thing under it, and every gap after.

**One mechanism and not two.** ``.ma-page-header`` used to carry a
``margin-bottom: 24px`` of its own *and* sit inside a stack with a gap, so the
two stacked and put the KPI banner 44px below the title where the design asks
for 24. A margin on a flex child and a gap on its parent are both real, and
neither is visible from the other's file — so the margin went and the gap is
the only thing that spaces this page.
"""


def page_header(
    title: str | rx.Var,
    subtitle: str | rx.Var | None = None,
    actions: rx.Component | None = None,
) -> rx.Component:
    """The title block every page opens with.

    ``actions`` is on the right of the same row rather than below the title:
    a refresh button or a range switch belongs beside the thing it acts on,
    and a second row would push the first card another 40px down the page for
    no gain.

    There is no search field and no filter button here on purpose — the shell
    has no header bar, and a page-level search that only some pages carried
    would read as one that is broken on the others.

    Carries no bottom margin of its own: the page stack's :data:`PAGE_GAP` is
    what puts space under it, and a margin here would stack with that gap.
    """
    heading = mn.stack(
        mn.text(title, class_name="ma-page-title"),
        *(
            []
            if subtitle is None
            else [mn.text(subtitle, class_name="ma-page-subtitle")]
        ),
        gap=4,
        style={"minWidth": 0},
    )
    if actions is None:
        return mn.box(heading, class_name="ma-page-header", w="100%")
    return mn.group(
        heading,
        actions,
        justify="space-between",
        align="flex-start",
        wrap="nowrap",
        w="100%",
        class_name="ma-page-header",
    )
