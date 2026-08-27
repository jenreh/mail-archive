"""What the report statements answer with, and what the cross-check made of it.

Value objects and nothing else — no session, no statement, no I/O — which is
the same split :mod:`mailarc_analytics.derived.model` makes and for the same
reason. :class:`~mailarc_analytics.queries.reports.AnalyticsReader` hands these
up to a page that outlives the session that read them, and a page holding a
live runic entity would be holding a closed driver by the time it rendered.

Every row carries exactly one catalogue statement's columns under the names
that statement gives them, so a renamed column fails in the decoder next to the
statement rather than three layers up in a template. Nothing is left nullable
that the graph can answer for: a missing ``r.count`` decodes to zero and a
missing timestamp to ``None``, because a report renders a state and never
raises.

:class:`CoAddressedAgreement` is the odd one out and the reason this file is
worth reading. It is not a row but a verdict — A1's definition and A1's
materialisation run against each other — and the whole argument about what
"agree" can honestly mean when both sides were truncated lives on
:meth:`CoAddressedAgreement.between`.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict

from mailarc_analytics.derived.model import TemplateDirection

logger = logging.getLogger(__name__)


class CoRecipientRow(BaseModel):
    """One pair as :data:`~mailarc_analytics.queries.catalog.CO_RECIPIENTS`
    counts it: straight off ``SENT_TO``/``COPIED_TO``, no derived edge anywhere
    in the walk.

    This is A1's *definition*. Whatever it says is what the archive says, which
    is why it is worth a value object of its own rather than a shape shared
    with :class:`CoAddressedRow` — the two are the same question asked of two
    different things, and a reader comparing them should not be able to lose
    track of which one they are holding.
    """

    model_config = ConfigDict(frozen=True)

    left_id: str
    right_id: str
    """The two addresses, smaller id first — the statement's ``a.id < b.id``
    is what makes one unordered pair appear once."""

    together: int = 0
    """Messages the two were addressed on together."""


class CoAddressedRow(BaseModel):
    """The same pair as the materialised ``CO_ADDRESSED`` edge carries it.

    Two columns the definition cannot answer come with it: an edge accumulates
    a span while a self-join only ever counts rows.
    """

    model_config = ConfigDict(frozen=True)

    left_id: str
    right_id: str
    together: int = 0
    """``r.count`` — what the last rebuild wrote, not what the archive holds
    now. The difference is the whole point of the cross-check."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


class GroupRow(BaseModel):
    """A circle of people that keeps being written to, as A1 wrote it down."""

    model_config = ConfigDict(frozen=True)

    id: str
    """The ``participant_key`` the import hashed — not a name, and not
    durable across a change of membership."""

    size: int = 0
    """Addresses the key was hashed from, sender and Bcc included."""

    message_count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class TopicRow(BaseModel):
    """One topic, split by the signal that drew its edges.

    A topic appears **once per method**, not once:
    :data:`~mailarc_analytics.queries.catalog.TOPIC_BREAKDOWN` groups on
    ``r.method`` on purpose, so a cluster holding messages joined by a ticket
    token and messages joined by nothing stronger than a shared attachment
    comes back as two rows that add up. Folding them together here would throw
    away the one column that tells a fact from a suggestion.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    label: str = ""
    """The most common normalised subject among the members; may be empty."""

    method: str = ""
    """A :class:`~mailarc_analytics.derived.model.TopicSignal` value — a plain
    string, because a graph written by a build that knows one more signal still
    has to decode here."""

    messages: int = 0
    """Messages joined to this topic *by this method*."""


class TemplateRow(BaseModel):
    """A text that gets written again and again, best candidate first."""

    model_config = ConfigDict(frozen=True)

    id: str
    direction: TemplateDirection
    """Which way it travelled. Not a column in the result set —
    :data:`~mailarc_analytics.queries.catalog.TOP_TEMPLATES` filters on it, so
    the statement has no reason to return it — and put back here anyway,
    because sent and received are read separately and then shown next to each
    other, and a row that lost its direction on the way is a row nobody can
    rank honestly."""

    occurrences: int = 0
    automation_score: float = 0.0
    """Frequency times regularity times brevity, each in ``0..1``. Comparable
    within one direction and meaningless across them."""

    sample_text: str = ""
    """The shortest member's cleaned body, as far as the rebuild kept it."""

    first_seen: datetime | None = None
    last_seen: datetime | None = None


