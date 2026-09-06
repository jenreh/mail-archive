/* A workbook, rendered in the browser from bytes.
 *
 * The conventions are the ones `viewer_pptx.jsx` documents: props arrive
 * camelCased, the payload is the memo key, and the wasm URL is handed over
 * explicitly so nothing reaches a CDN.
 *
 * Three decisions specific to a spreadsheet:
 *
 * 1. `readOnly`. This is an archive — what was sent is what is shown, and an
 *    editable grid would invite a reader to change a number that is evidence.
 *    Row and column resizing still works under it, which is reading.
 *
 * 2. **No page rail.** The other viewers put page previews down the side;
 *    a workbook's pages are its sheets, and a thumbnail of a sheet is a
 *    picture of a grid, which tells a reader nothing its name does not. So
 *    the shell is given no items and draws no rail, and the library's own
 *    sheet tabs along the bottom stay as the navigation.
 *
 * 3. The header is `viewer_shell.jsx`'s, not the library's. It ships a
 *    perfectly good toolbar; it looks like Extend rather than like this
 *    archive, and a reader who opens a deck and then a workbook should not
 *    get two different headers. The zoom hook needs the provider's context,
 *    which is why there is an inner component.
 */

import { useCallback } from "react";
import {
  XlsxViewer,
  XlsxViewerProvider,
  setWasmSource,
  useXlsxViewerZoom,
} from "@extend-ai/react-xlsx";
import xlsxWasmUrl from "@extend-ai/react-xlsx/duke_sheets_wasm_bg.wasm?url";

import { useBuffer } from "./viewer_bytes.js";
import { ViewerBoundary, ViewerShell } from "./viewer_shell.jsx";

setWasmSource(xlsxWasmUrl);

function XlsxWorkbook({ encoded = "", fileName = "", isDark = false }) {
  const file = useBuffer(encoded);

  if (!file) return null;

  return (
    <XlsxViewerProvider file={file} fileName={fileName} readOnly isDark={isDark}>
      <Sheet fileName={fileName} />
    </XlsxViewerProvider>
  );
}

/* Inside the provider, because `useXlsxViewerZoom` reads its context. */
function Sheet({ fileName }) {
  const { zoomScale, setZoomScale } = useXlsxViewerZoom();

  /* `zoomScale` is already a percentage despite the name — 100 is 100%, not
   * a factor of one. Measured, not assumed: multiplying showed "10000%". */
  const onZoom = useCallback((percent) => setZoomScale(percent), [setZoomScale]);

  return (
    <ViewerShell zoom={Math.round(zoomScale ?? 100)} onZoom={onZoom}>
      <XlsxViewer
        className="ma-viewer-surface"
        aria-label={fileName}
        toolbar={null}
      />
    </ViewerShell>
  );
}

/* Wrapped rather than exported bare: a parser that throws while rendering
 * would otherwise take the whole preview panel down with it. See
 * `ViewerBoundary` in `viewer_shell.jsx`. */
export default function XlsxViewer_(props) {
  return (
    <ViewerBoundary fileName={props.fileName}>
      <XlsxWorkbook {...props} />
    </ViewerBoundary>
  );
}
