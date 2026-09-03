"""What a topic is about, in its own members' words — TF-IDF over the topics.

Over the *topics* and not over the messages, which is the whole design. A term
every topic uses — "rechnung", "meeting", "hallo" — carries no information about
which piece of work this is, and inverse document frequency computed across
topics is what removes it. A stop list only removes the words that are common
in the *language*; the archive's own boilerplate is different in every archive
and cannot be listed in advance.

Every test here is arithmetic over hand-written texts. The keywords of the
planted corpus are a property of German business mail rather than of this
function, and pinning them would make the corpus the specification.
"""

import corpus
import pytest

from mailarc_analytics import TopicCluster, TopicMember
from mailarc_analytics.derived.keywords import (
    STOPWORDS,
    keyword_members,
    topic_keywords,
)

CONFIG = corpus.calibrated_config()


def _topic(key: str, *members: str) -> TopicCluster:
    return TopicCluster(
        id=key,
        members=tuple(
            TopicMember(message_id=one, score=1.0, method="ref") for one in members
        ),
    )


def test_a_term_only_one_topic_uses_beats_one_they_all_use() -> None:
    """The finding TF-IDF exists for, on the smallest archive that has one.

    "rechnung" appears in every topic and says nothing about which of them a
    message belongs to; "datenmigration" appears in one. A plain term count
    answers with the first, which is the failing case.
    """
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"), _topic("t3", "m3"))
    texts = {
        "m1": "rechnung rechnung datenmigration",
        "m2": "rechnung mahnung",
        "m3": "rechnung lieferschein",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert found["t1"][0] == "datenmigration"


def test_stop_words_never_reach_the_ranking() -> None:
    """The language's own filler, in both languages this archive speaks."""
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {
        "m1": "und der die das with that from angebot",
        "m2": "und der die das with that from mahnung",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert set(found["t1"]) & STOPWORDS == set()
    assert found["t1"] == ("angebot",)


def test_ticket_tokens_and_numbers_are_not_keywords() -> None:
    """A2 already joined the topic on the ticket; repeating it says nothing.

    ``NORD-42`` is the reason these messages are one topic, so as a keyword it
    is the label of the finding rather than a description of it — and the
    fragment "nord" that a naive letter tokeniser leaves behind is worse,
    because it reads like a word.
    """
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {
        "m1": "NORD-42 #4711 2026 angebot datenmigration",
        "m2": "mahnung lieferschein",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert "nord" not in found["t1"]
    assert set(found["t1"]) == {"angebot", "datenmigration"}


def test_a_word_of_two_letters_is_not_a_keyword() -> None:
    """Three letters, because "zu", "am", "an" outnumber everything else."""
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {"m1": "zu am an abc", "m2": "mahnung"}

    assert topic_keywords(clusters, texts, CONFIG)["t1"] == ("abc",)


def test_a_topic_keeps_at_most_the_configured_number_of_keywords() -> None:
    """Enough to recognise a piece of work by, few enough to render as chips."""
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {
        "m1": " ".join(f"wort{'x' * index}" for index in range(40)),
        "m2": "mahnung",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert len(found["t1"]) == CONFIG.topic_keyword_count == 8


def test_only_the_first_members_of_a_topic_are_read() -> None:
    """Half the cost ceiling, and the half that bounds the read.

    A topic of five hundred messages is described as well by twenty of them,
    and reading all five hundred would put the archive's text next to an
    in-process FalkorDB for no better answer.
    """
    members = tuple(f"m{index:03d}" for index in range(50))
    clusters = (_topic("t1", *members), _topic("t2", "other"))
    texts = dict.fromkeys(members, "gemeinsam") | {
        members[-1]: "geheimnis",
        "other": "mahnung",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert CONFIG.topic_keyword_members == 20
    assert "geheimnis" not in found["t1"]


def test_the_members_a_read_has_to_fetch_are_the_ones_that_are_counted() -> None:
    """The reader and the counter have to agree, or the ceiling is a lie.

    ``keyword_members`` is what the rebuild hands to
    :func:`~mailarc_analytics.derived.reader.read_texts`; a text fetched for a
    member this function then ignores is a page paid for and thrown away.
    """
    members = tuple(f"m{index:03d}" for index in range(50))
    clusters = (_topic("t1", *members),)

    asked = keyword_members(clusters, CONFIG)

    assert asked == members[: CONFIG.topic_keyword_members]


def test_each_body_is_cut_to_the_configured_length() -> None:
    """The other half of the ceiling, applied here as well as in the store.

    ``MESSAGE_TEXTS`` cuts with ``left()`` so a page carries what it needs, and
    this cuts again so the count is the same whoever supplied the text — a
    small archive that filled the texts in up front gets the same keywords as
    a large one that paged them.
    """
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {
        "m1": "angebot " * 400 + "geheimnis",
        "m2": "mahnung",
    }

    found = topic_keywords(clusters, texts, CONFIG)

    assert CONFIG.topic_keyword_chars == 2000
    assert "geheimnis" not in found["t1"]


def test_a_topic_with_no_text_at_all_is_absent_from_the_answer() -> None:
    """``keyworded_topics`` counts topics that came out with a keyword.

    An empty list written to the node would be indistinguishable from a topic
    whose every member was quoted history and a footer.
    """
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))

    found = topic_keywords(clusters, {"m2": "mahnung"}, CONFIG)

    assert "t1" not in found


def test_two_runs_over_the_same_texts_agree() -> None:
    """Ties are broken by the term itself, because a set has no order."""
    clusters = (_topic("t1", "m1"), _topic("t2", "m2"))
    texts = {"m1": "alpha beta gamma delta", "m2": "mahnung"}

    assert topic_keywords(clusters, texts, CONFIG) == topic_keywords(
        clusters, texts, CONFIG
    )


def test_a_single_topic_falls_back_to_what_it_says_most() -> None:
    """The degenerate archive, and the reason the idf is floored at zero.

    With one topic every term has the same document frequency, and
    ``log(1 / 2)`` is negative — so an unfloored TF-IDF would rank the terms
    that appear *least* first and answer with a topic's rarest words. Floored,
    the ranking falls back to the term count, which is the sensible reading of
    "what is this one topic about".
    """
    found = topic_keywords(
        (_topic("t1", "m1"),), {"m1": "angebot angebot angebot mahnung"}, CONFIG
    )

    assert found["t1"][0] == "angebot"


@pytest.mark.parametrize("word", ["und", "the", "mit", "with", "hallo"])
def test_the_stop_list_covers_both_languages_this_archive_speaks(word: str) -> None:
    """German with transliterated umlauts and English, which is what the
    corpus plants and what a real German mailbox holds."""
    assert word in STOPWORDS


def test_an_archive_whose_topics_carry_no_text_at_all_answers_with_nothing() -> None:
    """A rebuild over an archive of attachments and empty bodies is a real
    state, and writing an empty keyword list to every topic would say the
    stage ran and found the topics to be about nothing."""
    assert topic_keywords((_topic("t1", "m1"), _topic("t2", "m2")), {}, CONFIG) == {}
