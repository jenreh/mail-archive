"""The search form's file filter: which families to offer, and what they mean.

The archive answers a question with no taxonomy in it — *which files do my
messages carry* — and the form asks one with a taxonomy in it: *show me the
messages with a spreadsheet on them*. This module is the two-way join between
them, and it lives here rather than in ``mailarc-core`` because deciding that
``application/vnd.ms-excel`` and ``.xlsx`` are both "a spreadsheet" is a
decision about how a person reads a file list.

Three consequences worth stating, because all three are deliberate.

**The picker offers only what the archive holds.** An option that can return
nothing is worse than a missing option: it reads as an empty archive rather
than as a filter nobody could satisfy. That is the same bargain the **From**
and **To** pickers already strike with
:class:`~mailarc_core.archive.search.KnownAddresses`.

**A file whose sender said nothing is read by its name.** Several mail
programs send ``application/octet-stream`` for every attachment, so an archive
holds invoices under a type that means "some bytes". Classified by type alone
they collapse into one option called *File* — which is how a person picking
**PDF** misses twenty of their own PDFs, while the card on the message calls
each of them *PDF* because it read the filename. The archive hands those files
over by name instead — :attr:`~mailarc_core.archive.search.KnownAttachments`
— and they are classified here exactly as the card classifies them.

**The chosen family is expanded here, not there.** What crosses into
:class:`~mailarc_core.archive.search.SearchFilters` is a tuple of exact content
types and a tuple of exact filenames, so the archive is only ever asked
something it can answer by comparing strings it stores. Nothing in
``mailarc-core`` has to learn what a spreadsheet is, and nothing here has to
learn Cypher.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from mailarc_ui.message_detail import FILE_LABELS, FileKind, file_kind

logger = logging.getLogger(__name__)

KIND_LIMIT = 15
"""How many families a picker may offer.

There are fifteen and an archive that somehow held every one of them should
show every one of them: this is a guard against a list growing a scrollbar, not
a shortening of a list that has a natural length.
"""

UNTYPED = ""
"""The content type an unnamed-type file is classified under.

The empty string is one of :data:`~mailarc_core.mail.model.UNTYPED_CONTENT_TYPES`,
so :func:`~mailarc_ui.message_detail.file_kind` reads the filename — which is
the whole point of the second list, and the same call the attachment card
makes for the same file.
"""


def name_kind(filename: str) -> FileKind:
    """The family a file with no stated type falls into, by its name alone."""
    return file_kind(UNTYPED, filename)


def families_of(
    content_types: Sequence[str], untyped_names: Sequence[str] = ()
) -> list[str]:
    """The families these files fall into, commonest first.

    Order is the archive's own — it hands both lists back ordered by how many
    messages carry them — so the family a person is most likely to want is
    near the top. Stated types come first as a group, because a sender who
    named the type is the stronger evidence and because two orderings cannot
    be interleaved without counts neither list carries.

    Deduplicated as it goes, since several types map to one family and a
    picker offering *Image* three times is a picker nobody trusts.
    """
    seen: list[str] = []
    kinds = [file_kind(one).value for one in content_types]
    kinds += [name_kind(one).value for one in untyped_names]
    for kind in kinds:
        if kind not in seen:
            seen.append(kind)
    return seen[:KIND_LIMIT]


def kind_options(
    content_types: Sequence[str], untyped_names: Sequence[str] = ()
) -> list[dict[str, str]]:
    """What the file picker offers: a value per family, under its own word.

    ``value`` is the :class:`~mailarc_ui.message_detail.FileKind`'s own string
    rather than a content type, because one option stands for several types —
    and, where the sender stated none, for several filenames — and the value
    is what comes back to be expanded again.
    """
    return [
        {"value": kind, "label": FILE_LABELS[FileKind(kind)]}
        for kind in families_of(content_types, untyped_names)
    ]


def types_of(kinds: Iterable[str], content_types: Sequence[str]) -> tuple[str, ...]:
    """The exact content types the picked families stand for, in this archive.

    Only types the archive actually holds, which is what makes the filter an
    ``IN`` over a short list rather than a guess at every type a spreadsheet
    might ever have. A family nobody picked contributes nothing; an empty pick
    contributes nothing at all, which is how "no file filter" is spelled.
    """
    wanted = _picked(kinds)
    if not wanted:
        return ()
    found = tuple(
        content_type
        for content_type in content_types
        if file_kind(content_type).value in wanted
    )
    logger.debug(
        "The file filter picked %d of %d types", len(found), len(content_types)
    )
    return found


def names_of(kinds: Iterable[str], untyped_names: Sequence[str]) -> tuple[str, ...]:
    """The exact filenames the picked families stand for, in this archive.

    :func:`types_of` for the other half of the catalogue: these are the files
    whose sender stated no type, so the name is the only thing that says what
    they are. Whole names and not suffixes — the archive matches what it
    stores, and a suffix match would be the archive deciding what ``.pdf``
    means.
    """
    wanted = _picked(kinds)
    if not wanted:
        return ()
    found = tuple(one for one in untyped_names if name_kind(one).value in wanted)
    logger.debug(
        "The file filter picked %d of %d untyped files", len(found), len(untyped_names)
    )
    return found


def offered_kinds(
    picked: Sequence[str], options: Sequence[dict[str, str]]
) -> list[str]:
    """One pick narrowed to what the page actually offered.

    The guard every picker on this form keeps: the value arrives over the
    socket, and a family this archive holds no files of could only ever answer
    with an empty list — which reads like an empty archive rather than like a
    bad filter. Order is kept as the person picked it and duplicates dropped.
    """
    known = {str(one.get("value", "")) for one in options}
    kept: list[str] = []
    for one in picked:
        if one in known and one not in kept:
            kept.append(one)
    return kept


def _picked(kinds: Iterable[str]) -> set[str]:
    """The families in a pick that are actually families — blanks dropped."""
    return {one for one in kinds if one}