class ArchiveTotals(BaseModel):
    """The six counts that say what is in the archive and what was derived
    from it.

    Ground truth and derived layer in one object because the only useful
    reading is the ratio: two hundred templates out of thirty messages is a
    calibration failure, and either number on its own looks fine.
    """

    model_config = ConfigDict(frozen=True)

    messages: int = 0
    """``Message`` nodes a rebuild could read — the ones with a canonical
    id."""

    unidentified: int = 0
    """``Message`` nodes without one, which every read steps over. Non-zero
    means the graph holds something the writer cannot produce."""

    groups: int = 0
    topics: int = 0
    templates: int = 0
    co_addressed: int = 0

    @property
    def derived(self) -> int:
        """Everything a rebuild wrote, added up.

        Zero over a non-empty archive has exactly one meaning — no rebuild has
        run yet — and that is a different sentence from "the analyses found
        nothing", which a reader would otherwise have to guess between.
        """
        return self.groups + self.topics + self.templates + self.co_addressed


class ArchivedDay(BaseModel):
    """One day of the archive's growth — what arrived, and how much of it.

    Two series in one row, because they are read from one statement and drawn
    on one time axis: a page that asked for messages and bytes separately would
    be asking the same aggregation twice and could get two different moments.

    The odd one out among these rows in one way: **a day the archive did
    nothing still gets one.**
    :meth:`~mailarc_analytics.queries.reports.AnalyticsReader.archived_per_day`
    fills the gaps its statement leaves, so a chart draws a flat line over a
    quiet week rather than a hole that reads as missing data. That is why every
    field but the key defaults to zero — a filled day is one constructor call
    and not a second row type.
    """

    model_config = ConfigDict(frozen=True)

    day: str
    """``YYYY-MM-DD``, in **UTC**, exactly as the statement cut it out of
    ``ArchivedFrom.archived_at``.

    A string and not a :class:`~datetime.date`, for the same reason every other
    row here carries the column its statement named: this is what the store
    answered, and it is also what a chart plots along its x-axis. The reader
    parses it to place the day inside a window and hands the key back
    untouched.
    """

    messages: int = 0
    """Copies archived on this day — one per ``ARCHIVED_FROM`` edge, so a mail
    that reached two accounts counts twice. The chart is of what the archive
    did, and importing the same mail into a second account is work it did."""

    bytes: int = 0
    """``Message.size_bytes`` summed over those copies.

    A whole number here although FalkorDB's ``sum()`` answers with a float:
    :func:`~mailarc_analytics.queries.rows.as_int` is what makes it one, and a
    page rendering ``3139.0`` bytes is what it prevents.
    """


class ComparedPair(BaseModel):
    """One unordered pair, with what each side of the cross-check said about it.

    ``None`` on a side means that side did not list the pair at all, which is a
    weaker statement than zero: on a listing that was cut it means "not in the
    top rows", not "never happened". :meth:`CoAddressedAgreement.between` is
    what decides when the difference between the two is provable.
    """

    model_config = ConfigDict(frozen=True)

    left_id: str
    right_id: str
    truth: int | None = None
    """What :data:`~mailarc_analytics.queries.catalog.CO_RECIPIENTS` counted."""

    edge: int | None = None
    """What the ``CO_ADDRESSED`` edge carries."""

    @property
    def heaviest(self) -> int:
        """The louder of the two claims; a silent side counts as zero.

        What a listing of pairs is ranked by: a pair is worth as much
        attention as the largest number anybody puts on it, and a side that
        never named it puts none.
        """
        return max(self.truth or 0, self.edge or 0)


def _heaviest_first(pair: ComparedPair) -> tuple[int, str, str]:
    """Biggest claim first, then by address, so a listing is stable."""
    return (-pair.heaviest, pair.left_id, pair.right_id)


def _canonical(left: str, right: str) -> tuple[str, str]:
    """A pair in the one order both sides can be looked up under.

    Both statements already emit ``a.id < b.id``, so this changes nothing
    today. It is here because ``CO_ADDRESSED`` is undirected in meaning and
    directed in storage: the day a statement is edited and drops that filter,
    the cross-check should report the counts that differ rather than a graph
    full of pairs it thinks only one side has.
    """
    return (left, right) if left <= right else (right, left)


