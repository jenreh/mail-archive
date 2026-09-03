"""The MCP server itself: the tools, what they answer with, and what they read.

One capability, three modules, layered so nothing points back up.

``model``
    The value objects a tool answers with — the contract a model on the far end
    reads, kept apart from the component row types so that an internal rename
    inside ``mailarc-analytics`` is not a published breaking change. No I/O.
``reads``
    :class:`~mailarc_mcp.server.reads.ArchiveAccess` — where an answer comes
    from. Holds five factories it was handed, asks each on first use, builds
    none of them itself, and carries the two graph reads no reader above it
    hands out: one conversation, and one topic's members.
``server``
    :func:`~mailarc_mcp.server.server.build_server`, the ten tools bound to one
    ``ArchiveAccess``, the failure translation, and the FastMCP logging fix a
    stdio process needs before it serves anything.
"""

from mailarc_mcp.server.model import (
    ArchivedMessage,
    ArchiveTag,
    Conversation,
    ConversationMessage,
    CorrespondentPair,
    ImportantMessage,
    MessageHit,
    MessageTemplate,
    SearchAnswer,
    TimelineEntry,
    TopicCluster,
    TopicMessage,
    TopicMessages,
)
from mailarc_mcp.server.reads import (
    AnalyticsFactory,
    ArchiveAccess,
    ArchiveFactory,
    GraphSessionFactory,
    SearchFactory,
    TagFactory,
)
from mailarc_mcp.server.server import (
    INSTRUCTIONS,
    MAX_ROWS,
    MAX_THREAD,
    NO_MESSAGES,
    NOT_DERIVED,
    READ_ONLY,
    SERVER_NAME,
    UNREACHABLE,
    UPSTREAM_REFUSED,
    build_server,
    route_fastmcp_logging,
)

__all__ = [
    "INSTRUCTIONS",
    "MAX_ROWS",
    "MAX_THREAD",
    "NOT_DERIVED",
    "NO_MESSAGES",
    "READ_ONLY",
    "SERVER_NAME",
    "UNREACHABLE",
    "UPSTREAM_REFUSED",
    "AnalyticsFactory",
    "ArchiveAccess",
    "ArchiveFactory",
    "ArchiveTag",
    "ArchivedMessage",
    "Conversation",
    "ConversationMessage",
    "CorrespondentPair",
    "GraphSessionFactory",
    "ImportantMessage",
    "MessageHit",
    "MessageTemplate",
    "SearchAnswer",
    "SearchFactory",
    "TagFactory",
    "TimelineEntry",
    "TopicCluster",
    "TopicMessage",
    "TopicMessages",
    "build_server",
    "route_fastmcp_logging",
]
