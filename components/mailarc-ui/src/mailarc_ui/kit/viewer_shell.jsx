/* The chrome around a document: a header, a rail of page previews, the page.
 *
 * One header for every kind of file the panel opens, so that a Word document,
 * a deck and a PDF are the same thing to read even though three different
 * renderers draw them. Modelled on Extend UI's document viewer toolbar:
 * a rail toggle and the page count on the left, then rotation, zoom, search
 * and an overflow on the right, in groups separated by hairlines.
 *
 * **Every control is optional and absent by default.** A renderer that cannot
 * rotate is not given a rotate handler, and the button is then not rendered
 * rather than rendered dead — a spreadsheet has no pages to count and a Word
 * document cannot rotate, and a toolbar that offered either would be lying.
 * That is why this takes a dozen small props instead of a `features` flag.
 *
 * The rail's breakpoint measures the *container*, not the window: the panel
 * is a drawer whose width is a fraction of the page, so a media query would
 * be asking about the wrong box. Above the breakpoint the rail sits beside
 * the page; below, it comes over it.
 *
 * Nothing here names a colour — see `assets/css/mail-archive.css`, keyed on
 * the `ma-viewer-*` classes.
 */

import { Component, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Minus,
  PanelLeft,
  Plus,
  RotateCcw,
  RotateCw,
  Search,
  X,
} from "lucide-react";

const INLINE_MIN_WIDTH = 768;
/* Below this many pixels of container the rail stops being a column beside
 * the page and becomes something you pull over it. Extend's own figure, kept
 * because it is the width at which a page plus a 160px rail stops leaving a
 * readable column. */

const ZOOM_STEPS = [50, 75, 100, 125, 150, 200, 400];

/* The one thing a function component cannot be. Each viewer here hands a
 * vendored WASM parser a file somebody else wrote, and a malformed .docx or
 * .pptx — routine in a real archive — makes several of those throw
 * *synchronously during render* rather than rejecting a promise. React's
 * answer to a throw in render is to unmount the whole tree above it, so
 * without this the panel vanished and `preview.py`'s promise that every file
 * gets a panel was broken by exactly the files that most need one.
 *
 * Deliberately not a retry: the bytes will not parse the second time either.
 * It says which file failed and leaves the rest of the page alone. */
export class ViewerBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error) {
    console.error("The viewer could not read this file", error);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="ma-viewer-broken" role="status">
        <p>This file could not be displayed.</p>
        <p className="ma-viewer-broken-detail">
          {this.props.fileName
            ? `${this.props.fileName} is damaged, or uses a feature this viewer does not read.`
            : "It is damaged, or uses a feature this viewer does not read."}{" "}
          You can still download it.
        </p>
      </div>
    );
  }
}

/* The wrapper every viewer in this folder is exported through.
 *
 * A helper rather than four hand-written wrappers, because the wrapping is the
 * kind of step that is only noticed when it is missing: `viewer_pdf.jsx` was
 * exported bare for exactly as long as this was three copies of the same four
 * lines. `test_ui_kit_components.py` asserts that every `viewer_*.jsx` calls
 * this, so a fifth viewer cannot repeat that.
 *
 * The displayed name is kept so React's own stack traces still say which
 * viewer failed rather than naming the wrapper four times. */
export function withBoundary(Inner) {
  const Wrapped = (props) => (
    <ViewerBoundary fileName={props.fileName}>
      <Inner {...props} />
    </ViewerBoundary>
  );
  Wrapped.displayName = `WithBoundary(${Inner.displayName || Inner.name})`;
  return Wrapped;
}

