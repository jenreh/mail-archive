"""Circles of correspondents — the inexact answer, and the rules that pin it.

``Group`` is the exact question: who is repeatedly on the same message, keyed
by the hash the import already computed. A ``Community`` is the question a hash
cannot answer — who forms a circle nobody ever addressed as a set — and label
propagation answers it without a seed, which is R1. Three rules make that
usable rather than merely available, and each of them is a test below:

* the node is keyed by the **digest of its members**, so an unchanged partition
  is the same node however the algorithm numbered it;
* the label is the most common **domain**, with the tie going to the domain of
  the best-ranked member, so it does not depend on set order either;
* a message joins **at most one** circle, the one most of its participants are
  in, ties going to the smaller id.

Pure throughout: label propagation's output is a mapping this module is handed,
so every claim here is arithmetic over hand-built facts and the graph is only
needed for the one thing a graph decides, in
``test_derived_algorithms_local.py``.
"""

from datetime import UTC, datetime

import corpus

from mailarc_analytics import (
    CommunityFacts,
    CommunityFindings,
    MessageFacts,
    build_communities,
    community_id,
)

CONFIG = corpus.calibrated_config()

ANNA = "anna@kunde.example"
THOMAS = "thomas@kunde.example"
LENA = "lena@kunde.example"
OWN = "jens@nordlicht.example"
TEAM = "team@nordlicht.example"
CHIEF = "chef@nordlicht.example"

CUSTOMERS = {ANNA: 0, THOMAS: 0, LENA: 0}
COLLEAGUES = {OWN: 1, TEAM: 1, CHIEF: 1}


def _fact(key: str, *participants: str, sent: datetime | None = None) -> MessageFacts:
    """One message reduced to who was on it and when."""
    return MessageFacts(id=key, participants=tuple(sorted(participants)), sent_at=sent)


def _by_id(found: CommunityFindings) -> dict[str, CommunityFacts]:
    return {one.id: one for one in found.communities}


def test_the_key_is_the_membership_and_not_the_number_lpa_chose() -> None:
    """R1's answer. Two runs that numbered the same circle differently agree.

    Label propagation takes no seed, so the community number is the one thing
    about its output that may legitimately move between two runs over an
    unchanged graph. A node keyed on it would be renamed every time that
    happened — and every ``MEMBER_OF`` and ``IN_CIRCLE`` written last time
    would hang off a circle nothing points at any more.
    """
    facts = (_fact("m1", *CUSTOMERS),)

    first = build_communities(facts, CUSTOMERS, {}, CONFIG)
    again = build_communities(facts, dict.fromkeys(CUSTOMERS, 47), {}, CONFIG)

    assert [one.id for one in first.communities] == [community_id(CUSTOMERS)]
    assert first == again


def test_the_key_does_not_depend_on_the_order_the_members_arrived_in() -> None:
    """A mapping's iteration order is not a fact about the partition."""
    forwards = build_communities((), dict.fromkeys((ANNA, THOMAS, LENA), 0), {}, CONFIG)
    backwards = build_communities(
        (), dict.fromkeys((LENA, THOMAS, ANNA), 0), {}, CONFIG
    )

    assert [one.id for one in forwards.communities] == [
        one.id for one in backwards.communities
    ]


def test_the_label_is_the_most_common_domain_among_the_members() -> None:
    """A name a human recognises, and never a name a model invented."""
    found = build_communities((), CUSTOMERS, {}, CONFIG)

    assert [one.label for one in found.communities] == ["kunde.example"]


def test_a_tied_label_goes_to_the_best_ranked_member_s_domain() -> None:
    """Two domains, three members each — the ranking breaks the tie.

    Counting alone would leave the answer to whichever domain the iteration
    reached first, which is a dict order and not a fact. The rank is the number
    ``CENTRALITY`` has already written, which is why the stage order puts it
    first.
    """
    members = dict.fromkeys((ANNA, THOMAS, OWN, TEAM), 0)
    ranks = {ANNA: 0.1, THOMAS: 0.1, OWN: 0.9, TEAM: 0.1}

    found = build_communities((), members, ranks, CONFIG)

    assert [one.label for one in found.communities] == ["nordlicht.example"]


def test_a_circle_under_the_minimum_size_is_not_a_circle() -> None:
    """Two people who write to each other are a correspondence, not a circle.

    Every archive holds thousands of those, and a node per pair would bury the
    finding under itself.
    """
    found = build_communities((), {ANNA: 0, THOMAS: 0}, {}, CONFIG)

    assert found.communities == ()
    assert CONFIG.community_min_size == 3


def test_a_message_joins_the_circle_most_of_its_people_are_in() -> None:
    """``IN_CIRCLE.score`` is the share of the participants, on the edge.

    The same circle holds a mail everybody on it is a member of and a mail with
    one member on it, and the two are not the same statement — so the number
    belongs on the edge and the threshold decides which of them is a
    membership at all.
    """
    facts = (_fact("m1", ANNA, THOMAS, LENA), _fact("m2", ANNA, OWN, TEAM, CHIEF))

    found = build_communities(facts, CUSTOMERS | COLLEAGUES, {}, CONFIG)

    circles = _by_id(found)
    assert circles[community_id(CUSTOMERS)].messages == {"m1": 1.0}
    assert circles[community_id(COLLEAGUES)].messages == {"m2": 0.75}


