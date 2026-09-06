"""What the model is told, which is the question and the calendar and no more.

**Everything in this module is a constant except one date.** No address, no
mailbox name, no file type this archive happens to hold, no message text. That
is not an oversight to be filled in later — it is the feature's whole privacy
argument, and it is only true because the grounding happens afterwards:
:mod:`mailarc_analytics.nlq.resolve` matches the words the model hands back
against the archive's own vocabulary locally, so the model never has to be
shown a candidate list. A candidate list of correspondents is a list of who a
person knows.

Semantic search makes the opposite trade and says so on its own page: it
uploads message bodies once, and gets meaning for it. This uploads a sentence
somebody typed and gets a filled-in form. Both are defensible; a reader has to
be able to tell which one they turned on.

Two things the wording deliberately does:

**It says "answer with JSON" even though every adapter constrains the schema.**
Ollama's own guidance asks for it, and without it a constrained small model
pads its answer until it runs out of tokens.

**It tells the model not to invent an address.** The one failure that would
survive grounding is a plausible-looking address, because a mention nothing
matches is reported while a mention that matches the wrong entry is not.
"""

from datetime import date

SYSTEM = """\
You turn a question about somebody's email archive into a search filter.

Today is {today} ({weekday}). Use it for anything the question dates relative \
to now.

The question will usually be in German, sometimes in English. Answer for \
either.

Rules:
- Put only the subject matter in `text` - the words that would appear in the \
message itself. Never put a person, a company, a date or a file type there.
- Put names in `from_mentions` and `to_mentions` exactly as the question says \
them. Never invent or guess an email address; you have not been shown any.
- Use `period` for a relative stretch of time and leave `after`/`before` \
empty. Use `after`/`before` only for a date the question states outright, and \
then leave `period` empty.
- Set `order` to "newest" when the question asks for the most recent or the \
last one - wann, wann zuletzt, letzte, neueste, last, latest. Otherwise \
"relevance".
- Set `has_attachments` to true only when the question really asks for files, \
not merely because it names a kind of document.
- Put the kind of file in `attachment_words`, in the question's own words - \
Rechnung, PDF, Tabelle, Bilder. Do not translate it and do not guess a MIME \
type.
- Leave a field at its empty value when the question does not say. Do not \
guess.

Answer with JSON only. No explanation, no code fence.\
"""
"""The whole instruction, with one date substituted into it.

Written as one block rather than assembled from parts, because the parts would
invite a caller to leave one out. :func:`system_prompt` is the only way to fill
it in.
"""

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
"""Spelled out rather than taken from ``strftime``, which is locale-dependent:
under a German locale the prompt would say *Sonntag* to a model being addressed
in English, and the day name is there to help it read "letzten Freitag"."""


def system_prompt(today: date) -> str:
    """The instruction for one particular day.

    ``today`` is passed in rather than read, which is what makes the prompt
    testable, and it is expected to be **local wall clock**: ``sent_at`` is
    stored as the header's own wall clock, so a UTC "today" would tell the
    model it is yesterday for anybody east of Greenwich in the small hours.

    The weekday goes in beside the date because a question can name one —
    "letzten Freitag" — and a model that has to work out which day of the week
    it is will sometimes work it out wrong.
    """
    return SYSTEM.format(today=today.isoformat(), weekday=_WEEKDAYS[today.weekday()])
