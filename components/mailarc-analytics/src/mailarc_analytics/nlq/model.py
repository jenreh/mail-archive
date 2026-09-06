"""What a question means, and what it means once the archive has been consulted.

Value objects and one schema transform. No I/O, no session, no provider — the
same promise :mod:`mailarc_analytics.semantic.model` makes, so a state can hold
an interpretation after the client that produced it is gone.

Two objects and the pipeline between them:

:class:`QueryIntent` is what the **model** answers — a reading of the sentence
and nothing else. Every entity in it is still the words the person used:
``from_mentions`` holds ``"Hausverwaltung Santak"``, not an address, because the
model was never shown one. That is the privacy boundary of this whole feature
and it is enforced by the shape of this class: there is no field here that
could carry something out of the archive, so there is nothing for a prompt to
put in.

:class:`ResolvedQuery` is what :mod:`mailarc_analytics.nlq.resolve` makes of it
once the archive's own vocabulary is laid beside it — real addresses, real file
families, real dates. It also carries what it could **not** resolve, which is
not decoration: a mention that vanished silently would turn "everything from my
property manager" into "everything", and the caller has no other way to know
that happened.

:class:`QueryIntent` is deliberately **flat** — scalars and ``list[str]``, no
nested models. Pydantic emits ``$defs``/``$ref`` for a nested model or an enum
field, and while OpenAI's structured output resolves those, Ollama's ``format``
does so unevenly. :func:`strict_schema` inlines them for exactly that reason,
and a flat class is what keeps that transform small enough to trust.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Period(StrEnum):
    """A relative stretch of time, as a name rather than as two dates.

    The model classifies into this closed set and
    :func:`~mailarc_analytics.nlq.resolve.window_of` does the arithmetic. That
    split is the whole point: asking a model for the first day of last quarter
    is asking it to do calendar arithmetic, which it gets wrong often enough to
    return a silently wrong window — and a wrong window looks exactly like a
    right one that found nothing.

    An *explicit* date ("im März 2024", "seit dem 1.5.") is different and stays
    the model's job: it is reading, not arithmetic, and
    :attr:`QueryIntent.after` is where it lands. Where both are present the
    explicit dates win.
    """

    NONE = ""
    """No period was named. The default, and not a filter."""

    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_QUARTER = "this_quarter"
    LAST_QUARTER = "last_quarter"
    THIS_YEAR = "this_year"
    LAST_YEAR = "last_year"
    LAST_12_MONTHS = "last_12_months"


class Order(StrEnum):
    """Whether the question is about the best match or the most recent one.

    Not a sort field —
    :class:`~mailarc_core.archive.search.SearchFilters` has none and this
    feature does not add one. It is a reading of the *question*, and what it
    decides is which of
    :meth:`~mailarc_core.ArchiveReader.search_messages`' three branches
    answers: a filter carrying text is ranked by relevance, and a filter
    carrying only structure is ordered newest first. So "wann war die letzte
    Rechnung" is honoured by putting the sender in the filter and leaving the
    words out — which loses the words, and is why
    :attr:`ResolvedQuery.notes` has to say so.
    """

    RELEVANCE = "relevance"
    """The default. The best matches for the words, in the index's order."""

    NEWEST = "newest"
    """"letzte", "neueste", "wann" — the most recent one is the answer."""


