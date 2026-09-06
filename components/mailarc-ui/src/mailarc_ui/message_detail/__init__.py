"""One open message: the half of a mail page that is the same on both of them.

``model``
    The views a header, a chip or an attachment is projected onto — no graph
    node ever reaches the browser — plus the formatting that gets a datetime,
    a label name or a byte count printable, and the document a mail's HTML is
    framed in before a sandbox loads it. Knows no I/O.
``state``
    ``MessageDetailState``, a **mixin**: selection, tabs, the two views and the
    remote-content decision. A page's own state lists it and brings the list;
    Reflex copies the vars and handlers in, so every page gets its own copy of
    them rather than sharing one open message.
``filetype``
    What kind of file an attachment is: its family, its glyph and what to call
    it. One table, read by the header card, the preview panel and the search
    form's file filter, so none of the three can disagree about one file.
``preview``
    What one attachment becomes when a reader opens it — the shapes a browser
    can draw a file in, and the size past which it draws none. Knows no I/O
    either.
``contentline``
    The ``NAME;PARAM=value:value`` grammar iCalendar and vCard share, read
    once for both. Knows nothing about either format's properties.
``invite``
    A ``.ics`` read into the meeting it describes — when, where, who, and
    whether it is still on.
``vcard``
    A ``.vcf`` read into the people it describes. Drops a ``PHOTO`` that is a
    URL rather than fetching one.
``records``
    The two of those laid out — the only preview shapes that draw fields
    rather than the file.
``components``
    The pane itself, each function taking the concrete state class as its
    argument — ``message_pane(MailSearchState)`` on the search page.

It is a package rather than a module because reading a mail is not the search's
own business: the pane takes whichever state lists the mixin, so a second page
that wants a reading pane brings its own list and nothing else.

The names re-exported below are the ones *other packages* use, and no more.
This list was eighty-five for a while, which is not a public surface — it is
every name the package happens to define, and it made every internal rename a
change to something four other packages could be reading. The eighty-five were
kept alive almost entirely by tests importing through here; a test is inside
the wall it is testing and reaches for the module it means, so
``test_ui_message_preview.py`` asks ``message_detail.preview`` and
``test_ui_message_filetype.py`` asks ``message_detail.filetype``. What is left
is what ``graph``, ``search``, ``accounts`` and ``repair`` actually import.
"""

from mailarc_ui.message_detail.components import (
    COLUMN,
    ROW_BORDER,
    message_pane,
)
from mailarc_ui.message_detail.filetype import FILE_LABELS, FileKind, file_kind
from mailarc_ui.message_detail.model import ChipKind, LabelChip, archive_reader
from mailarc_ui.message_detail.state import MessageDetailState

__all__ = [
    "COLUMN",
    "FILE_LABELS",
    "ROW_BORDER",
    "ChipKind",
    "FileKind",
    "LabelChip",
    "MessageDetailState",
    "archive_reader",
    "file_kind",
    "message_pane",
]
