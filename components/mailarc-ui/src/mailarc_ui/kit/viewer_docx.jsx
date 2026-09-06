/* A Word document, rendered in the browser from bytes.
 *
 * The conventions are the ones `viewer_pptx.jsx` documents. Three things are
 * specific to this one:
 *
 * 1. It is the only viewer here that has to *import* rather than be handed a
 *    source. `useDocxEditor` returns a controller and the document arrives
 *    through `controller.importDocxFile(File)`, so the decoded bytes are
 *    wrapped in a `File` and pushed in from an effect.
 *
 * 2. `mode="read-only"`. The library's viewer is an editor with editing
 *    turned off; this is an archive, and a Word document a reader could
 *    silently alter is a correctness problem, not a cosmetic one.
 *
 * 3. Page previews are painted, not fetched. `useDocxPageThumbnails` gives a
 *    `paintThumbnail(pageIndex, canvas)` rather than an image URL, so each
 *    rail entry is a `<canvas>` whose ref callback asks for the paint.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DocxEditorViewer,
  setWasmSource,
  useDocxEditor,
  useDocxPageThumbnails,
} from "@extend-ai/react-docx";
import docxWasmUrl from "@extend-ai/react-docx/docx_wasm_bg.wasm?url";

import { useDecoded } from "./viewer_bytes.js";
import { ViewerBoundary, ViewerShell } from "./viewer_shell.jsx";

setWasmSource(docxWasmUrl);

const DOCX_TYPE =
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

function DocxDocument({ encoded = "", fileName = "" }) {
  const bytes = useDecoded(encoded);
  const editor = useDocxEditor({ initialFileName: fileName });
  const [page, setPage] = useState(0);
  const root = useRef(null);
  const { thumbnails, paintThumbnail } = useDocxPageThumbnails(editor);

  /* Keyed on the bytes, so re-opening the same file does not re-import it and
   * a different one always does. `importDocxFile` is async and the controller
   * is stable, so there is nothing to cancel — a second import simply wins. */
  useEffect(() => {
    if (!bytes) return;
    const file = new File([bytes], fileName || "document.docx", {
      type: DOCX_TYPE,
    });
    editor.importDocxFile(file);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bytes, fileName]);

  const renderThumb = useCallback(
    (item) => (
      <canvas
        className="ma-viewer-thumb-canvas"
        ref={(canvas) => paintThumbnail(item.index, canvas)}
      />
    ),
    [paintThumbnail],
  );

  const items = thumbnails.map((one) => ({
    index: one.pageIndex,
    number: one.pageNumber,
    /* Per page rather than once for the document: a section break can turn
     * one page of a letter landscape. */
    aspect: one.aspectRatio || null,
  }));

  /* Picking a page in the rail scrolls the document to it. The editor exposes
   * no "go to page", so this reaches for the page element the viewer has
   * already mounted — which is what `onRequestPageReveal` would have done had
   * the navigation come from inside the document.
   *
   * The attribute is the library's own (`data-docx-page-index`), read off the
   * rendered DOM rather than guessed: an invented one silently matches
   * nothing and the rail then updates the page number without moving the
   * document. Scoped to this viewer's root so a second one on the page — or
   * the thumbnails, which mount pages of their own — cannot be scrolled by
   * mistake. */
  const reveal = useCallback((index) => {
    setPage(index);
    const surface = root.current;
    const scroller = surface?.querySelector(".ma-viewer-page");
    const target = surface?.querySelector(`[data-docx-page-index="${index}"]`);
    if (!scroller || !target) return;
    /* Our own scroller, moved by the measured delta, rather than
     * `scrollIntoView`. That walks *every* scrollable ancestor, so inside a
     * drawer it also scrolls the drawer and the two cancel out — measured:
     * the page number changed and the document did not move. */
    scroller.scrollTop +=
      target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
  }, []);

  if (!bytes) return null;

  return (
    <ViewerShell
      rootRef={root}
      label="Page"
      items={items}
      active={page}
      onPick={reveal}
      renderThumb={renderThumb}
    >
      <DocxEditorViewer
        editor={editor}
        mode="read-only"
        className="ma-viewer-surface"
        onRequestPageReveal={setPage}
        /* The two colours the library will not infer. Left to its defaults a
         * page and the space between pages are the same white, so a two-page
         * letter reads as one long scroll with no edge — the PDF beside it
         * puts each page on a ground, and the two should not disagree.
         * Passed as custom properties rather than hexes because nothing in
         * the kit names a colour; the stylesheet owns both. */
        pageBackgroundColor="var(--ma-viewer-sheet)"
        pageGapBackgroundColor="var(--ma-surface-hover)"
      />
    </ViewerShell>
  );
}

/* Wrapped rather than exported bare: a parser that throws while rendering
 * would otherwise take the whole preview panel down with it. See
 * `ViewerBoundary` in `viewer_shell.jsx`. */
export default function DocxViewer(props) {
  return (
    <ViewerBoundary fileName={props.fileName}>
      <DocxDocument {...props} />
    </ViewerBoundary>
  );
}