class CoAddressedAgreement(BaseModel):
    """Whether the materialised ``CO_ADDRESSED`` edge still says what the
    ground truth says.

    :data:`~mailarc_analytics.queries.catalog.CO_RECIPIENTS` computes A1 by
    walking ``SENT_TO``/``COPIED_TO``;
    :data:`~mailarc_analytics.queries.catalog.TOP_CO_ADDRESSED` reads the same
    answer off the edge a rebuild wrote. The catalogue's own words: if the two
    ever disagree, the edge is wrong. This object is that comparison, in four
    buckets — pairs both sides count the same, pairs both sides know but count
    differently, pairs only the edge has, pairs only the ground truth has.

    **A disagreement is not automatically a bug, and its direction says which
    kind it is.** Three things make the ground truth show *more* than the edge,
    and none of them is a defect in the write path:

    - no rebuild has run since the last import, so the edge is simply older
      than the archive;
    - the rebuild ran under
      :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages`
      and only ever saw a prefix of the archive;
    - a message addressed to more people than
      ``AnalyticsConfig.co_addressed_max_recipients`` allows contributes no
      pair by design, while the self-join counts it like any other — so every
      archive holding one mail to a big distribution list is *expected* to
      show a truth-side excess.

    A fourth reading looks like a cause and measurably is not. An address that
    stood in both ``To`` and ``Cc`` has two edges from the same message, so the
    self-join's pattern appears to match that message twice and ``count(m)``
    appears to count pattern rows rather than distinct messages. It does not,
    because :data:`~mailarc_analytics.queries.catalog.CO_RECIPIENTS` never
    *references* either relationship: FalkorDB prunes the duplicate
    ``(a, m, b)`` binding before the aggregation ever sees it.

    That safety is a property of the whole statement, not of ``count`` — and
    not of that one sentence about references either, which is why it is
    *measured* rather than reasoned about. ``test_the_three_spellings_of_a1s_
    count_agree_on_this_statement`` runs ``count(m)``, ``count(*)`` and
    ``count(DISTINCT m)`` against the planted corpus and asserts one answer;
    against the statement as it stood before the canonical-id filter,
    ``count(*)`` answered 1 where ``count(m)`` answered 2 on that same corpus.
    Two spellings agreeing here is a fact about this text on this backend, and
    nothing to lean on when editing either.

    What the reference rule does predict, and what
    ``test_a_recipient_in_both_to_and_cc_is_counted_once`` measures, is the
    other direction: bind a relationship *and* use it downstream — say to
    report which header carried the pair — and the second binding comes back,
    ``count(m)`` answers two and only ``count(DISTINCT m)`` still answers one.
    Anyone adding such a column has to switch to ``count(DISTINCT m)`` in the
    same edit, or the truth side starts genuinely over-counting every reply-all
    that copied someone twice.

    Nothing explains the other direction. An edge that counts a pair *higher*
    than the ground truth, or names a pair the ground truth has never seen, is
    claiming something no message supports — :attr:`edge_overstates` is that
    subset, and it is the one a reader should be shown first.
    """

    model_config = ConfigDict(frozen=True)

    limit: int = 0
    """Rows each side was asked for. The only knob that widens what the verdict
    covers."""

    truth_floor: int = 0
    edge_floor: int = 0
    """Where each listing stops proving anything, because it was cut there.

    Zero means that side came back short of *limit* and is therefore
    exhaustive: a pair missing from it really has no rows at all, not merely
    too few to make the top *limit*. See :meth:`between` for what the two
    numbers are used for.
    """

    unjudged: int = 0
    """Pairs one side named and the comparison refused to rule on.

    Purely an artefact of truncation — the other side was cut above this
    pair's count, so its silence proves nothing. Counted rather than dropped
    quietly, because a verdict that covered two of three thousand pairs and a
    verdict that covered all of them read identically otherwise.
    """

    matched: tuple[ComparedPair, ...] = ()
    """In both listings, with the same count."""

    count_mismatches: tuple[ComparedPair, ...] = ()
    """In both listings, with different counts."""

    edge_only: tuple[ComparedPair, ...] = ()
    """Claimed by the edge, and provably absent from the ground truth."""

    truth_only: tuple[ComparedPair, ...] = ()
    """Counted by the ground truth, and provably absent from the edge."""

    duplicate_pairs: int = 0
    """Pairs the edge listing named more than once.

    One pair carrying two physical ``CO_ADDRESSED`` relationships comes back as
    two rows with the same key, and building the lookup as a dict comprehension
    quietly kept whichever arrived last: the check then judged one arbitrary
    count and never saw the other, while both rows ate a slot against
    :attr:`limit` and inflated ``len(counts)`` past the point where
    :func:`_floor` calls a listing full.

    Counted rather than merged silently, and red rather than yellow. The
    writer's undirected ``MERGE`` cannot produce this — measured: it matches a
    pre-existing reversed edge and updates it — so a pair with two edges is a
    write-path bug by the same argument that makes :attr:`edge_overstates` one,
    and this is the only place that could ever have noticed it.
    """

    @property
    def agrees(self) -> bool:
        """Nothing the comparison ruled on differed.

        A property rather than a stored field, for the reason
        :attr:`~mailarc_analytics.derived.model.TopicCluster.message_count` is
        one: a flag written beside the buckets it summarises is a second copy
        of the same truth, and the copy is the half that goes stale.

        Deliberately the strict reading — it is false when the ground truth
        shows more than the edge, which a stale or capped rebuild does on its
        own. The class docstring says which excess means what, and
        :attr:`edge_overstates` is the half that has no innocent reading.
        """
        return not (
            self.count_mismatches
            or self.edge_only
            or self.truth_only
            or self.duplicate_pairs
        )

    @property
    def compared(self) -> int:
        """Pairs the verdict actually covers, agreeing and not."""
        return (
            len(self.matched)
            + len(self.count_mismatches)
            + len(self.edge_only)
            + len(self.truth_only)
        )

    @property
    def edge_overstates(self) -> tuple[ComparedPair, ...]:
        """The disagreements where the edge claims more than any message
        supports.

        Every pair the ground truth has never seen, plus every pair the edge
        counts higher than the self-join does. None of the four innocent
        explanations in the class docstring produces one of these — they all
        make the edge see *less* of the archive, never more. So a non-empty
        answer here is a bug in the A1 write path, which is the whole reason
        this cross-check exists.
        """
        overstated = self.edge_only + tuple(
            one for one in self.count_mismatches if (one.edge or 0) > (one.truth or 0)
        )
        return tuple(sorted(overstated, key=_heaviest_first))

    @classmethod
    def between(
        cls,
        truth: Sequence[CoRecipientRow],
        edge: Sequence[CoAddressedRow],
        *,
        limit: int,
    ) -> Self:
        """Compare the two listings, ruling only where the answer is provable.

        **The caller's contract:** *limit* must be the limit these two listings
        were fetched with. Everything below is derived from it — a listing that
        came back *full* was cut and proves nothing below its smallest entry,
        one that came back short is exhaustive — and neither reading survives
        a *limit* the listings cannot confirm. Pass a bigger one and both
        floors collapse to zero, so every asymmetry between two truncated
        listings is reported as a real disagreement; pass a smaller one and
        genuine disagreements go unjudged. A violated precondition here changes
        the verdict silently, which is the one thing this class must not do, so
        the half that *can* be checked raises instead.

        **Why a set difference is the wrong answer.** Both statements are
        ``ORDER BY together DESC LIMIT $limit``, so on any archive with more
        pairs than *limit* each listing is a top-N under its own ordering. Ties
        at the cut are broken by the store and by nothing either query says, so
        two sides that agree perfectly can still hand back different pairs at
        the bottom. A symmetric difference over two top-N listings would report
        that as a disagreement every single time, and a cross-check that cries
        wolf on a healthy archive is worse than none — the first false alarm is
        the last one anybody reads.

        **What is provable instead.** A listing cut at *limit* rows is still
        complete *above its own last row*: anything it left out sorts below
        that row and therefore counts no more than it. That number is the
        side's floor, and a listing that came back short of *limit* was never
        cut at all, so its floor is zero and its silence about a pair means the
        pair does not exist. Three cases follow, and each is decided against
        the floor of the side that stayed silent:

        - **Both listings name the pair.** Both counts were read, so they are
          simply compared. Truncation cannot touch this case at all.
        - **Only the ground truth names it,** at *t*. The edge listing holds
          every pair it has above :attr:`edge_floor`, so the edge's count is at
          most that floor. If ``t > edge_floor`` the two really do differ; if
          not, the silence proves nothing and the pair goes to
          :attr:`unjudged`.
        - **Only the edge names it,** at *e*. Mirror image, against
          :attr:`truth_floor`.

        Two floors and not one shared one, because the sides truncate
        independently: an exhaustive edge listing settles a pair the ground
        truth's cut listing would have left open, and folding both into a
        single conservative floor would throw that away and under-report real
        disagreements.

        What is left unjudged is a false *negative* — two small counts can
        differ down there unseen — and that is the honest way to be wrong. The
        only thing that shrinks it is a bigger *limit*, which is why the reader
        defaults it far above what a human would read row by row.
        """
        if len(truth) > limit or len(edge) > limit:
            msg = (
                f"between() was given {len(truth)} truth and {len(edge)} edge "
                f"rows for a limit of {limit}; the floors it derives from "
                "`limit` are only sound when it is the limit the two listings "
                "were actually fetched with"
            )
            raise ValueError(msg)
        by_truth = {
            _canonical(row.left_id, row.right_id): row.together for row in truth
        }
        by_edge, duplicated = _keyed(edge)
        truth_floor = _floor([row.together for row in truth], limit)
        edge_floor = _floor([row.together for row in edge], limit)
        judged: list[ComparedPair] = []
        unjudged = 0
        for key in sorted({*by_truth, *by_edge}):
            found = by_truth.get(key)
            stored = by_edge.get(key)
            if _provable(found, stored, truth_floor, edge_floor):
                judged.append(
                    ComparedPair(
                        left_id=key[0], right_id=key[1], truth=found, edge=stored
                    )
                )
            else:
                unjudged += 1
        judged.sort(key=_heaviest_first)
        return cls(
            limit=limit,
            truth_floor=truth_floor,
            edge_floor=edge_floor,
            unjudged=unjudged,
            matched=tuple(one for one in judged if one.truth == one.edge),
            count_mismatches=tuple(
                one
                for one in judged
                if one.truth is not None
                and one.edge is not None
                and one.truth != one.edge
            ),
            edge_only=tuple(one for one in judged if one.truth is None),
            truth_only=tuple(one for one in judged if one.edge is None),
            duplicate_pairs=duplicated,
        )


