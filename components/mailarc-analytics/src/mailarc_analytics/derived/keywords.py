"""What a topic is about, in its own members' words — and never in a model's.

TF-IDF over the **topics** rather than over the messages, which is the one
decision this module makes. A term every topic uses says nothing about which
piece of work a message belongs to, and inverse document frequency computed
across topics is what removes it. A stop list can only remove what is common in
the *language*; "rechnung", "meeting", "freigabe" are common in one archive and
absent from the next, and no list written here would know which.

Deterministic and explainable, like every other number in this package. §1.2's
rule is that a label on a derived node is something a human wrote — these are
words out of the archive's own mail, counted, and a reader can go and find the
message a keyword came from.

**Bounded twice, because this is the one stage that reads text at scale.**
``topic_keyword_members`` caps how many messages of a topic are read and
``topic_keyword_chars`` how much of each; the product is the ceiling R4 names.
The store cuts the body with ``left()`` in ``MESSAGE_TEXTS`` and this cuts again
so a small archive that filled its texts in up front gets the same keywords as
a large one that paged them.

:func:`keyword_members` and :func:`topic_keywords` have to agree about *which*
members those are, which is why the first exists at all — a text fetched for a
member the counter then ignores is a page paid for and thrown away.
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.model import Topic, TopicCluster
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog

logger = logging.getLogger(__name__)

MIN_LETTERS = 3
"""Letters a token needs before it can be a keyword.

Two would admit "zu", "am", "an", "in", "of", "to" and every other preposition
in both languages, which outnumber the content words by an order of magnitude
and would need a stop list ten times this one's size to hold back.
"""

STOPWORDS: frozenset[str] = frozenset((
    "aber", "alle", "allem", "allen", "aller", "alles", "als", "also",
    "andere", "anderen", "auch", "auf", "aus", "bei", "beim", "bin",
    "bis", "bitte", "dabei", "dann", "das", "dass", "dazu", "dein",
    "dem", "den", "denn", "der", "des", "dessen", "die", "dies", "diese",
    "diesem", "diesen", "dieser", "dieses", "doch", "dort", "durch",
    "ein", "eine", "einem", "einen", "einer", "eines", "einige", "und",
    "uns", "unser", "unsere", "etwa", "etwas", "euch", "euer", "fuer",
    "gegen", "gern", "gruesse", "gruss", "gut", "guten", "haben",
    "hallo", "hat", "hatte", "hatten", "herr", "herrn", "heute", "hier",
    "ihm", "ihn", "ihnen", "ihr", "ihre", "ihrem", "ihren", "immer",
    "info", "ist", "jede", "jeden", "jeder", "jetzt", "kann", "kein",
    "keine", "koennen", "liebe", "lieber", "mail", "mehr", "mein",
    "meine", "mich", "mir", "mit", "nach", "nicht", "noch", "nun", "nur",
    "oder", "ohne", "schon", "sehr", "sein", "seine", "seinem", "seinen",
    "sich", "sie", "sind", "soll", "sollen", "sowie", "tag", "ueber",
    "uhr", "und", "viele", "vielen", "vom", "von", "vor", "waere", "war",
    "waren", "was", "wenn", "wer", "werden", "wie", "wieder", "will",
    "wir", "wird", "wurde", "wurden", "zum", "zur", "zwar", "zwei",
    "about", "all", "also", "and", "any", "are", "been", "but", "can",
    "cannot", "could", "did", "does", "dont", "each", "for", "from",
    "get", "has", "have", "her", "here", "him", "his", "how", "its",
    "just", "like", "made", "make", "many", "may", "more", "most",
    "much", "must", "not", "now", "off", "one", "only", "other", "our",
    "out", "over", "per", "said", "same", "see", "she", "should",
    "since", "some", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "too",
    "under", "use", "very", "was", "way", "were", "what", "when",
    "where", "which", "while", "who", "will", "with", "would", "you",
    "your", "yours",
))  # fmt: skip
"""The language's own filler, in the two languages this archive speaks.

Small on purpose. A long list starts removing words that are filler in one
archive and the subject of another — "termin", "angebot", "rechnung" are all
somebody's project — and the idf above already removes whatever this particular
archive says everywhere. German is spelled with transliterated umlauts because
that is how the corpus plants it and how a real German mailbox arrives: clients
lose umlauts, and the parser's own rules are written for both spellings.
"""

_REFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,9}-\d{1,8}|#\d{2,8}")
"""Ticket tokens, removed before anything is tokenised.

