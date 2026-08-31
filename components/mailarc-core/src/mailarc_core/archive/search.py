"""What a search asks the archive, and what it gets back.

Value objects only, plus one sanitizer — no I/O and no session. The
repository turns a :class:`SearchFilters` into statements and the reader
turns the rows into a :class:`SearchPage`; this module is the language the
two of them and every page above them share, so a state can hold a filled
form and a result page after the session that answered it is gone.

:func:`searchable_terms` is a **deliberate twin** of
:func:`mailarc_analytics.semantic.search.searchable_terms`. The core cannot
import it — ``mailarc-analytics`` sits *on top of* this component — and the
full-text index lives in the ground-truth graph, so the core's own search
path needs the same guard: the query text reaches RediSearch, which is a
second query language behind the bound parameter. Six lines twice beats a
dependency pointing the wrong way; if one twin changes, change the other.
"""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from mailarc_core.archive.model import MessageSummary

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
"""A run of letters or digits, in any script.

Written as "not a non-word character and not an underscore" so that German
umlauts and accented names survive — ``[a-z0-9]+`` would cut *Rechnung
Müller* into two useless halves. Everything else, and that is exactly the
set of RediSearch's operators, is dropped rather than escaped.
"""


class SearchFilters(BaseModel):
    """One filled search form, as the archive reads it.

    Every field is optional and an unset one filters nothing. ``text`` goes
    to the full-text index; the rest narrow by graph structure. Strings stay
    as the form held them — normalisation is the repository's business,
    because what "matches" means is decided where the statement is built.

    ``has_attachments`` is a tri-state on purpose: ``None`` is "either",
    ``False`` is a real filter ("only messages without files"), and a plain
    bool could not say both.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    sender: str = ""
    recipient: str = ""
    sent_from: datetime | None = None
    sent_until: datetime | None = None
    has_attachments: bool | None = None
    account_id: str = ""

    @property
    def structured(self) -> bool:
        """Whether any graph-shaped filter is set — everything but ``text``."""
        return bool(
            self.sender.strip()
            or self.recipient.strip()
            or self.account_id.strip()
            or self.sent_from is not None
            or self.sent_until is not None
            or self.has_attachments is not None
        )

    @property
    def empty(self) -> bool:
        """Whether this form asks for nothing — the browse-recent case."""
        return not self.text.strip() and not self.structured


class ScoredId(BaseModel):
    """One full-text match before hydration: which message, how well.

    ``relevance`` is the index's raw score — unbounded, and meaningless
    across two different queries. The reader scales a page of these against
    its best hit before anything renders them.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    relevance: float = 0.0


class MessageHit(BaseModel):
    """One result row: the summary a listing shows, plus how it ranked.

    ``relevance`` is ``None`` outside full-text mode — a structured filter
    matches or it does not, and printing a score for that would invent a
    ranking nobody computed. In full-text mode it is scaled to ``0..1``
    within one answer: a ranking, not a measurement.
    """

    model_config = ConfigDict(frozen=True)

    summary: MessageSummary
    relevance: float | None = None


class SearchPage(BaseModel):
    """One page of results, and — where it can be known — how many there are.

    ``total`` is ``None`` in full-text mode on purpose: counting a full-text
    answer would mean running the procedure a second time without the page
    cut, and a relevance-ranked listing reads fine without a denominator.
    The structured and browse paths count cheaply and say so.
    """

    model_config = ConfigDict(frozen=True)

    hits: tuple[MessageHit, ...] = ()
    total: int | None = None


def searchable_terms(text: str) -> str:
    """The caller's words, with every query operator taken out.

    The twin of :func:`mailarc_analytics.semantic.search.searchable_terms`
    — same regex, same joining, same refusal — kept in step by hand because
    the import may only point the other way. It raises :class:`ValueError`
    where the analytics twin raises its own ``SearchQueryError``: this
    component has no search-error hierarchy and does not need one for
    "that was not a question".

    Raising beats returning ``""``: an empty result would read as "your
    archive holds nothing on the subject", and ``"-@subject:*"`` is a query
    with no words in it, not an empty archive. The surviving words are
    re-joined with spaces, which RediSearch reads as **AND** — two words
    are a narrowing, and a caller wanting either can ask twice.
    """
    terms = " ".join(_WORD.findall(text))
    if not terms:
        raise ValueError(
            f"the search {text!r} holds no searchable words — query operators "
            "are removed before the archive sees them, so ask with plain words"
        )
    return terms
