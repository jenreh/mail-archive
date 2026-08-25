"""The one shape every derived write takes: a catalogue ``MERGE`` over rows.

All three analyses write the same way — a named statement from
:mod:`mailarc_analytics.queries.catalog`, a list of dictionaries bound as
``$rows``, repeated until the findings run out — so the loop that does it lives
here instead of three times over. The alternative is not a shorter file, it is
three copies of a batching bug.

``MERGE`` and never ``session.add``. ``add`` compiles to ``CREATE``, and the
derived labels carry no unique constraint, so a second rebuild would silently
grow a second ``Group`` beside the first and then hang every edge off both of
them. Idempotence is this phase's contract, and it rests entirely on the
statements in the catalogue being upserts.

Nothing here decides *what* to write. It takes a statement the caller chose,
rows the caller built and the model those rows describe, which is what keeps
the analyses readable as algorithms.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from runic.ogm import Edge, Node, Session, encode_rows

from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import rows_of

logger = logging.getLogger(__name__)

WRITE_BATCH = 1000
"""Rows one ``UNWIND`` carries.

Not configuration. It trades round trips against the size of a single
parameter payload, and neither is something a user has an opinion about.
"""


def merge_rows(
    session: Session,
    statement: Statement,
    rows: Iterable[Mapping[str, Any]],
    *,
    model: type[Node | Edge],
) -> int:
    """Run one catalogue statement over *rows* in batches; return how many.

    Taken as an iterable and consumed as it arrives, so a rebuild over a large
    archive never holds every row it is about to write. An empty batch is not
    sent: ``UNWIND []`` is a legal no-op, but a round trip to say nothing is
    still a round trip.

    **Bound, never built.** A catalogue statement declares its ``$rows``
    parameter rather than spelling it into its text, so it goes through
    :func:`~mailarc_analytics.queries.rows.rows_of` — ``session.all_rows``,
    which runs a write as happily as a read and answers with the ``RETURN``
    clause's rows or an empty list where a ``MERGE`` has none. Running one as
    ``session.execute(*statement.build())`` instead fails outright: the
    declared parameters are not in the auto-bound dict, and the store answers
    with a parse error.

    *model* is the node or edge class the rows describe, and each batch goes
    through :func:`runic.ogm.encode_rows` under it before it is bound. An
    ``UNWIND`` payload never passes through the mapper on its own, so a
    ``datetime`` reaching the driver as an object is refused with
    ``ResponseError: Failed to parse query parameter 'rows' value``; encoding
    applies the model's own converters and turns it into the ISO-8601 string
    every other date in the graph is stored as. A key the model does not
    declare — ``left``, ``right``, ``message_id``, ``group_id`` — is passed
    through untouched, which is exactly what an edge row needs, and a value
    that was already encoded is left as it is, which is measured and is what
    made moving the three analyses over one at a time safe.
    """
    written = 0
    batch: list[Mapping[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= WRITE_BATCH:
            _send(session, statement, model, batch)
            written += len(batch)
            batch = []
    if batch:
        _send(session, statement, model, batch)
        written += len(batch)
    logger.debug("Merged %d rows", written)
    return written


def _send(
    session: Session,
    statement: Statement,
    model: type[Node | Edge],
    batch: list[Mapping[str, Any]],
) -> None:
    """One round trip: the batch encoded under its model and bound as ``$rows``."""
    rows_of(session, statement, {"rows": encode_rows(model, batch)})
