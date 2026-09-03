"""``rebuild-derived`` — delete every derived type and compute them all again.

The promise §5.2 makes about this package is that an analysis bug costs one run
and not a restore, and this module is where that promise is either kept or
broken. It deletes every ``Group``, ``Topic``, ``Template`` and ``Community``
together with the edges hanging off them, deletes every ``CO_ADDRESSED`` edge
between two addresses and every ``SUGGESTED`` edge between a message and a tag,
nulls the four properties this phase writes onto ground-truth nodes, and then
runs the nine analyses over the ground truth as it stands.

**It cannot touch ground truth, and that is structural rather than intended.**
Three things make it so, and all three are checkable:

1. Nothing in this module composes Cypher. The six delete statements and the
   two property clears are named constants in
   :mod:`mailarc_analytics.queries.catalog` with their labels written into the
   text, and :func:`rebuild_derived` takes no statement, no label and no
   pattern from its caller.
2. Those eight constants are matched against :data:`_DERIVED_NODE_DELETE`,
   :data:`_DERIVED_EDGE_DELETE`, :data:`_SUGGESTED_EDGE_DELETE` and
   :data:`_PROPERTY_CLEAR` **at import time**. A node deletion has to read
   ``MATCH (n:Group|Topic|Template|Community) … DETACH DELETE n``; an edge
   deletion has to read ``MATCH (a:Address) MATCH (a)-[r:CO_ADDRESSED]->
   (b:Address) … DELETE r`` or ``MATCH (t:Tag) MATCH (t)<-[r:SUGGESTED]-
   (m:Message) … DELETE r`` — deleting a *relationship* variable, never a node
   one; a clear has to be a ``SET … = NULL`` with no ``DELETE`` in it at all.
   Anything else and this module refuses to import, so an edit that would
   delete a ``Message`` fails before a session is ever opened rather than
   after. The match is against each statement's compiled Cypher, since a
   catalogue statement is a query-builder object rather than a string.
3. Every write below is a ``MERGE`` or a ``SET`` from the same catalogue, and
   the derived labels are the only labels those statements name. There is no
   ``DETACH DELETE`` over an unlabelled pattern anywhere in this package.

The distinction the second point turns on: ``DETACH DELETE`` on a derived label
removes that node and the edges incident to it, which is exactly right —
``ADDRESSED_GROUP``, ``ABOUT``, ``INSTANCE_OF``, ``MEMBER_OF`` and
``IN_CIRCLE`` go with their nodes and no ``Message`` is even matched.
``CO_ADDRESSED`` and ``SUGGESTED`` are the two derived things that live
*between* nodes this package may not delete, which is why each needs a
statement of its own: detaching the first would take both addresses down and
every ``SENT_TO`` in the archive with them, and detaching the second would take
a ``Message`` and — worse — a ``Tag``, the annotation layer that holds what a
person decided and that no rebuild may touch.

**Nothing here reaches the label ``Tag`` except to walk off it.** Ground truth
can at least be imported again; a tag cannot, because nothing outside the graph
ever held it.

The four *properties* are a fourth case and are neither a node nor an edge.
``Message.importance``, ``importance_reasons``, ``importance_version`` and
``Address.rank`` (with its version) sit on ground-truth nodes the way
``Message.embedding`` already does: the import never writes them, this module
nulls them in the delete stage and computes them again, and the delete guards
above are unaffected because a ``SET`` removes nothing.

Not atomic, and it does not pretend to be. FalkorDB has no multi-statement
transaction, so a rebuild is a delete followed by a recompute with a window in
between where the derived layer is gone. That is acceptable precisely because
derived nodes are disposable — but it makes re-runnability the requirement, and
``MERGE`` plus batched ``DETACH DELETE`` is what provides it.

**An orchestrator and nothing else.** Every stage below is two or three lines:
ask a module for its findings, hand them to that module's write half, report.
What each finding *means* belongs to the module that computes it, all of which
are pure and testable without a store — which is what keeps this file the one
place a reader has to look to know what a rebuild does and in what order.

Synchronous, because every runic driver blocks. The ``derive`` job wraps this in
``asyncio.to_thread``; the progress hook is called from that thread and is
expected to be cheap.
"""

import logging
import re
from collections.abc import Callable, Mapping, Sequence

from runic.ogm import Session

