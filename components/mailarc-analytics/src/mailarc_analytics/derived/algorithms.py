"""§5.1 — the capability probe, and the guard every procedure call goes through.

FalkorDB's ``algo.*`` procedures **throw on a label or a relationship type the
graph does not hold**, and an archive whose first rebuild has not written a
``CO_ADDRESSED`` edge is exactly that graph. Measured on the vendored 4.20.3:
``algo.labelPropagation`` and ``algo.betweenness`` both raise
``ResponseError: … configuration, unknown label Address``, while
``algo.pageRank`` answers with no rows instead. So a rebuild cannot simply call
them, and it must not fall over when one refuses either — the derived layer is
disposable, and "the graph has no circles" and "nothing looked" are two
different answers a job row has to be able to tell apart.

Two things stand between a caller and the store:

**The probe.** ``dbms.procedures()`` names no label and no relationship type,
so it is the one call that is safe on an empty graph. Its answer is lower-cased
and cached **per session** — the store's own spelling is ``algo.WCC`` and
``algo.BFS`` while its error messages lower-case them, and one round trip per
procedure per stage per rebuild would be a round trip for a set that cannot
change while a session is open.

**The guard.** Every call is wrapped: a procedure the probe did not name is not
attempted at all, and one that refuses the graph it was given is logged, counted
and answered with nothing. The count reaches the job row as
:attr:`~mailarc_analytics.derived.model.DerivedCounts.algorithms_skipped`.

The exception this catches is deliberately ``Exception`` and not
``redis.exceptions.ResponseError``. ``mailarc-analytics`` sits on top of
``mailarc-core`` and names no driver — the same rule
:func:`mailarc_analytics.semantic.search._asked` follows, and for the same
reason: the import table has no ``redis`` in it.

**Nothing here decides anything.** These four functions run a statement and
shape its rows; what a partition *means* is
:mod:`mailarc_analytics.derived.communities`, which is pure and needs no store.
That split is what lets every rule about circles be tested without a server and
leaves only "does the procedure answer" for the ``graph_local`` file.
"""

import logging
import threading
import weakref
from collections.abc import Mapping, MutableMapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import as_float, as_int, as_text, rows_of

logger = logging.getLogger(__name__)

LPA_PROCEDURE = "algo.labelpropagation"
PAGERANK_PROCEDURE = "algo.pagerank"
BETWEENNESS_PROCEDURE = "algo.betweenness"
"""The three procedure names this phase asks for, in the probe's spelling.

Named apart from the catalogue's ``LABEL_PROPAGATION`` and its siblings, which
are the Cypher that *calls* them: these three are the strings the probe answers
with, and a file holding both had better not spell them the same.

Lower-cased, because that is what :func:`graph_algorithms` normalises to. The
binary writes ``algo.labelPropagation`` and the store's errors write
``labelPropagation`` again in one message and ``betweenness`` in the next, so a
single spelling has to be chosen here rather than guessed at each call site.
"""

_PROBED: MutableMapping[Session, frozenset[str]] = weakref.WeakKeyDictionary()
"""What each open session's store answered, kept for as long as that session is.

Weakly keyed for :data:`~mailarc_analytics.queries.rows._IN_USE`'s reason: a
session that goes away takes its entry with it rather than holding a closed
connection alive in a module-level dict for the life of the process. Per
session and not per process, because the set is a property of the *store* and
two sessions in one process can legitimately be talking to two of them — a test
suite that ran a second FalkorDB would otherwise inherit the first one's
answer.
"""

_PROBE_GUARD = threading.Lock()
"""Guards the weak map itself.

Held only long enough to read or write one entry, never across the round trip:
this application really does run graph reads from several threads at once, and
a lock held over a query would serialise the rebuild behind whichever page
asked first. Two threads racing to probe the same session cost one extra round
trip and agree about the answer, which is cheaper than that.
"""


class Partition(BaseModel):
    """Which circle each address landed in, and whether the call ran at all.

    A value object rather than a bare ``dict`` because the second number is
    load-bearing: an empty partition from a procedure that threw and an empty
    partition from an archive where nobody is co-addressed are the same ``{}``,
    and only one of them means the rebuild found something out.

    Declared here rather than beside the six in
    :mod:`mailarc_analytics.derived.findings` because it is not what an
    *analysis* passes between its halves — it is the shape of a guarded
    procedure call, and this is the only module that can produce one.
    """

    model_config = ConfigDict(frozen=True)

    labels: Mapping[str, int] = Field(default_factory=dict)
    """Address id → the community number the algorithm chose.

    The number is the algorithm's own and means nothing outside this call:
    label propagation has no seed, so two runs over an unchanged graph may
    number the same partition differently.
    :func:`~mailarc_analytics.derived.model.community_id` is what makes that
    harmless, by keying a circle on its members instead.
    """

    skipped: int = 0
    """One if the guard stepped over this call, zero if it ran."""


class Ranking(BaseModel):
    """A score per node id, and whether the call ran at all.

    :class:`Partition`'s argument for the two centralities. Separate from it
    because the values are genuinely different things — a community number is
    an identifier and a rank is a quantity — and a single class carrying
    ``Mapping[str, float]`` would invite somebody to average community numbers.
    """

    model_config = ConfigDict(frozen=True)

    scores: Mapping[str, float] = Field(default_factory=dict)
    skipped: int = 0


