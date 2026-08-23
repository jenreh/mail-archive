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

Nothing here decides *what* to write. It takes a statement the caller chose and
rows the caller built, which is what keeps the analyses readable as algorithms.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from runic.ogm import Session

logger = logging.getLogger(__name__)

WRITE_BATCH = 1000
"""Rows one ``UNWIND`` carries.

Not configuration. It trades round trips against the size of a single
parameter payload, and neither is something a user has an opinion about.
"""


def merge_rows(
    session: Session, statement: str, rows: Iterable[Mapping[str, Any]]
) -> int:
    """Run one catalogue statement over *rows* in batches; return how many.

    Taken as an iterable and consumed as it arrives, so a rebuild over a large
    archive never holds every row it is about to write. An empty batch is not
    sent: ``UNWIND []`` is a legal no-op, but a round trip to say nothing is
    still a round trip.
    """
    written = 0
    batch: list[Mapping[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= WRITE_BATCH:
            session.execute(statement, {"rows": batch})
            written += len(batch)
            batch = []
    if batch:
        session.execute(statement, {"rows": batch})
        written += len(batch)
    logger.debug("Merged %d rows", written)
    return written
