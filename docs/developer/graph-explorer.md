# The graph explorer

`/graph` is the one page in this application that wraps a foreign React
component. Everything else in `mailarc-ui` is a recipe over an `mn.*`
component; the canvas is [cytoscape.js](https://js.cytoscape.org/), which has an
imperative API and a DOM node of its own.

For what the page does, see [the user guide](../user/graph-explorer.md). This
page is about the wrapper and the contract behind it.

## The split, and why it is where it is

| File | Holds | Testable without |
| --- | --- | --- |
| `mailarc_ui/kit/graph_canvas.jsx` | The cytoscape instance, three listeners, five effects | — |
| `mailarc_ui/kit/graph.py` | The Reflex component, its props and its events | a browser |
| `mailarc_ui/graph/model.py` | Elements, stylesheet and layout out of a `Subgraph` | Reflex |
| `mailarc_ui/graph/reads.py` | The four registry lookups | a state |
| `mailarc_ui/graph/state.py` | The question, the picture and the selection | a graph |
| `mailarc_ui/graph/components.py` | The three columns | — |

Everything that can be decided in Python is. `elements_of` colours and sizes
every node, `stylesheet_of` states the rules and `layout_of` names the
arrangement, so the JSX is left with the part that genuinely needs a browser: an
instance that owns a `<div>`, survives re-renders and reports taps back.

That split is what makes the interesting decisions testable. The two halves also
fail differently, which is the stronger argument. A state bug leaves a read
spinning or misreads a query parameter, and you see it. A projection bug sizes a
node off a number it does not carry or draws an edge to a node that was hidden,
and cytoscape refuses the whole `cy.add` and leaves the canvas **blank without
reporting anything**.

## The asset

```python
_GRAPH_JSX = rx.asset("graph_canvas.jsx", shared=True)
_GRAPH_LIBRARY = _GRAPH_JSX.importable_path
```

`rx.asset(..., shared=True)` symlinks the file into the compiling app's
`assets/external/`, so the JSX ships with the component rather than with the
application. The pattern is `appkit_mantine`'s, and so is the second line.

**`importable_path` and never `path`.** `path` carries a `?v=` content hash,
which Vite reads as an optimised-dependency URL and caches immutably. A cached
one pins a stale React instance, and the symptom is hooks failing in a component
that was not changed.

## The component

```python
class _GraphCanvas(NoSSRComponent):
    library = _GRAPH_LIBRARY
    tag = "GraphCanvas"
    is_default = True
    lib_dependencies = ["cytoscape@3.34.2", "react-cytoscapejs@2.0.0"]
```

`NoSSRComponent` for two reasons. Cytoscape touches `document` at construction
and throws when rendered on the server, and a graph library is about 400 KB that
only this page should pay for. It arrives as its own lazy chunk.

Both npm versions are pinned exactly rather than ranged. A graph library's
defaults *are* the design, and a minor release that re-tunes a layout would
silently redraw every picture.

`react-cytoscapejs` is declared and **not imported**. The wrapper drives
cytoscape directly. It is in the list because it is the React binding this canvas
would move to if the imperative wrapper stopped paying for itself, and having it
resolved keeps that a change to one JSX file rather than to the dependency set.
Nothing imports it, so nothing of it is bundled.

The class stays private. What `mailarc_ui.kit` exports is `graph_canvas()`,
which is the wrapper inside a box carrying `.ma-graph-canvas` and an explicit
height. **Cytoscape measures its container on mount**, and a container that is
still nought pixels tall draws an empty canvas without reporting anything, so
the height is set inline as well as in the stylesheet.

## The prop and event contract

| Prop | Type | Meaning |
| --- | --- | --- |
| `elements` | `list[dict]` | What to draw. Nodes first, because cytoscape refuses an edge whose ends are not in yet |
| `stylesheet` | `list[dict]` | How to draw it, with the palette's hexes already substituted in |
| `layout` | `dict` | How to arrange it |
| `selected` | `str` | The id of the one selected node, or empty |
| `fit_token` | `int` | Bumped to re-fit the viewport |

| Event | Spec | Fires on |
| --- | --- | --- |
| `on_select` | `lambda node_id: [node_id]` | `tap` on a node |
| `on_expand` | `lambda node_id: [node_id]` | `dbltap` on a node |
| `on_background` | `_no_arguments` | `tap` on the canvas itself |

Five things about that table are load-bearing.

**Props are camelCased on the way out.** Reflex renames every prop, so Python's
`fit_token` is `fitToken` in the JSX. A prop read under its Python spelling is
silently `undefined`.

**The canvas follows the selection rather than owning it.** A node picked from a
table beside the canvas and one tapped on the canvas are the same state, so
there is one place where "what is picked" lives.

**`fit_token` is a counter and not a flag.** Pressing "Fit" on a picture that is
already fitted has to fit it again, and there is no other state change to
observe.

**`on_background`'s spec is a named function** rather than `lambda: []`. Ruff's
PIE807 rewrites the lambda to `EventHandler[list]`, and Reflex reads the spec's
*signature* to name the event's arguments. `list` has one unannotated
`iterable`, so the rewrite raises `MissingAnnotationError` the moment a page
hands the trigger a handler.

**The stylesheet carries hexes and never CSS custom properties.** Cytoscape
paints into a `<canvas>`, where no custom property is resolvable. The colours
therefore exist twice, as `--ma-graph-<kind>` in `assets/css/mail-archive.css`
and as `Palette.LIGHT` / `Palette.DARK` in `mailarc_ui/theme.py`.
`test_ui_graph_model.TestThePaletteHasTwoHomes` parses the stylesheet and holds
the two against each other, because two homes for one colour is a thing that
drifts.

Which of the two stylesheets a reader gets is decided in `components.py` with
`rx.color_mode_cond`, and the state never learns which. The colour scheme is a
fact about the browser rather than about the archive, and a var that only ever
held one value would be a state that could disagree with the screen.

## Inside the JSX

Three things a reader would otherwise have to work out.

**The callbacks are re-read through a ref rather than captured.** The listeners
are bound once, on mount. A Reflex event handler prop is a new function identity
on every render, so binding them in an effect that depended on them would tear
down and rebuild every listener each time the state changed.

**The element and stylesheet effects key on a serialised signature**, not on the
array identity. Reflex delivers a fresh array with every state delta, so an
identity dependency would rebuild the whole graph and re-run the layout, moving
every node, whenever anything else on the page changed.

**The style effect is declared before the element effect.** React runs effects in
source order, and `cose` reads a node's diameter, so the sizes have to be in
place the first time a layout is computed. It is also a separate effect so that
re-colouring, e.g. a colour scheme flipping, does not re-run the layout.

`FIT_PADDING` is stated in both files and the two have to agree. Every layout is
given it as its own `padding`, and if the two disagreed the canvas would shift
between the layout settling and the fit.

## The state

`GraphExplorerState(TagActionsState, MessageDetailState, rx.State)` holds three
things that are deliberately separable.

- **The question** is a view, a root and a depth. It is what a URL carries and
  what a rebuild can invalidate.
- **The picture** is one `Subgraph`, held and redrawn without asking the graph
  anything. Sizing by importance, hiding the addresses and changing the layout
  are rearrangements of an answer the page already has, and a page that re-read
  for one of them would stutter on every dropdown.
- **The selection** is what a person clicked, which is a message to read or a
  cluster to promote.

**The state's own view var is called `view_name`.** That is not a preference.
`MessageDetailState.view` is the open message, and a second var of that name
would silently replace it, after which the reading pane would be asked for
`view.body_html` on a string. The URL parameter stays `?view=`, which is what a
person reads.

Query parameters come off `self.router.url.query_parameters`. `RouterData.page`
is deprecated. A `?view=` nobody serves falls back to the overview **and drops
the id with it**, because an id minted for a view that no longer exists is not
an id this one can use.

## R7, in code

A `Topic.id` is a digest of its members and every rebuild mints a new one, so
`/graph?view=topic&id=…` goes stale by design. The state answers a cluster it
can no longer find with a sentence:

```python
RECOMPUTED = (
    "This topic was recomputed — pick it again. A cluster's id is a digest of "
    "its members, so every rebuild mints a new one; a tag is the reference "
    "that survives."
)
```

An empty canvas would read as "this topic is empty", which is a different claim
and a false one. `_why_empty` picks between five sentences, so every blank
canvas says which of the five it is.

The durable reference is a `Tag`. A promoted tag stores `origin=topic` and never
the topic's id, which is why nothing about it can go stale.

## Where the page reads from

`mailarc-ui` may not import `app`, so every service comes out of the registry,
and each lookup happens inside the function that needs it. A lookup at module
level would run while `app/app.py` was still importing, before anything had been
published.

| Service | Answers |
| --- | --- |
| `GraphReader` | The picture |
| `AnalyticsReader` | The topics and circles in the root dropdown |
| `TagStore` | The tags in the dropdown, and every write the tag actions make |
| `ArchiveReader` | The message the reading pane opens |

Four for one page is more than any other page here reaches for, and it is the
honest count.

`GraphReader` itself lives in `mailarc-analytics`
([`queries/graphs.py`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-analytics/src/mailarc_analytics/queries/graphs.py))
and does three things no statement can do:

- **Degree is counted over the drawn edges.** A node's degree in the archive is
  not a number a canvas can size by, because the picture holds a dozen of a
  correspondent's four thousand edges.
- **Every weight is normalised within the subgraph**, so the heaviest node in
  this view is 1.0. A node with no such number keeps no key at all, because a
  missing weight and a weight of zero must not be drawn the same.
- **A dangling edge is dropped.** Every listing is cut at a limit, so an overlap
  can name a node the reader stopped short of, and cytoscape throws on an edge
  with an end it was never given.

## Testing it

| Test | Covers |
| --- | --- |
| `test_ui_graph_model.py` | Elements, stylesheet, layout, and the palette's two homes |
| `test_ui_graph_state.py` | A fake `GraphReader` in the registry: query parameters drive `load`, select opens a message, expand merges, sizing redraws without a re-read |
| `test_ui_tags_state.py` | A fake `TagStore`: promote validates the name, accept-all writes `source=accepted` |
| `test_ui_kit_components.py` | `graph_canvas` renders `GraphCanvas` with the props |
| `test_queries_graphs.py` | The reader against a fake session, per statement |
| `test_queries_graphs_local.py` | The reader against the vendored FalkorDB and a planted corpus |

The registry fakes go in and come out with `snapshot` / `restore`, the pattern
the dashboard tests established.
