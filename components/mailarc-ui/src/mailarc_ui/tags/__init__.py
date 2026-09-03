"""The annotation layer, as the half of a page that tags things.

The archive's one durable label. A ``Topic`` is a digest of its members and is
minted afresh by every rebuild; a ``Tag`` is what somebody named, and no delete
regex in the derived layer can reach it — which is why promoting a cluster is a
gesture worth having a package for rather than a button on one page.

``model``
    ``TagView`` and ``SuggestionView``, the projections a chip and a row print.
    No I/O, no Reflex.
``reads``
    The two services this reaches out for and the shape of one blocking call.
``state``
    ``TagActionsState``, a **mixin**: a page lists it and brings the cluster,
    Reflex copies the vars and handlers in, so each page gets its own tag
    listing rather than sharing one.
``components``
    The chips, the promote form and the suggestion table, each taking the
    concrete state class — ``tag_chips(GraphExplorerState, …)``.

It is a package rather than a module because tagging is not the graph
explorer's own business: phase 5 hosts the same mixin on the insights page, and
the only thing that changes is where the cluster comes from.
"""

from mailarc_ui.tags.components import (
    DEFAULT_TAG_COLOR,
    TAG_ROWS,
    promote_form,
    suggestion_rows,
    tag_chips,
    tags_panel,
)
from mailarc_ui.tags.model import (
    NAME_TAKEN,
    NOTHING_TO_PROMOTE,
    PROMOTE_FIELD,
    SuggestionView,
    TagView,
    short_date,
)
from mailarc_ui.tags.reads import analytics_reader, answered, tag_store
from mailarc_ui.tags.state import (
    SUGGESTION_LIMIT,
    TagActionsState,
    read_suggestions,
    read_tags,
)

__all__ = [
    "DEFAULT_TAG_COLOR",
    "NAME_TAKEN",
    "NOTHING_TO_PROMOTE",
    "PROMOTE_FIELD",
    "SUGGESTION_LIMIT",
    "TAG_ROWS",
    "SuggestionView",
    "TagActionsState",
    "TagView",
    "analytics_reader",
    "answered",
    "promote_form",
    "read_suggestions",
    "read_tags",
    "short_date",
    "suggestion_rows",
    "tag_chips",
    "tag_store",
    "tags_panel",
]
