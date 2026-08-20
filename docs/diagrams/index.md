# Diagrams

Each diagram is two files generated from one description:

| File | For |
| --- | --- |
| `<name>.drawio` | Opening and editing in draw.io. Plain XML, so a change shows in a diff |
| `<name>.svg` | Markdown. No viewer of this repository has to have draw.io installed |

```sh
uv run python docs/diagrams/build.py
```

## Editing one

**Preferred: edit the description.** [`diagrams.py`](https://github.com/jenreh/mail-archive/blob/main/docs/diagrams/diagrams.py) holds every
diagram as data — boxes with coordinates, links with a named exit and entry
side. Change it, re-run `build.py`, and both files stay in step.

**Also fine: edit the `.drawio`.** [draw.io](https://app.diagrams.net), the
desktop app, or the VS Code *Draw.io Integration* extension all open it. But the
SVG is then stale until someone re-exports it by hand, which is the trade every
diagram-as-code setup makes.

## The description

```python
Box(
    id="core",
    label="mailarc-core",
    sub="mail · archive · graph",
    x=310,
    y=350,
    w=440,
    h=60,
    kind="core",
)
Link(src="sync", dst="core", exit="s", entry="n", label="imports")
```

`kind` picks a palette entry (`core`, `sync`, `provider`, `ui`, `app`, `store`,
`external`, `note`, `accent`, or `group` for a dashed frame).

An edge names the side it leaves and the side it enters — `n`, `s`, `e`, `w`,
plus offset variants like `ne` (top side, 85 % along) and `en` (east side, upper
quarter). That is deliberate: naming the sides is what lets draw.io's
`orthogonalEdgeStyle` and the small router in [`render.py`](https://github.com/jenreh/mail-archive/blob/main/docs/diagrams/render.py) produce
the same path, instead of each guessing its own.

`stub` widens the lane an edge travels before it may turn — raise it for a
feedback loop that has to clear the boxes it runs past.

## Layout tips

Coordinates are hand-placed on a 10-pixel grid, the same grid draw.io snaps to.

- Give an edge a clear lane rather than letting it cross a box. Widen the gap
  between two boxes if it needs one.
- `label_dx` / `label_dy` nudge a label off a collision.
- Edges crossing each other is fine. An edge crossing a box is not.
- Keep a diagram to one idea. If it would need a legend, it is two diagrams.

## Checking one

There is no draw.io on a CI machine, and no SVG rasteriser is a project
dependency. To look at one locally:

```sh
brew install librsvg
rsvg-convert -z 1.2 docs/diagrams/architecture.svg -o /tmp/check.png
```

Or just open the `.svg` in a browser.
