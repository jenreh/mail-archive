"""What the graph server is doing, as a page a human can read.

Moved out of ``app/`` whole. It was the one Reflex state left in the
application layer, and not by oversight: it needed a status snapshot and the
reason a start had failed, and the only place both existed was
``app.composition`` — which a component may not import.
:class:`~mailarc_core.graph.health.GraphHealth` is the façade that put both
behind one object, and this package reads it out of the service registry like
every other panel reads its own collaborator.

``state`` projects the core's :class:`GraphServerStatus` onto plain,
serialisable vars; ``components`` renders them. No component here knows a core
type.
"""

from mailarc_ui.status.components import status_panel
from mailarc_ui.status.state import (
    GraphRow,
    GraphStatusState,
    format_uptime,
    graph_health,
)

__all__ = [
    "GraphRow",
    "GraphStatusState",
    "format_uptime",
    "graph_health",
    "status_panel",
]
