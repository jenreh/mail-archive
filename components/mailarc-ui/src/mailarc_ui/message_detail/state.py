"""The half of a mail page that is one open message, as a state mixin.

A page that shows a mail differs from the next one only in how the list beside
it was filled. A mixin is what lets that half be written once: Reflex copies a
mixin's vars, computed vars and event handlers into every concrete state that
lists it, so each page ends up with its **own** selection, its own tab and its
own trust decision, rather than sharing one substate and with it one open
message.

What a concrete state has to bring is the list. It finds the row a click names
and hands the two things reading needs — the id and the digest of the stored
original — to :meth:`MessageDetailState._open_message`; everything after that,
including both views and the remote-content question, happens here.

Nothing on this mixin is a background handler. A background task takes the
state lock and yields, and a handler copied into two unrelated states would be
two tasks writing the same vars under two locks; the list side, where a slow
read actually belongs, keeps its own.
"""

import asyncio
import logging

import reflex as rx

from mailarc_core.mail import render_message
from mailarc_ui.message_detail.model import (
    TAB_MESSAGE,
    TAB_SOURCE,
    MessageView,
    archive_reader,
    decode_raw,
    frame_document,
)

logger = logging.getLogger(__name__)

_NO_VIEW = MessageView()
"""Nothing chosen yet. A sentinel keeps every component free of `None`."""


class MessageDetailState(rx.State, mixin=True):
    """One open message: what was read, how it is shown, what it may load."""

    selected_id: str = ""
    tab: str = TAB_MESSAGE
    view: MessageView = _NO_VIEW
    remote_allowed: bool = False
    """Whether this selection may fetch remote content — once, or by trust."""
    raw: str = ""
    raw_truncated: bool = False
    loading_raw: bool = False
    message_note: str = ""
    """One quiet sentence about the current message — never a page error."""

    @rx.var
    def has_selection(self) -> bool:
        return self.selected_id != ""

    @rx.var
    def has_html_body(self) -> bool:
        return self.view.body_html != ""

    @rx.var
    def frame_html(self) -> str:
        """The document the sandbox loads, framed under the current policy."""
        if self.view.body_html == "":
            return ""
        return frame_document(self.view.body_html, allow_remote=self.remote_allowed)

    @rx.var
    def remote_blocked(self) -> bool:
        """Whether the bar asking about remote content should be up."""
        return self.view.remote_references > 0 and not self.remote_allowed

    @rx.var
    def remote_notice(self) -> str:
        count = self.view.remote_references
        thing = "remote reference" if count == 1 else "remote references"
        return f"This message wants to load {count} {thing} — blocked."

    @rx.var
    def has_attachments(self) -> bool:
        return len(self.view.attachments) > 0

    @rx.var
    def has_cc(self) -> bool:
        return self.view.cc != ""

    @rx.event
    def show_tab(self, value: str) -> None:
        """Which of the two views is up. The choice outlives the selection."""
        self.tab = value if value in (TAB_MESSAGE, TAB_SOURCE) else TAB_MESSAGE

    @rx.event
    def allow_remote_once(self) -> None:
        """Load the remote content of this one message, this one time."""
        self.remote_allowed = True

    @rx.event
    async def allow_remote_for_sender(self) -> None:
        """Load it now and from this sender always — recorded on the address."""
        address = self.view.sender_address
        if not address:
            return
        try:
            recorded = await asyncio.to_thread(
                archive_reader().trust_remote_content, address
            )
        except Exception:
            logger.exception("Could not record the trust decision for %s", address)
            recorded = False
        if not recorded:
            self.message_note = "The decision could not be stored; allowed once."
        self.remote_allowed = True

    async def _open_message(self, message_id: str, eml_sha256: str) -> None:
        """Show one message — readable, and as it came off the wire.

        Both views come out of one read of the original; the parse runs off
        the event loop beside it, because a big HTML mail is real work.
        """
        self.selected_id = message_id
        self._clear_views()
        if not eml_sha256:
            self._explain("No original was stored for this message.")
            return
        self.loading_raw = True
        try:
            data = await asyncio.to_thread(archive_reader().raw_message, eml_sha256)
            if data is None:
                self._explain("The original of this message is missing from the store.")
                return
            self.raw, self.raw_truncated = decode_raw(data)
            self.view = MessageView.from_rendered(
                await asyncio.to_thread(render_message, data)
            )
            if self.view.remote_references > 0 and self.view.sender_address:
                self.remote_allowed = await asyncio.to_thread(
                    archive_reader().remote_content_trusted, self.view.sender_address
                )
        except Exception as error:
            logger.exception("Could not read message %s", message_id)
            self._explain(f"Could not read the original: {error}")
        finally:
            self.loading_raw = False

    def _explain(self, sentence: str) -> None:
        """Put one sentence where both views would show the message."""
        self.raw = sentence
        self.view = MessageView(body_text=sentence)

    def _clear_views(self) -> None:
        self.raw = ""
        self.raw_truncated = False
        self.view = _NO_VIEW
        self.remote_allowed = False
        self.message_note = ""
