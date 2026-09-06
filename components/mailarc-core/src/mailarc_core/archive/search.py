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
    senders: tuple[str, ...] = ()
    """The addresses the message may have come from, each matched **exactly**.

    Exact and not by containment, because these are picked from the addresses
    the archive holds rather than typed: ``anna@example.com`` is a substring of
    ``hanna@example.com``, so a containment match on a picked address would
    quietly widen a filter a person chose from a list.

    Several of them can only ever mean **any** — a message has exactly one
    sender, so "from all of these" is a question with no answer. That is why
    there is no ``senders_all`` beside :attr:`recipients_all`: the choice the
    recipients have is not a choice this field has.
    """

    recipients: tuple[str, ...] = ()
    """The addresses the message has to have reached, each matched exactly.

    A tuple rather than one string because the form offers a multi-select, and
    :attr:`recipients_all` is what several of them mean.
    """

    recipients_all: bool = True
    """Whether every one of :attr:`recipients` has to be on the message.

    ``True`` narrows — the message reached all of them — and ``False`` widens:
    any one is enough. The default is the narrowing one, because every other
    field on this form narrows too. A message can carry any number of
    recipients, which is what makes this askable at all — see :attr:`senders`.
    """

    sent_from: datetime | None = None
    sent_until: datetime | None = None
    has_attachments: bool | None = None

    attachment_types: tuple[str, ...] = ()
    """Content types a file on the message may have, each matched **exactly**.

    Exact and not by family, for the reason :attr:`senders` is exact: these are
    picked from what the archive holds rather than typed. *What counts as a
    spreadsheet* is a question about how a person reads a file list, and the
    answer belongs where the list is drawn — the form expands its choice into
    the types this archive actually stores and sends those. The archive is then
    only asked something it can answer without a taxonomy of its own.

    Several of them mean **any**, and can mean nothing else: one file has one
    content type, so "a message carrying a PDF *and* a spreadsheet" and "a
    message carrying either" would need two readings of a pick the form offers
    as one list. Any is the reading a person expects of a file filter — the
    same call :attr:`senders` makes, and the opposite of
    :attr:`recipients_all`, where a message really can satisfy every value.

    One thing this filter inherits and cannot fix: an ``Attachment`` node is
    keyed by the **sha256 of its bytes**, so the same file arriving twice under
    two different declared types is one node carrying whichever type was
    written first. That is content addressing doing what it is for, and the
    place to change it would be the node, not the query.
    """

    attachment_names: tuple[str, ...] = ()
    """Filenames a file on the message may have, each matched **exactly**.

    The other half of the same pick, and it exists because a content type can
    be a shrug: a sender who says ``application/octet-stream`` has said
    nothing, and the only thing left that says *PDF* is ``Rechnung.pdf``. The
    form expands a picked family into both lists — the types this archive
    stores for it, and the names of the shrugs that belong to it — and this
    filter ORs them, so a message matches when either half does.

    Exact whole names, from :attr:`KnownAttachments.untyped_names`, for the
    reason :attr:`senders` is exact: they are picked from what the archive
    holds rather than typed. A suffix match would be the archive deciding what
    ``.pdf`` means, which is the taxonomy this component does not carry.
    """

    account_id: str = ""

    @property
    def sent_by(self) -> tuple[str, ...]:
        """:attr:`senders` with the blanks dropped — the ones that filter."""
        return _picked(self.senders)

    @property
    def addressed(self) -> tuple[str, ...]:
        """:attr:`recipients` with the blanks dropped — the ones that filter.

        With :attr:`sent_by`, the one place an empty pick is thrown away, so
        that "is this filter set" and "which addresses does it name" cannot
        disagree.
        """
        return _picked(self.recipients)

    @property
    def attached_types(self) -> tuple[str, ...]:
        """:attr:`attachment_types` with the blanks dropped — the ones that
        filter. The same guard :attr:`sent_by` keeps, for the same reason."""
        return _picked(self.attachment_types)

    @property
    def attached_names(self) -> tuple[str, ...]:
        """:attr:`attachment_names` with the blanks dropped — the ones that
        filter. A file part can reach the graph with an empty name, and an
        empty pick must not become a filter for it."""
        return _picked(self.attachment_names)

    @property
    def attached(self) -> bool:
        """Whether the file picker is set at all, in either of its halves.

        The one place the two lists are read as one question, so that "is this
        filter on" cannot answer differently from what the statement builds.
        """
        return bool(self.attached_types or self.attached_names)

    @property
    def structured(self) -> bool:
        """Whether any graph-shaped filter is set — everything but ``text``."""
        return bool(
            self.sent_by
            or self.addressed
            or self.attached
            or self.account_id.strip()
            or self.sent_from is not None
            or self.sent_until is not None
            or self.has_attachments is not None
        )

    @property
    def empty(self) -> bool:
        """Whether this form asks for nothing — the browse-recent case."""
        return not self.text.strip() and not self.structured


def _picked(values: tuple[str, ...]) -> tuple[str, ...]:
    """The values in a pick that are actually values — blanks dropped."""
    return tuple(one.strip() for one in values if one.strip())


class KnownAttachments(BaseModel):
    """The files this archive's messages actually carry, in two lists.

    What the search form's file filter is built from, and the same bargain
    :class:`KnownAddresses` strikes: the form offers what the archive holds
    rather than a fixed menu, so no option can return nothing. Both lists are
    ordered commonest first, so a picker cut to its first options shows the
    ones a person is most likely to want.

    Raw content types and names, never families. Grouping ``application/pdf``
    and ``image/png`` into *PDF* and *Images* is a presentation decision, and
    this component has no opinion about how a person reads a file list — see
    :attr:`SearchFilters.attachment_types`.

    **What is left out is as deliberate as what is in.** A part the HTML
    references by ``cid:`` and the sender never named is a logo in a
    signature, not a file: it is counted out here exactly as
    :attr:`~mailarc_core.mail.model.ParsedMessage.has_attachments` counts it
    out, so the tri-state above the picker and the picker itself cannot
    disagree about whether a message carries anything.
    """

    model_config = ConfigDict(frozen=True)

    content_types: tuple[str, ...] = ()
    """The types of the files whose sender said what they were."""

    untyped_names: tuple[str, ...] = ()
    """The *filenames* of the files whose sender said nothing.

    A second list rather than four more entries in the first one, because
    ``application/octet-stream`` — see
    :data:`~mailarc_core.mail.model.UNTYPED_CONTENT_TYPES` — is what several
    mail programs send for every attachment, and an archive's invoices arrive
    under it. Offered as a type it would be one option called *File* standing
    for a folder of PDFs; offered by name, ``Rechnung.pdf`` can be read as
    what it is by the one layer that knows how to read a file name.

    Their content types are deliberately **not** in :attr:`content_types`:
    a filter over them would match every shrug in the archive, which is the
    one answer no picked family means.
    """


class KnownAddresses(BaseModel):
    """The addresses a search form can be narrowed by, split by role.

    Two lists rather than one, because the two fields ask different questions:
    an address the archive only ever received mail *at* is not a sender, and
    offering it under **From** would be offering a filter that cannot match.
    Each is ordered busiest first, so a picker showing only its first hundred
    options shows the hundred a person is most likely to want.
    """

    model_config = ConfigDict(frozen=True)

    senders: tuple[str, ...] = ()
    recipients: tuple[str, ...] = ()


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
