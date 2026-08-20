"""One diagram description, two outputs: an editable ``.drawio`` and an SVG.

The ``.drawio`` file is what a human edits — draw.io, diagrams.net or the
VS Code Draw.io Integration extension all open it, and it is plain
(uncompressed) XML so a change shows up in a diff. The ``.svg`` beside it is
what Markdown renders, because no viewer of this repository is required to have
draw.io installed.

Both come out of the same :class:`Diagram`, so they cannot drift. Edit the
description in ``diagrams.py`` and run ``python docs/diagrams/build.py``; edit
the ``.drawio`` by hand and the SVG is stale until someone re-exports it, which
is the trade every diagram-as-code setup makes.

Only what the diagrams here actually need is implemented: rectangles, rounded
rectangles, dashed group frames, notes, and orthogonal edges that leave and
enter a named side of a box. An edge names its exit and entry side rather than
letting a router guess, so draw.io's ``orthogonalEdgeStyle`` and the small
router in :func:`_route` agree on the same path.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict

FONT = "Helvetica, Arial, sans-serif"
"""Named identically in both outputs so the SVG matches a draw.io export."""

PADDING = 24
"""Space between the content bounds and the SVG edge."""

STUB = 14
"""How far an edge travels straight out of a box before it may turn."""

TEXT = "#1F2328"
MUTED = "#57606A"

PALETTE: dict[str, tuple[str, str]] = {
    "core": ("#E8F0FE", "#3B5BA5"),
    "sync": ("#E6F4EA", "#2E7D46"),
    "provider": ("#FEF7E0", "#A6740A"),
    "ui": ("#F3E8FD", "#6B3FA0"),
    "app": ("#EDEFF2", "#44546A"),
    "store": ("#FCE8E6", "#B3261E"),
    "external": ("#F1F3F4", "#5F6368"),
    "note": ("#FFFDE7", "#C9A227"),
    "accent": ("#E0F7FA", "#00707F"),
}
"""Fill and stroke per box kind. Explicit colours, so the SVG reads the same
whether the page around it is light or dark."""

SIDES: dict[str, tuple[float, float]] = {
    "n": (0.5, 0.0),
    "s": (0.5, 1.0),
    "e": (1.0, 0.5),
    "w": (0.0, 0.5),
    "ne": (0.85, 0.0),
    "nw": (0.15, 0.0),
    "se": (0.85, 1.0),
    "sw": (0.15, 1.0),
    "en": (1.0, 0.25),
    "es": (1.0, 0.75),
    "wn": (0.0, 0.25),
    "ws": (0.0, 0.75),
}
"""Relative attachment points, named by the side they sit on.

Two-letter names are the same side, offset along it — ``en`` is the upper
third of the east side. draw.io takes these as ``exitX``/``exitY`` verbatim.
"""

_OUTWARD: dict[str, tuple[int, int]] = {
    "n": (0, -1),
    "s": (0, 1),
    "e": (1, 0),
    "w": (-1, 0),
}


class Box(BaseModel):
    """One rectangle: where it is, what it says, and which palette it uses."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str
    x: int
    y: int
    w: int = 180
    h: int = 60
    kind: str = "core"
    """A key of :data:`PALETTE`, or ``group`` for a dashed frame."""

    sub: str = ""
    """A second, smaller line under the label. Empty means one line."""

    rounded: bool = True
    bold: bool = True

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def point(self, side: str) -> tuple[float, float]:
        """The absolute attachment point for one of :data:`SIDES`."""
        rx, ry = SIDES[side]
        return self.x + self.w * rx, self.y + self.h * ry


class Link(BaseModel):
    """One orthogonal edge, from a named side of a box to a named side of another."""

    model_config = ConfigDict(frozen=True)

    src: str
    dst: str
    label: str = ""
    exit: str = "s"
    entry: str = "n"
    dashed: bool = False
    arrow: bool = True
    colour: str = MUTED
    stub: int = STUB
    """How far this edge runs straight before it may turn.

    Raised for a feedback loop, so the lane it comes back along clears the
    boxes it runs past instead of grazing them.
    """

    label_dx: int = 0
    """Nudges the label off the line where two edges would otherwise overlap."""

    label_dy: int = 0