from mailarc_analytics.derived import (
    algorithms,
    centrality,
    communities,
    conversations,
    correspondents,
    importance,
    keywords,
    reader,
    suggestions,
    templates,
    topics,
)
from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.findings import (
    CommunityFacts,
    CommunityFindings,
    MessageSignals,
)
from mailarc_analytics.derived.model import (
    CorrespondentFindings,
    DerivedCounts,
    MessageFacts,
    RebuildProgress,
    RebuildStage,
    SimilarityEdge,
    TemplateCluster,
    TemplateGrouping,
    TopicCluster,
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
    r"MATCH \(n:(?:Group|Topic|Template|Community)\) "
    r"WITH n LIMIT \$batch "
    r"DETACH DELETE n "
    r"RETURN count\(n\) AS removed"
)
"""The only shape a node deletion in this package may have.

Deliberately exact. A delete statement is the one thing here that could destroy
an archive, so it is read character by character rather than trusted to contain
the right label — and the layout is normalised first (see :func:`_normalised`)
so that reformatting the catalogue is not mistaken for tampering with it.

Four labels now, and the fourth is why the alternation is written out rather
than left open: every derived label has to be *added* here, so a new one is a
visible edit in a guard, while a label that is not derived — ``Message``,
``Address``, ``Tag`` — has no way in at all.
"""

_DERIVED_EDGE_DELETE = re.compile(
    r"MATCH \(a:Address\) "
    r"MATCH \(a\)-\[r:CO_ADDRESSED\]->\(b:Address\) "
    r"WITH r LIMIT \$batch "
    r"DELETE r "
    r"RETURN count\(r\) AS removed"
)
"""The only shape the co-addressing deletion may have: ``DELETE`` on a
relationship variable, both endpoints ``Address`` and both kept.

The endpoints are **named** ``a`` and ``b`` where they used to be anonymous,
and the pattern arrives as two ``MATCH`` clauses rather than one. That is what
``traverse()`` emits for a declared relation and it changes nothing the guard
is for — the deleted variable is still ``r``, there is still no ``DETACH``, and
a statement that deleted ``a`` or ``b`` would fail this match as surely as
before. The regex is exact for the reason it always was: a delete is the one
thing in this package that could destroy an archive, so it is read character by
character rather than trusted to contain the right label.
"""

_SUGGESTED_EDGE_DELETE = re.compile(
    r"MATCH \(t:Tag\) "
    r"MATCH \(t\)<-\[r:SUGGESTED\]-\(m:Message\) "
    r"WITH r LIMIT \$batch "
    r"DELETE r "
    r"RETURN count\(r\) AS removed"
)
"""The only shape the suggestion deletion may have, and the strictest guard here.

A shape of its own rather than a second alternation inside
:data:`_DERIVED_EDGE_DELETE`, because the two statements are not the same risk.
``CO_ADDRESSED`` runs between two ``Address`` nodes, which an import could
write again; ``SUGGESTED`` runs from a ``Message`` to a **``Tag``**, and a tag
is what a person decided a set of messages is called — the one thing in this
graph that no re-import could restore, because nothing outside the graph ever
held it.

So: ``DELETE r`` and never ``DETACH``, the tag matched only in order to be
walked off, and both endpoint variables left standing. ``DELETE t`` is one
character away and would empty the annotation layer of every tag an analysis
had anything to suggest for.

Rooted at the ``Tag`` rather than at the ``Message`` because runic emits a
predicate naming a *traversed* variable after the whole pipeline: walked in
from the message end, the batching ``WITH`` lands behind the ``DELETE`` it was
meant to bound. The guard pins the working order rather than trusting it.
"""

_PROPERTY_CLEAR = re.compile(
    r"MATCH \(m:Message\) "
    r"WHERE \(m\.importance IS NOT NULL\) OR \(m\.importance_version IS NOT NULL\) "
    r"SET m\.importance = NULL, m\.importance_reasons = NULL, "
    r"m\.importance_version = NULL "
    r"RETURN count\(m\) AS cleared"
    r"|"
    r"MATCH \(a:Address\) "
    r"WHERE \(a\.rank IS NOT NULL\) OR \(a\.rank_version IS NOT NULL\) "
    r"SET a\.rank = NULL, a\.rank_version = NULL "
    r"RETURN count\(a\) AS cleared"
)
"""The two shapes a property clear may have — both of them a ``SET``, neither a
delete.

The four properties this phase writes sit on *ground-truth* nodes, so the two
statements that reset them are the only ones in the rebuild that name
``Message`` and ``Address`` outside a read. A ``SET … = NULL`` removes a
property and nothing else, which is exactly why they are safe and exactly why
they need a guard of their own: they are the two statements where changing one
word would turn a reset into a deletion of the archive, and the delete guards
above would never see it because these are not deletes.

Spelled out in full for both, so an edit to either statement is a visible edit
here as well.
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
    because neither a delete nor a null assignment has a dialect-supplied
    function in it — and the emitted Cypher is what this module refuses to
    import over.
    """
    cypher = statement if isinstance(statement, str) else statement.build()[0]
    if not shape.fullmatch(_normalised(cypher)):
        raise ValueError(
            f"Refusing a destructive statement of an unknown shape: {cypher!r} "
            f"does not match {shape.pattern!r}"
        )
    return statement


