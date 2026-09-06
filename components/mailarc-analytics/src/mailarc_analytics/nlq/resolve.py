"""Turning what the model read into what this archive actually holds.

Pure functions over their arguments: no session, no client, no configuration
read. That is what makes the cases this feature lives or dies on — "meine
Hausverwaltung Santak" against a display name, "AWS" against
``amazonaws.com``, "letztes Quartal" on a particular Tuesday — testable
without a model and without a graph.

Three rules run through all of it.

**Nothing is invented.** Every value that comes out is one the archive handed
in: an address it holds, a file family its picker offers, a mailbox it
imports from. :attr:`~mailarc_core.archive.search.SearchFilters.senders` is
matched **exactly**, so a plausible-looking address that is not in the
directory would filter to zero and read as an empty archive.

**Nothing disappears.** A mention nothing matches goes back into the text and
is named in :attr:`~mailarc_analytics.nlq.model.ResolvedQuery.unresolved`.
Dropping it would turn "everything from my property manager" into "everything"
— a narrow question answered with the whole archive, looking like it worked.

**The calendar is arithmetic, not reading.** The model classifies a period into
:class:`~mailarc_analytics.nlq.model.Period` and :func:`window_of` computes the
two days. Quarter boundaries and month lengths are exactly what a model gets
quietly wrong, and a wrong window is indistinguishable from a right one that
found nothing.
"""

import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import date, timedelta

from mailarc_analytics.nlq.model import (
    AccountOption,
    DirectoryEntry,
    FamilyOption,
    Order,
    Period,
    QueryIntent,
    ResolvedQuery,
    Vocabulary,
)

logger = logging.getLogger(__name__)

EARLIEST = date(1990, 1, 1)
"""The oldest day a filter may name.

Not a claim about what an archive holds — it is a guard against a model that
answers ``0202-03-01`` for "im März". A date centuries adrift produces a filter
that matches nothing, which reads as an empty archive rather than as a bad
date, so it is refused and said out loud instead.
"""

_MIN_TOKEN = 3
"""How short a word has to be before it stops narrowing anything.

Two letters match half a directory by substring. The words this cuts — "AG",
"co", "eG" — are the ones that carry no identity anyway.
"""

_NOISE = frozenset(
    {
        "der",
        "die",
        "das",
        "dem",
        "den",
        "des",
        "ein",
        "eine",
        "einer",
        "einem",
        "mein",
        "meine",
        "meiner",
        "meinem",
        "meinen",
        "von",
        "vom",
        "bei",
        "the",
        "and",
        "und",
        "for",
        "from",
        "with",
        "gmbh",
        "mbh",
        "ltd",
        "inc",
        "llc",
        "kg",
        "ohg",
        "e.v",
    }
)
"""Words a mention may carry that identify nobody.

The prompt asks the model to hand over the name and not the sentence around
it, and mostly it does. This is the defensive half: "meiner Hausverwaltung
Santak" arriving whole must still find Santak, and ``mein`` matching every
address that happens to contain those letters would be worse than not matching
at all. The legal-form suffixes are here for the same reason — every second
German company is a ``GmbH``.
"""

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
"""A run of letters or digits in any script.

:func:`~mailarc_core.archive.search.searchable_terms`' expression, and chosen
for the same property: ``[a-z0-9]+`` would cut *Müller* in half, and this
feature is aimed at German mail.
"""


def resolve(
    intent: QueryIntent,
    vocabulary: Vocabulary,
    *,
    today: date,
    max_addresses: int = 5,
) -> ResolvedQuery:
    """One reading of a question, grounded in what this archive holds.

    The order matters in one place: the dates are settled before the text is,
    because a date the model got wrong is refused and its note has to be
    carried, and the text is where every unresolved mention ends up.
    """
    notes: list[str] = []
    unresolved: list[str] = []

    senders = _addresses(
        intent.from_mentions, vocabulary.senders, max_addresses, unresolved
    )
    recipients = _addresses(
        intent.to_mentions, vocabulary.recipients, max_addresses, unresolved
    )
    families, unmatched_files = resolve_families(
        intent.attachment_words, vocabulary.families
    )
    account_id = resolve_account(intent.account_mention, vocabulary.accounts)
    after, before = _window(intent, today=today, notes=notes)

    if intent.account_mention.strip() and not account_id:
        unresolved.append(intent.account_mention.strip())
    unresolved.extend(unmatched_files)

    text, dropped = _text(
        intent,
        senders,
        recipients,
        unresolved,
        notes,
        structural=bool(
            senders
            or recipients
            or families
            or account_id.strip()
            or after
            or before
            or intent.has_attachments is not None
        ),
    )
    if dropped:
        notes.append(dropped)

    return ResolvedQuery(
        text=text,
        senders=senders,
        recipients=recipients,
        after=after,
        before=before,
        has_attachments=intent.has_attachments,
        families=families,
        account_id=account_id,
        order=intent.order,
        unresolved=tuple(dict.fromkeys(unresolved)),
        notes=tuple(notes),
    )