def _keyed(
    rows: Sequence[CoAddressedRow],
) -> tuple[dict[tuple[str, str], int], int]:
    """The edge listing as a lookup, plus how many pairs it named twice.

    A dict comprehension does the first half and throws the second away. The
    collapse is deliberate here — one count per pair is what the comparison
    needs — but a pair that had two rows is itself the finding, so it is
    counted on the way past instead of disappearing.
    """
    keyed: dict[tuple[str, str], int] = {}
    duplicated = 0
    for row in rows:
        key = _canonical(row.left_id, row.right_id)
        if key in keyed:
            duplicated += 1
            logger.warning("Two CO_ADDRESSED rows for one pair: %s", key)
        keyed[key] = row.together
    return keyed, duplicated


def _provable(
    truth: int | None, edge: int | None, truth_floor: int, edge_floor: int
) -> bool:
    """Whether this pair's two counts can be ruled on at all.

    Both read is always provable. One side silent is provable exactly when the
    count that *was* read stands above the silent side's floor, because only
    then does the silence mean "fewer" rather than "cut off here".
    """
    if truth is not None and edge is not None:
        return True
    if truth is not None:
        return truth > edge_floor
    return edge is not None and edge > truth_floor


def _floor(counts: Sequence[int], limit: int) -> int:
    """The count below which a listing proves nothing about what it omits.

    A listing that came back full was cut, and everything it dropped sorts at
    or below its smallest entry — so that entry is the floor. One that came
    back short was never cut and is exhaustive, which is a floor of zero: a
    pair it does not name has no rows at all.

    The smallest count is read off the whole listing rather than off its last
    row. Both statements order by it, so the two are the same value, and not
    depending on that keeps the rule true of any listing somebody hands in.
    """
    return min(counts) if limit > 0 and len(counts) >= limit else 0