class QueryIntent(BaseModel):
    """One question as the model read it, before the archive is consulted.

    Every field is answered on every call: :func:`strict_schema` puts them all
    in ``required``, because OpenAI's strict mode has no other way to express
    optionality and a model choosing which keys to send is a model that sends
    a different shape each time. An unset field is its empty value, never a
    missing key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(
        default="",
        description=(
            "The words to search the message subject and body for, with the "
            "sender, date and attachment parts of the question removed. Empty "
            "if the question names no subject matter."
        ),
    )
    from_mentions: list[str] = Field(
        default_factory=list,
        description=(
            "Who the message came from, exactly as the question names them - "
            "a person, a company or a domain. Never invent an email address."
        ),
    )
    to_mentions: list[str] = Field(
        default_factory=list,
        description="Who the message was sent to, as the question names them.",
    )
    period: Period = Field(
        default=Period.NONE,
        description=(
            "A relative stretch of time the question names, as one of the "
            "listed names. Empty if it names none or names exact dates."
        ),
    )
    after: str = Field(
        default="",
        description=(
            "The earliest day to include, as YYYY-MM-DD, when the question "
            "names an explicit date. Empty otherwise."
        ),
    )
    before: str = Field(
        default="",
        description="The latest day to include, as YYYY-MM-DD. Empty otherwise.",
    )
    has_attachments: bool | None = Field(
        default=None,
        description=(
            "True if the question asks for messages that carry files, false if "
            "it asks for ones that carry none, null if it does not say."
        ),
    )
    attachment_words: list[str] = Field(
        default_factory=list,
        description=(
            "The kind of file the question asks for, in the question's own "
            "words - for example Rechnung, PDF, Tabelle, Bilder."
        ),
    )
    account_mention: str = Field(
        default="",
        description="The mailbox the question names, if it names one.",
    )
    order: Order = Field(
        default=Order.RELEVANCE,
        description=(
            "newest when the question asks for the most recent or the last "
            "one - wann, letzte, neueste, last, latest. relevance otherwise."
        ),
    )


class FamilyOption(BaseModel):
    """One entry of the file-type picker, as the resolver is shown it.

    A value and a label, which is exactly the shape the search form's picker
    already holds. The taxonomy behind it — that ``application/vnd.ms-excel``
    and ``.xlsx`` are both *a spreadsheet* — stays where
    :mod:`mailarc_ui.search.files` argues it belongs, and this package is
    handed the result rather than learning the rule. So the interpreter is
    matching against the picker's own options and the two cannot disagree.
    """

    model_config = ConfigDict(frozen=True)

    value: str
    label: str = ""


class AccountOption(BaseModel):
    """One mailbox, as the account picker lists it. The same bargain again."""

    model_config = ConfigDict(frozen=True)

    value: str
    label: str = ""


class DirectoryEntry(BaseModel):
    """One address a question may be talking about, and what it calls itself.

    :attr:`names` is why this exists at all. "Hausverwaltung Santak" is a
    display name, not an address, and
    :attr:`~mailarc_core.archive.search.SearchFilters.senders` matches an
    address **exactly** — so without the names a question naming a company by
    the only name its mail ever shows resolves to nothing.
    """

    model_config = ConfigDict(frozen=True)

    address: str
    names: tuple[str, ...] = ()
    domain: str = ""


class Vocabulary(BaseModel):
    """Everything of this archive's own the resolver is allowed to see.

    Passed in rather than read, and that is a layering statement as much as a
    testing one: this package may not open a graph session of the caller's, and
    the caller already holds all three lists — the search page loads them when
    it opens. It also makes :mod:`mailarc_analytics.nlq.resolve` a pure
    function of its arguments, so the German cases that carry this feature test
    without a model and without a graph.
    """

    model_config = ConfigDict(frozen=True)

    senders: tuple[DirectoryEntry, ...] = ()
    """Addresses that have sent something, with the names they signed."""

    recipients: tuple[DirectoryEntry, ...] = ()
    """Addresses that have received something."""

    families: tuple[FamilyOption, ...] = ()
    accounts: tuple[AccountOption, ...] = ()


class ResolvedQuery(BaseModel):
    """A question after the archive has been laid beside it.

    Everything here is one of the archive's own values, so every field can be
    written into the search form as-is and every one of them is visible to the
    person who asked. That visibility is the deal: an interpretation that could
    not be seen would be a search that could not be corrected.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    senders: tuple[str, ...] = ()
    recipients: tuple[str, ...] = ()
    after: str = ""
    """The earliest day, ``YYYY-MM-DD``, as the form's date field holds it.

    A string and not a ``datetime`` on purpose: the bound has to travel through
    :func:`~mailarc_ui.search.model.filters_of`, which is where ``_end_of_day``
    is applied to the upper one. A ``sent_until`` at midnight silently drops
    everything sent on the day that was asked for, and bypassing that function
    would re-implement the fix.
    """

    before: str = ""
    has_attachments: bool | None = None
    families: tuple[str, ...] = ()
    account_id: str = ""
    order: Order = Order.RELEVANCE

    unresolved: tuple[str, ...] = ()
    """The mentions the archive had nothing to match.

    Never silently dropped: "von meiner Hausverwaltung Santak" with no matching
    address must not become "show me everything". The resolver puts these back
    into :attr:`text` and names them here so the page can say what it did.
    """

    notes: tuple[str, ...] = ()
    """What was done that the filled-in form does not show on its own.

    The words dropped to honour :attr:`Order.NEWEST`, a date refused for being
    outside the archive's plausible range, a file word nothing matched. Each is
    a sentence, shown as written.
    """


