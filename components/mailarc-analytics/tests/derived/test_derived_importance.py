"""How much a message matters, and the eight words it is allowed to say why.

The reasons are the point. A score with no vocabulary behind it is a ranking a
user cannot argue with, and the only reason this is arithmetic over headers
rather than a model is that every term can be named. So every test below fires
exactly one reason and checks the word it produced, and the vocabulary itself
is pinned as a set — a ninth string appearing in it is a change somebody has to
make on purpose.

``"flagged by the provider"`` is the one that is honest rather than complete:
only Gmail brings ``IMPORTANT`` and ``STARRED`` into the graph as ``Label``
names, so on an IMAP or M365 archive it simply never fires. That is stated in
the module under test and asserted here.
"""

import corpus
import pytest

from mailarc_analytics import ImportanceScore, MessageFacts, MessageSignals
from mailarc_analytics.derived.importance import (
    ADDRESSED_DIRECTLY,
    CENTRAL_SENDER,
    FEW_RECIPIENTS,
    FLAGGED,
    HAS_ATTACHMENTS,
    IMPORTANCE_VERSION,
    IMPORTANCE_WEIGHTS,
    LOOKS_AUTOMATED,
    REPLIED_BY_YOU,
    score_messages,
)

CONFIG = corpus.calibrated_config()

OWN = "jens@nordlicht.example"
ANNA = "anna.meier@kunde.example"

VOCABULARY = frozenset(
    {
        REPLIED_BY_YOU,
        CENTRAL_SENDER,
        ADDRESSED_DIRECTLY,
        FEW_RECIPIENTS,
        HAS_ATTACHMENTS,
        LOOKS_AUTOMATED,
        FLAGGED,
    }
)
"""The seven fixed strings. The eighth reason carries a count and is checked
by shape rather than by identity."""


def _scored(
    signals: MessageSignals | None = None,
    *,
    sender: str = ANNA,
    sender_rank: dict[str, float] | None = None,
    reply_rank: dict[str, float] | None = None,
    template_scores: dict[str, float] | None = None,
) -> ImportanceScore:
    """One message scored on its own, so a reason cannot borrow another's."""
    facts = (MessageFacts(id="m1", sender=sender),)
    found = score_messages(
        facts,
        {"m1": signals} if signals else {},
        sender_rank=sender_rank or {},
        reply_rank=reply_rank or {},
        template_scores=template_scores or {},
        own=frozenset({OWN}),
    )
    return found[0]


def _signals(
    *,
    sent_to: tuple[str, ...] = (),
    reply_count: int = 0,
    replied_by: tuple[str, ...] = (),
    label_names: tuple[str, ...] = (),
    has_attachments: bool = False,
) -> MessageSignals:
    """One signals row, spelled out so the type checker sees every field."""
    return MessageSignals(
        id="m1",
        sent_to=sent_to,
        reply_count=reply_count,
        replied_by=replied_by,
        label_names=label_names,
        has_attachments=has_attachments,
    )


def test_a_message_nothing_can_be_said_about_scores_zero_with_no_reasons() -> None:
    """The floor, and the shape a page renders as an empty bar.

    Every message is scored, including the ones that carry no signal at all —
    ``None`` on the node means "never scored" and must not be produced by a
    run that did score it.
    """
    scored = _scored()

    assert (scored.score, scored.reasons) == (0.0, ())


def test_an_answer_from_the_archive_s_own_address_is_the_strongest_reason() -> None:
    """ "Replied by you" is the one signal no provider flag can fake."""
    scored = _scored(_signals(reply_count=1, replied_by=(OWN,)))

    assert REPLIED_BY_YOU in scored.reasons


def test_the_reply_count_is_a_reason_that_says_the_number() -> None:
    """ "3 replies" — the count is in the sentence, because the count is the
    argument."""
    scored = _scored(_signals(reply_count=3, replied_by=(ANNA,)))

    assert "3 replies" in scored.reasons


def test_one_answer_is_one_reply_and_not_one_replies() -> None:
    """The vocabulary is read by a person, and a plural nobody wrote is a bug
    the reader sees before any of the arithmetic."""
    scored = _scored(_signals(reply_count=1, replied_by=(ANNA,)))

    assert "1 reply" in scored.reasons


def test_a_message_nobody_answered_says_nothing_about_replies() -> None:
    """The reply centrality may strengthen the reason and may never invent it.

    A message with no answers has a reply-graph rank only because something
    downstream of it does, and "0 replies" is not a reason for anything.
    """
    scored = _scored(_signals(), reply_rank={"m1": 1.0})

    assert scored.reasons == ()


def test_a_central_sender_is_a_reason_and_a_peripheral_one_is_not() -> None:
    """The rank is normalised against the archive's own top, not against one.

    A PageRank over a hundred thousand addresses puts every value near zero, so
    an absolute threshold would fire for nobody. The comparison is with the
    most central address this rebuild found.
    """
    central = _scored(sender_rank={ANNA: 0.9, OWN: 0.9})
    peripheral = _scored(sender_rank={ANNA: 0.01, OWN: 0.9})

    assert CENTRAL_SENDER in central.reasons
    assert CENTRAL_SENDER not in peripheral.reasons


