"""The document viewers — a deck, a workbook and a Word document from bytes.

Shaped exactly like :mod:`mailarc_ui.kit.graph`, and for the same reasons. The
wrapper classes stay private because their props are a contract with one JSX
file rather than a vocabulary pages should learn; what the kit exports are the
three builders, each of which puts its viewer in a box with a height. A viewer
with no height measures its container once, finds nought pixels, and draws
nothing.

All three are ``NoSSRComponent``. Rendered on a server they would throw — each
touches ``document`` and instantiates a WebAssembly runtime at construction —
and each is roughly a megabyte that only an opened attachment should pay for.
They arrive as their own lazy chunks, so a reader who never opens a workbook
never fetches one.

One module for three wrappers because they are one idea with three
substitutions, and the differences that remain are worth seeing side by side:
only :class:`_DocxViewer` needs a page rail built for it, and only
:class:`_XlsxViewer` cares which way round the colour scheme is.
"""

from __future__ import annotations

from typing import Any

import reflex as rx
from reflex.components.component import NoSSRComponent
from reflex.utils.imports import ImportDict
from reflex.vars.base import Var

from mailarc_ui.kit.viewer_assets import VIEWER_LIBRARIES, VIEWER_PACKAGES

VIEWER_HEIGHT = "var(--ma-viewer-height)"
"""The box's height, stated as the token the stylesheet defines it under.

Inline as well as in ``.ma-viewer`` because this is the one measurement a
renderer needs resolved at mount rather than at paint: each of the three reads
its container's height once, when it is created.
"""


class _DocumentViewer(NoSSRComponent):
    """What the four wrappers share: a file, a name, and lazy loading.

    Props are camelCased on their way out, so :attr:`file_name` reaches the
    JSX as ``fileName``.

    What each subclass adds is three facts and no behaviour: which JSX module
    it lives in, which tag that module exports, and which npm packages have to
    be installed for it. All three come out of
    :mod:`mailarc_ui.kit.viewer_assets` under one key, so a wrapper cannot name
    a library that has no JSX or a JSX file that installs nothing.
    """

    is_default = True

    encoded: Var[str]
    """The file as base64, with no ``data:`` envelope — see
    :attr:`~mailarc_ui.message_detail.preview.AttachmentPreview.encoded`."""

    file_name: Var[str]
    """What the file is called, for the accessible name and — for a Word
    document — for the ``File`` the library is handed. A viewer that announced
    itself as "document" would be the only unlabelled region on the page."""

    def add_imports(self) -> ImportDict:
        """The lazy-loading machinery the dynamic import is written against."""
        return {
            "react": ["lazy"],
            f"$/{rx.constants.Dirs.UTILS}/context": ["ClientSide"],
        }


class _DocxViewer(_DocumentViewer):
    """A Word document, with the page rail from ``viewer_shell.jsx``."""

    library = VIEWER_LIBRARIES["docx"]
    tag = "DocxViewer"
    lib_dependencies: list[str] = list(VIEWER_PACKAGES["docx"])


class _XlsxViewer(_DocumentViewer):
    """A workbook, read-only, navigated by its own sheet tabs."""

    library = VIEWER_LIBRARIES["xlsx"]
    tag = "XlsxViewer_"
    lib_dependencies: list[str] = list(VIEWER_PACKAGES["xlsx"])

    is_dark: Var[bool]
    """Which way round the workbook's palette goes.

    The one viewer that has to be told, and the reason these stay four written
    classes rather than one generated from the table: a spreadsheet paints its
    own grid and its own cell fills, so left to a default it would be a white
    sheet in a dark panel. The others draw a document, which is white paper in
    any theme.
    """


class _PptxViewer(_DocumentViewer):
    """A slide deck, with the library's own toolbar and slide rail."""

    library = VIEWER_LIBRARIES["pptx"]
    tag = "PptxViewer"
    lib_dependencies: list[str] = list(VIEWER_PACKAGES["pptx"])


class _PdfViewer(_DocumentViewer):
    """A PDF, drawn by pdfium rather than by the browser's own plugin.

    The one viewer here that replaced something already working. An
    ``<iframe>`` at a ``data:`` URI rendered a PDF perfectly well; what it
    could not do was carry the same header and page rail as the other three,
    so a reader who opened an invoice after a deck got a different viewer.
    """

    library = VIEWER_LIBRARIES["pdf"]
    tag = "PdfViewer"
    lib_dependencies: list[str] = list(VIEWER_PACKAGES["pdf"])


def _in_box(component: rx.Component) -> rx.Component:
    """The sized box that makes a viewer visible."""
    return rx.box(component, class_name="ma-viewer", height=VIEWER_HEIGHT)


def docx_viewer(**props: Any) -> rx.Component:
    """A Word document, in the sized box that makes it visible."""
    return _in_box(_DocxViewer.create(**props))


def xlsx_viewer(**props: Any) -> rx.Component:
    """A workbook, in the sized box that makes it visible."""
    return _in_box(_XlsxViewer.create(**props))


def pptx_viewer(**props: Any) -> rx.Component:
    """A slide deck, in the sized box that makes it visible."""
    return _in_box(_PptxViewer.create(**props))


def pdf_viewer(**props: Any) -> rx.Component:
    """A PDF, in the sized box that makes it visible."""
    return _in_box(_PdfViewer.create(**props))
