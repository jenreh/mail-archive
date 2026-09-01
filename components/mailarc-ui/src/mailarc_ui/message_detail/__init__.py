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
``components``
    The pane itself, each function taking the concrete state class as its
    argument — ``message_tabs(MailSearchState)`` on the search page.

It is a package rather than a module because reading a mail is not the search's
own business: the pane takes whichever state lists the mixin, so a second page
that wants a reading pane brings its own list and nothing else.
"""

from mailarc_ui.message_detail.components import (
    COLUMN,
    GROW,
    ROW_BORDER,
    TABS_STYLES,
    message_body,
    message_header,
    message_tabs,
    message_view,
    raw_message_view,
    remote_content_bar,
)
from mailarc_ui.message_detail.model import (
    FRAME_CSP,
    FRAME_CSP_REMOTE,
    FRAME_STYLE,
    LABEL_COLORS,
    NO_SUBJECT,
    RAW_LIMIT,
    TAB_MESSAGE,
    TAB_SOURCE,
    AttachmentRow,
    LabelChip,
    MessageView,
    address_label,
    archive_reader,
    decode_raw,
    frame_document,
    label_text,
    long_date_label,
    size_label,
)
from mailarc_ui.message_detail.state import MessageDetailState

__all__ = [
    "COLUMN",
    "FRAME_CSP",
    "FRAME_CSP_REMOTE",
    "FRAME_STYLE",
    "GROW",
    "LABEL_COLORS",
    "NO_SUBJECT",
    "RAW_LIMIT",
    "ROW_BORDER",
    "TABS_STYLES",
    "TAB_MESSAGE",
    "TAB_SOURCE",
    "AttachmentRow",
    "LabelChip",
    "MessageDetailState",
    "MessageView",
    "address_label",
    "archive_reader",
    "decode_raw",
    "frame_document",
    "label_text",
    "long_date_label",
    "message_body",
    "message_header",
    "message_tabs",
    "message_view",
    "raw_message_view",
    "remote_content_bar",
    "size_label",
]
