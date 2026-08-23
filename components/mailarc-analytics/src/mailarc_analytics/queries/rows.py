"""Reading a raw result set back — the other half of the calling convention.

A statement out of :mod:`mailarc_analytics.queries.catalog` runs through
``Session.execute`` and therefore past runic's entity mapper, so what comes
back is a list of lists plus a header, and every column is whatever the driver
made of the stored value. That is a property of *these statements* rather than
of anything that calls them, which is the same argument
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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from runic.ogm import Session

logger = logging.getLogger(__name__)


def rows_of(
    session: Session, statement: str, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """One catalogue statement's rows, each keyed by its column name.

    The driver hands back the rows and the header separately, so zipping them
    here means a caller reads ``row["together"]`` instead of counting columns,
    and a statement that gains a column breaks nothing that does not want it.

    A result with no rows at all comes back as an empty list rather than as
    ``None``: every caller above wants to iterate, and a report over an archive
    nothing has been derived from is a legitimate state.
    """
    result = session.execute(statement, dict(params or {}))
    columns = result.columns
    return [dict(zip(columns, row, strict=True)) for row in result.rows or []]


def as_datetime(value: Any) -> datetime | None:
    """A stored timestamp as an **aware** ``datetime``, or ``None``.

    The graph hands back the ISO-8601 string runic's mapper wrote, because raw
    Cypher goes past the converter that would have decoded it. A value that
    does not parse costs one row its date and nothing else — a report that died
    over one malformed property would be worse than one that shows a row
    without a span.

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