def _text(
    intent: QueryIntent,
    senders: tuple[str, ...],
    recipients: tuple[str, ...],
    unresolved: Sequence[str],
    notes: list[str],
    *,
    structural: bool,
) -> tuple[str, str]:
    """The words to search for, and the note owed for the ones left out.

    Two decisions meet here and the second depends on the first.

    Every unresolved mention joins the text. It is the only honest place for
    it: the archive has no address to filter on, but the words are still what
    the person asked about, and a full-text hit on "Santak" is a far better
    answer than every message ever archived.

    :attr:`~mailarc_analytics.nlq.model.Order.NEWEST` then drops the text —
    but only when something structural survives to filter on. That is not a
    preference, it is which branch of
    :meth:`~mailarc_core.ArchiveReader.search_messages` answers: a filter
    carrying text is ranked by relevance, and one carrying only structure comes
    back newest first. So "wann war die letzte Aufladung von Telekom" is
    honoured by keeping the sender and letting the word go — and the note says
    so, because a word silently ignored is the one way a search can lie about
    what it searched.

    With nothing structural to fall back on, the text stays: an empty
    :class:`~mailarc_core.archive.search.SearchFilters` is the browse-the-whole
    -archive read, and answering a question with the entire archive to honour
    an ordering would be the worse of the two failures.

    ``structural`` is the caller's answer to that question and must mirror
    :attr:`~mailarc_core.archive.search.SearchFilters.structured`, which counts
    the file families, the account and both date bounds as well. Reading only
    the senders and the attachment flag here told a question like "die neueste
    Rechnung als PDF vom letzten Monat" that nothing but words was asked for —
    a note that was false, and a text kept that sent the read down the
    relevance-ranked branch when the person had asked for the newest.
    """
    words = " ".join(part for part in (intent.text.strip(), *unresolved) if part)
    if intent.order is not Order.NEWEST or not words:
        return words, ""
    if not structural:
        notes.append(
            "Nothing but words was asked for, so the newest is not "
            "necessarily first — these are the best matches."
        )
        return words, ""
    return "", (
        f"Sorted newest first, so the words {words!r} were not used — "
        "clear the other filters to search for them instead."
    )


def _window(intent: QueryIntent, *, today: date, notes: list[str]) -> tuple[str, str]:
    """The two date bounds, explicit dates winning over a named period.

    Explicit wins because it is the more specific reading of the same sentence:
    a question naming both "letzten Monat" and a day means the day.
    """
    after = _day(intent.after, today=today, notes=notes)
    before = _day(intent.before, today=today, notes=notes)
    if after or before:
        return after, before
    return window_of(intent.period, today=today)


def _day(value: str, *, today: date, notes: list[str]) -> str:
    """One explicit date, refused out loud when it cannot be one.

    Both failures are said rather than swallowed. An unparseable date silently
    dropped narrows nothing and looks like it did; a date centuries out narrows
    to nothing and looks like an empty archive.
    """
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        notes.append(f"{text!r} is not a date I could read, so I ignored it.")
        return ""
    if not (EARLIEST <= parsed <= today + timedelta(days=1)):
        notes.append(
            f"The date {text} is outside the archive's range, so I ignored it."
        )
        return ""
    return parsed.isoformat()


def window_of(period: Period, *, today: date) -> tuple[str, str]:
    """The two days a named period covers, as ``YYYY-MM-DD`` strings.

    Inclusive at both ends, and both are days rather than instants: the upper
    bound becomes the end of its day in
    :func:`~mailarc_ui.search.model.filters_of`, which is the only place that
    conversion may happen — a ``sent_until`` left at midnight silently drops
    everything sent on the day that was asked for.

    ``today`` is passed in rather than read, which is what makes every case
    below testable, and it is expected to be **local wall clock**: ``sent_at``
    is stored as the header's own wall clock, so a UTC "today" would name
    yesterday for anybody east of Greenwich between midnight and their offset.
    """
    if period is Period.NONE:
        return "", ""
    if period is Period.LAST_7_DAYS:
        return _span(today - timedelta(days=6), today)
    if period is Period.LAST_30_DAYS:
        return _span(today - timedelta(days=29), today)
    if period is Period.LAST_12_MONTHS:
        return _span(_shifted(today, months=-12), today)
    if period is Period.THIS_MONTH:
        return _span(today.replace(day=1), today)
    if period is Period.LAST_MONTH:
        first = _shifted(today.replace(day=1), months=-1)
        return _span(first, _month_end(first))
    if period is Period.THIS_QUARTER:
        return _span(_quarter_start(today), today)
    if period is Period.LAST_QUARTER:
        first = _shifted(_quarter_start(today), months=-3)
        return _span(first, _month_end(_shifted(first, months=2)))
    if period is Period.THIS_YEAR:
        return _span(today.replace(month=1, day=1), today)
    if period is Period.LAST_YEAR:
        return _span(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    raise AssertionError(f"no window defined for {period!r}")


def _span(first: date, last: date) -> tuple[str, str]:
    """Two days as the form's date fields hold them."""
    return first.isoformat(), last.isoformat()


def _quarter_start(day: date) -> date:
    """The first day of the quarter this day falls in."""
    return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)