export function useContainerWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const measure = () => {
      const next = element.getBoundingClientRect().width;
      /* Keep the last real measurement while the element is hidden. A drawer
       * that has just closed measures zero, and re-laying the viewer out for
       * that would drop every rendered canvas and flash on the way back. */
      if (next === 0) return;
      setWidth(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function IconButton({ label, onClick, pressed, children }) {
  return (
    <button
      type="button"
      className="ma-viewer-icon"
      aria-label={label}
      aria-pressed={pressed === undefined ? undefined : String(pressed)}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <span className="ma-viewer-divider" role="presentation" />;
}

/* One page button. Split out because it owns an IntersectionObserver: a
 * two-hundred-page PDF must not rasterise two hundred previews on mount, so a
 * thumbnail is painted when it is first scrolled near. */
function Thumb({ item, active, onPick, renderThumb }) {
  const ref = useRef(null);
  const [seen, setSeen] = useState(false);

  useEffect(() => {
    if (seen || !ref.current) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((one) => one.isIntersecting)) setSeen(true);
      },
      { rootMargin: "200px" },
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [seen]);

  return (
    <button
      ref={ref}
      type="button"
      className="ma-viewer-thumb"
      aria-current={active ? "true" : undefined}
      aria-label={`Page ${item.number}`}
      tabIndex={active ? 0 : -1}
      onClick={() => onPick(item.index)}
    >
      {/* The aspect comes from the document, not from this file. A slide is
        * 16:9 and a letter is A4, and a preview that assumed either would
        * letterbox the other into a strip at the top of a tall white box. */}
      <span
        className="ma-viewer-thumb-page"
        style={item.aspect ? { aspectRatio: item.aspect } : undefined}
      >
        {seen ? renderThumb(item) : null}
      </span>
      <span className="ma-viewer-thumb-number">{item.number}</span>
    </button>
  );
}

export function ViewerShell({
  rootRef = null,
  items = null,
  pageCount = null,
  rail = null,
  active = 0,
  onPick = () => {},
  renderThumb = () => null,
  label = "Page",
  zoom = null,
  onZoom = null,
  onRotate = null,
  onSearch = null,
  extras = null,
  children,
}) {
  const [ref, width] = useContainerWidth();
  const inline = width >= INLINE_MIN_WIDTH;
  const [open, setOpen] = useState(true);
  const [query, setQuery] = useState(null);
  const railRef = useRef(null);

  /* Two ways to have a rail, because the four renderers do not agree on what
   * a page preview is. Three of them hand over a list and let this draw it;
   * `@embedpdf` brings its own virtualised pane — the right thing for a
   * two-hundred-page document — and hands the whole rail over instead. The
   * header reads the same either way, which is the point of the shell. */
  const pages = pageCount ?? items?.length ?? 0;
  const hasRail = pages > 1;

  /* An overlay rail left open when the panel is narrowed would sit on top of
   * the page; one left closed when it is widened would waste the room. */
  useEffect(() => setOpen(inline), [inline]);

  const showRail = hasRail && open;

  /* Roving tabindex: the rail is one tab stop and the arrows move within it.
   * None of the renderers can supply this once the rail is ours. */
  const onKeyDown = useCallback(
    (event) => {
      const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[
        event.key
      ];
      if (!step) return;
      event.preventDefault();
      const next = Math.min(Math.max(active + step, 0), pages - 1);
      if (next !== active) onPick(next);
    },
    [active, pages, onPick],
  );

  useEffect(() => {
    railRef.current
      ?.querySelector('[aria-current="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const step = (by) => {
    /* A viewer may hand us a zoom of its own — "fit width" is computed from
     * the page, not picked from this table — and that can sit above the last
     * step. `findIndex` answers -1 there, and -1 + by clamped at 0 sent both
     * buttons to the smallest step instead of stepping down from the top. */
    const found = ZOOM_STEPS.findIndex((one) => one >= zoom);
    const at = found === -1 ? ZOOM_STEPS.length : found;
    const next = ZOOM_STEPS[Math.min(Math.max(at + by, 0), ZOOM_STEPS.length - 1)];
    if (next !== zoom) onZoom(next);
  };

  /* Two refs on one node: this file measures the container, and a viewer may
   * need to reach into what it rendered — the Word document scrolls itself to
   * a page the rail picked. */
  const attach = useCallback(
    (node) => {
      ref.current = node;
      if (rootRef) rootRef.current = node;
    },
    [ref, rootRef],
  );

  return (
    <div
      ref={attach}
      className="ma-viewer-shell"
      data-rail={showRail ? "open" : "closed"}
      data-rail-mode={inline ? "inline" : "overlay"}
    >
      <header className="ma-viewer-header">
        {hasRail ? (
          <IconButton
            label={showRail ? `Hide ${label}s` : `Show ${label}s`}
            pressed={showRail}
            onClick={() => setOpen((was) => !was)}
          >
            <PanelLeft size={18} strokeWidth={1.75} />
          </IconButton>
        ) : null}
        {pages ? (
          <p className="ma-viewer-count">
            {label} <b>{active + 1}</b> of {pages}
          </p>
        ) : null}

        <span className="ma-viewer-spacer" />

        {onRotate ? (
          <>
            <IconButton label="Rotate left" onClick={() => onRotate(-90)}>
              <RotateCcw size={18} strokeWidth={1.75} />
            </IconButton>
            <IconButton label="Rotate right" onClick={() => onRotate(90)}>
              <RotateCw size={18} strokeWidth={1.75} />
            </IconButton>
            <Divider />
          </>
        ) : null}

        {onZoom ? (
          <>
            <IconButton label="Zoom out" onClick={() => step(-1)}>
              <Minus size={18} strokeWidth={1.75} />
            </IconButton>
            <span className="ma-viewer-zoom">
              <select
                aria-label="Zoom"
                value={ZOOM_STEPS.includes(zoom) ? zoom : ""}
                onChange={(event) => onZoom(Number(event.target.value))}
              >
                {ZOOM_STEPS.includes(zoom) ? null : (
                  <option value="">{Math.round(zoom)}%</option>
                )}
                {ZOOM_STEPS.map((one) => (
                  <option key={one} value={one}>
                    {one}%
                  </option>
                ))}
              </select>
              <ChevronDown size={14} strokeWidth={2} aria-hidden="true" />
            </span>
            <IconButton label="Zoom in" onClick={() => step(1)}>
              <Plus size={18} strokeWidth={1.75} />
            </IconButton>
          </>
        ) : null}

        {onSearch ? (
          <>
            <Divider />
            {query === null ? (
              <IconButton label="Search this file" onClick={() => setQuery("")}>
                <Search size={18} strokeWidth={1.75} />
              </IconButton>
            ) : (
              <span className="ma-viewer-find">
                <Search size={15} strokeWidth={1.75} aria-hidden="true" />
                <input
                  autoFocus
                  type="search"
                  aria-label="Search this file"
                  placeholder="Find"
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    onSearch(event.target.value);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      setQuery(null);
                      onSearch("");
                    }
                  }}
                />
                <button
                  type="button"
                  aria-label="Close search"
                  onClick={() => {
                    setQuery(null);
                    onSearch("");
                  }}
                >
                  <X size={14} strokeWidth={2} />
                </button>
              </span>
            )}
          </>
        ) : null}

        {extras}
      </header>

      <div className="ma-viewer-body">
        {hasRail ? (
          <nav
            ref={railRef}
            className="ma-viewer-rail"
            /* A caller-supplied rail brings its own virtualiser: it positions
             * rows absolutely and scrolls itself, so the padding and the
             * overflow this file would otherwise apply have to come off. */
            data-rail-kind={rail ? "pane" : "list"}
            aria-label={`${label}s`}
            onKeyDown={onKeyDown}
            hidden={!showRail}
          >
            {rail ??
              items?.map((one) => (
                <Thumb
                  key={one.index}
                  item={one}
                  active={one.index === active}
                  onPick={onPick}
                  renderThumb={renderThumb}
                />
              ))}
          </nav>
        ) : null}
        {!inline && showRail ? (
          <div
            className="ma-viewer-scrim"
            role="presentation"
            onClick={() => setOpen(false)}
          />
        ) : null}
        <div className="ma-viewer-page">{children}</div>
      </div>
    </div>
  );
}
