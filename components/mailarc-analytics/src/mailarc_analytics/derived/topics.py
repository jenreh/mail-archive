"""A2 — which messages belong to one piece of work.

Six signals, each with a weight, joined into a similarity graph and cut into
connected components: a shared ticket token, a shared thread, a shared
normalised subject, a shared attachment, a shared participant group, and last
of all semantic nearness. The first five are facts the import already computed,
which is why this analysis is allowed to write nodes at all. The sixth is a
*suggestion* — §6.2 ranks it last because it is the only one of the six that
can be wrong about a fact, and ``ABOUT.method`` is what lets a reader tell the
two apart.

**Nothing here imports anything semantic, and that is checked** in
``tests/test_analytics_package.py``. Signal 6 arrives as an argument —
:paramref:`build_topics.extra_edges` — from a caller that holds an embedder,
which is what keeps A1-A3 running in full on a machine that has none: §7.4
makes ``provider=none`` the default, so the empty sequence is the state nearly
every archive runs in and is a complete one rather than a degraded one.

The weights in :data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS` are
calibrated against
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_min_score` so
that the set splits in three, and the split is what makes the analysis
tractable:

*strong* (``ref``, ``thread``, ``subject``)
    One occurrence already clears the threshold, so the pair never needs to be
    scored. A bucket of *k* messages is joined by *k-1* consecutive unions
    instead of *k(k-1)/2* pairs — the same component, at linear cost. That
    equivalence is the whole trick: a chain connects what a clique connects.
*weak* (``attachment``, ``participants``)
    Only meaningful added up — a shared attachment *and* a shared participant
    group clears the threshold, either alone does not — so these are the only
    ones that need a pair table, and the only ones a memory budget can
    degrade. Which is the correct failure direction: §6.2 calls a ticket
    cluster a fact and everything else a suggestion, and memory pressure must
    never cost a fact.
*a suggestion* (``embedding``, and anything else a later phase names)
    Not in that table at all. It arrives already scored, as a cosine
    similarity, and it is ranked **below every exact signal whatever that
    number says** — which is the one thing about signal 6 that is not
    self-evident: a similarity runs from 0.82 to 1.0 and outscores three of the
    five exact weights on arithmetic alone, so an ordering by score would print
    "embedding" on the edge of a pair a shared ticket had already joined.
    :func:`_reason_rank` is where that is prevented.

Two guards sit on top. A bucket over
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_bucket_cap` is
dropped as boilerplate — a ``subject_norm`` of "rechnung" shared by five
thousand messages is not a project, and joining them would produce one topic
holding a fifth of the archive. And the pair table stops growing at
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_max_weak_pairs`,
smallest buckets first, because a four-message shared-attachment bucket is a
project while a hundred-and-eighty-member participant bucket is a distribution
list. **The suggestions spend that same ceiling**, after the weak table and
never instead of it: a KNN answers per message, so ``k`` neighbours over a
hundred thousand messages is a million rows offered in one call, and a second
ceiling under the same setting's name would let one rebuild hold twice what
the user asked for. Every drop is counted rather than logged and forgotten.

Connected components come from
:class:`~mailarc_analytics.derived.model.DisjointSet` — thirty lines of
union-find rather than networkx. The spec writes "ggf." beside that name, and a
dependency whose entire job is to find the components of a graph this module
builds itself would have to be resolved, pinned, audited and vendored into a
Tauri bundle for the sake of one loop.

The compute half is pure and needs no graph; the write half is two ``MERGE``
statements out of the catalogue.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import combinations, pairwise

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.model import (
    SIGNAL_WEIGHTS,
    MessageFacts,
    SimilarityEdge,
    TopicCluster,
    TopicFindings,
    TopicMember,
    TopicSignal,
    topic_id,
)
from mailarc_analytics.derived.partition import DisjointSet
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import as_graph_datetime

logger = logging.getLogger(__name__)

type _Buckets = dict[TopicSignal, dict[str, list[str]]]
"""Per signal, the message ids that share each value of it."""

type _Best = dict[str, tuple[float, str]]
"""Per message, the strongest reason it ended up in its component."""


def build_topics(
    facts: Sequence[MessageFacts],
    config: AnalyticsConfig,
    *,
    extra_edges: Sequence[SimilarityEdge] = (),
) -> TopicFindings:
    """Cluster the messages by the six signals. No I/O, no graph, no embedder.

    *extra_edges* is signal 6, and it is a parameter rather than a computation
    because of the import table: an embedding KNN lives in
    ``mailarc_analytics.semantic``, this package may not name it, and a caller
    that has one hands the neighbours in already scored. Empty is the ordinary
    case — §7.4's ``provider=none`` — and then this function does exactly what
    it did with five signals.

    The edges are applied **after** the exact signals have settled, so a
    suggestion can only join what a fact left open, which is precisely what
    §6.2 promises about signal 6. What arrives is
    :class:`~mailarc_analytics.derived.model.SimilarityEdge` values carrying
    ``method="embedding"`` and a cosine similarity as the weight; the caller
    has already applied its own cut-off, and
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_min_score`
    is a floor under it rather than a substitute for it.
    """
    partition = DisjointSet(one.id for one in facts)
    best: _Best = {}
    known = {one.id: one for one in facts}
    buckets, dropped_buckets = _buckets(facts, config)

    _join_strong(partition, best, buckets, config)
    spent, dropped_pairs = _join_weak(partition, best, buckets, config)
    dropped_pairs += _join_extra(
        partition, best, extra_edges, config, set(known), spent=spent
    )

    clusters = tuple(
        _cluster(members, known, best)
        for members in partition.components()
        if len(members) > 1
    )
    logger.info(
        "A2 found %d topics over %d messages (%d buckets and %d weak pairs dropped)",
        len(clusters),
        len(facts),
        dropped_buckets,
        dropped_pairs,
    )
    return TopicFindings(
        clusters=clusters,
        dropped_buckets=dropped_buckets,
        dropped_weak_pairs=dropped_pairs,
    )


def write_topics(session: Session, clusters: Sequence[TopicCluster]) -> None:
    """Write the topics and the ``ABOUT`` edge that explains each membership.

    Topics before their edges, because
    :data:`~mailarc_analytics.queries.catalog.MERGE_ABOUT` matches both
    endpoints instead of merging them: a topic that is not there yet is a bug
    in this ordering, and merging it would hide that behind an empty node.

    The score and the method go on the *edge* as well as on the node. The node
    ranks the topic; the edge says why this particular message is in it, and a
    message pulled in by a ticket token does not get to borrow the confidence
    of one pulled in by a shared attachment.
    """
    merge_rows(
        session,
        catalog.MERGE_TOPICS,
        (
            {
                "id": topic.id,
                "label": topic.label,
                "method": topic.method,
                "score": topic.score,
                "message_count": topic.message_count,
                "first_seen": as_graph_datetime(topic.first_seen),
                "last_seen": as_graph_datetime(topic.last_seen),
            }
            for topic in clusters
        ),
    )
    merge_rows(
        session,
        catalog.MERGE_ABOUT,
        (
            {
                "message_id": member.message_id,
                "topic_id": topic.id,
                "score": member.score,
                "method": member.method,
            }
            for topic in clusters
            for member in topic.members
        ),
    )
    logger.info("Wrote %d topics", len(clusters))


def _buckets(
    facts: Sequence[MessageFacts], config: AnalyticsConfig
) -> tuple[_Buckets, int]:
    """The inverted index per signal, boilerplate buckets already removed.

    An over-sized bucket is dropped for *every* signal, strong or weak, and the
    number is reported: it is a semantic guard before it is a performance one,
    and a rebuild that quietly ignored the archive's five thousand invoices
    would look exactly like a rebuild that never saw them.

    The bucket's *key* stays out of the line. For ``SUBJECT`` it is somebody's
    mail subject, there is one line per dropped bucket, and an archive full of
    boilerplate would write a page of real correspondence into a log a user
    might attach to a bug report. Debug rather than info for the same reason —
    the number a reader needs is in :func:`build_topics`'s one-line summary.
    """
    found: _Buckets = {signal: {} for signal in TopicSignal}
    for message in facts:
        for token in message.refs:
            found[TopicSignal.REF].setdefault(token, []).append(message.id)
        if message.thread_id:
            found[TopicSignal.THREAD].setdefault(message.thread_id, []).append(
                message.id
            )
        if message.subject_norm:
            found[TopicSignal.SUBJECT].setdefault(message.subject_norm, []).append(
                message.id
            )
        for sha in message.attachments:
            found[TopicSignal.ATTACHMENT].setdefault(sha, []).append(message.id)
        if message.participant_key:
            found[TopicSignal.PARTICIPANTS].setdefault(
                message.participant_key, []
            ).append(message.id)

    dropped = 0
    for signal, keyed in found.items():
        for _key, members in sorted(keyed.items()):
            if len(members) > config.topic_bucket_cap:
                logger.debug(
                    "Signal %s joins %d messages on one value — boilerplate",
                    signal.value,
                    len(members),
                )
                dropped += 1
        found[signal] = {
            key: sorted(members)
            for key, members in keyed.items()
            if 1 < len(members) <= config.topic_bucket_cap
        }
    return found, dropped


def _join_strong(
    partition: DisjointSet,
    best: _Best,
    buckets: _Buckets,
    config: AnalyticsConfig,
) -> None:
    """Chain every bucket of a signal that clears the threshold on its own.

    Consecutive unions rather than every pair. A pair carrying a strong signal
    is joined unconditionally, and adding weak weight to an already-joined pair
    changes nothing, so the chain and the clique produce the same components —
    at *k* steps instead of *k(k-1)/2*. Without it, a mailing list whose
    subject normalises the same way across two thousand messages would be two
    million dictionary entries before the first topic exists.
    """
    for signal in _signals(strong=True, threshold=config.topic_min_score):
        weight = SIGNAL_WEIGHTS[signal]
        for _key, members in sorted(buckets[signal].items()):
            for left, right in pairwise(members):
                partition.union(left, right)
            for member in members:
                _offer(best, member, weight, signal.value)


def _join_weak(
    partition: DisjointSet,
    best: _Best,
    buckets: _Buckets,
    config: AnalyticsConfig,
) -> tuple[int, int]:
    """Accumulate the weak signals per pair and join what adds up. Returns the
    pairs the table holds and the pairs the budget refused.

    Smallest buckets first, because that is the right triage: a four-message
    shared-attachment bucket is a project, a hundred-and-eighty-member
    participant bucket is a distribution list. Once one bucket does not fit,
    no later one can — they are sorted by size — so the rest is counted and the
    pass stops.

    The first number is what :func:`_join_extra` has left to spend. Reported
    rather than recomputed from the ceiling, because two buckets of the same
    messages produce the *same* pairs — a shared attachment and a shared
    participant group usually do — and the table is then smaller than the
    budget charged for it. Signal 6 gets that difference back.
    """
    pending = sorted(
        (
            (signal, key, members)
            for signal in _signals(strong=False, threshold=config.topic_min_score)
            for key, members in buckets[signal].items()
        ),
        key=lambda one: (len(one[2]), one[0].value, one[1]),
    )
    score: dict[tuple[str, str], float] = {}
    carrier: dict[tuple[str, str], str] = {}
    counted: dict[tuple[str, str], set[TopicSignal]] = {}
    dropped = 0
    for index, (signal, _key, members) in enumerate(pending):
        additions = len(members) * (len(members) - 1) // 2
        if len(score) + additions > config.topic_max_weak_pairs:
            dropped = sum(
                len(one[2]) * (len(one[2]) - 1) // 2 for one in pending[index:]
            )
            logger.warning(
                "Weak-pair budget of %d spent; %d pairs in %d buckets not scored",
                config.topic_max_weak_pairs,
                dropped,
                len(pending) - index,
            )
            break
        weight = SIGNAL_WEIGHTS[signal]
        for pair in combinations(members, 2):
            # Once per signal, not once per bucket. A signal's buckets are keyed
            # by the thing that matched — an attachment digest, a participant
            # key — so a pair that shares two files appears in two attachment
            # buckets. Adding the weight each time gave it 0.4 + 0.4 = 0.8 and
            # cleared a `topic_min_score` of 0.5 that the weight was chosen to
            # sit under, which made two colleagues who both attach the company
            # logo and the same signature image into a topic. Sharing a second
            # file is more of the same evidence, not a second kind of it.
            seen = counted.setdefault(pair, set())
            if signal in seen:
                continue
            seen.add(signal)
            score[pair] = score.get(pair, 0.0) + weight
            if weight > _weight_of(carrier.get(pair, "")):
                carrier[pair] = signal.value

    for pair, total in sorted(score.items()):
        if total < config.topic_min_score:
            continue
        partition.union(*pair)
        for endpoint in pair:
            _offer(best, endpoint, min(1.0, total), carrier[pair])
    return len(score), dropped


def _join_extra(
    partition: DisjointSet,
    best: _Best,
    edges: Sequence[SimilarityEdge],
    config: AnalyticsConfig,
    known: set[str],
    *,
    spent: int,
) -> int:
    """Apply signal 6, after the exact signals have decided everything. Returns
    the suggestions the budget refused.

    **What this costs, measured.** A KNN asks per message and answers with *k*
    neighbours, so a caller over an archive of *n* messages offers up to
    ``n × k`` rows: a million at a hundred thousand messages and ``k=10``, ten
    million at ``k=100``. At that first shape, on the vendored runtime, the
    caller's own list of
    :class:`~mailarc_analytics.derived.model.SimilarityEdge` is 504 bytes per
    row — half a gigabyte before this function is entered — the folded pair
    table below is 170 bytes per pair, and the whole pass takes 3.4 seconds.
    So the expensive half is the caller's ``k`` and nothing here can bound it;
    what this ceiling bounds is the part that outlives the call.

    Charged against whatever
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.topic_max_weak_pairs`
    the weak table left over, which at the default of two million refuses
    nothing at ``k=10`` and most of a ``k=100`` run. The union-find itself is
    near-linear and would survive either; the dict entry per pair is what a
    memory ceiling is for, and a *suggestion* may not be the thing that makes a
    rebuild run a machine out of memory.

    Refusals are counted into
    :attr:`~mailarc_analytics.derived.model.TopicFindings.dropped_weak_pairs`
    with the weak table's own, because they are the same thing happening to
    the same ceiling and a job row that separated them would be asking a user
    to care which half of the suggestions went missing. What is *not* counted
    is a row the threshold rejected: that is the analysis deciding, not the
    budget running out.

    Sorted by similarity before the cut, so a spent budget keeps the strongest
    suggestions and the result cannot depend on the order a nearest-neighbour
    search happened to answer in — a rebuild that renamed a topic because a KNN
    reshuffled itself would be a data change that is not one.
    """
    offered = _extra_pairs(edges, config, known)
    budget = max(0, config.topic_max_weak_pairs - spent)
    ranked = sorted(offered.items(), key=lambda one: (-one[1][0], one[0]))
    dropped = max(0, len(ranked) - budget)
    if dropped:
        logger.warning(
            "Weak-pair budget of %d spent; %d of %d semantic pairs not applied",
            config.topic_max_weak_pairs,
            dropped,
            len(ranked),
        )
    for pair, (weight, method) in ranked[:budget]:
        partition.union(*pair)
        for endpoint in pair:
            _offer(best, endpoint, min(1.0, weight), method)
    return dropped


def _extra_pairs(
    edges: Sequence[SimilarityEdge], config: AnalyticsConfig, known: set[str]
) -> dict[tuple[str, str], tuple[float, str]]:
    """The distinct message pairs the suggestions actually propose.

    Three shapes a nearest-neighbour search produces are folded out here, and
    each of them is what the store really returns rather than a hypothetical.
    A vector's own node is its nearest neighbour, so a self-pair arrives for
    every message; two messages that are each other's neighbour arrive twice,
    once from each end and with the endpoints the other way round; and an index
    that has not been rebuilt since the last import names messages this run
    never read. The first two would each spend a pair of
    :func:`_join_extra`'s budget on something that is not a new pair, and the
    third would invent a member, so all three go before anything is counted.

    The pair is keyed in a canonical order for the same reason ``CO_ADDRESSED``
    is written in one: ``(a, b)`` and ``(b, a)`` are one edge, and a table that
    held both would report twice the pairs a rebuild actually applied.
    """
    found: dict[tuple[str, str], tuple[float, str]] = {}
    for edge in edges:
        if edge.weight < config.topic_min_score:
            continue
        if edge.left == edge.right:
            logger.debug("Ignoring a similarity edge from a message to itself")
            continue
        if edge.left not in known or edge.right not in known:
            logger.debug("Ignoring similarity edge over unknown message")
            continue
        pair = (min(edge.left, edge.right), max(edge.left, edge.right))
        current = found.get(pair)
        if current is None or (-edge.weight, edge.method) < (-current[0], current[1]):
            found[pair] = (edge.weight, edge.method)
    return found


def _cluster(
    members: Sequence[str], known: Mapping[str, MessageFacts], best: _Best
) -> TopicCluster:
    """One component, described the way a human will read it back.

    ``method`` is the strongest reason anything in the component was joined,
    so the node can be believed or distrusted at a glance; ``score`` is the
    mean of the members' own scores, so a component held together by one
    ticket and a lot of coincidence does not read as certain.
    """
    reasons = [(member, best.get(member, (0.0, ""))) for member in members]
    scored = tuple(
        TopicMember(message_id=member, score=score, method=method)
        for member, (score, method) in reasons
    )
    dated: list[datetime] = []
    for member in members:
        when = known[member].sent_at
        if when is not None:
            dated.append(when)
    strongest = min(
        (method for _member, (_score, method) in reasons),
        key=_method_rank,
        default="",
    )
    return TopicCluster(
        id=topic_id(members),
        label=_label(members, known),
        method=strongest,
        score=round(sum(one.score for one in scored) / len(scored), 6),
        first_seen=min(dated, default=None),
        last_seen=max(dated, default=None),
        members=scored,
    )


def _label(members: Sequence[str], known: Mapping[str, MessageFacts]) -> str:
    """What to call the topic: the subject its messages most often carry.

    Ties go to the lexicographically smallest subject rather than to whichever
    one was counted first. ``Counter.most_common`` is insertion-ordered, and a
    rebuild that renamed a topic it had not changed looks like a data change
    and is not.

    A component whose members all normalise to different subjects — five mails
    about one ticket usually do — falls back to the ticket token they share,
    and to nothing at all if even that is not common to all of them.
    """
    counts: dict[str, int] = {}
    for member in members:
        subject = known[member].subject_norm
        if subject:
            counts[subject] = counts.get(subject, 0) + 1
    if counts:
        return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]

    shared: set[str] | None = None
    for member in members:
        refs = set(known[member].refs)
        shared = refs if shared is None else shared & refs
    return min(shared) if shared else ""


def _signals(*, strong: bool, threshold: float) -> tuple[TopicSignal, ...]:
    """The signals on one side of the threshold, strongest first.

    Read off :data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS` against
    the configured threshold rather than listed here, so a user who lowers
    ``topic_min_score`` moves the split itself instead of leaving a hard-coded
    list disagreeing with their setting.
    """
    chosen = [
        signal
        for signal, weight in SIGNAL_WEIGHTS.items()
        if (weight >= threshold) is strong
    ]
    return tuple(sorted(chosen, key=lambda one: (-SIGNAL_WEIGHTS[one], one.value)))


def _signal_of(name: str) -> TopicSignal | None:
    """The signal a stored method name refers to, or ``None`` if it is newer.

    Phase 6 writes ``method="embedding"``, which this build has no weight for.
    Answering ``None`` rather than raising is what lets an older build read a
    newer graph, which is why the edge property is a string in the first place.
    """
    try:
        return TopicSignal(name)
    except ValueError:
        return None


def _offer(best: _Best, member: str, score: float, method: str) -> None:
    """Record why this message was joined, keeping the strongest reason.

    Ties go to the smaller method name so that two reasons of equal weight
    always resolve the same way; without it the answer would depend on which
    bucket the iteration reached first.
    """
    current = best.get(member)
    if current is None or _reason_rank(score, method) < _reason_rank(*current):
        best[member] = (score, method)


def _reason_rank(score: float, method: str) -> tuple[int, float, str]:
    """How good a reason for a membership is, smallest first.

    An exact signal comes before a suggestion **whatever the two scores are**,
    and that first element is the whole of signal 6's containment. Compare on
    score alone and a cosine similarity of 0.95 takes the edge off a shared
    subject worth 0.5; compare on score and name and a perfect similarity of
    1.0 takes it off a shared ticket, because ``"embedding" < "ref"``. Either
    way ``ABOUT.method`` would read as a suggestion on a pair §6.2 calls a
    fact, and the badge the UI draws from it would be a lie.

    Among the five exact signals nothing changed and nothing may: the score is
    accumulated weight, so the higher one is the better reason, and an archive
    clustered before signal 6 existed has to cluster identically after.
    """
    return (0 if _signal_of(method) is not None else 1, -score, method)


def _method_rank(name: str) -> tuple[int, float, str]:
    """How strong a *signal* is, smallest first — what names a component.

    Ranked by the calibrated weight rather than by whatever score a member
    happened to reach with it, because the question the node's ``method``
    answers is "what kind of thing is this topic", not "how sure are we". A
    method this build has no weight for is a suggestion by definition and sorts
    last, which is what lets a newer graph's signal 7 be read here without
    being mistaken for a fact.
    """
    return (0 if _signal_of(name) is not None else 1, -_weight_of(name), name)


def _weight_of(name: str) -> float:
    """The weight behind a method name; zero for one this build does not know."""
    signal = _signal_of(name)
    return SIGNAL_WEIGHTS[signal] if signal is not None else 0.0
