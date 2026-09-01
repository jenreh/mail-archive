"""``rebuild-derived`` — delete all four derived types and compute them again.

The promise §5.2 makes about this package is that an analysis bug costs one run
and not a restore, and this module is where that promise is either kept or
broken. It deletes every ``Group``, ``Topic`` and ``Template`` together with the
edges hanging off them, deletes every ``CO_ADDRESSED`` edge between two
addresses, and then runs the three analyses over the ground truth as it stands.

**It cannot touch ground truth, and that is structural rather than intended.**
Three things make it so, and all three are checkable:

1. Nothing in this module composes Cypher. The four delete statements are named
   constants in :mod:`mailarc_analytics.queries.catalog` with their labels
   written into the text, and :func:`rebuild_derived` takes no statement, no
   label and no pattern from its caller.
2. Those four constants are matched against :data:`_DERIVED_NODE_DELETE` and
   :data:`_DERIVED_EDGE_DELETE` **at import time**. A node deletion has to read
   ``MATCH (n:Group|Topic|Template) … DETACH DELETE n``; an edge deletion has to
   read ``MATCH (a:Address) MATCH (a)-[r:CO_ADDRESSED]->(b:Address) … DELETE r``
   — deleting a *relationship* variable, never a node one. Anything else and
   this module refuses to import, so an edit that would delete a ``Message``
   fails before a session is ever opened rather than after. The match is
   against each statement's compiled Cypher, since a catalogue statement is a
   query-builder object rather than a string.
3. Every write below is a ``MERGE`` from the same catalogue, and the derived
   labels are the only labels those statements name. There is no ``DETACH
   DELETE`` over an unlabelled pattern anywhere in this package.

The distinction the second point turns on: ``DETACH DELETE`` on a derived label
removes that node and the edges incident to it, which is exactly right —
``ADDRESSED_GROUP``, ``ABOUT`` and ``INSTANCE_OF`` go with their nodes and no
``Message`` is even matched. ``CO_ADDRESSED`` is the one derived thing that
lives *between* two ground-truth nodes, which is why it needs a statement of its
own and why detaching there would take both addresses down and every
``SENT_TO`` in the archive with them.

Not atomic, and it does not pretend to be. FalkorDB has no multi-statement
transaction, so a rebuild is a delete followed by a recompute with a window in
between where the derived layer is gone. That is acceptable precisely because
derived nodes are disposable — but it makes re-runnability the requirement, and
``MERGE`` plus batched ``DETACH DELETE`` is what provides it.

Synchronous, because every runic driver blocks. The ``derive`` job wraps this in
``asyncio.to_thread``; the progress hook is called from that thread and is
expected to be cheap.
"""

import logging
import re
from collections.abc import Callable, Sequence

from runic.ogm import Session

from mailarc_analytics.derived import correspondents, reader, templates, topics
from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.model import (
    DerivedCounts,
    RebuildProgress,
    RebuildStage,
    SimilarityEdge,
)
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import Statement
from mailarc_analytics.queries.rows import as_int, rows_of

logger = logging.getLogger(__name__)

type ProgressHook = Callable[[RebuildProgress], None]
"""Told what each stage produced; a job row is the usual reader."""

DELETE_BATCH = 10_000
"""Nodes or edges one delete statement removes per round trip.

FalkorDB has no ``CALL … IN TRANSACTIONS``, so an unbounded delete over a large
archive is one long stall on a store the UI is reading at the same time.
Batching turns that into a sequence of short ones.
"""

_DERIVED_NODE_DELETE = re.compile(
    r"MATCH \(n:(?:Group|Topic|Template)\) "
    r"WITH n LIMIT \$batch "
    r"DETACH DELETE n "
    r"RETURN count\(n\) AS removed"
)
"""The only shape a node deletion in this package may have.

Deliberately exact. A delete statement is the one thing here that could destroy
an archive, so it is read character by character rather than trusted to contain
the right label — and the layout is normalised first (see :func:`_normalised`)
so that reformatting the catalogue is not mistaken for tampering with it.
"""

