"""Reading a result set back — the other half of the calling convention.

Two kinds of entry come out of :mod:`mailarc_analytics.queries.catalog` and
they are run differently, so exactly one function in this package knows which
is which. A builder statement goes through ``session.all_rows(statement,
params)``, which binds the declared parameters and hands back column-keyed
dicts; the one raw statement
(:data:`~mailarc_analytics.queries.catalog.VECTOR_INDEX_OPTIONS`) goes through
``session.execute`` and comes back as a header plus a list of lists, which is
zipped here. :func:`rows_of` is that dispatch, and it is why no caller above
has to ask what kind of statement it is holding.

**A projected column is not decoded, whichever way it was run** — measured on
the vendored FalkorDB, both before and after the statements became builder
objects. ``all_rows`` decodes a column only when the whole node or edge is
returned under a mapped alias; ``project(m.sent_at)`` returns a *value*, so
``sent_at`` still arrives as the ISO-8601 string the mapper wrote and
``simhash`` still as the *signed* 64-bit integer the writer had to store. The
four coercions below are therefore exactly as necessary as they were, and
:func:`~mailarc_core.archive.model.to_unsigned_64` still runs at the reader's
boundary.

That is a property of *these statements* rather than of anything that calls
them, which is the same argument
:func:`~mailarc_analytics.queries.catalog.as_graph_datetime` makes for living
beside the statements: it converts a value on its way *into* a bound
parameter, and this module converts one on its way back out.

Kept out of ``catalog.py`` all the same. That file's one job is the statements;
this one is about the shape a driver answers in, and the two go wrong for
different reasons.

The decisions here were bought once already, in
:mod:`mailarc_analytics.derived.reader`, and are repeated rather than
re-derived — a naive timestamp read as naive crashed a whole rebuild with
"can't compare offset-naive and offset-aware datetimes" the first time an
analysis took a ``min()`` over the archive's dates.
"""

import logging
import threading
import weakref
from collections.abc import Mapping, MutableMapping
from datetime import UTC, datetime
from typing import Any

from runic.ogm import QueryBuilder, Session

from mailarc_analytics.queries.catalog import Statement

logger = logging.getLogger(__name__)

_IN_USE: MutableMapping[QueryBuilder[Any], threading.RLock] = (
    weakref.WeakKeyDictionary()
)
"""One lock per statement object, held while that statement is executing.

**A catalogue statement is shared mutable state, and a Cypher string was not.**
``Session.all_rows`` runs ``with stmt._bound_to(self)``, which assigns
``stmt._session`` and restores the value it found in a ``finally``. The
statements are module-level constants, so two threads running the *same* one
interleave on that one attribute: the second to enter saves the first's session
as the value to restore, the first to leave restores ``None``, and the second
then decodes its result against ``None``. Measured on the vendored FalkorDB —
8 threads × 400 executions of :data:`~mailarc_analytics.queries.catalog.MESSAGE_PROPERTIES`
raised ``AttributeError: 'NoneType' object has no attribute 'mapper'`` once,
and the same load with a statement built fresh per call raised nothing.

This application really does run graph reads from several threads at once: the
worker rebuilds and embeds inside ``asyncio.to_thread`` while the Insights and
Embedder pages read through Reflex background events. So the window is not
theoretical, and neither is its cost — the crash surfaces as a failed page or a
failed rebuild, out of a statement that is correct.

**Per statement rather than one lock for the catalogue**, because the whole
contention is two threads on one object. A rebuild paging through
``MESSAGE_PROPERTIES`` for minutes holds only that statement's lock, and only
for one round trip at a time; a page reading ``TOP_CO_ADDRESSED`` beside it
takes a different lock and never waits. A single lock would have serialised
every graph read in the process behind the longest job in it.

Weakly keyed so a statement built for one call is not kept alive by its lock,
and re-entrant so that a caller who somehow re-entered with the same statement
would meet the old behaviour rather than a deadlock — nothing does today, and a
hung archive would be worse than a rare crash. Identity is the key: a
``QueryBuilder`` hashes by identity, which is exactly the thing being guarded.

The raw entry needs none of this. A ``str`` is immutable and
``session.execute`` binds nothing onto it.
"""

