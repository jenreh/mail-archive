/* A PowerPoint deck, rendered in the browser from bytes.
 *
 * Three things to know before changing anything, the first two shared with
 * every viewer in this folder:
 *
 * 1. Props arrive camelCased. Reflex renames every prop on its way out, so
 *    Python's `file_name` is `fileName` here. A prop read under its Python
 *    spelling is silently `undefined`.
 *
 * 2. The wasm URL is handed over explicitly rather than left to the library.
 *    `@extend-ai/react-pptx` resolves it with `import.meta.url` from inside a
 *    worker, which under Vite resolves against the dep cache where the file
 *    is not. `maps_base/maps.jsx` documents the identical failure for
 *    maplibre and fixes it the same way. It is also what keeps the bytes
 *    local: nothing here may reach a CDN.
 *
 * 3. The library's own toolbar and slide rail are turned off. They work, and
 *    they look like Extend rather than like this archive — so the deck goes
 *    through `viewer_shell.jsx` for the same header a Word document and a PDF
 *    get, and the controller behind the ref is what that header drives.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ReactPptxViewer, setWasmSource } from "@extend-ai/react-pptx";
import "@extend-ai/react-pptx/styles.css";
import pptxWasmUrl from "@extend-ai/react-pptx/pptx_wasm_bg.wasm?url";

import { useDecoded } from "./viewer_bytes.js";
import { ViewerBoundary, ViewerShell } from "./viewer_shell.jsx";

setWasmSource(pptxWasmUrl);

function PptxDeck({ encoded = "", fileName = "" }) {
  const source = useDecoded(encoded);
  const controller = useRef(null);
  const [slides, setSlides] = useState(0);
  const [aspect, setAspect] = useState(null);
  const [slide, setSlide] = useState(0);
  const [zoom, setZoom] = useState(100);

  /* The slides hang off `document`, not off the parse result itself: what
   * `onLoad` hands over is `{kind, document, warnings}`. */
  const onLoad = useCallback((presentation) => {
    const doc = presentation?.document;
    setSlides(doc?.slides?.length ?? 0);
    /* Slide size is reported in EMU — English Metric Units, 914400 to the
     * inch — so only the ratio is useful here, and it is the ratio a preview
     * has to be drawn at. A 16:9 deck in a portrait box is a strip of slide
     * above an inch of nothing. */
    const size = doc?.size;
    setAspect(
      size?.widthEmu && size?.heightEmu ? size.widthEmu / size.heightEmu : null,
    );
  }, []);

  /* The rail's previews are painted by the controller into an element we
   * hand it, and `renderThumbnail` returns its own teardown. */
  const renderThumb = useCallback(
    (item) => <SlideThumb controller={controller} index={item.index} />,
    [],
  );

  const pick = useCallback((index) => {
    setSlide(index);
    controller.current?.goToSlide(index);
  }, []);

  const zoomTo = useCallback((percent) => {
    setZoom(percent);
    controller.current?.setZoom(percent);
  }, []);

  const search = useCallback((query) => {
    if (!query) {
      controller.current?.clearSearchHighlights();
      return;
    }
    const [first] = controller.current?.search(query) ?? [];
    if (first) controller.current.highlightSearchResult(first);
  }, []);

  if (!source) return null;

  const items = Array.from({ length: slides }, (_, index) => ({
    index,
    number: index + 1,
    aspect,
  }));

  return (
    <ViewerShell
      label="Slide"
      items={items}
      active={slide}
      onPick={pick}
      renderThumb={renderThumb}
      zoom={zoom}
      onZoom={zoomTo}
      onSearch={search}
    >
      <ReactPptxViewer
        ref={controller}
        source={source}
        aria-label={fileName}
        showToolbar={false}
        showThumbnails={false}
        slideIndex={slide}
        zoom={zoom}
        fitMode="width"
        onLoad={onLoad}
        onSlideChange={setSlide}
        height="100%"
        style={{ height: "100%" }}
      />
    </ViewerShell>
  );
}

/* A slide preview. The controller paints into a node rather than returning an
 * image, so this owns the node and the teardown the paint hands back. */
function SlideThumb({ controller, index }) {
  const host = useRef(null);

  useEffect(() => {
    let release;
    let live = true;
    const node = host.current;
    if (!node || !controller.current) return undefined;
    controller.current
      .renderThumbnail(index, node)
      .then((teardown) => {
        if (live) release = teardown;
        else teardown?.();
      })
      .catch(() => {});
    return () => {
      live = false;
      release?.();
    };
  }, [controller, index]);

  return <span ref={host} className="ma-viewer-thumb-canvas" />;
}

/* Wrapped rather than exported bare: a parser that throws while rendering
 * would otherwise take the whole preview panel down with it. See
 * `ViewerBoundary` in `viewer_shell.jsx`. */
export default function PptxViewer(props) {
  return (
    <ViewerBoundary fileName={props.fileName}>
      <PptxDeck {...props} />
    </ViewerBoundary>
  );
}