def _month_end(day: date) -> date:
    """The last day of this day's month, without a calendar table."""
    return _shifted(day.replace(day=1), months=1) - timedelta(days=1)


def _shifted(day: date, *, months: int) -> date:
    """The same day-of-month a number of months away, clamped to what exists.

    31 March shifted back a month is 28 or 29 February, not an error: every
    caller here starts from the first of a month, and the clamp is what keeps
    the one that does not (:attr:`Period.LAST_12_MONTHS`) from raising on a
    leap day.
    """
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    last = [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
        month
    ]
    return date(year, month + 1, min(day.day, last))


def _leap(year: int) -> bool:
    """Whether a year has a 29 February."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _addresses(
    mentions: Sequence[str],
    directory: Sequence[DirectoryEntry],
    limit: int,
    unresolved: list[str],
) -> tuple[str, ...]:
    """Every mention resolved against one role's directory, in order.

    Deduplicated across mentions, because two names for one correspondent are
    one filter — and on the recipients' *all* reading a repeat would be a
    second pattern matching the first one's node.
    """
    found: list[str] = []
    for mention in mentions:
        matched = resolve_addresses(mention, directory, limit=limit)
        if not matched:
            if mention.strip():
                unresolved.append(mention.strip())
            continue
        found.extend(one for one in matched if one not in found)
    return tuple(found)


def resolve_addresses(
    mention: str, directory: Sequence[DirectoryEntry], *, limit: int = 5
) -> tuple[str, ...]:
    """The archived addresses one name in a question stands for.

    Scored rather than filtered, in two tiers. A mention's word matching a
    *whole word* of an address, a domain or a display name is strong evidence;
    matching only as a substring is weak — "AWS" inside ``amazonaws.com`` is
    the case that needs the weak tier, and ``law`` inside ``lawsociety`` is why
    the strong tier wins outright wherever it exists.

    Only the best-scoring tier is returned, which is what keeps one good match
    from arriving beside forty poor ones. Order is the caller's own and is
    carried through rather than imposed: what comes back fills a picker, and it
    should sit there in the order that picker offers its options in.

    Returns nothing when the best match is still more than ``limit`` addresses:
    a **From** picker that silently filled itself with thirty addresses would
    be a filter nobody could read, and the caller reports the mention as
    unresolved instead. ``limit`` is
    :attr:`~mailarc_analytics.nlq.config.LlmConfig.max_resolved_addresses`.
    """
    tokens = _tokens(mention)
    if not tokens:
        return ()
    ranked: list[tuple[int, str]] = []
    for entry in directory:
        score = _score(tokens, entry)
        if score:
            ranked.append((score, entry.address))
    if not ranked:
        return ()
    best = max(score for score, _ in ranked)
    found = tuple(address for score, address in ranked if score == best)
    if len(found) > limit:
        logger.debug(
            "The mention %r matches %d addresses, past the %d a picker shows",
            mention,
            len(found),
            limit,
        )
        return ()
    return found


def _score(tokens: Sequence[str], entry: DirectoryEntry) -> int:
    """How well one directory entry answers to a mention's words.

    Two points for a whole word, one for a substring, summed over the mention's
    words — so a two-word name matching both beats a one-word coincidence, and
    an exact word beats a fragment.
    """
    haystack = " ".join((entry.address, entry.domain, *entry.names)).casefold()
    words = set(_WORD.findall(_folded(haystack)))
    flat = _folded(haystack)
    total = 0
    for token in tokens:
        if token in words:
            total += 2
        elif token in flat:
            total += 1
    return total


def _tokens(mention: str) -> tuple[str, ...]:
    """A mention's identifying words: folded, de-noised and long enough."""
    return tuple(
        word
        for word in _WORD.findall(_folded(mention))
        if len(word) >= _MIN_TOKEN and word not in _NOISE
    )