_NOISE = frozenset({"title", "default"})
"""Keywords stripped on the way out, because the reader is a model.

``title`` is pydantic restating the field name it sits next to. ``default`` is
meaningless once :func:`strict_schema` has put every property in ``required``,
and OpenAI's strict mode lists it among the keywords it does not support — so
leaving it in risks a 400 for a value that could not have been used.
"""


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """One JSON Schema both providers accept, from the pydantic model.

    Three transforms, and each one is a provider requirement rather than a
    preference:

    **Every ``$ref`` is inlined and ``$defs`` is dropped.** A field typed as an
    enum makes pydantic emit a reference; OpenAI resolves those and Ollama's
    ``format`` does not do so reliably. Inlining costs nothing here because
    :class:`QueryIntent` is flat by construction.

    **Every object gets ``additionalProperties: false``.** OpenAI's
    ``strict: true`` refuses a schema without it, on every object, including
    nested ones.

    **Every object's ``required`` lists all of its properties.** Strict mode
    has no notion of an optional property, so optionality is expressed as a
    nullable type instead — which is why :class:`QueryIntent` spells "not
    answered" as an empty string or ``null`` rather than as a missing key.

    One function for all three adapters. Two spellings of this — one for
    Ollama's ``format`` and one for OpenAI's ``json_schema`` — would drift, and
    only whichever provider the author happened to run would be tested.
    """
    schema = _strict(_inlined(model.model_json_schema()))
    # The root's description is the class docstring — written for a developer,
    # Sphinx roles and all. Every *field* carries a description written for the
    # model instead, and the system prompt says what the object as a whole is.
    schema.pop("description", None)
    return schema


def _inlined(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema with every ``$ref`` replaced by what it points at."""
    defs = dict(schema.get("$defs", {}))
    body = {key: value for key, value in schema.items() if key != "$defs"}
    return _resolve(body, defs, depth=0)


def _resolve(node: Any, defs: Mapping[str, Any], *, depth: int) -> Any:
    """One node with its references replaced, recursively.

    ``depth`` is a guard rather than a feature: a self-referential model would
    otherwise recurse until the interpreter gave up, and this package's own
    models cannot nest more than a couple of levels. Past the bound the node is
    left as it stands, which produces a schema a provider will reject by name
    instead of a crash with no explanation.
    """
    if depth > 12:
        return node
    if isinstance(node, Mapping):
        siblings = {
            key: _resolve(value, defs, depth=depth + 1)
            for key, value in node.items()
            if key not in {"$ref", "$defs"} and key not in _NOISE
        }
        target = node.get("$ref")
        if isinstance(target, str) and target.startswith("#/$defs/"):
            found = defs.get(target.removeprefix("#/$defs/"))
            if found is not None:
                # Siblings last: pydantic emits the *field's* description
                # beside the reference and the referenced class's docstring
                # inside it, and the field's is the one written for the model.
                return {**_resolve(found, defs, depth=depth + 1), **siblings}
        return siblings
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_resolve(one, defs, depth=depth + 1) for one in node]
    return node


def _strict(node: Any) -> Any:
    """One node with strict mode's two object rules applied throughout."""
    if isinstance(node, Mapping):
        out = {key: _strict(value) for key, value in node.items()}
        properties = out.get("properties")
        if isinstance(properties, Mapping):
            out["additionalProperties"] = False
            out["required"] = list(properties)
        return out
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_strict(one) for one in node]
    return node