_NODE_DELETIONS: tuple[Statement, ...] = tuple(
    _verified(statement, _DERIVED_NODE_DELETE)
    for statement in (
        catalog.DELETE_GROUPS,
        catalog.DELETE_TOPICS,
        catalog.DELETE_TEMPLATES,
        catalog.DELETE_COMMUNITIES,
    )
)
"""Every derived label, checked. The four the spec names and no other."""

_EDGE_DELETIONS: tuple[Statement, ...] = (
    _verified(catalog.DELETE_CO_ADDRESSED, _DERIVED_EDGE_DELETE),
    _verified(catalog.DELETE_SUGGESTED, _SUGGESTED_EDGE_DELETE),
)
"""The two derived edge types that outlive their endpoints, each checked
against its own shape."""

_PROPERTY_CLEARS: tuple[Statement, ...] = tuple(
    _verified(statement, _PROPERTY_CLEAR)
    for statement in (catalog.CLEAR_IMPORTANCE, catalog.CLEAR_ADDRESS_RANKS)
)
"""The four derived properties on ground-truth nodes, reset rather than
removed."""


def rebuild_derived(
    session: Session,
    config: AnalyticsConfig,
    *,
    on_progress: ProgressHook | None = None,
    extra_edges: Sequence[SimilarityEdge] = (),
) -> DerivedCounts:
    """Delete the derived layer and compute it again. Returns what it did.

    Runs the ten stages of §3.3 in the order they depend on each other: delete
    everything, read the ground truth once, then the analyses. Reading once is
    not an optimisation — several reads of a live archive could disagree with
    each other, and a rebuild that clustered a different message set per
    analysis would produce a graph that never existed at any instant.

    Four of the ten sit where they do because of a dependency and not because
    of taste. ``CENTRALITY`` precedes ``COMMUNITIES``, whose labels and whose
    ``MEMBER_OF.rank`` are ranks. ``KEYWORDS`` follows ``TOPICS``, because the
    TF-IDF is over the clusters. ``IMPORTANCE`` follows ``TEMPLATES`` and
    ``CENTRALITY``, whose automation score and sender rank are two of its
    reasons. ``SUGGESTIONS`` is last, because it argues from the topics, the
    circles and the ``TAGGED`` memberships as they stand after everything else.

    A second call over an unchanged archive writes the same graph. Every id is
    a function of the messages behind it, every statement is a ``MERGE`` or a
    ``SET``, and the delete is unconditional, so there is nothing left over
    from the first run for the second to disagree with. The one algorithm that
    could break that — FalkorDB's unseeded label propagation — is keyed on its
    members rather than on the community number it happened to assign.

    *extra_edges* is A2's sixth signal, passed through to
    :func:`~mailarc_analytics.derived.topics.build_topics` beside the seventh.
    It is a parameter all the way down rather than something this module
    computes, because the KNN that produces it lives in
    ``mailarc_analytics.semantic`` and this package may not name it —
    :mod:`app.derive` is the layer allowed to name both. Empty is the ordinary
    case (§7.4's ``provider=none``) and then this function does exactly what it
    did without signal 6.
    """
    deleted_nodes, deleted_edges = _delete_derived(session)
    _clear_properties(session)
    _report(on_progress, RebuildStage.DELETE, deleted_nodes + deleted_edges)

    unidentified = reader.count_unidentified(session)
    facts = reader.read_facts(session, config)
    signals = reader.read_signals(session, config)
    own = reader.read_account_addresses(session)
    exchanges = conversations.conversation_edges(
        facts, reader.read_replies(session, config)
    )
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

    ranks, ranked_addresses = _rank_addresses(session, config, found)
    replies = algorithms.message_pagerank(session)
    _report(on_progress, RebuildStage.CENTRALITY, ranked_addresses, len(ranks))

    circles = _build_circles(session, config, facts, ranks)
    _report(on_progress, RebuildStage.COMMUNITIES, len(circles.communities), len(ranks))

    clustered = topics.build_topics(
        facts, config, extra_edges=(*exchanges, *extra_edges)
    )
    topics.write_topics(session, clustered.clusters)
    _report(on_progress, RebuildStage.TOPICS, len(clustered.clusters), len(facts))

    keyworded = _describe_topics(session, config, clustered.clusters)
    _report(on_progress, RebuildStage.KEYWORDS, keyworded, len(clustered.clusters))

    grouping, written = _build_templates(session, config, facts)
    _report(on_progress, RebuildStage.TEMPLATES, len(written), len(facts))

    scored = _score_importance(
        session,
        facts,
        signals,
        ranks=ranks,
        replies=replies.scores,
        written=written,
        own=own,
    )
    _report(on_progress, RebuildStage.IMPORTANCE, scored, len(facts))

    offered = _offer_suggestions(
        session, config, exchanges, clustered.clusters, circles.communities
    )
    _report(on_progress, RebuildStage.SUGGESTIONS, offered, len(facts))

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
        communities=len(circles.communities),
        circles=sum(one.message_count for one in circles.communities),
        ranked_addresses=ranked_addresses,
        ranked_messages=len(replies.scores),
        keyworded_topics=keyworded,
        scored_messages=scored,
        suggestions=offered,
        algorithms_skipped=circles.skipped + replies.skipped,
        deleted_nodes=deleted_nodes,
        deleted_edges=deleted_edges,
    )
    logger.info(
        "Rebuilt the derived layer: %d groups, %d topics, %d templates, "
        "%d communities from %d messages (%d nodes and %d edges removed first)",
        counts.groups,
        counts.topics,
        counts.templates,
        counts.communities,
        counts.messages,
        counts.deleted_nodes,
        counts.deleted_edges,
    )
    return counts