class Diagram(BaseModel):
    """A page: its name, its boxes and the edges between them."""

    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    boxes: tuple[Box, ...]
    links: tuple[Link, ...] = ()
    caption: str = ""

    def box(self, box_id: str) -> Box:
        for candidate in self.boxes:
            if candidate.id == box_id:
                return candidate
        raise KeyError(f"{self.name}: no box named {box_id!r}")


def write(diagram: Diagram, directory: Path) -> tuple[Path, Path]:
    """Write ``<name>.drawio`` and ``<name>.svg``; return both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    drawio = directory / f"{diagram.name}.drawio"
    svg = directory / f"{diagram.name}.svg"
    drawio.write_text(to_drawio(diagram), encoding="utf-8")
    svg.write_text(to_svg(diagram), encoding="utf-8")
    return drawio, svg


# --------------------------------------------------------------------------- #
# draw.io
# --------------------------------------------------------------------------- #


def to_drawio(diagram: Diagram) -> str:
    """The mxGraph XML draw.io opens, uncompressed so a diff is readable."""
    cells = [
        '<mxCell id="0" />',
        '<mxCell id="1" parent="0" />',
    ]
    # Groups first, so a box drawn inside one is not hidden behind it.
    ordered = sorted(diagram.boxes, key=lambda one: one.kind != "group")
    cells.extend(_drawio_box(box) for box in ordered)
    cells.extend(
        _drawio_link(diagram, link, index) for index, link in enumerate(diagram.links)
    )
    body = "\n        ".join(cells)
    return (
        '<mxfile host="mail-archive" type="device">\n'
        f'  <diagram name="{escape(diagram.title)}" id="{escape(diagram.name)}">\n'
        '    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" '
        'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        'pageWidth="1169" pageHeight="826" math="0" shadow="0">\n'
        "      <root>\n"
        f"        {body}\n"
        "      </root>\n"
        "    </mxGraphModel>\n"
        "  </diagram>\n"
        "</mxfile>\n"
    )


def _drawio_box(box: Box) -> str:
    label = escape(box.label)
    if box.sub:
        label += "&lt;br&gt;&lt;font style=&quot;font-size: 10px&quot; color=&quot;"
        label += f"{MUTED}&quot;&gt;{escape(box.sub)}&lt;/font&gt;"
    return (
        f'<mxCell id="{escape(box.id)}" value="{label}" '
        f'style="{_drawio_style(box)}" vertex="1" parent="1">'
        f'<mxGeometry x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" '
        'as="geometry" /></mxCell>'
    )


def _drawio_style(box: Box) -> str:
    if box.kind == "group":
        return (
            "rounded=1;arcSize=6;whiteSpace=wrap;html=1;fillColor=none;"
            f"strokeColor=#9AA0A6;dashed=1;verticalAlign=top;align=left;"
            f"spacingLeft=10;spacingTop=4;fontSize=11;fontColor={MUTED};"
            f"fontFamily={FONT};"
        )
    fill, stroke = PALETTE.get(box.kind, PALETTE["core"])
    weight = 1 if box.bold else 0
    shape = f"rounded={1 if box.rounded else 0};arcSize=12;"
    return (
        f"{shape}whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
        f"fontColor={TEXT};fontSize=12;fontStyle={weight};fontFamily={FONT};"
        "verticalAlign=middle;align=center;"
    )


def _drawio_link(diagram: Diagram, link: Link, index: int) -> str:
    ex, ey = SIDES[link.exit]
    nx, ny = SIDES[link.entry]
    style = (
        f"edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;jettySize={link.stub};"
        f"exitX={ex};exitY={ey};exitDx=0;exitDy=0;"
        f"entryX={nx};entryY={ny};entryDx=0;entryDy=0;"
        f"strokeColor={link.colour};fontColor={MUTED};fontSize=11;"
        f"fontFamily={FONT};labelBackgroundColor=#FFFFFF;"
        f"dashed={1 if link.dashed else 0};"
        f"endArrow={'blockThin' if link.arrow else 'none'};endFill=1;"
    )
    # Referenced so a renamed box fails here rather than silently unlinking.
    diagram.box(link.src)
    diagram.box(link.dst)
    return (
        f'<mxCell id="edge{index}" value="{escape(link.label)}" style="{style}" '
        f'edge="1" parent="1" source="{escape(link.src)}" '
        f'target="{escape(link.dst)}">'
        '<mxGeometry relative="1" as="geometry" /></mxCell>'
    )


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #


def to_svg(diagram: Diagram) -> str:
    """A standalone SVG of the same page, for Markdown to embed."""
    bounds = _bounds(diagram)
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    shift_x, shift_y = -bounds[0], -bounds[1]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{escape(diagram.title)}">',
        f"<title>{escape(diagram.title)}</title>",
        _svg_defs(),
        f'<rect width="{width}" height="{height}" fill="#FFFFFF" />',
        f'<g transform="translate({shift_x},{shift_y})">',
    ]
    parts.extend(
        _svg_box(box)
        for box in sorted(diagram.boxes, key=lambda one: one.kind != "group")
    )
    parts.extend(_svg_link(diagram, link) for link in diagram.links)
    parts.append("</g></svg>\n")
    return "\n".join(parts)


def _svg_defs() -> str:
    """One arrowhead per edge colour used, keyed by a sanitised colour name."""
    markers = [
        f'<marker id="a{colour.lstrip("#")}" viewBox="0 0 10 10" refX="9" '
        'refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 9 5 L 0 9 z" fill="{colour}" /></marker>'
        for colour in {MUTED, TEXT, "#B3261E", "#2E7D46"}
    ]
    return "<defs>" + "".join(markers) + "</defs>"


def _svg_box(box: Box) -> str:
    radius = 8 if box.rounded else 0
    if box.kind == "group":
        rect = (
            f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" '
            f'rx="{radius}" fill="none" stroke="#9AA0A6" stroke-width="1" '
            'stroke-dasharray="6 4" />'
        )
        label = (
            f'<text x="{box.x + 12}" y="{box.y + 18}" font-family="{FONT}" '
            f'font-size="11" fill="{MUTED}">{escape(box.label)}</text>'
        )
        return rect + label

    fill, stroke = PALETTE.get(box.kind, PALETTE["core"])
    rect = (
        f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" '
        f'rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.4" />'
    )
    lines = [(box.label, 12, "600" if box.bold else "400", TEXT)]
    if box.sub:
        lines.append((box.sub, 10, "400", MUTED))
    return rect + _svg_lines(box.cx, box.cy, lines)


def _svg_lines(cx: float, cy: float, lines: list[tuple[str, int, str, str]]) -> str:
    """Centre a stack of text lines on ``(cx, cy)``."""
    gaps = [size + 4 for _, size, _, _ in lines]
    total = sum(gaps)
    top = cy - total / 2
    out = []
    for (text, size, weight, colour), gap in zip(lines, gaps, strict=True):
        top += gap
        baseline = top - gap * 0.25
        out.append(
            f'<text x="{cx:.1f}" y="{baseline:.1f}" text-anchor="middle" '
            f'font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
            f'fill="{colour}">{escape(text)}</text>'
        )
    return "".join(out)


def _svg_link(diagram: Diagram, link: Link) -> str:
    points = _points(diagram, link)
    path = " ".join(
        f"{'M' if index == 0 else 'L'} {x:.1f} {y:.1f}"
        for index, (x, y) in enumerate(points)
    )
    marker = f' marker-end="url(#a{link.colour.lstrip("#")})"' if link.arrow else ""
    dash = ' stroke-dasharray="5 4"' if link.dashed else ""
    line = (
        f'<path d="{path}" fill="none" stroke="{link.colour}" '
        f'stroke-width="1.4"{dash}{marker} />'
    )
    if not link.label:
        return line
    mx, my = _midpoint(points)
    mx += link.label_dx
    my += link.label_dy
    width = _plate_width(link.label)
    plate = (
        f'<rect x="{mx - width / 2:.1f}" y="{my - 9:.1f}" width="{width:.1f}" '
        'height="15" rx="3" fill="#FFFFFF" fill-opacity="0.95" />'
    )
    text = (
        f'<text x="{mx:.1f}" y="{my + 2:.1f}" text-anchor="middle" '
        f'font-family="{FONT}" font-size="10.5" fill="{MUTED}">'
        f"{escape(link.label)}</text>"
    )
    return line + plate + text


def _plate_width(label: str) -> float:
    """Roughly how wide the white plate behind an edge label has to be.

    An estimate, because measuring text needs a font engine neither output has
    at hand. It errs wide: an oversized plate hides a little of the line it
    sits on, an undersized one lets the line strike through the words.
    """
    return len(label) * 5.6 + 8


def _points(diagram: Diagram, link: Link) -> list[tuple[float, float]]:
    """The path one link takes, resolved against the diagram's boxes."""
    return _route(
        diagram.box(link.src), link.exit, diagram.box(link.dst), link.entry, link.stub
    )


