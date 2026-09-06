"""The join between an interpreted question and the form it fills in.

:mod:`mailarc_analytics.nlq` answers in the *archive's* vocabulary — real
addresses, real file families, ISO days. This form speaks its own: an
attachment tri-state spelled ``with``/``without``/``any``, a date field holding
whatever :func:`~mailarc_ui.search.model.parse_date` will read, a file picker
holding family values. Two small translations, and they live here rather than
in the state for the reason :mod:`mailarc_ui.search.model` gives: a pure
function is checkable without instantiating Reflex.

The other half is :func:`understood`, the sentence shown under the **Mode**
segment. It is not decoration and it is not the result's ``notice``: it says
what the *question* was taken to mean, so it has to outlive an answer — a
reader who presses **Load more** has not stopped needing to know that the word
they typed was dropped.

What this module does **not** do is narrow. Every value it produces still goes
through the state's own guards — :func:`~mailarc_ui.search.state._offered`,
:func:`~mailarc_ui.search.files.offered_kinds` — because a model is a second
source of values that arrive from outside, and the one that invents plausible
ones is more dangerous than the browser. See
:meth:`~mailarc_ui.search.state.MailSearchState.submit`.
"""

import logging
from collections.abc import Sequence

from mailarc_analytics.nlq import (
    AccountOption,
    DirectoryEntry,
    FamilyOption,
    ResolvedQuery,
    Vocabulary,
)
from mailarc_core.archive.search import AddressDirectory, KnownAddresses
from mailarc_ui.search.model import ATTACH_ANY, ATTACH_WITH, ATTACH_WITHOUT

logger = logging.getLogger(__name__)


def vocabulary_of(
    directory: AddressDirectory,
    offered: KnownAddresses,
    families: Sequence[dict[str, str]],
    accounts: Sequence[dict[str, str]],
) -> Vocabulary:
    """Everything of this archive's own the interpreter is allowed to see.

    Built from what the page already holds, which is the point: the address
    pickers, the file picker and the account picker are loaded when the page
    opens, and the directory is one extra scan beside them. So the interpreter
    is grounded in exactly the options a person can see, and a reading that
    named something the form cannot show would be a reading the form could not
    display.

    The role split comes from :class:`~mailarc_core.archive.search
    .KnownAddresses` rather than from the directory, because the directory is
    one flat scan and does not know who sent what. A "from" that resolved to an
    address which has only ever *received* mail would be a filter that cannot
    match.
    """
    return Vocabulary(
        senders=_entries(directory, offered.senders),
        recipients=_entries(directory, offered.recipients),
        families=tuple(_option(FamilyOption, one) for one in families),
        accounts=tuple(_option(AccountOption, one) for one in accounts),
    )


def _option[T: FamilyOption | AccountOption](
    kind: type[T], option: dict[str, str]
) -> T:
    """One picker option as the interpreter's value object.

    Read through ``get`` rather than unpacked: these dicts are what Reflex
    holds for a Mantine picker, so their shape is a UI concern and an option
    that ever grew a third key must not break the reading.
    """
    return kind(value=str(option.get("value", "")), label=str(option.get("label", "")))


def _entries(
    directory: AddressDirectory, addresses: Sequence[str]
) -> tuple[DirectoryEntry, ...]:
    """One role's addresses with their names, in the role's own order."""
    return tuple(
        DirectoryEntry(address=one.address, names=one.names, domain=one.domain)
        for one in directory.named(addresses)
    )


def attachment_segment(has_attachments: bool | None) -> str:
    """The tri-state as this form spells it.

    ``None`` is "either" and is a real answer rather than an absent one, which
    is why the segment has three positions and not a checkbox.
    """
    if has_attachments is True:
        return ATTACH_WITH
    if has_attachments is False:
        return ATTACH_WITHOUT
    return ATTACH_ANY


def understood(query: ResolvedQuery) -> str:
    """What the reading did, in one sentence, or nothing when it is obvious.

    Empty is the common case and deliberately so. The filled-in fields already
    show what was searched — that is the whole design — so a sentence restating
    them would be noise a reader learns to skip, and then misses the one time
    it matters.

    What it does say is everything the fields **cannot** show: a name the
    archive had no address for, a date refused for being outside its range, the
    words dropped to answer a "wann war die letzte". Each of those is a way the
    question was not honoured as asked, and none of them is visible in a form
    that simply looks narrower than expected.
    """
    said = list(query.notes)
    if query.unresolved:
        named = ", ".join(repr(one) for one in query.unresolved)
        said.append(
            f"No archived address or mailbox matches {named}, "
            "so I searched for the words instead."
        )
    return " ".join(said)