_DERIVED_EDGE_DELETE = re.compile(
    r"MATCH \(a:Address\) "
    r"MATCH \(a\)-\[r:CO_ADDRESSED\]->\(b:Address\) "
    r"WITH r LIMIT \$batch "
    r"DELETE r "
    r"RETURN count\(r\) AS removed"
)
"""The only shape an edge deletion may have: ``DELETE`` on a relationship
variable, both endpoints ``Address`` and both kept.

The endpoints are **named** ``a`` and ``b`` where they used to be anonymous,
and the pattern arrives as two ``MATCH`` clauses rather than one. That is what
``traverse()`` emits for a declared relation and it changes nothing the guard
is for — the deleted variable is still ``r``, there is still no ``DETACH``, and
a statement that deleted ``a`` or ``b`` would fail this match as surely as
before. The regex is exact for the reason it always was: a delete is the one
thing in this package that could destroy an archive, so it is read character by
character rather than trusted to contain the right label.
"""


def _normalised(cypher: str) -> str:
    """Return *cypher* as one line, with the compiler's backticks removed.

    Neither difference carries meaning. runic compiles a statement over several
    lines and backtick-quotes every identifier it emits — ``AS `removed``` —
    so that a model may declare a field named after a Cypher keyword. Both are
    the compiler's formatting, not the statement's shape, and normalising them
    away is what lets the guards below stay written as the Cypher a reader
    would type. A backtick can only wrap an identifier, so dropping it cannot
    hide a clause the shape would otherwise reject.
    """
    return " ".join(cypher.replace("`", "").split())


def _verified(statement: Statement, shape: re.Pattern[str]) -> Statement:
    """Return *statement* if it matches *shape*; raise at import time if not.

    The shape is matched against the statement's **compiled Cypher**, not
    against a Python object: a catalogue statement is a
    :class:`~runic.ogm.QueryBuilder` now, and what has to be checked is the text
    the store will run. ``build()`` is enough here — it needs no session,
    because a delete has no dialect-supplied function in it — and the emitted
    Cypher is what this module refuses to import over.
    """
    cypher = statement if isinstance(statement, str) else statement.build()[0]
    if not shape.fullmatch(_normalised(cypher)):
        raise ValueError(
            f"Refusing a delete statement of an unknown shape: {cypher!r} "
            f"does not match {shape.pattern!r}"
        )
    return statement


_NODE_DELETIONS: tuple[Statement, ...] = tuple(
    _verified(statement, _DERIVED_NODE_DELETE)
    for statement in (
        catalog.DELETE_GROUPS,
        catalog.DELETE_TOPICS,
        catalog.DELETE_TEMPLATES,
    )
)
"""Every derived label, checked. The three the spec names and no other."""

_EDGE_DELETIONS: tuple[Statement, ...] = (
    _verified(catalog.DELETE_CO_ADDRESSED, _DERIVED_EDGE_DELETE),
)
"""The one derived edge type that outlives its endpoints, checked."""