def _route(
    source: Box, exit_side: str, target: Box, entry_side: str, stub: int = STUB
) -> list[tuple[float, float]]:
    """An orthogonal path: out of one box, into the other, at most two turns.

    The same shape draw.io's ``orthogonalEdgeStyle`` produces for the same
    exit and entry points, which is the whole reason a side is named rather
    than inferred.
    """
    start = source.point(exit_side)
    end = target.point(entry_side)
    out = _OUTWARD[exit_side[0]]
    into = _OUTWARD[entry_side[0]]

    first = (start[0] + out[0] * stub, start[1] + out[1] * stub)
    last = (end[0] + into[0] * stub, end[1] + into[1] * stub)

    horizontal_out = out[0] != 0
    horizontal_in = into[0] != 0

    if horizontal_out and horizontal_in:
        mid = (first[0] + last[0]) / 2
        middle = [(mid, first[1]), (mid, last[1])]
    elif not horizontal_out and not horizontal_in:
        mid = (first[1] + last[1]) / 2
        middle = [(first[0], mid), (last[0], mid)]
    elif horizontal_out:
        middle = [(last[0], first[1])]
    else:
        middle = [(first[0], last[1])]

    return _dedupe([start, first, *middle, last, end])


def _dedupe(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop consecutive duplicates, which a straight run through a stub makes."""
    kept: list[tuple[float, float]] = []
    for point in points:
        if (
            not kept
            or abs(point[0] - kept[-1][0]) > 0.5
            or abs(point[1] - kept[-1][1]) > 0.5
        ):
            kept.append(point)
    return kept


def _midpoint(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The point half way along the path, measured by length rather than index."""
    segments = list(pairwise(points))
    lengths = [abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in segments]
    half = sum(lengths) / 2
    walked = 0.0
    for (a, b), length in zip(segments, lengths, strict=True):
        if walked + length >= half and length:
            ratio = (half - walked) / length
            return a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio
        walked += length
    return points[len(points) // 2]


def _bounds(diagram: Diagram) -> tuple[int, int, int, int]:
    """The content box, grown by :data:`PADDING` on every side.

    Edge paths count, not only boxes: a feedback loop comes back along a lane
    outside every box it passes, and measuring the boxes alone would crop it.
    """
    xs = [box.x for box in diagram.boxes] + [box.x + box.w for box in diagram.boxes]
    ys = [box.y for box in diagram.boxes] + [box.y + box.h for box in diagram.boxes]
    for link in diagram.links:
        points = _points(diagram, link)
        for x, y in points:
            xs.append(int(x))
            ys.append(int(y))
        if link.label:
            mx, my = _midpoint(points)
            half = _plate_width(link.label) / 2
            xs += [int(mx + link.label_dx - half), int(mx + link.label_dx + half)]
            ys += [int(my + link.label_dy - 9), int(my + link.label_dy + 6)]
    return (
        min(xs) - PADDING,
        min(ys) - PADDING,
        max(xs) + PADDING,
        max(ys) + PADDING,
    )
