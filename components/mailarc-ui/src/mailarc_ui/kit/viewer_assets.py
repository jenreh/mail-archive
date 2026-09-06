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

from collections.abc import Mapping
from types import MappingProxyType

import reflex as rx

VIEWER_PACKAGES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "docx": ("@extend-ai/react-docx@0.9.1",),
        "xlsx": ("@extend-ai/react-xlsx@0.16.3",),
        "pptx": ("@extend-ai/react-pptx@0.2.0",),
        "pdf": (
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
        ),
    }
)
"""What each viewer installs, keyed by the name its ``.jsx`` file carries.

One table rather than four constants beside four classes. The keys are the
whole vocabulary of this pair of modules — ``viewer_docx.jsx`` is the ``docx``
entry's JSX and :class:`~mailarc_ui.kit.viewer._DocxViewer` is its wrapper —
so a fifth viewer is one entry here and one class there, and the two cannot be
added out of step without a test failing.

A tuple per entry even where it holds one name. Three of these libraries are a
single package and ``@embedpdf`` is twelve, and the difference is not one the
caller should have to know: it is headless, one package per capability, and
every entry is a plugin the header actually offers. Nothing is registered
speculatively. It is what Extend UI's own PDF viewer is built on — that viewer
is not an npm package but an 88k-character shadcn file, so the archive wraps
the same libraries directly rather than porting it.

Which files they read, for the record: Word documents through an editor held
read-only, because that is the half of the library that can paint page
thumbnails; workbooks navigated by their own sheet tabs, since a picture of a
grid says nothing a name does not; slide decks, the one package that ships a
toolbar and a rail good enough to use as they are.

The versions are pinned exactly, for the reason :mod:`mailarc_ui.kit.graph`
gives about cytoscape: a renderer's defaults *are* the design, and a minor
release that re-tunes a layout would silently redraw every document.
"""

_JSX = {name: rx.asset(f"viewer_{name}.jsx", shared=True) for name in VIEWER_PACKAGES}

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

VIEWER_LIBRARIES: Mapping[str, str] = MappingProxyType(
    {name: asset.importable_path for name, asset in _JSX.items()}
)
"""Where each viewer's JSX is imported from, keyed the same way.

``importable_path`` omits the ``?v=`` content hash, which Vite would treat as
an optimised-dep URL and cache immutably — pinning a stale React instance. The
same reason :mod:`mailarc_ui.kit.graph` and ``appkit_mantine.tiptap`` both
give.
"""