def _rank_addresses(
    session: Session, config: AnalyticsConfig, found: CorrespondentFindings
) -> tuple[dict[str, float], int]:
    """Stage 4: who is at the centre of this archive's correspondence.

    Power iteration in Python over A1's pairs rather than ``algo.pageRank``
    over the stored edge, and the reason is in
    :mod:`mailarc_analytics.derived.centrality`: ``CO_ADDRESSED`` is written
    with the smaller id first, so a PageRank following that arrow would rank
    the alphabet. The pairs are already in memory, so the input costs nothing.

    Returns the ranks — the next two stages both read them — and what the store
    said it touched.
    """
    ranks = centrality.weighted_pagerank(
        found.pairs, max_edges=config.centrality_max_edges
    )
    return ranks, centrality.write_address_ranks(session, ranks)


def _build_circles(
    session: Session,
    config: AnalyticsConfig,
    facts: Sequence[MessageFacts],
    ranks: Mapping[str, float],
) -> CommunityFindings:
    """Stage 5: the circles label propagation found, keyed by their members.

    The procedure may refuse the graph outright — FalkorDB's ``algo.*`` throw
    on a label the store does not hold, which an archive before its first
    ``CO_ADDRESSED`` edge is — so what comes back carries whether it ran, and
    that number travels into ``algorithms_skipped`` rather than being lost in
    an empty partition.
    """
    partition = algorithms.label_propagation(
        session, max_iterations=config.community_max_iterations
    )
    circles = communities.build_communities(
        facts, partition.labels, ranks, config, skipped=partition.skipped
    )
    communities.write_communities(session, circles)
    return circles


def _describe_topics(
    session: Session, config: AnalyticsConfig, clusters: Sequence[TopicCluster]
) -> int:
    """Stage 7: what each topic is about, in its own members' words.

    The read is asked for exactly the members the counter will look at —
    :func:`~mailarc_analytics.derived.keywords.keyword_members` is what makes
    the two agree — so no page of text is fetched and then ignored.

    Answers with the topics that came out with a keyword rather than the topics
    that were looked at, because a topic whose members had nothing to say is
    absent from the finding and not present with an empty tuple.
    """
    members = keywords.keyword_members(clusters, config)
    texts = reader.read_texts(session, members, config.topic_keyword_chars)
    found = keywords.topic_keywords(clusters, texts, config)
    keywords.write_keywords(session, found)
    return len(found)


