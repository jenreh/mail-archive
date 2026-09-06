"""Where the document viewers' JSX and their npm pins are declared.

One module for all of them, and that is the whole reason it exists.
``rx.asset(shared=True)`` derives the symlink's folder from the *calling
module's* name, so an asset registered from ``viewer_docx.py`` would land in
``assets/external/mailarc_ui/kit/viewer_docx/`` and one registered from
``viewer_pptx.py`` next door. Registered from here they all land in
``assets/external/mailarc_ui/kit/viewer_assets/``, which is what lets
``viewer_docx.jsx`` write ``import { ViewerShell } from "./viewer_shell.jsx"``
and have Vite resolve it. Split across modules, the shell and the decoder
would have to be symlinked once per viewer and bundled once per copy.

The npm versions are pinned exactly, for the reason :mod:`mailarc_ui.kit.graph`
gives about cytoscape: a renderer's defaults *are* the design, and a minor
release that re-tunes a layout would silently redraw every document.

**Nothing here may reach a CDN.** Each package resolves its WebAssembly from
inside a worker with ``import.meta.url``, which under Vite points at the
dependency cache where the file is not; each also exports the wasm as a
subpath and a ``setWasmSource`` to be told better, and the ``.jsx`` files do
exactly that. ``appkit_mantine``'s ``maps_base/maps.jsx`` documents the same
failure and the same fix for maplibre.
"""

from __future__ import annotations

import reflex as rx

DOCX = "@extend-ai/react-docx@0.9.1"
"""Word documents. The viewer is an editor held read-only, because it is the
half of the library that can paint page thumbnails."""

XLSX = "@extend-ai/react-xlsx@0.16.3"
"""Workbooks. Sheet tabs are the navigation; a spreadsheet's pages are its
sheets and a picture of a grid says nothing a name does not."""

PDF = (
    "@embedpdf/core@2.15.0",
    "@embedpdf/engines@2.15.0",
    "@embedpdf/models@2.15.0",
    "@embedpdf/pdfium@2.15.0",
    "@embedpdf/plugin-document-manager@2.15.0",
    "@embedpdf/plugin-interaction-manager@2.15.0",
    "@embedpdf/plugin-render@2.15.0",
    "@embedpdf/plugin-rotate@2.15.0",
    "@embedpdf/plugin-scroll@2.15.0",
    "@embedpdf/plugin-thumbnail@2.15.0",
    "@embedpdf/plugin-viewport@2.15.0",
    "@embedpdf/plugin-zoom@2.15.0",
)
"""PDFs. Headless and one package per capability, so this is a list rather
than a name: every entry is a plugin the header actually offers, and nothing
is registered speculatively. It is what Extend UI's own PDF viewer is built
on — that viewer is not an npm package but an 88k-character shadcn file, so
the archive wraps the same libraries directly rather than porting it."""

PPTX = "@extend-ai/react-pptx@0.2.0"
"""Slide decks. The one package that ships a toolbar and a thumbnail rail
good enough to use as they are, which is why it does not reach for
``viewer_shell.jsx``."""

_JSX = {
    name: rx.asset(f"viewer_{name}.jsx", shared=True)
    for name in ("docx", "xlsx", "pptx", "pdf")
}

_SHARED = (
    rx.asset("viewer_shell.jsx", shared=True),
    rx.asset("viewer_bytes.js", shared=True),
)
"""Imported by the viewers rather than by Python, and registered anyway.

``rx.asset`` is what puts the file in the compiling app's assets at all; a
sibling that nothing registered is an import Vite cannot resolve. Nothing
reads these two names, and removing them breaks the build — which is what the
docstring is for.
"""

DOCX_LIBRARY = _JSX["docx"].importable_path
XLSX_LIBRARY = _JSX["xlsx"].importable_path
PPTX_LIBRARY = _JSX["pptx"].importable_path
PDF_LIBRARY = _JSX["pdf"].importable_path
"""``importable_path`` omits the ``?v=`` content hash, which Vite would treat
as an optimised-dep URL and cache immutably — pinning a stale React instance.
The same reason :mod:`mailarc_ui.kit.graph` and ``appkit_mantine.tiptap``
both give."""
