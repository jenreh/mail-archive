/* The archive's one wrapped React component: a cytoscape.js canvas.
 *
 * Python decides everything that can be decided in Python — which elements,
 * what colour each is, how big, which layout — and hands it over as plain
 * data (see `mailarc_ui/graph/model.py`). What is left here is the part that
 * genuinely needs a browser: an imperative graph instance that owns a DOM
 * node, survives re-renders, and reports taps back.
 *
 * Three things to know before changing anything:
 *
 * 1. Props arrive camelCased. Reflex renames every prop on its way out, so
 *    Python's `fit_token` is `fitToken` here. A prop read under its Python
 *    spelling is silently `undefined`.
 *
 * 2. The callbacks are re-read through a ref rather than captured. The
 *    listeners are bound once, on mount; a Reflex event handler prop is a new
 *    function identity on every render, and binding them in an effect that
 *    depends on them would tear down and rebuild every listener each time the
 *    state changed.
 *
 * 3. The element and stylesheet effects key on a serialised signature, not on
 *    the array identity. Reflex delivers a fresh array with every state delta,
 *    so an identity dependency would rebuild the whole graph — and re-run the
 *    layout, moving every node — whenever anything else on the page changed.
 */

import { useEffect, useRef } from "react";
import cytoscape from "cytoscape";

/* Drawn when the page has not decided on a layout yet. Never animated, and
 * never randomised: two runs over one subgraph have to draw one picture. */
const FALLBACK_LAYOUT = { name: "cose", animate: false, randomize: false };

/* Matches `FIT_PADDING` in `mailarc_ui/graph/model.py`, which every layout is
 * given as its own `padding` — if the two disagreed the canvas would shift
 * between the layout settling and the fit. */
const FIT_PADDING = 24;

export default function GraphCanvas({
  elements = [],
  stylesheet = [],
  layout = FALLBACK_LAYOUT,
  selected = "",
  fitToken = 0,
  onSelect,
  onExpand,
  onBackground,
}) {
  const containerRef = useRef(null);
  const cyRef = useRef(null);
  const callbacks = useRef({});

  callbacks.current = { onSelect, onExpand, onBackground };

  const elementsKey = JSON.stringify(elements);
  const styleKey = JSON.stringify(stylesheet);
  const layoutKey = JSON.stringify(layout);

  // The instance, and the three listeners, bound once for its lifetime.
  useEffect(() => {
    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      style: [],
      layout: { name: "preset" },
      wheelSensitivity: 0.2,
      // A box selection would fight the double-click that expands a node.
      boxSelectionEnabled: false,
    });
    cyRef.current = cy;

    cy.on("tap", "node", (event) => {
      callbacks.current.onSelect?.(event.target.id());
    });
    cy.on("dbltap", "node", (event) => {
      callbacks.current.onExpand?.(event.target.id());
    });
    cy.on("tap", (event) => {
      if (event.target === cy) {
        callbacks.current.onBackground?.();
      }
    });

    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // How it is drawn. Its own effect, so re-colouring — a colour scheme
  // flipping — does not re-run the layout, and declared before the elements
  // effect so the sizes are in place the first time a layout is computed:
  // React runs effects in source order, and `cose` reads a node's diameter.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.style().fromJson(Array.isArray(stylesheet) ? stylesheet : []).update();
  }, [styleKey]);

  // What is on the canvas, and where it lands.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().remove();
      cy.add(Array.isArray(elements) ? elements : []);
    });
    cy.layout(layout || FALLBACK_LAYOUT).run();
  }, [elementsKey, layoutKey]);

  // Exactly one node is selected, and Python says which. Re-applied when the
  // elements change, because the node was removed and re-added with them.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.$(":selected").unselect();
    if (selected) {
      cy.$id(selected).select();
    }
  }, [selected, elementsKey]);

  // A counter rather than a boolean: pressing "Fit" twice has to fit twice,
  // and there is no state change to observe in a picture that is already fit.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.fit(undefined, FIT_PADDING);
  }, [fitToken]);

  return (
    <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
  );
}
