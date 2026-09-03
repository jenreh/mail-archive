"""How much a message probably matters — arithmetic over headers, and why.

Eight terms, a weighted sum, clamped to ``0..1``, and **every term names
itself**. That is the whole design and the reason it is not a model: a ranking
a user cannot argue with is a ranking a user cannot correct, and "this mail is
important because you answered it, it went to three people and it carries an
attachment" is a sentence somebody can disagree with in a way that changes what
they do next.

The vocabulary is fixed. Seven of the eight strings are constants in this
module; the eighth carries the reply count because the count *is* the argument.
Nothing here may invent a word — a page renders these as chips and an MCP tool
hands them to a model, so a reason that appeared once and never again would be
noise in both.

**One reason is honest rather than complete.** ``"flagged by the provider"``
fires on a ``Label`` named ``IMPORTANT`` or ``STARRED``, and only Gmail brings
those into the graph: it passes its label names through unchanged
(:func:`mailarc_google.source.mapping.label_info`), while IMAP's ``\\Flagged``
and Microsoft 365's flags are not imported at all. So on an IMAP or M365
archive this reason simply never fires. Importing those flags is a provider
change outside this phase's scope; the vocabulary stays truthful in the
meantime rather than pretending the signal is there.

**``importance`` is a property on a ground-truth node**, which is R10 and is
:attr:`~mailarc_core.archive.model.Message.embedding`'s arrangement exactly: the
import never writes it, a rebuild nulls it and computes it again, and the
delete guards never see it because it is neither a node nor an edge.

Pure except for the writer at the bottom. :func:`score_messages` takes what the
earlier stages produced and returns value objects; nothing in it opens a
session.
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType

from runic.ogm import Session

from mailarc_analytics.derived.findings import ImportanceScore, MessageSignals
from mailarc_analytics.derived.model import MessageFacts
from mailarc_analytics.derived.writes import set_rows
from mailarc_analytics.queries import catalog
from mailarc_core.archive.model import Message

logger = logging.getLogger(__name__)

IMPORTANCE_VERSION = "1"
"""What :attr:`~mailarc_core.archive.model.Message.importance_version` is
stamped with.

Bumped whenever a weight, a threshold or a word below changes, so a message
still carrying the old string is one this rebuild did not reach rather than one
that scored the same. One value for the whole run, bound as the statement's
``$version`` rather than as a key on every row — the shape ``$model`` has on
the embedding write.
"""

REPLIED_BY_YOU = "replied by you"
REPLIES = "replies"
"""The one reason that is a *shape* rather than a word: it renders as
``"3 replies"``, because the count is the argument."""

CENTRAL_SENDER = "sent by a central correspondent"
ADDRESSED_DIRECTLY = "addressed directly"
FEW_RECIPIENTS = "few recipients"
HAS_ATTACHMENTS = "has attachments"
LOOKS_AUTOMATED = "looks automated"
FLAGGED = "flagged by the provider"

IMPORTANCE_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        REPLIED_BY_YOU: 0.30,
        FLAGGED: 0.25,
        REPLIES: 0.20,
        CENTRAL_SENDER: 0.15,
        ADDRESSED_DIRECTLY: 0.15,
        FEW_RECIPIENTS: 0.10,
        HAS_ATTACHMENTS: 0.10,
        LOOKS_AUTOMATED: -0.40,
    }
)
"""What each reason is worth, positive ones summing past one on purpose.