def test_being_on_the_to_line_is_a_reason_and_being_copied_in_is_not() -> None:
    """ "Addressed directly" is a claim about ``SENT_TO`` alone.

    ``MESSAGE_SIGNALS`` collects that edge and no other, so a message the
    archive's owner was only ever Cc'd on arrives with an empty ``sent_to`` —
    which is the case below, and the reason folding Cc in would make this true
    of every mail sent to a department.
    """
    addressed = _scored(_signals(sent_to=(OWN,)))
    copied = _scored(_signals(sent_to=(ANNA,)))

    assert ADDRESSED_DIRECTLY in addressed.reasons
    assert ADDRESSED_DIRECTLY not in copied.reasons


def test_a_short_to_line_is_a_reason_and_a_distribution_list_is_not() -> None:
    """Mail written to three people is likelier to want an answer than mail
    written to thirty."""
    few = _scored(_signals(sent_to=("a@x.example", "b@x.example")))
    many = _scored(
        _signals(sent_to=tuple(f"p{index}@x.example" for index in range(30)))
    )

    assert FEW_RECIPIENTS in few.reasons
    assert FEW_RECIPIENTS not in many.reasons


def test_an_attachment_is_a_reason() -> None:
    assert HAS_ATTACHMENTS in _scored(_signals(has_attachments=True)).reasons


@pytest.mark.parametrize("flag", ["IMPORTANT", "STARRED"])
def test_the_provider_s_own_flag_is_a_reason(flag: str) -> None:
    """Gmail passes its label names through unchanged, so these two arrive as
    ``Label`` names (``mailarc_google.source.mapping.label_info``)."""
    assert FLAGGED in _scored(_signals(label_names=(flag,))).reasons


def test_a_label_somebody_named_themselves_is_not_a_provider_flag() -> None:
    """A user label called "Kunden" is filing, not a flag."""
    assert FLAGGED not in _scored(_signals(label_names=("Kunden",))).reasons


def test_a_template_pulls_the_score_down_and_says_so() -> None:
    """The one negative term. A form letter that ticks three boxes is still a
    form letter, and the reason has to be visible next to the ones that
    raised it."""
    signals = _signals(sent_to=(OWN,), has_attachments=True)

    plain = _scored(signals)
    automated = _scored(signals, template_scores={"m1": 0.9})

    assert automated.score < plain.score
    assert LOOKS_AUTOMATED in automated.reasons


def test_a_template_nobody_would_call_automated_does_not_fire() -> None:
    """A2 gives every template a score; only a high one is an argument."""
    assert (
        LOOKS_AUTOMATED not in _scored(_signals(), template_scores={"m1": 0.05}).reasons
    )


def test_the_score_is_clamped_to_the_range_a_bar_can_render() -> None:
    """Every reason at once sums past one, and a template can push past zero.

    Both ends are clamped rather than rescaled, because rescaling would make
    the number mean something different on an archive that happened to hold a
    message with every signal.
    """
    everything = _scored(
        _signals(
            sent_to=(OWN,),
            reply_count=9,
            replied_by=(OWN,),
            label_names=("STARRED",),
            has_attachments=True,
        ),
        sender_rank={ANNA: 1.0},
    )
    nothing_but_a_template = _scored(_signals(), template_scores={"m1": 1.0})

    assert everything.score == 1.0
    assert nothing_but_a_template.score == 0.0


def test_the_reasons_come_back_sorted() -> None:
    """Two rebuilds have to write the same list, and a set has no order."""
    scored = _scored(
        _signals(sent_to=(OWN,), has_attachments=True, label_names=("IMPORTANT",))
    )

    assert list(scored.reasons) == sorted(scored.reasons)


def test_every_score_is_stamped_with_the_version_that_produced_it() -> None:
    """A changed formula leaves a visible mark, so a message carrying the old
    string is one this rebuild did not reach rather than one that agreed."""
    assert _scored().version == IMPORTANCE_VERSION == "1"


def test_the_vocabulary_is_fixed_and_every_weight_belongs_to_one_of_its_words() -> None:
    """A reason nothing weighs, or a weight no reason names, is a silent term.

    The calibration lives beside the vocabulary for
    :data:`~mailarc_analytics.derived.model.SIGNAL_WEIGHTS`' reason: the eight
    numbers only mean anything relative to each other and to the clamp, and
    exposing them as settings would let a well-meant edit make two archives
    incomparable while both report a property called ``importance``.
    """
    named = set(IMPORTANCE_WEIGHTS)

    assert VOCABULARY < named
    assert len(named) == 8, "seven fixed words plus the one that carries a count"


def test_a_message_no_signal_row_came_back_for_is_still_scored() -> None:
    """A missing row costs a message its reasons, never the run.

    The signals read is paged and the facts read is paged separately, so the
    two prefixes can disagree at a ceiling — and a scorer that raised on the
    difference would turn a bounded read into a failed rebuild.
    """
    found = score_messages(
        (MessageFacts(id="m1"), MessageFacts(id="m2")),
        {"m1": _signals(has_attachments=True)},
        sender_rank={},
        reply_rank={},
        template_scores={},
        own=frozenset({OWN}),
    )

    assert [one.message_id for one in found] == ["m1", "m2"]
    assert found[1].reasons == ()