_REGISTRY_GUARD = threading.Lock()
"""Guards the weak map itself — held only long enough to hand a lock back."""


def _in_use(statement: QueryBuilder[Any]) -> threading.RLock:
    """The lock that says "this statement object is executing"."""
    with _REGISTRY_GUARD:
        lock = _IN_USE.get(statement)
        if lock is None:
            lock = threading.RLock()
            _IN_USE[statement] = lock
        return lock


def rows_of(
    session: Session, statement: Statement, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """One catalogue statement's rows, each keyed by its column name.

    **Bind, do not build.** A builder statement carrying ``param()`` is run by
    the session itself: ``session.all_rows`` compiles it, binds the declared
    parameters and answers with the dicts this function's contract promises.
    ``session.execute(*statement.build())`` is the trap it avoids — the
    declared parameters are not in the auto-bound dict the builder returns, and
    the store answers with a parse error rather than with a missing value. A
    binding that leaves a declared parameter out raises instead of passing a
    null, which is the security boundary the catalogue is for.

    Raw Cypher takes the other path, and it is the reason this function still
    exists rather than every caller reaching for ``all_rows``: the driver hands
    back the rows and the header separately, so zipping them here means a
    caller reads ``row["together"]`` instead of counting columns, and a
    statement that gains a column breaks nothing that does not want it. Both
    halves answer in the same shape, so nothing above knows which ran.

    Writes come through here too — ``all_rows`` runs a ``MERGE`` or a
    ``DELETE`` as happily as a ``MATCH``, returning the ``RETURN`` clause's
    rows or an empty list where there is none.

    A result with no rows at all comes back as an empty list rather than as
    ``None``: every caller above wants to iterate, and a report over an archive
    nothing has been derived from is a legitimate state.

    **One thread at a time per statement**, which is :data:`_IN_USE`'s whole
    subject: a builder binds the executing session onto itself, so a
    module-level constant run from two threads at once can have one clear the
    other's mid-flight. Being the single place every catalogue statement is
    executed is what lets this function close that, and being *per statement*
    is what keeps two different reads running in parallel.
    """
    if isinstance(statement, str):
        result = session.execute(statement, dict(params or {}))
        columns = result.columns
        return [dict(zip(columns, row, strict=True)) for row in result.rows or []]
    with _in_use(statement):
        return session.all_rows(statement, params)


def as_datetime(value: Any) -> datetime | None:
    """A stored timestamp as an **aware** ``datetime``, or ``None``.

    The graph hands back the ISO-8601 string runic's mapper wrote, because a
    projected column goes past the converter that would have decoded it — a
    builder statement returns values rather than entities, so this is as true
    of ``all_rows`` as it was of raw Cypher, and it was re-measured rather than
    assumed. A value that does not parse costs one row its date and nothing
    else — a report that died over one malformed property would be worse than
    one that shows a row without a span.

    A value that parses but carries no offset is the expensive case rather than
    the harmless one: every ``min``, ``max`` and ``sorted`` over a set of these
    raises the moment a naive value meets an aware one, which takes out the
    whole listing instead of one row. So a missing zone is read as UTC, the
    same decision :func:`~mailarc_core.mail.parsing._sent_at` makes for a
    ``Date`` header that withholds one.
    """
    if isinstance(value, datetime):
        return _aware(value)
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        logger.warning("Ignoring unparseable timestamp %r", value)
        return None


def as_int(value: Any) -> int:
    """A count column as a number; a missing property counts as zero.

    ``None`` is the one thing tolerated, because a derived property that was
    never set really does mean nothing was counted. Anything else that is not a
    number raises, and should: a count column holding text is a broken
    statement, not a state an archive can be in.
    """
    return 0 if value is None else int(value)


def as_float(value: Any) -> float:
    """A score column as a number; a missing property scores zero."""
    return 0.0 if value is None else float(value)


def as_text(value: Any) -> str:
    """A text column as a string; a missing property reads as empty.

    Never ``None``, so nothing above has to decide between "no label" and "an
    empty label" — for everything this package reports, they are the same
    thing and the empty string is the one a page can render.
    """
    return "" if value is None else str(value)


def _aware(value: datetime) -> datetime:
    """The same instant, with UTC put back where a zone is missing."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
