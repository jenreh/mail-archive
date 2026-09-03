"""How central an address is — power iteration in Python, and why not in the store.

FalkorDB has ``algo.pageRank`` and this does not call it. ``CO_ADDRESSED`` is
written **directed with the smaller id first** (see
:class:`~mailarc_analytics.derived.model.CoAddressedPair`, which enforces that
ordering so one unordered pair is one edge), and a PageRank follows arrows. Run
over that edge it would push rank from every alphabetically-earlier address to
every later one and answer with a ranking of the alphabet wearing centrality's
clothes — a number that looks right, that nothing would flag, and that would
change if somebody's address changed domain. So the arrow is dropped here: the
graph this walks is undirected and weighted by the pair count, and R2 is
measured in ``test_derived_centrality.py`` by relabelling the hub so the ids
sort the other way round.

``algo.pageRank`` still runs, on the one edge in this archive that is genuinely
directed — ``REPLIES_TO``, where the arrow means what an arrow means. That call
lives in :mod:`mailarc_analytics.derived.algorithms`.

**Deterministic to the bit, because idempotence is the phase's contract.**
Float addition is not associative, so the iteration order is sorted rather than
whatever a dict happened to hold: the node list is sorted, each node's
neighbours are sorted, and the answer is rounded the way every other score in
this package is. Two rebuilds over an unchanged archive have to write the same
property value, not a value that is close.

The pairs come from A1 and are already in memory when this runs, so the input
costs nothing extra — which is the other half of why this is Python.
"""

import logging
from collections.abc import Mapping, Sequence

from runic.ogm import Session

from mailarc_analytics.derived.model import CoAddressedPair
from mailarc_analytics.derived.writes import set_rows
from mailarc_analytics.queries import catalog
from mailarc_core.archive.model import Address

logger = logging.getLogger(__name__)

DAMPING = 0.85
"""The share of a node's rank that flows along its edges each pass.

PageRank's own constant, and not a setting for the reason
:data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS` is not: it only means
anything together with the iteration count and the ceiling below, and an
archive ranked at 0.7 could not be compared with one ranked at 0.85 while both
report a property called ``rank``.
"""

ITERATIONS = 20
"""Passes of power iteration. The twenty R4 costed the ceiling against.

Enough for the ranking to have settled on every archive measured here — the
*order* stabilises long before the values do, and the order is what a page
shows. A fixed count rather than a convergence test, because a threshold makes
the number of passes depend on the data and this has to answer identically
twice.
"""

RANK_VERSION = "1"
"""What :attr:`~mailarc_core.archive.model.Address.rank_version` is stamped with.

Bumped when the formula changes, so an address still carrying the old string is
one this rebuild did not reach rather than one that scored the same.
:data:`~mailarc_analytics.derived.importance.IMPORTANCE_VERSION`'s argument, on
the other ground-truth node.
"""

_PRECISION = 12
"""Decimal places a rank keeps.

More than the six a topic's score keeps, because a rank is a probability over
every address in the archive: at a hundred thousand of them the mean is 1e-5,
and six places would round most of the population to the same number.
"""


def weighted_pagerank(
    pairs: Sequence[CoAddressedPair],
    *,
    damping: float = DAMPING,
    iterations: int = ITERATIONS,
    max_edges: int = 0,
) -> dict[str, float]:
    """Rank every address in *pairs*, keyed by id and sorted by it. No I/O.

    The graph is the co-addressing relation read **undirected**: each pair
    contributes its count to both endpoints, so an address's outgoing weight is
    the total mail it shares with anybody and its incoming rank is the share of
    that mail coming from people who are themselves central. That is what "who
    is at the centre of this archive's correspondence" means, and it is not what
    a PageRank over the stored arrow would answer.

    *max_edges* is R4's ceiling and ``0`` means none, the spelling
    :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages` uses.
    The pairs arrive in canonical order, so the prefix a ceiling keeps is
    reproducible; what it drops is logged, because an address that went unranked
    for want of budget and an address nobody writes to are the same ``None`` on
    the node otherwise.

    Every node in this graph has at least one edge — it came out of a pair — so
    there are no dangling nodes to redistribute, and the ranks sum to one for as
    long as the arithmetic is exact.
    """
    walked = _within(pairs, max_edges)
    weights, total = _adjacency(walked)
    if not weights:
        return {}

    nodes = sorted(weights)
    share = 1.0 / len(nodes)
    ranks = dict.fromkeys(nodes, share)
    leak = (1.0 - damping) * share
    for _ in range(max(0, iterations)):
        ranks = {
            node: leak
            + damping
            * sum(
                ranks[other] * weight / total[other]
                for other, weight in sorted(weights[node].items())
            )
            for node in nodes
        }
    found = {node: round(ranks[node], _PRECISION) for node in nodes}
    logger.info(
        "Ranked %d addresses over %d co-addressing pairs in %d passes",
        len(found),
        len(walked),
        iterations,
    )
    return found


def write_address_ranks(
    session: Session, ranks: Mapping[str, float], *, version: str = RANK_VERSION
) -> int:
    """Set ``Address.rank`` on every ranked address; return what the store touched.

    A ``MATCH`` and a ``SET`` and never a ``MERGE``: an id that is not in the
    archive any more writes nothing, where merging it would invent an
    ``Address`` node carrying a centrality and no mail. The clearing half is
    ``CLEAR_ADDRESS_RANKS``, run by the rebuild's delete stage, so an address
    that dropped out of the archive between two runs does not keep the rank it
    had when it was still in it.
    """
    written = set_rows(
        session,
        catalog.WRITE_ADDRESS_RANKS,
        ({"id": address, "rank": rank} for address, rank in ranks.items()),
        model=Address,
        params={"version": version},
    )
    logger.info("Wrote %d address ranks at version %s", written, version)
    return written


def _within(
    pairs: Sequence[CoAddressedPair], max_edges: int
) -> Sequence[CoAddressedPair]:
    """The pairs this pass will actually walk, and a warning for the rest."""
    if max_edges <= 0 or len(pairs) <= max_edges:
        return pairs
    logger.warning(
        "Centrality ceiling of %d pairs reached; %d of %d co-addressing pairs "
        "were not walked and their addresses go unranked",
        max_edges,
        len(pairs) - max_edges,
        len(pairs),
    )
    return pairs[:max_edges]


def _adjacency(
    pairs: Sequence[CoAddressedPair],
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """The undirected weighted graph, and each node's total incident weight.

    A pair repeated in the input adds to the weight it already has rather than
    replacing it. A1 produces each unordered pair exactly once, so that cannot
    happen from a rebuild; it can from a hand-built finding, and summing is the
    reading that keeps the graph symmetric either way.
    """
    weights: dict[str, dict[str, float]] = {}
    total: dict[str, float] = {}
    for pair in pairs:
        if pair.left == pair.right:
            continue
        weight = float(max(pair.count, 1))
        for one, other in ((pair.left, pair.right), (pair.right, pair.left)):
            edges = weights.setdefault(one, {})
            edges[other] = edges.get(other, 0.0) + weight
            total[one] = total.get(one, 0.0) + weight
    return weights, total
