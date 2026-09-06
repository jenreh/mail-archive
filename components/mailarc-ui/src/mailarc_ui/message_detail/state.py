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
    TAB_GRAPH,
    TAB_MESSAGE,
    TAB_SOURCE,
    MessageChip,
    MessageView,
    address_suffix,
    archive_reader,
    decode_raw,
    frame_document,
)
from mailarc_ui.message_detail.preview import (
    PREVIEW_FAILED,
    AttachmentPreview,
    preview_of,
)
from mailarc_ui.message_detail.reads import chips_of

logger = logging.getLogger(__name__)

_NO_VIEW = MessageView()
_NO_PREVIEW = AttachmentPreview()
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

    chips: list[MessageChip] = []
    """What the open message belongs to: its topic, its tags, its group."""

    chip_open: bool = False
    """Whether the third tab is there at all — a chip has been pressed and
    not yet dropped by opening another message."""

    chip_kind: str = ""
    chip_id: str = ""
    """The chip that tab stands for. Two vars and not the
    :class:`~mailarc_ui.message_detail.model.MessageChip` itself: what the
    hook needs is what to root a picture at, and a page that could draw one
    from a stale chip is a page holding two answers to one question."""

    chip_label: str = ""
    """What the tab is called — the chip's own name, so the strip reads
    ``Message | Source | Rechnungen`` rather than naming a kind."""

    eml_sha256: str = ""
    """The digest of the original this selection was read from.

    Kept because opening an attachment reads the message a second time: the
    payloads are megabytes of bytes and belong in neither
    :class:`~mailarc_ui.message_detail.model.MessageView` nor the socket that
    carries it, so the strip lists names and sizes and the file itself is
    fetched when somebody actually asks for one.
    """

    preview: AttachmentPreview = _NO_PREVIEW
    preview_open: bool = False
    preview_loading: bool = False
    """The attachment the slide-in panel is showing, and whether it is up.

    Three vars rather than a nullable one, for the reason ``chip_open`` is
    separate from ``chip_id``: the panel animates out, so what it holds has to
    survive the frame in which it closes, and a component reading a sentinel
    reads it without a guard.
    """

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
    def sender_address_line(self) -> str:
        """The grey ``<address>`` the header sets after the sender's name."""
        return address_suffix(self.view.sender_name, self.view.sender_address)

    @rx.var
    def has_attachments(self) -> bool:
        return len(self.view.attachments) > 0

    @rx.var
    def preview_note_shown(self) -> bool:
        """Whether the panel has a sentence to print under its heading."""
        return self.preview.note != ""

    @rx.var
    def has_cc(self) -> bool:
        return len(self.view.cc) > 0

    @rx.event
    def show_tab(self, value: str) -> None:
        """Which of the views is up. The choice outlives the selection.

        The graph tab is only a tab while a chip is open, so it is accepted
        only then: a stored ``?tab=graph`` from a message that had one would
        otherwise select a panel that is not on the strip.
        """
        allowed = [TAB_MESSAGE, TAB_SOURCE]
        if self.chip_open:
            allowed.append(TAB_GRAPH)
        self.tab = value if value in allowed else TAB_MESSAGE

    @rx.event
    async def show_chip_graph(self, kind: str, chip_id: str, label: str) -> None:
        """Draw what one of the header's chips names, in a tab of its own.

        The tab is opened before the read, so a slow graph shows an empty
        canvas under the right heading rather than nothing at all under the
        message. What fills it is :meth:`_load_chip_graph`, which does nothing
        here — see the hook.
        """
        if not chip_id:
            return
        self.chip_kind, self.chip_id, self.chip_label = kind, chip_id, label
        self.chip_open = True
        self.tab = TAB_GRAPH
        await self._load_chip_graph()

    async def _load_chip_graph(self) -> None:
        """Read the picture the open chip stands for. Nothing here, on purpose.

        The reading pane may not know what a graph is: drawing one needs
        :mod:`mailarc_ui.graph`, and that package already reads *this* one for
        its own reading column, so an import in this direction would close the
        circle. The hook is what keeps the arrow pointing one way —
        :class:`~mailarc_ui.graph.ChipGraphState` overrides it, and a state
        that lists that mixin instead of this one draws chips as well as
        showing them. The same shape ``GraphExplorerState._cluster_members``
        has, for the same reason.
        """
        return

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

    @rx.event
    async def show_attachment(self, index: int) -> None:
        """Open the file the strip's nth card names, in the panel beside it.

        The panel goes up before the read, so a scanned invoice shows its own
        name over a spinner rather than nothing at all: the reader has already
        pressed and is owed the acknowledgement even while the bytes are still
        coming off disk.

        The index arrives over the socket and is therefore checked against what
        is on screen rather than trusted — the same guard the list keeps for a
        message id. Everything else that can go wrong ends as a sentence in
        the panel, because a file that will not open is not a broken page.
        """
        if not 0 <= index < len(self.view.attachments):
            return
        row = self.view.attachments[index]
        self.preview = AttachmentPreview(
            filename=row.filename,
            content_type=row.content_type,
            size_label=row.size_label,
        )
        self.preview_open = True
        self.preview_loading = True
        try:
            self.preview = await asyncio.to_thread(self._read_attachment, index)
        except Exception:
            logger.exception(
                "Could not open attachment %d of %s", index, self.selected_id
            )
            self.preview = self.preview.model_copy(update={"note": PREVIEW_FAILED})
        finally:
            self.preview_loading = False

    def _read_attachment(self, index: int) -> AttachmentPreview:
        """The nth file of the open message, off the event loop.

        One function so that the read, the parse and the projection all happen
        on the worker thread — reading a stored original and rendering it is
        the same work opening the message was, and none of it belongs on the
        loop.
        """
        data = archive_reader().raw_message(self.eml_sha256)
        if data is None:
            return self.preview.model_copy(update={"note": PREVIEW_FAILED})
        attachments = render_message(data).attachments
        if index >= len(attachments):
            return self.preview.model_copy(update={"note": PREVIEW_FAILED})
        return preview_of(attachments[index])

    @rx.event
    def close_attachment(self) -> None:
        """Put the panel away. What it held stays until the next file opens,
        so nothing blinks out from under the closing animation."""
        self.preview_open = False

    async def _open_message(self, message_id: str, eml_sha256: str) -> None:
        """Show one message — readable, and as it came off the wire.

        Both views come out of one read of the original; the parse runs off
        the event loop beside it, because a big HTML mail is real work.
        """
        self.selected_id = message_id
        self.eml_sha256 = eml_sha256
        self._clear_views()
        self.chips = await asyncio.to_thread(chips_of, message_id)
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
        self.chips = []
        self.preview = _NO_PREVIEW
        self.preview_open = False
        self.preview_loading = False
        self._close_chip()

    def _close_chip(self) -> None:
        """Drop the chip tab, and the tab bar with it if that is what was up.

        A new message means the topic on the strip is another message's topic.
        The tab goes, and a reader who was looking at it lands on the message
        rather than on a panel that is no longer there.
        """
        self.chip_open = False
        self.chip_kind = self.chip_id = self.chip_label = ""
        if self.tab == TAB_GRAPH:
            self.tab = TAB_MESSAGE
