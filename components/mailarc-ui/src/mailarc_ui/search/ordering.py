"""The order every picker on the search page offers its options in.

One rule, stated once: **alphabetical, by what a person reads**. Five controls
narrow a search — **From**, **To**, **File type**, **Account** and **Group by**
— and each of them used to arrive in the order whichever read produced it. The
archive hands its addresses over busiest-first, the file catalogue commonest
first, and the account list came out of the database already sorted. Three
orders on one form is a form in which finding an option is a different skill
per field, and the two busiest-first lists are the ones it costs most: an
address book is looked up by name, and nobody knows how busy an address is
before they have found it.

The reads keep their own order — busiest-first is what
:func:`~mailarc_ui.search.files.families_of` truncates by and what
:func:`~mailarc_analytics.nlq.resolve.resolve_addresses` reads a mention
against — so this is a presentation step at the last moment, not a change to
what any of them mean.

**One consequence worth stating.** A searchable picker only puts its first
:data:`~mailarc_ui.kit.OPTION_LIMIT` options in the DOM, so on an archive with
more addresses than that, what a person sees *before typing* is now the first
hundred alphabetically rather than the hundred busiest. The search behind the
field still runs over the whole list, which is how any of them are reached.

Sorting folds case and accents, because a list a German archive fills is a list
in which ``Ärzte`` belongs beside ``Anwalt`` and not past ``Zoll`` — which is
where the raw code point puts it.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

LABEL = "label"
"""The key a Mantine option is read under — see :func:`by_label`."""


def alphabetical(values: Sequence[str]) -> list[str]:
    """Plain option strings in reading order. The **From** and **To** lists."""
    return sorted(values, key=sort_key)


def by_label(options: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Mantine options in reading order — by ``label``, never by ``value``.

    The two differ wherever an option is a row id under a name, which is
    exactly the **Account** picker: sorting those by value would order a list
    of mailboxes by the order they were added, under labels that say nothing
    of the sort.

    Read through ``get`` rather than unpacked, the way
    :func:`~mailarc_ui.search.natural._option` reads the same dicts: their
    shape is a Mantine concern, and an option that grew a third key must not
    break an ordering.
    """
    return sorted(options, key=lambda one: sort_key(str(one.get(LABEL, ""))))


def sort_key(value: str) -> tuple[str, str]:
    """How one option is compared: accent-folded and case-folded.

    The second half is the tie-break, so two options that fold together —
    ``Muller`` and ``Müller`` — still land in a fixed order rather than in
    whichever one the read happened to hand over first.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(one for one in decomposed if not unicodedata.combining(one))
    return (folded.casefold(), value.casefold())
