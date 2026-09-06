/* A PDF, rendered in the browser from bytes.
 *
 * This one replaced something that already worked: the panel used to point an
 * `<iframe>` at a `data:` URI and let the browser's own plugin draw it. That
 * was fine and it is gone for one reason — it was the only one of the four
 * document kinds with no page rail and no shared header, so a reader who
 * opened a deck and then an invoice got two different things.
 *
 * `@embedpdf/*` is what Extend UI's own PDF viewer is built on. It is
 * headless: every capability is a plugin, and the ones registered below are
 * exactly the ones the header offers. Nothing is registered speculatively.
 *
 * **Two overrides here are load-bearing and must not be dropped.**
 * `usePdfiumEngine` defaults to fetching its WebAssembly from
 * `cdn.jsdelivr.net`, and its font fallback pulls typefaces from the same
 * place. Either would be invisible on a developer machine with internet and
 * fatal in the offline desktop bundle — and both would break the archive's
 * posture that the browser never fetches anything. So the wasm arrives as a
 * bundled asset through Vite's `?url`, and the font fallback is turned off
 * outright, which the library documents as "no external font requests".
 * `test_ui_kit_components.py` fails on any absolute URL in this folder.
 */

import { useCallback, useMemo } from "react";
import { createPluginRegistration } from "@embedpdf/core";
import { EmbedPDF } from "@embedpdf/core/react";
import { usePdfiumEngine } from "@embedpdf/engines/react";
import pdfiumWasmUrl from "@embedpdf/pdfium/pdfium.wasm?url";
import {
  DocumentContent,
  DocumentManagerPluginPackage,
  useActiveDocument,
  useOpenDocuments,
} from "@embedpdf/plugin-document-manager/react";
import { InteractionManagerPluginPackage } from "@embedpdf/plugin-interaction-manager/react";
import { RenderLayer, RenderPluginPackage } from "@embedpdf/plugin-render/react";
import { RotatePluginPackage, useRotate } from "@embedpdf/plugin-rotate/react";
import { ScrollPluginPackage, Scroller, useScroll } from "@embedpdf/plugin-scroll/react";
import {
  ThumbImg,
  ThumbnailPluginPackage,
  ThumbnailsPane,
} from "@embedpdf/plugin-thumbnail/react";
import { Viewport, ViewportPluginPackage } from "@embedpdf/plugin-viewport/react";
import { ZoomPluginPackage, useZoom } from "@embedpdf/plugin-zoom/react";

import { useBuffer } from "./viewer_bytes.js";
import { ViewerShell } from "./viewer_shell.jsx";

export default function PdfViewer({ encoded = "", fileName = "" }) {
  const buffer = useBuffer(encoded);
  const { engine, error: engineError } = usePdfiumEngine({
    wasmUrl: pdfiumWasmUrl,
    fontFallback: null,
    /* Main thread rather than a worker. The worker build resolves its own
     * wasm with `import.meta.url` from inside the worker chunk, which under
     * Vite points at the dependency cache where the file is not — the same
     * failure `maps_base/maps.jsx` documents for maplibre, except that
     * `usePdfiumEngine` swallows it and the document simply never leaves
     * "loading". Measured: with the worker, status stayed `loading` forever
     * and no error was raised. An attachment is capped by `PREVIEW_LIMIT` in
     * `message_detail/preview.py`; the number is not restated here, because it
     * was raised once already and this comment went on claiming the old one. */
    worker: false,
  });

  /* Memoised on the bytes, and that is not an optimisation. `EmbedPDF` reads
   * this list to build its plugin registry; a fresh array on every render
   * tears the registry down and rebuilds it, so the document never finishes
   * opening and the viewport keeps registering against a document that is not
   * there yet. Same trap `graph_canvas.jsx` documents about Reflex handing
   * down fresh arrays with every state delta. */
  const plugins = useMemo(
    () =>
      buffer
        ? [
            /* The document is handed over as bytes. This is why the viewer
             * needs no URL, and therefore no route that serves archived mail
             * — see `message_detail/preview.py` for that argument. */
            createPluginRegistration(DocumentManagerPluginPackage, {
              initialDocuments: [
                {
                  buffer,
                  name: fileName || "document.pdf",
                  autoActivate: true,
                },
              ],
            }),
            createPluginRegistration(ViewportPluginPackage),
            createPluginRegistration(ScrollPluginPackage),
            createPluginRegistration(RenderPluginPackage),
            createPluginRegistration(InteractionManagerPluginPackage),
            createPluginRegistration(ZoomPluginPackage),
            createPluginRegistration(RotatePluginPackage),
            /* The pane reserves each row as `imagePadding * 2 + height +
             * labelHeight`, and its defaults are 0 and 16 — a preview with
             * its highlight drawn tight against its edges. These are the
             * measurements the rail is actually drawn at, so the pane has to
             * be told them rather than have CSS add padding it did not
             * reserve, which would put the highlight off the row. */
            createPluginRegistration(ThumbnailPluginPackage, {
              width: 116,
              imagePadding: 14,
              labelHeight: 24,
              gap: 10,
              paddingY: 12,
            }),
          ]
        : null,
    [buffer, fileName],
  );

  if (engineError) {
    return <p className="ma-viewer-state">This PDF could not be opened.</p>;
  }
  if (!plugins || !engine) return null;

  return (
    <EmbedPDF
      engine={engine}
      plugins={plugins}
      autoMountDomElements={false}
    >
      <Opened fileName={fileName} />
    </EmbedPDF>
  );
}