def graph_algorithms(session: Session) -> frozenset[str]:
    """Which procedures this store has, lower-cased. Cached per session.

    The one call that is safe on an empty graph, which is why every guard below
    starts here: ``dbms.procedures()`` names no label and no relationship type,
    so there is nothing for it to be unknown about.

    A store that will not answer the probe at all is treated as a store with no
    procedures — every ``algo.*`` call is then skipped and counted, and the
    rebuild produces its other nine stages. Raising instead would make one
    unreachable capability cost the whole derived layer.
    """
    with _PROBE_GUARD:
        found = _PROBED.get(session)
    if found is not None:
        return found
    found = _probe(session)
    with _PROBE_GUARD:
        _PROBED[session] = found
    return found


def label_propagation(session: Session, *, max_iterations: int) -> Partition:
    """Which addresses form a circle, over ``CO_ADDRESSED``.

    *max_iterations* is pinned by the caller rather than left to the
    procedure's default, because FalkorDB's label propagation takes no seed: an
    unconverged run is where two rebuilds over an unchanged graph can label an
    ambiguous node differently, and the iteration count is the one thing this
    end can hold still.

    The edge is read undirected in effect — a label spreads along every
    incident edge — so the writer's smaller-id-first ordering does not bias the
    partition the way a PageRank over the same edge would. That is R2, and it
    is measured in ``test_derived_algorithms_local.py`` rather than argued.
    """
    rows = _call(
        session,
        LPA_PROCEDURE,
        catalog.LABEL_PROPAGATION,
        {"max_iterations": max_iterations},
    )
    if rows is None:
        return Partition(skipped=1)
    labels = {
        as_text(row.get("id")): as_int(row.get("community"))
        for row in rows
        if row.get("id")
    }
    logger.debug("Label propagation labelled %d addresses", len(labels))
    return Partition(labels=labels)


def message_pagerank(session: Session) -> Ranking:
    """Which messages sit at the centre of the archive's conversations.

    Over ``REPLIES_TO``, the one edge in this graph that is genuinely directed:
    ``(reply)-[:REPLIES_TO]->(parent)`` means what an arrow is supposed to
    mean. Address centrality is *not* here for exactly that reason — see
    :mod:`mailarc_analytics.derived.centrality`.

    Measured: this is the one procedure that does not throw on a graph without
    the relationship type — it answers with no rows — so the guard around it is
    a formality that stays for symmetry rather than for safety.
    """
    rows = _call(session, PAGERANK_PROCEDURE, catalog.MESSAGE_PAGERANK, {})
    if rows is None:
        return Ranking(skipped=1)
    return Ranking(scores=_scores(rows))


def address_betweenness(session: Session, *, sampling_size: int, seed: int) -> Ranking:
    """Which addresses are the bridges between circles — optional, and off.

    A *sampling size of zero means do not call it*, and that is a decision
    rather than something the guard stepped over, so nothing is counted for it:
    a number in ``algorithms_skipped`` has to mean the store refused, or it
    stops being worth reading. The setting is a size rather than a flag because
    the procedure refuses zero outright (``'samplingSize' should be a positive
    integer``, measured), so "off" has nowhere else to live.

    *seed* is bound for the same reason ``max_iterations`` is above. An
    unseeded sample is a second way for two rebuilds over an unchanged graph to
    disagree.
    """
    if sampling_size <= 0:
        logger.debug("Betweenness is switched off; not calling the procedure")
        return Ranking()
    rows = _call(
        session,
        BETWEENNESS_PROCEDURE,
        catalog.ADDRESS_BETWEENNESS,
        {"sampling_size": sampling_size, "sampling_seed": seed},
    )
    if rows is None:
        return Ranking(skipped=1)
    return Ranking(scores=_scores(rows))


def _probe(session: Session) -> frozenset[str]:
    """One ``dbms.procedures()`` round trip, lower-cased."""
    try:
        rows = rows_of(session, catalog.PROCEDURES)
    except Exception as error:
        logger.warning("The store would not list its procedures: %s", error)
        return frozenset()
    found = frozenset(
        as_text(row.get("name")).lower() for row in rows if row.get("name")
    )
    logger.debug("The store offers %d procedures", len(found))
    return found


def _call(
    session: Session,
    procedure: str,
    statement: Statement,
    params: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    """Run one procedure, or answer ``None`` if it could not be run.

    ``None`` and not ``[]``, because those are the two answers this whole
    module exists to keep apart: no rows is a graph with nothing in it, and no
    call is a graph the procedure would not look at.

    Catches ``Exception`` rather than the driver's own class on purpose. This
    package sits on top of ``mailarc-core`` and names no driver, which is the
    import table's rule and
    :func:`mailarc_analytics.semantic.search._asked`'s precedent; what would be
    gained by narrowing it is telling one refusal from another, and every one
    of them has the same answer here.
    """
    if procedure not in graph_algorithms(session):
        logger.warning("This store has no %s; the stage reports nothing", procedure)
        return None
    try:
        return rows_of(session, statement, dict(params))
    except Exception as error:
        logger.warning(
            "%s would not run over this graph and was skipped: %s", procedure, error
        )
        return None


def _scores(rows: list[dict[str, Any]]) -> dict[str, float]:
    """A procedure's ``(id, score)`` rows as a mapping, ids without one dropped."""
    return {
        as_text(row.get("id")): as_float(row.get("score"))
        for row in rows
        if row.get("id")
    }