A2 joined the topic *on* this token, so as a keyword it is the label of the
finding rather than a description of it — and the fragment a letter tokeniser
leaves behind ("nord", out of ``NORD-42``) is worse, because it reads like a
word somebody chose.
"""

_WORD = re.compile(rf"[^\W\d_]{{{MIN_LETTERS},}}", re.UNICODE)
"""Runs of letters. Digits and underscores are not letters, so a year, an
amount and an order number never become terms."""


def keyword_members(
    clusters: Sequence[TopicCluster], config: AnalyticsConfig
) -> tuple[str, ...]:
    """The message ids the keyword stage needs the text of, deduplicated.

    What the rebuild hands to
    :func:`~mailarc_analytics.derived.reader.read_texts`. The same prefix
    :func:`topic_keywords` counts, so the read and the count cannot disagree
    about the ceiling.
    """
    return tuple(
        sorted({one for cluster in clusters for one in _members(cluster, config)})
    )


def topic_keywords(
    clusters: Sequence[TopicCluster],
    texts: Mapping[str, str],
    config: AnalyticsConfig,
) -> dict[str, tuple[str, ...]]:
    """The keywords of every topic that had something to say. No I/O.

    ``idf = log(T / (1 + df))`` over the topics, **floored at zero**. The floor
    is what makes the degenerate archive answer sensibly: with a single topic
    every term has ``df = 1`` and ``log(1/2)`` is negative, so an unfloored
    TF-IDF would rank a topic's *rarest* words first. Floored, that case falls
    back to the term count, and in every other case a term used by every topic
    scores nothing and drops out — which is the whole reason the idf is over
    topics rather than over messages.

    A topic no text came back for is **absent** from the answer rather than
    present with an empty tuple, so the count a job row reports is topics that
    came out with a keyword rather than topics that were looked at.
    """
    counted = {cluster.id: _count(cluster, texts, config) for cluster in clusters}
    counted = {key: terms for key, terms in counted.items() if terms}
    if not counted:
        logger.info("No topic carried enough text for a keyword")
        return {}

    total = len(counted)
    frequency: dict[str, int] = {}
    for terms in counted.values():
        for term in terms:
            frequency[term] = frequency.get(term, 0) + 1
    idf = {
        term: max(0.0, math.log(total / (1 + seen))) for term, seen in frequency.items()
    }
    found = {
        key: tuple(
            term
            for term, _ in sorted(
                terms.items(),
                key=lambda one: (-one[1] * idf[one[0]], -one[1], one[0]),
            )[: max(0, config.topic_keyword_count)]
        )
        for key, terms in counted.items()
    }
    found = {key: terms for key, terms in found.items() if terms}
    logger.info("Described %d of %d topics with keywords", len(found), len(clusters))
    return found


def write_keywords(session: Session, found: Mapping[str, Sequence[str]]) -> int:
    """Attach the keywords to their topics; return how many rows were sent.

    A ``MATCH`` and a ``SET`` although the statement is named like its
    siblings: the topic was written one stage earlier and this is a second pass
    over the same node. Merging it here would create an empty ``Topic`` for a
    keyword row whose cluster had gone, which can only happen if the two stages
    disagree — and an empty node is a worse way to find that out than a row
    that wrote nothing.
    """
    written = merge_rows(
        session,
        catalog.MERGE_TOPIC_KEYWORDS,
        (
            {"topic_id": topic, "keywords": list(keywords)}
            for topic, keywords in sorted(found.items())
        ),
        model=Topic,
    )
    logger.info("Wrote keywords for %d topics", written)
    return written


def _members(cluster: TopicCluster, config: AnalyticsConfig) -> tuple[str, ...]:
    """The prefix of a topic's members this stage looks at, in id order.

    Sorted by id rather than by the member's own score, because the score is
    the strength of the *join* and says nothing about how much the message has
    to say. The id order is the one two rebuilds agree on.
    """
    ceiling = max(0, config.topic_keyword_members)
    ordered = sorted(one.message_id for one in cluster.members)
    return tuple(ordered[:ceiling]) if ceiling else tuple(ordered)


def _count(
    cluster: TopicCluster, texts: Mapping[str, str], config: AnalyticsConfig
) -> dict[str, int]:
    """How often each term appears anywhere in this topic's read members."""
    ceiling = max(0, config.topic_keyword_chars)
    counted: dict[str, int] = {}
    for member in _members(cluster, config):
        text = texts.get(member)
        if not text:
            continue
        for term in _terms(text[:ceiling] if ceiling else text):
            counted[term] = counted.get(term, 0) + 1
    return counted


def _terms(text: str) -> list[str]:
    """One body reduced to the words that could describe it.

    Lower-cased first so the stop list and the counting agree about a word at
    the start of a sentence, references removed before tokenising so no
    fragment of one survives, and everything shorter than
    :data:`MIN_LETTERS` or in :data:`STOPWORDS` dropped.
    """
    stripped = _REFERENCE.sub(" ", text.lower())
    return [term for term in _WORD.findall(stripped) if term not in STOPWORDS]