def rebuild_derived(
    session: Session,
    config: AnalyticsConfig,
    *,
    on_progress: ProgressHook | None = None,
    extra_edges: Sequence[SimilarityEdge] = (),
) -> DerivedCounts:
    """Delete the derived layer and compute it again. Returns what it did.

    Runs in the order the analyses depend on: delete everything, read the
    ground truth once, then A1, A2 and A3 over the same facts. Reading once is
    not an optimisation — three reads of a live archive could disagree with
    each other, and a rebuild that clustered a different message set per
    analysis would produce a graph that never existed at any instant.

    A second call over an unchanged archive writes the same graph. Every id is
    a function of the messages behind it, every statement is a ``MERGE``, and
    the delete is unconditional, so there is nothing left over from the first
    run for the second to disagree with.

    *extra_edges* is A2's sixth signal, passed straight through to
    :func:`~mailarc_analytics.derived.topics.build_topics`. It is a parameter
    all the way down rather than something this module computes, because the
    KNN that produces it lives in ``mailarc_analytics.semantic`` and this
    package may not name it — :mod:`app.derive` is the layer allowed to name
    both. Empty is the ordinary case (§7.4's ``provider=none``) and then this
    function does exactly what it did with five signals, which is what "all
    phase-5 analyses run unchanged" is asserted against.
    """
    deleted_nodes, deleted_edges = _delete_derived(session)
    _report(on_progress, RebuildStage.DELETE, deleted_nodes + deleted_edges)

    unidentified = reader.count_unidentified(session)
    facts = reader.read_facts(session, config)
    known = {one.id: one for one in facts}
    beyond = _beyond_ceiling(session, config, len(facts))
    _report(
        on_progress,
        RebuildStage.READ,
        len(facts),
        len(facts) + beyond + unidentified,
    )

    found = correspondents.build_correspondents(facts, config)
    correspondents.write_correspondents(session, found)
    _report(on_progress, RebuildStage.CORRESPONDENTS, len(found.groups), len(facts))

    clustered = topics.build_topics(facts, config, extra_edges=extra_edges)
    topics.write_topics(session, clustered.clusters)
    _report(on_progress, RebuildStage.TOPICS, len(clustered.clusters), len(facts))

    grouping = templates.group_templates(facts, config)
    bodies = reader.read_bodies(session, grouping.member_ids)
    written = templates.describe_templates(grouping, known, bodies, config)
    templates.write_templates(session, written)
    _report(on_progress, RebuildStage.TEMPLATES, len(written), len(facts))

    counts = DerivedCounts(
        messages=len(facts),
        beyond_ceiling=beyond,
        unidentified=unidentified,
        groups=len(found.groups),
        co_addressed=len(found.pairs),
        wide_messages=found.wide_messages,
        topics=len(clustered.clusters),
        dropped_buckets=clustered.dropped_buckets,
        dropped_weak_pairs=clustered.dropped_weak_pairs,
        templates=len(written),
        unhashable_messages=grouping.unhashable_messages,
        dropped_template_buckets=grouping.dropped_buckets,
        deleted_nodes=deleted_nodes,
        deleted_edges=deleted_edges,
    )
    logger.info(
        "Rebuilt the derived layer: %d groups, %d topics, %d templates "
        "from %d messages (%d nodes and %d edges removed first)",
        counts.groups,
        counts.topics,
        counts.templates,
        counts.messages,
        counts.deleted_nodes,
        counts.deleted_edges,
    )
    return counts


def _beyond_ceiling(session: Session, config: AnalyticsConfig, read: int) -> int:
    """Messages the ceiling stopped this rebuild from reading.

    The archive's total is asked for only when there is a ceiling to compare
    it against, because on an uncapped rebuild the answer is ``len(facts)`` and
    a count over every ``Message`` node is a scan nobody needed.
    """
    if config.max_messages <= 0:
        return 0
    return max(0, reader.count_messages(session) - read)


def _delete_derived(session: Session) -> tuple[int, int]:
    """Drop every derived node and every ``CO_ADDRESSED`` edge. Returns both.

    ``deleted_nodes`` counts ``Group``, ``Topic`` and ``Template`` nodes;
    ``deleted_edges`` counts ``CO_ADDRESSED`` edges only. The ``ADDRESSED_GROUP``,
    ``ABOUT`` and ``INSTANCE_OF`` edges are not counted because they are not
    separately deleted — they leave with the node they hang off, which is the
    property that makes this safe in the first place.
    """
    nodes = sum(_drain(session, statement) for statement in _NODE_DELETIONS)
    edges = sum(_drain(session, statement) for statement in _EDGE_DELETIONS)
    logger.info("Removed %d derived nodes and %d co-addressed edges", nodes, edges)
    return nodes, edges


def _drain(session: Session, statement: Statement) -> int:
    """Run one batched delete until it reports nothing left to remove.

    The statement's own ``RETURN count(…) AS removed`` is the loop condition
    rather than the driver's write statistics: those live on a private
    attribute of the raw result and come back as a float. Read by *name* now
    rather than by position — ``rows_of`` keys every row by its column, and a
    delete that gained a second column would otherwise start counting the wrong
    one.

    Bound and never built. A catalogue statement declares ``$batch``, so it
    goes through ``session.all_rows``; ``session.execute(*statement.build())``
    would reach the store without the declared parameter and be refused.
    """
    total = 0
    while True:
        rows = rows_of(session, statement, {"batch": DELETE_BATCH})
        removed = as_int(rows[0]["removed"]) if rows else 0
        if removed == 0:
            return total
        total += removed


def _report(
    hook: ProgressHook | None, stage: RebuildStage, done: int, total: int = 0
) -> None:
    """Tell the caller where the rebuild stands, if anybody asked."""
    if hook is not None:
        hook(RebuildProgress(stage=stage, done=done, total=total))