/* Every component and hook below takes an explicit `documentId`, and the
 * viewport refuses to register against a document that is not open yet —
 * "document must be opened before registering viewport", which is what it
 * says if you mount it eagerly. `DocumentContent` is the library's own gate
 * for that, so nothing inside it renders until the bytes are parsed. */
function Opened({ fileName }) {
  const { activeDocumentId } = useActiveDocument();
  const open = useOpenDocuments();

  /* Falling back to the first open document is not defensiveness. A viewer
   * built from `initialDocuments` has exactly one, and `autoActivate` does
   * not reliably mark it active before the first render — measured: the
   * manager reports one open document while `activeDocumentId` is still
   * null, and everything downstream needs an id. With one document, "the
   * active one" and "the only one" cannot disagree. */
  const documentId = activeDocumentId ?? open[0]?.id ?? null;

  if (!documentId) return null;

  return (
    <DocumentContent documentId={documentId}>
      {({ documentState, isLoaded, isError }) => {
        if (isLoaded) return <Pages documentId={documentId} fileName={fileName} />;
        /* A PDF that will not open is the one case the old `<iframe>` handled
         * by showing the browser's own error. Saying it here keeps the panel's
         * rule that every file gets a sentence rather than a blank rectangle —
         * an encrypted or truncated invoice is common enough in mail. */
        return (
          <p className="ma-viewer-state" data-status={documentState?.status}>
            {isError
              ? (documentState?.error ?? "This PDF could not be opened.")
              : "Opening…"}
          </p>
        );
      }}
    </DocumentContent>
  );
}

function Pages({ documentId: activeDocumentId, fileName }) {
  const { state: scroll, provides: scroller } = useScroll(activeDocumentId);
  const { state: zoom, provides: zoomer } = useZoom(activeDocumentId);
  const { provides: rotator } = useRotate(activeDocumentId);

  const pick = useCallback(
    (index) => scroller?.scrollToPage({ pageNumber: index + 1 }),
    [scroller],
  );
  const onZoom = useCallback(
    (percent) => zoomer?.requestZoom(percent / 100),
    [zoomer],
  );
  const onRotate = useCallback(
    (degrees) => (degrees > 0 ? rotator?.rotateForward() : rotator?.rotateBackward()),
    [rotator],
  );

  const pages = scroll?.totalPages ?? 0;
  const page = (scroll?.currentPage ?? 1) - 1;

  /* embedpdf brings its own virtualised thumbnail pane, so the shell is given
   * the rail whole rather than a list of items to draw. A two-hundred-page
   * PDF is exactly the case that pane exists for. */
  /* `ThumbMeta` carries the geometry and the pane is virtualised: each row is
   * positioned absolutely at its own `top` inside a container the pane sizes.
   * Every field below is one the pane computed — inventing a `wrapperStyle`
   * and leaving `top` off puts all five pages at zero and shows one. */
  const rail =
    pages > 1 ? (
      <ThumbnailsPane documentId={activeDocumentId} style={{ height: "100%" }}>
        {(meta) => (
          <div
            key={meta.pageIndex}
            style={{
              position: "absolute",
              top: meta.top,
              height: meta.wrapperHeight,
              width: "100%",
            }}
          >
            <button
              type="button"
              className="ma-viewer-thumb"
              aria-current={meta.pageIndex === page ? "true" : undefined}
              aria-label={`Page ${meta.pageIndex + 1}`}
              onClick={() => pick(meta.pageIndex)}
              /* The pane reserved `padding * 2 + height + labelHeight` for
               * this row, so the button spends it the same way. Any other
               * padding here and the highlight would be a box that does not
               * line up with the row the pane positioned. */
              style={{ padding: meta.padding ?? 8 }}
            >
              <span
                className="ma-viewer-thumb-page"
                style={{ width: meta.width, height: meta.height }}
              >
                <ThumbImg meta={meta} documentId={activeDocumentId} />
              </span>
              <span
                className="ma-viewer-thumb-number"
                style={{ height: meta.labelHeight, padding: 0, lineHeight: `${meta.labelHeight}px` }}
              >
                {meta.pageIndex + 1}
              </span>
            </button>
          </div>
        )}
      </ThumbnailsPane>
    ) : null;

  return (
    <ViewerShell
      label="Page"
      pageCount={pages}
      active={page}
      onPick={pick}
      rail={rail}
      zoom={Math.round((zoom?.currentZoomLevel ?? 1) * 100)}
      onZoom={onZoom}
      onRotate={onRotate}
    >
      <Viewport
        documentId={activeDocumentId}
        style={{ height: "100%" }}
        aria-label={fileName}
      >
        <Scroller
          documentId={activeDocumentId}
          renderPage={({ width, height, pageIndex, scale }) => (
            /* The sheet is drawn by us, not by the renderer: `RenderLayer`
             * paints an `<img>` of the page content and nothing else, so
             * without this a page has no edge, an unrendered one is invisible,
             * and a scrolled document reads as text floating in a void. */
            <div className="ma-viewer-sheet" style={{ width, height }}>
              <RenderLayer
                documentId={activeDocumentId}
                pageIndex={pageIndex}
                scale={scale}
                style={{ width: "100%", height: "100%", display: "block" }}
              />
            </div>
          )}
        />
      </Viewport>
    </ViewerShell>
  );
}
