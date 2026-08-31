"""Everything the search page reaches outside itself for, and nothing else.

The seam :mod:`mailarc_ui.dashboard.reads` established: what is here touches
the service registry and the archive's own database, holds no state lock,
writes no var, and is callable from a test without a Reflex state at all.
What stays in :mod:`mailarc_ui.search.state` is the Reflex class.

Both services are read out of the registry **inside** the function that needs
one. ``mailarc-ui`` may not import ``app`` (§6), so everything the composition
root built arrives that way; a lookup at module level would run while
``app/app.py`` is still being imported, before anything had been published.

:func:`archive_reader` is the pane's own lookup, re-exported rather than
copied — the search page and the review page read the same archive through
the same object, and two spellings of one registry key is how they would
eventually stop being the same object.
"""

from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry

from mailarc_analytics.semantic import SemanticSearch
from mailarc_core.database.repositories import MailAccountRepository
from mailarc_ui.message_detail.model import archive_reader

_ACCOUNTS = MailAccountRepository()
"""Stateless, so one is enough for the whole application."""

NO_SEARCH = (
    "Search is not wired up in this build — no SemanticSearch is registered. "
    "The composition root publishes it; did app.composition run?"
)
"""The developer's error, in the one place a user could meet it.

Its own sentence rather than a bare ``KeyError``: the page cannot tell a
half-wired application apart from a broken one, and the two have completely
different fixes.
"""

__all__ = ["NO_SEARCH", "account_options", "archive_reader", "semantic_search"]


def semantic_search() -> SemanticSearch:
    """The search the composition root published. Call inside a method only."""
    try:
        return service_registry().get(SemanticSearch)
    except KeyError as error:
        raise RuntimeError(NO_SEARCH) from error


async def account_options() -> list[dict[str, str]]:
    """Every mailbox this archive imports from, as the picker lists them.

    The value is the account's SQLite row id **as a string**, because that is
    what the graph keys an ``Account`` node under and therefore what
    :attr:`~mailarc_core.archive.search.SearchFilters.account_id` is matched
    against. The label is what a person named the mailbox, falling back to its
    address — an account whose name was left empty still has to be pickable.

    The projection runs inside the session: Reflex serialises what a state
    holds, and a row whose session has closed hands back nothing.
    """
    async with get_asyncdb_session() as session:
        entities = await _ACCOUNTS.find_all(session)
        options = [
            {
                "value": str(entity.id),
                "label": entity.display_name or entity.email_address,
            }
            for entity in entities
        ]
    return sorted(options, key=lambda one: one["label"].casefold())