def test_a_message_below_the_share_threshold_joins_nothing() -> None:
    """One member and three strangers is not mail circulating in a circle."""
    facts = (_fact("m1", ANNA, "x@far.example", "y@far.example", "z@far.example"),)

    found = build_communities(facts, CUSTOMERS, {}, CONFIG)

    assert [one.messages for one in found.communities] == [{}]
    assert CONFIG.circle_min_share == 0.5


def test_a_message_belongs_to_at_most_one_circle() -> None:
    """ "Which circle is this mail in" has one answer or none.

    Both circles clear the threshold here — which at the default half is only
    possible in an exact tie, so the share is lowered for this one claim — and
    the larger of the two has to win outright rather than the message joining
    both.
    """
    generous = CONFIG.model_copy(update={"circle_min_share": 0.3})
    facts = (_fact("m1", ANNA, THOMAS, LENA, OWN, TEAM),)

    found = build_communities(facts, CUSTOMERS | COLLEAGUES, {}, generous)

    placed = {one.id: one.messages for one in found.communities}
    assert placed[community_id(CUSTOMERS)] == {"m1": 0.6}
    assert placed[community_id(COLLEAGUES)] == {}


def test_an_exact_tie_between_two_circles_goes_to_the_smaller_id() -> None:
    """Deterministic where the data itself does not decide.

    Both circles hold exactly half the participants, so the share is equal and
    something other than the data has to break it. The smaller community id is
    the only tie-break that survives a rebuild, because the id is a digest of
    the membership and the membership has not changed.
    """
    facts = (_fact("m1", ANNA, THOMAS, LENA, OWN, TEAM, CHIEF),)

    found = build_communities(facts, CUSTOMERS | COLLEAGUES, {}, CONFIG)

    placed = {one.id: one.messages for one in found.communities if one.messages}
    assert list(placed) == [min(community_id(CUSTOMERS), community_id(COLLEAGUES))]


def test_the_member_rank_is_carried_onto_the_edge() -> None:
    """``MEMBER_OF.rank`` so a subgraph read can size a node without a hop.

    A member the centrality stage never ranked gets a zero rather than a
    missing key: the edge property is declared nullable, but a rebuild that
    wrote nulls for half its members would be reporting a stage that did not
    run as a graph that has no hubs.
    """
    found = build_communities((), CUSTOMERS, {ANNA: 0.4}, CONFIG)

    assert found.communities[0].members == {ANNA: 0.4, LENA: 0.0, THOMAS: 0.0}


def test_the_circle_is_dated_by_the_mail_that_circulates_in_it() -> None:
    """``first_seen``/``last_seen`` come off the messages, not off the members.

    An address has no date; the mail does, and a circle with no mail in it has
    neither end rather than an invented one.
    """
    early = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
    late = datetime(2026, 2, 10, 16, 0, tzinfo=UTC)
    facts = (
        _fact("m1", ANNA, THOMAS, LENA, sent=early),
        _fact("m2", ANNA, THOMAS, LENA, sent=late),
    )

    found = build_communities(facts, CUSTOMERS, {}, CONFIG)

    assert (found.communities[0].first_seen, found.communities[0].last_seen) == (
        early,
        late,
    )


def test_the_counts_are_read_off_the_mappings() -> None:
    """``size`` and ``message_count`` can never disagree with the edges written."""
    facts = (_fact("m1", ANNA, THOMAS, LENA),)

    circle = build_communities(facts, CUSTOMERS, {}, CONFIG).communities[0]

    assert (circle.size, circle.message_count) == (3, 1)


def test_what_the_guard_stepped_over_is_carried_through() -> None:
    """A rebuild that skipped the call must not look like one that found none.

    ``algo.labelPropagation`` throws on a graph with no ``CO_ADDRESSED`` edge,
    which is exactly the state of an archive whose first rebuild has not run
    A1 yet. The empty partition that follows is indistinguishable from an
    archive of hermits unless the number travels with it.
    """
    found = build_communities((), {}, {}, CONFIG, skipped=1)

    assert (found.communities, found.skipped) == ((), 1)


def test_the_circles_come_back_in_a_stable_order() -> None:
    """Sorted by id, so the rows the writer builds are stable too."""
    found = build_communities((), CUSTOMERS | COLLEAGUES, {}, CONFIG)

    assert [one.id for one in found.communities] == sorted(
        one.id for one in found.communities
    )


def test_a_message_with_no_participants_at_all_joins_nothing() -> None:
    """A share over an empty set is not zero, it is not a question."""
    found = build_communities((_fact("m1"),), CUSTOMERS, {}, CONFIG)

    assert [one.messages for one in found.communities] == [{}]


def test_a_circle_of_addresses_with_no_domain_has_no_label() -> None:
    """An address the parser could not split is a name nobody recognises.

    The empty label is what a page renders as "unnamed circle"; inventing one
    out of the id would put a fragment of somebody's address on a node.
    """
    found = build_communities((), dict.fromkeys(("one", "two", "three"), 0), {}, CONFIG)

    assert [one.label for one in found.communities] == [""]
