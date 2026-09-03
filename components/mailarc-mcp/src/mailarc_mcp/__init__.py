"""The archive as an MCP server — a model reading it, and only reading it.

Six read-only tools over the query catalogue, so that "can I ask my archive
questions" (§7.5) is answered by a model *reading* the graph rather than
writing to it. Nothing here can add, change or delete a node, and no tool takes
a query string that reaches the store.

An optional component, which is the whole reason it is one. ``fastmcp`` is
around sixty distributions — ``keyring``, ``cryptography``, ``authlib``,
``uvicorn``, ``watchfiles`` — and a desktop bundle has no use for any of them,
so the root project declares this pair as the ``mcp`` extra: ``uv sync`` leaves
it out, ``uv sync --extra mcp`` puts it in. Nothing under ``app/`` may import
this package at module level except ``app/mcp_server.py``, the entry point the
console script names, or the web application and the worker would stop starting
on an installation that chose the smaller tree.

``server/`` is the whole of it. The three names below are what that entry point
uses — the access object it fills from the composition root, the builder it
hands it to, and the logging fix a stdio process needs — so the application
never has to reach into a submodule.

**Built from nothing.** :func:`build_server` takes an
:class:`~mailarc_mcp.server.reads.ArchiveAccess` and a version string and reads
no configuration of its own, because a component may not turn settings into an
object (§4.1). Before the split this package imported ``app.composition``
directly, which was correct in behaviour and fatal to the move.
"""

from mailarc_mcp.server import (
    INSTRUCTIONS,
    MAX_ROWS,
    MAX_THREAD,
    NO_MESSAGES,
    NOT_DERIVED,
    READ_ONLY,
    SERVER_NAME,
    UNREACHABLE,
    UPSTREAM_REFUSED,
    AnalyticsFactory,
    ArchiveAccess,
    ArchivedMessage,
    ArchiveFactory,
    ArchiveTag,
    Conversation,
    ConversationMessage,
    CorrespondentPair,
    GraphSessionFactory,
    ImportantMessage,
    MessageHit,
    MessageTemplate,
    SearchAnswer,
    SearchFactory,
    TagFactory,
    TimelineEntry,
    TopicCluster,
    TopicMessage,
    TopicMessages,
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