A calibration and not configuration, for
:data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS`' reason: the eight
numbers only mean anything relative to each other and to the clamp, and eight
settings would let a well-meant edit make two archives incomparable while both
report a property called ``importance``.

The positives add up to 1.25, so a message carrying every signal is clamped
rather than scaled — rescaling would make every other message's number depend
on whether such a message happens to exist in the archive. ``looks automated``
is the only negative and is heavy enough to take the three ordinary reasons off
a form letter, which is what it is for.
"""

REPLY_SATURATION = 3
"""Answers at which the reply term is worth its full weight.

Saturating rather than linear for :func:`~mailarc_analytics.derived.templates.automation_score`'s
reason: a thread of forty must not outweigh every other signal put together.
"""

CENTRAL_SENDER_SHARE = 0.5
"""Share of the archive's top rank a sender needs before the reason fires.

Relative, because a PageRank over a hundred thousand addresses puts every value
near zero and an absolute threshold would fire for nobody.
"""

AUTOMATED_SHARE = 0.25
"""Automation score at which a template starts arguing against its instances.

Low, because ``automation_score`` multiplies three factors that can each veto,
so a quarter is already a text somebody sends on a schedule.
"""

FEW_RECIPIENTS_MAX = 3
"""Addresses on the To line up to which the mail was written to *people*."""

PROVIDER_FLAGS = frozenset({"IMPORTANT", "STARRED"})
"""The two label names that mean the provider's own user marked this.

Gmail's, and today nobody else's. See this module's docstring.
"""


def score_messages(
    facts: Sequence[MessageFacts],
    signals: Mapping[str, MessageSignals],
    *,
    sender_rank: Mapping[str, float],
    reply_rank: Mapping[str, float],
    template_scores: Mapping[str, float],
    own: Collection[str],
) -> tuple[ImportanceScore, ...]:
    """Score every message in *facts*, with the reasons that produced it.

    *signals* is what :func:`~mailarc_analytics.derived.reader.read_signals`
    read, keyed by id; *sender_rank* is ``Address.rank`` from ``CENTRALITY``;
    *reply_rank* is ``algo.pageRank`` over ``REPLIES_TO``; *template_scores* is
    ``automation_score`` per message from A3; *own* is the archive's own
    account addresses.

    Every message gets a score, including the ones nothing can be said about:
    ``None`` on the node means "never scored" and a run that did score a
    message must not leave it looking like a run that did not. A message no
    signal row came back for is scored on its facts alone — the two reads are
    paged separately and can disagree at a ceiling, and a scorer that raised on
    the difference would turn a bounded read into a failed rebuild.

    **The reply centrality strengthens a reason and never invents one.** A
    message with no answers has a rank in the reply graph only because
    something downstream of it does, and "0 replies" is not a reason for
    anything. So the term fires on the count and is then worth the larger of
    what the count and the centrality say.

    Both ranks are normalised against the *archive's own* top rather than
    against one, because a PageRank over a hundred thousand nodes puts every
    value near zero.
    """
    mine = frozenset(one.strip().lower() for one in own if one)
    top_sender = max(sender_rank.values(), default=0.0)
    top_reply = max(reply_rank.values(), default=0.0)
    found = tuple(
        _score(
            one,
            signals.get(one.id),
            mine=mine,
            sender=_share(sender_rank.get(one.sender.strip().lower()), top_sender),
            replies=_share(reply_rank.get(one.id), top_reply),
            automation=float(template_scores.get(one.id) or 0.0),
        )
        for one in facts
    )
    logger.info(
        "Scored %d messages; %d carry at least one reason",
        len(found),
        sum(1 for one in found if one.reasons),
    )
    return found


def write_importance(
    session: Session,
    scores: Sequence[ImportanceScore],
    *,
    version: str = IMPORTANCE_VERSION,
) -> int:
    """Set the three importance properties; return what the store touched.

    A ``MATCH`` and a ``SET`` and never a ``MERGE``, for
    :data:`~mailarc_analytics.queries.catalog.WRITE_IMPORTANCE`'s reason: a row
    naming a message that is not there writes nothing, where merging it would
    invent an empty ``Message`` carrying a score and no mail — something no
    import could ever reconcile and every listing would happily return.
    """
    written = set_rows(
        session,
        catalog.WRITE_IMPORTANCE,
        (
            {
                "id": one.message_id,
                "importance": one.score,
                "reasons": list(one.reasons),
            }
            for one in scores
        ),
        model=Message,
        params={"version": version},
    )
    logger.info("Wrote %d importance scores at version %s", written, version)
    return written


def _score(
    facts: MessageFacts,
    signals: MessageSignals | None,
    *,
    mine: frozenset[str],
    sender: float,
    replies: float,
    automation: float,
) -> ImportanceScore:
    """One message's weighted sum, clamped, with its reasons sorted.

    Clamped and not rescaled at either end. Rescaling would make every other
    message's number depend on whether an archive happens to hold one carrying
    every signal, and a page comparing two archives would be comparing two
    different scales.
    """
    said = signals or MessageSignals(id=facts.id)
    terms: list[tuple[str, str, float]] = []

    if mine.intersection(said.replied_by):
        terms.append((REPLIED_BY_YOU, REPLIED_BY_YOU, 1.0))
    if said.reply_count > 0:
        counted = min(1.0, said.reply_count / REPLY_SATURATION)
        terms.append((REPLIES, _replies(said.reply_count), max(counted, replies)))
    if sender >= CENTRAL_SENDER_SHARE:
        terms.append((CENTRAL_SENDER, CENTRAL_SENDER, sender))
    if mine.intersection(said.sent_to):
        terms.append((ADDRESSED_DIRECTLY, ADDRESSED_DIRECTLY, 1.0))
    if 0 < len(said.sent_to) <= FEW_RECIPIENTS_MAX:
        terms.append((FEW_RECIPIENTS, FEW_RECIPIENTS, 1.0))
    if said.has_attachments:
        terms.append((HAS_ATTACHMENTS, HAS_ATTACHMENTS, 1.0))
    if {name.upper() for name in said.label_names} & PROVIDER_FLAGS:
        terms.append((FLAGGED, FLAGGED, 1.0))
    if automation >= AUTOMATED_SHARE:
        terms.append((LOOKS_AUTOMATED, LOOKS_AUTOMATED, min(1.0, automation)))

    total = sum(IMPORTANCE_WEIGHTS[key] * strength for key, _, strength in terms)
    return ImportanceScore(
        message_id=facts.id,
        score=round(min(1.0, max(0.0, total)), 6),
        reasons=tuple(sorted(reason for _, reason, _ in terms)),
        version=IMPORTANCE_VERSION,
    )


def _replies(count: int) -> str:
    """The one reason that carries a number — ``"1 reply"``, ``"3 replies"``.

    Written out rather than templated at the call site, because the plural is
    read by a person and a "1 replies" in a chip is a bug the reader sees
    before any of the arithmetic.
    """
    return "1 reply" if count == 1 else f"{count} replies"


def _share(value: float | None, top: float) -> float:
    """A rank as a share of the archive's highest, in ``0..1``.

    Zero when nothing was ranked at all, which is what an archive whose
    centrality stage was skipped looks like — and the reason the two thresholds
    above are shares rather than absolute numbers.
    """
    if not top or value is None:
        return 0.0
    return max(0.0, min(1.0, value / top))
