"""Reflex state for looking at what the import wrote: a list, and one original.

The archive answers through :class:`~mailarc_core.ArchiveReader`, which the
composition root builds and leaves in the service registry — ``mailarc-ui``
may not import ``app`` (§4.1), so it reads the reader out the same way the
accounts page reads its providers, and never at import time.

What is left here is the **list**: a page of newest-first summaries, the count
beside it, and the click that turns a row into an open message. Everything the
open message then does — both views, the tabs, the remote-content question —
is :class:`~mailarc_ui.message_detail.MessageDetailState`, because the search
page reads a mail exactly the same way and only fills its list differently.

Every name that moved is re-exported below, so ``from mailarc_ui.review.state
import date_label`` still means what it did.
"""

import asyncio
import logging
from datetime import UTC, datetime

import reflex as rx

from mailarc_core.archive.model import MessageSummary
from mailarc_ui.message_detail.model import (
    FRAME_CSP,
    FRAME_CSP_REMOTE,
    FRAME_INK,
    FRAME_PAPER,
    FRAME_STYLE,
    LABEL_COLORS,
    NO_SUBJECT,
    RAW_LIMIT,
    TAB_MESSAGE,
    TAB_SOURCE,
    YESTERDAY,
    AttachmentRow,
    LabelChip,
    MessageRow,
    MessageView,
    address_label,
    archive_reader,
    date_label,
    decode_raw,
    frame_document,
    label_text,
    long_date_label,
    size_label,
)
from mailarc_ui.message_detail.state import MessageDetailState

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
"""How many rows one read brings in. The list scrolls; "more" appends a page."""

__all__ = [
    "FRAME_CSP",
    "FRAME_CSP_REMOTE",
    "FRAME_INK",
    "FRAME_PAPER",
    "FRAME_STYLE",
    "LABEL_COLORS",
    "NO_SUBJECT",
    "PAGE_SIZE",
    "RAW_LIMIT",
    "TAB_MESSAGE",
    "TAB_SOURCE",
    "YESTERDAY",
    "AttachmentRow",
    "LabelChip",
    "MessageDetailState",
    "MessageReviewState",
    "MessageRow",
    "MessageView",
    "address_label",
    "archive_reader",
    "date_label",
    "decode_raw",
    "frame_document",
    "label_text",
    "long_date_label",
    "size_label",
]
"""The moved names as well as this module's own.

A re-export rather than a redirect: the names were here first, half the tests
and every existing import spell them ``mailarc_ui.review.state``, and moving a
file is not a reason to make a caller learn a second path to the same object.
"""


class MessageReviewState(MessageDetailState, rx.State):
    """The review page: the list on the left, the chosen original on the right.

    The mixin comes first in the bases, so what it defines is copied into this
    state before ``rx.State`` makes it a real one.
    """

    messages: list[MessageRow] = []
    total: int = 0
    loading: bool = False
    error: str = ""

    @rx.var
    def has_messages(self) -> bool:
        return len(self.messages) > 0

    @rx.var
    def has_more(self) -> bool:
        return len(self.messages) < self.total

    @rx.var
    def count_label(self) -> str:
        if self.total == 0:
            return "No messages"
        shown = len(self.messages)
        return f"{shown} of {self.total}" if shown < self.total else f"{self.total}"

    @rx.event
    async def load(self) -> None:
        """Start over: the first page and the count. The page's ``on_load``."""
        self.error = ""
        self.loading = True
        try:
            reader = archive_reader()
            summaries, self.total = await asyncio.gather(
                asyncio.to_thread(reader.list_messages, limit=PAGE_SIZE, offset=0),
                asyncio.to_thread(reader.count_messages),
            )
            self.messages = self._rows(summaries)
            if self.selected_id not in {row.id for row in self.messages}:
                self._clear_selection()
        except Exception as error:
            logger.exception("Could not list the archive")
            self.error = str(error) or type(error).__name__
        finally:
            self.loading = False

    @rx.event
    async def load_more(self) -> None:
        """Append the next page; the list keeps what it has."""
        if self.loading or not self.has_more:
            return
        self.loading = True
        try:
            summaries = await asyncio.to_thread(
                archive_reader().list_messages,
                limit=PAGE_SIZE,
                offset=len(self.messages),
            )
            self.messages = [*self.messages, *self._rows(summaries)]
        except Exception as error:
            logger.exception("Could not list more of the archive")
            self.error = str(error) or type(error).__name__
        finally:
            self.loading = False

    @rx.event
    async def select(self, message_id: str) -> None:
        """Open the message a row names; an id from nowhere is ignored."""
        row = next((one for one in self.messages if one.id == message_id), None)
        if row is None:
            return
        await self._open_message(message_id, row.eml_sha256)

    def _rows(self, summaries: list[MessageSummary]) -> list[MessageRow]:
        now = datetime.now(UTC)
        return [MessageRow.from_summary(one, now) for one in summaries]

    def _clear_selection(self) -> None:
        self.selected_id = ""
        self._clear_views()