def _folded(text: str) -> str:
    """Case- and accent-insensitive text, so *Müller* finds *mueller*'s node.

    Decomposed and stripped of combining marks rather than transliterated:
    ``ü`` becomes ``u``, which matches how most address local parts spell a
    name that has one. It does not turn ``ü`` into ``ue`` — that spelling is
    caught by the substring tier when it is present in either half.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(one for one in decomposed if not unicodedata.combining(one))


def resolve_account(mention: str, accounts: Sequence[AccountOption]) -> str:
    """The mailbox a question names, as the account picker's own value.

    One and never several:
    :attr:`~mailarc_core.archive.search.SearchFilters.account_id` is a single
    value, so "from my work or my private mailbox" has no expressible answer
    and the honest one is none — the caller reports the mention as unresolved
    and the words go into the text.

    Scored with :func:`_score` like an address, by putting the mailbox's own
    label and value where a directory entry's names would be, so "Gmail" finds
    a mailbox called *Gmail privat* and a bare word finds nothing by accident.
    """
    tokens = _tokens(mention)
    if not tokens:
        return ""
    ranked = [
        (score, option.value)
        for option in accounts
        if (
            score := _score(
                tokens,
                DirectoryEntry(address=option.value, names=(option.label,)),
            )
        )
    ]
    if not ranked:
        return ""
    best = max(score for score, _ in ranked)
    matched = [value for score, value in ranked if score == best]
    if len(matched) != 1:
        logger.debug("The mailbox %r matches %d accounts", mention, len(matched))
        return ""
    return matched[0]


def resolve_families(
    words: Sequence[str], options: Sequence[FamilyOption]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The file families a question's words name, and the words that named none.

    The options are the search form's own — value and label, built from what
    this archive actually carries — so the interpreter and the picker cannot
    disagree about what a *spreadsheet* is. That taxonomy stays where
    :mod:`mailarc_ui.search.files` argues it belongs; this only matches against
    the result.

    :data:`_SYNONYMS` is the bridge between the two languages a person asks in
    and the English values the picker uses. A word matching nothing is returned
    rather than dropped, and the caller puts it back into the text — "Rechnung"
    in an archive whose picker offers no PDF family is still a word worth
    searching for.
    """
    known = {option.value for option in options}
    labels = {_folded(option.label): option.value for option in options if option.label}
    found: list[str] = []
    missed: list[str] = []
    for word in words:
        matched = _families(word, known, labels)
        if matched:
            found.extend(one for one in matched if one not in found)
        elif word.strip():
            missed.append(word.strip())
    return tuple(found), tuple(missed)


def _families(word: str, known: set[str], labels: dict[str, str]) -> tuple[str, ...]:
    """The families one word stands for: its own value, its label, or a synonym."""
    folded = _folded(word).strip()
    if folded in known:
        return (folded,)
    if folded in labels:
        return (labels[folded],)
    named = _SYNONYMS.get(folded, ())
    return tuple(one for one in named if one in known)


_SYNONYMS: dict[str, tuple[str, ...]] = {
    "rechnung": ("pdf", "document"),
    "rechnungen": ("pdf", "document"),
    "beleg": ("pdf", "document"),
    "belege": ("pdf", "document"),
    "quittung": ("pdf", "document"),
    "invoice": ("pdf", "document"),
    "invoices": ("pdf", "document"),
    "receipt": ("pdf", "document"),
    "vertrag": ("pdf", "document"),
    "dokument": ("document", "pdf"),
    "dokumente": ("document", "pdf"),
    "schreiben": ("pdf", "document"),
    "tabelle": ("spreadsheet",),
    "tabellen": ("spreadsheet",),
    "excel": ("spreadsheet",),
    "xlsx": ("spreadsheet",),
    "csv": ("spreadsheet",),
    "bild": ("image",),
    "bilder": ("image",),
    "foto": ("image",),
    "fotos": ("image",),
    "screenshot": ("image",),
    "screenshots": ("image",),
    "praesentation": ("slides",),
    "powerpoint": ("slides",),
    "folien": ("slides",),
    "slides": ("slides",),
    "video": ("video",),
    "videos": ("video",),
    "audio": ("audio",),
    "musik": ("audio",),
    "archiv": ("archive",),
    "zip": ("archive",),
    "kalender": ("calendar",),
    "termin": ("calendar",),
}
"""What a person's word for a file means in the picker's vocabulary.

Deliberately a table and not a model call. It is the one part of a question
that is genuinely closed-vocabulary — there are fifteen families — and a table
is testable, instant and cannot invent a sixteenth. The values are
:class:`~mailarc_ui.message_detail.FileKind`'s own strings; a family this
archive holds no files of is filtered out by :func:`_families`, so a synonym
naming one costs nothing.

*Rechnung* maps to both ``pdf`` and ``document`` because an invoice arrives as
either and the filter reads several families as *any of these*.
"""