def _build_templates(
    session: Session, config: AnalyticsConfig, facts: Sequence[MessageFacts]
) -> tuple[TemplateGrouping, tuple[TemplateCluster, ...]]:
    """Stage 8: what gets retyped, and the two halves A3 falls into.

    The clustering needs nothing but fingerprints, which is what lets the
    bodies of a few hundred members be read afterwards instead of the whole
    archive's up front. Both halves come back because the grouping carries two
    counts a job row reports and the clusters carry the automation scores the
    next stage argues from.
    """
    grouping = templates.group_templates(facts, config)
    bodies = reader.read_bodies(session, grouping.member_ids)
    known = {one.id: one for one in facts}
    written = templates.describe_templates(grouping, known, bodies, config)
    templates.write_templates(session, written)
    return grouping, written


def _score_importance(
    session: Session,
    facts: Sequence[MessageFacts],
    signals: Mapping[str, MessageSignals],
    *,
    ranks: Mapping[str, float],
    replies: Mapping[str, float],
    written: Sequence[TemplateCluster],
    own: frozenset[str],
) -> int:
    """Stage 9: how much each message probably matters, and why.

    Every input is something an earlier stage produced — the sender's rank from
    ``CENTRALITY``, the reply centrality from the same stage's ``algo.pageRank``
    over ``REPLIES_TO``, the automation score from ``TEMPLATES``, the archive's
    own addresses from the read — which is what the stage order is for.

    Answers with what the **store** touched rather than with the number of
    scores built: a message purged between the read and the write is a row that
    did not land, and a stage reporting what it hoped for would hide that.
    """
    scores = importance.score_messages(
        facts,
        signals,
        sender_rank=ranks,
        reply_rank=replies,
        template_scores=templates.automation_by_message(written),
        own=own,
    )
    return importance.write_importance(session, scores)


def _offer_suggestions(
    session: Session,
    config: AnalyticsConfig,
    exchanges: Sequence[SimilarityEdge],
    clusters: Sequence[TopicCluster],
    circles: Sequence[CommunityFacts],
) -> int:
    """Stage 10: which messages each tag might want next.

    Last, because it argues from everything before it — the conversations the
    read found, the clusters A2 wrote, the circles label propagation found, and
    the ``TAGGED`` memberships as they stand right now.

    Nothing here writes a membership. ``TAGGED`` records what a person decided
    and belongs to :mod:`mailarc_core.archive.tags`; what this stage writes is
    a ``SUGGESTED`` edge, which the next rebuild deletes.
    """
    groups = suggestions.groupings(exchanges, clusters, circles)
    offered = suggestions.suggest(reader.read_tagged(session), groups, config)
    return suggestions.write_suggestions(session, offered)


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
    """Drop every derived node and both derived edge types. Returns both counts.

    ``deleted_nodes`` counts ``Group``, ``Topic``, ``Template`` and
    ``Community`` nodes; ``deleted_edges`` counts ``CO_ADDRESSED`` and
    ``SUGGESTED`` edges. The ``ADDRESSED_GROUP``, ``ABOUT``, ``INSTANCE_OF``,
    ``MEMBER_OF`` and ``IN_CIRCLE`` edges are not counted because they are not
    separately deleted — they leave with the node they hang off, which is the
    property that makes this safe in the first place.
    """
    nodes = sum(_drain(session, statement) for statement in _NODE_DELETIONS)
    edges = sum(_drain(session, statement) for statement in _EDGE_DELETIONS)
    logger.info("Removed %d derived nodes and %d derived edges", nodes, edges)
    return nodes, edges


def _clear_properties(session: Session) -> int:
    """Null the four derived properties on ground-truth nodes. Returns how many.

    Part of the delete stage although nothing is deleted, because it is the
    same argument one step over: a rebuild recomputes the derived layer, and a
    message that dropped out of the scoring — its template gone, its replies
    purged with an account — would otherwise keep the number the *previous* run
    gave it forever. Nulling first is what makes ``importance`` and ``rank``
    mean "this run's answer".

    Unbatched, like ``CLEAR_EMBEDDINGS``: both statements filter on the
    property being set, so a first rebuild clears nothing and a later one
    touches only what it wrote.
    """
    cleared = sum(_cleared(session, statement) for statement in _PROPERTY_CLEARS)
    logger.info("Reset %d derived properties on ground-truth nodes", cleared)
    return cleared


def _cleared(session: Session, statement: Statement) -> int:
    """Run one property clear and read the count it projects.

    By name rather than by position, for :func:`_drain`'s reason: ``rows_of``
    keys every row by its column, and a statement that gained a second one
    would otherwise start counting the wrong thing.
    """
    rows = rows_of(session, statement)
    return as_int(rows[0]["cleared"]) if rows else 0


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
