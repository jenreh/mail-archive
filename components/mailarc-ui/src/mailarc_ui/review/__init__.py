"""Looking at what the import wrote: the synced messages, and one original.

``state``
    ``MessageReviewState`` over ``mailarc_core``'s ``ArchiveReader``, plus the
    row it projects a summary onto — no graph node ever reaches the browser.
    Reading one message is ``mailarc_ui.message_detail``'s mixin; this state
    brings the list and the click that opens a row.
``components``
    The panel a page drops in: a mail-client list, and beside it the shared
    reading pane bound to this page's state — rendered like a client would,
    and as its raw source.

What this package exports has not changed with that split: the pane's
functions and the names that moved out of ``state`` are re-exported here, so
``from mailarc_ui.review import frame_document`` still resolves.
"""

from mailarc_ui.message_detail import (
    message_tabs,
    message_view,
    raw_message_view,
    remote_content_bar,
)
from mailarc_ui.review.components import (
    message_list,
    review_panel,
)
from mailarc_ui.review.state import (
    AttachmentRow,
    LabelChip,
    MessageReviewState,
    MessageRow,
    MessageView,
    archive_reader,
    date_label,
    decode_raw,
    frame_document,
)

__all__ = [
    "AttachmentRow",
    "LabelChip",
    "MessageReviewState",
    "MessageRow",
    "MessageView",
    "archive_reader",
    "date_label",
    "decode_raw",
    "frame_document",
    "message_list",
    "message_tabs",
    "message_view",
    "raw_message_view",
    "remote_content_bar",
    "review_panel",
]
