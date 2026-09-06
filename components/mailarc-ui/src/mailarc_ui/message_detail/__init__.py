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
"""

from mailarc_ui.message_detail.components import (
    COLUMN,
    GROW,
    PREVIEW_FRAME,
    ROW_BORDER,
    TABS_STYLES,
    attachment_panel,
    message_body,
    message_header,
    message_pane,
    message_tabs,
    message_view,
    raw_message_view,
    remote_content_bar,
)
from mailarc_ui.message_detail.model import (
    CHIP_ICONS,
    CHIP_KINDS,
    DRAWABLE_CHIPS,
    FRAME_CSP,
    GRAPH_GLYPH,
    FRAME_CSP_REMOTE,
    FRAME_STYLE,
    LABEL_COLORS,
    NO_FILENAME,
    NO_SUBJECT,
    RAW_LIMIT,
    TAB_GRAPH,
    TAB_MESSAGE,
    TAB_SOURCE,
    AddressRow,
    AttachmentRow,
    ChipKind,
    LabelChip,
    MessageChip,
    MessageView,
    address_label,
    address_rows,
    address_suffix,
    archive_reader,
    chips_for,
    decode_raw,
    frame_document,
    label_text,
    group_chip,
    long_date_label,
    sender_label,
    size_label,
    tag_chip,
    topic_chip,
)
from mailarc_ui.message_detail.filetype import (
    EXTENSION_KINDS,
    FILE_ICONS,
    FILE_LABELS,
    TYPE_KINDS,
    FileKind,
    file_kind,
    type_label,
)
from mailarc_ui.message_detail.preview import (
    PREVIEW_LIMIT,
    PREVIEW_OF_KIND,
    TEXT_LIMIT,
    AttachmentPreview,
    PreviewKind,
    data_uri,
    pretty_json,
    preview_kind,
    preview_of,
)
from mailarc_ui.message_detail.contentline import ContentLine, read_lines, unfold
from mailarc_ui.message_detail.invite import (
    ATTENDEE_LIMIT,
    EVENT_LIMIT,
    NO_TITLE,
    CalendarEvent,
    CalendarInvite,
    CalendarPerson,
    read_invite,
    recurrence_label,
    when_label,
)
from mailarc_ui.message_detail.reads import chips_of
from mailarc_ui.message_detail.records import calendar_preview, contact_preview
from mailarc_ui.message_detail.vcard import (
    CARD_LIMIT,
    NO_NAME,
    ContactCard,
    ContactDetail,
    ContactFile,
    read_contacts,
)
from mailarc_ui.message_detail.state import MessageDetailState

__all__ = [
    "ATTENDEE_LIMIT",
    "CARD_LIMIT",
    "CHIP_ICONS",
    "CHIP_KINDS",
    "COLUMN",
    "DRAWABLE_CHIPS",
    "EVENT_LIMIT",
    "EXTENSION_KINDS",
    "FILE_ICONS",
    "FILE_LABELS",
    "FRAME_CSP",
    "FRAME_CSP_REMOTE",
    "FRAME_STYLE",
    "GRAPH_GLYPH",
    "GROW",
    "LABEL_COLORS",
    "NO_FILENAME",
    "NO_NAME",
    "NO_SUBJECT",
    "NO_TITLE",
    "PREVIEW_FRAME",
    "PREVIEW_LIMIT",
    "PREVIEW_OF_KIND",
    "RAW_LIMIT",
    "ROW_BORDER",
    "TABS_STYLES",
    "TAB_GRAPH",
    "TAB_MESSAGE",
    "TAB_SOURCE",
    "TEXT_LIMIT",
    "TYPE_KINDS",
    "AddressRow",
    "AttachmentPreview",
    "AttachmentRow",
    "CalendarEvent",
    "CalendarInvite",
    "CalendarPerson",
    "ChipKind",
    "ContactCard",
    "ContactDetail",
    "ContactFile",
    "ContentLine",
    "FileKind",
    "LabelChip",
    "MessageChip",
    "MessageDetailState",
    "MessageView",
    "PreviewKind",
    "address_label",
    "address_rows",
    "address_suffix",
    "archive_reader",
    "attachment_panel",
    "calendar_preview",
    "chips_for",
    "chips_of",
    "contact_preview",
    "data_uri",
    "decode_raw",
    "file_kind",
    "frame_document",
    "group_chip",
    "label_text",
    "long_date_label",
    "message_body",
    "message_header",
    "message_pane",
    "message_tabs",
    "message_view",
    "pretty_json",
    "preview_kind",
    "preview_of",
    "raw_message_view",
    "read_contacts",
    "read_invite",
    "read_lines",
    "recurrence_label",
    "remote_content_bar",
    "sender_label",
    "size_label",
    "tag_chip",
    "topic_chip",
    "type_label",
    "unfold",
    "when_label",
]
