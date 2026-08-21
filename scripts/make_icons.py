#!/usr/bin/env python
"""Render the macOS app icon set for the Tauri bundle from the project mark.

Build-time only — this never runs on a user's machine. `docs/public/favicon.svg`
holds the mark at its smallest useful size; this lifts that artwork onto Apple's
macOS icon grid and rasterises it into everything `tauri.conf.json` bundles.

The grid is Apple's: a 1024px canvas holding an 824px rounded square, leaving a
100px margin for the drop shadow to fall into. The corner is a superellipse,
which tracks Apple's continuous-corner rectangle closely enough that the
silhouette sits flush with the system icons beside it in the Dock — a plain
`rx` rounded rect visibly does not.

The flat tile of the favicon becomes a shaded one, so the mark reads as a
physical object at 512px rather than a scaled-up sticker. Anything the source
drew in the tile colour (the message node's ring) is repainted with the same
gradient in the mark's own coordinates, so it stays invisibly seated on the
tile instead of turning into a mismatched blue ring.

Every size is rendered straight from the vector rather than downsampled from
one large bitmap. The sizes at or below COMPACT_MAX_SIZE render from a second,
simplified master instead of the full one: at 16px the mark's strokes land on
0.8px and the message node on 2px, so a faithful copy of the large artwork just
turns to mush.

Requires librsvg on the build machine: `brew install librsvg`.

Usage:
    uv run python scripts/make_icons.py [--source SVG] [--output DIR]
"""

from __future__ import annotations

import argparse
import colorsys
import copy
import logging
import math
import shutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("make_icons")

DEFAULT_SOURCE = Path("docs/public/favicon.svg")
DEFAULT_OUTPUT = Path("src-tauri/icons")

#: Apple's macOS icon grid: an 824px body centred in a 1024px canvas.
CANVAS = 1024
BODY = 824
MARGIN = (CANVAS - BODY) / 2

#: Superellipse exponent approximating Apple's continuous-corner rectangle, and
#: the number of samples along it — 512 puts the chord error below 0.01px at
#: 1024, well under a pixel at every size that gets rendered.
SQUIRCLE_EXPONENT = 5.0
SQUIRCLE_STEPS = 512

#: Lightness shifts that turn the flat source colour into the tile gradient.
TILE_TOP_SHIFT = 0.06
TILE_BOTTOM_SHIFT = -0.08

#: The lit top edge. Inset by half its width so it sits inside the silhouette.
RIM_WIDTH = 3.0

#: Drop shadow, tuned to the one Mail, Notes and Reminders bake into their own
#: icons: peak alpha ~72 directly under the body, fading out within ~27px. Its
#: whole spread has to fit inside MARGIN or the canvas would clip it.
SHADOW_OFFSET = 10
SHADOW_BLUR = 16
SHADOW_OPACITY = 0.22
SHADOW_COLOR = "#0B1220"

#: At or below this size the mark renders from a compact master instead: no
#: shadow, heavier strokes, and the message node dropped. A faithful downscale
#: turns to mush there — at 16px the node is 2px across and the mark's 2-unit
#: strokes land on 0.8px — so the small sizes get the simplified artwork
#: Apple's guidance asks for rather than a shrunken copy of the large one.
COMPACT_MAX_SIZE = 32
COMPACT_STROKE_SCALE = 1.4
COMPACT_DROPPED = ("circle",)

TILE_GRADIENT = "tile"
MARK_GRADIENT = "mark-tile"

#: Used only if the source has no full-bleed tile to take a colour from.
FALLBACK_BACKGROUND = "#3B5BA5"

#: Sizes tauri.conf.json references directly.
PNG_SIZES = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}

#: Contents of the .iconset `iconutil` compiles into icon.icns.
ICONSET_SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


class Artwork(BaseModel):
    """The mark lifted out of the source SVG, in the source's own units."""

    model_config = ConfigDict(frozen=True)

    body: str
    compact_body: str
    extent: float
    background: str


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _covers(element: ET.Element, extent: float) -> bool:
    """True for a rect spanning the whole viewBox — the tile behind the mark."""
    try:
        width = float(element.get("width", "0"))
        height = float(element.get("height", "0"))
    except ValueError:
        return False
    return width >= extent and height >= extent


def _repaint(element: ET.Element, background: str, stroke_scale: float = 1.0) -> str:
    """Serialise one mark element, tying tile-coloured paint to the gradient."""
    clone = copy.deepcopy(element)
    for node in clone.iter():
        node.tag = _local_name(node.tag)
        for attribute in ("fill", "stroke"):
            value = node.get(attribute)
            if value and value.lower() == background.lower():
                node.set(attribute, f"url(#{MARK_GRADIENT})")
        width = node.get("stroke-width")
        if width and stroke_scale != 1.0:
            node.set("stroke-width", f"{float(width) * stroke_scale:g}")
    return ET.tostring(clone, encoding="unicode").strip()


def load_artwork(source: Path) -> Artwork:
    """Read the mark out of an SVG, dropping its title and background tile."""
    root = ET.parse(source).getroot()  # noqa: S314 - a fixed file from this repo
    view_box = root.get("viewBox", "").split()
    extent = (
        float(view_box[2]) if len(view_box) == 4 else float(root.get("width", "32"))
    )

    children = [child for child in root if _local_name(child.tag) != "title"]
    tile = next(
        (
            child
            for child in children
            if _local_name(child.tag) == "rect" and _covers(child, extent)
        ),
        None,
    )
    background = (
        FALLBACK_BACKGROUND if tile is None else tile.get("fill", FALLBACK_BACKGROUND)
    )
    marks = [child for child in children if child is not tile]
    coarse = [mark for mark in marks if _local_name(mark.tag) not in COMPACT_DROPPED]

    logger.debug(
        "mark: %d elements on a %g grid over %s", len(marks), extent, background
    )
    return Artwork(
        body="\n".join(_repaint(mark, background) for mark in marks),
        compact_body="\n".join(
            _repaint(mark, background, COMPACT_STROKE_SCALE) for mark in coarse
        ),
        extent=extent,
        background=background,
    )


def _shade(color: str, shift: float) -> str:
    """Move a hex colour along lightness, keeping its hue and saturation."""
    red, green, blue = (int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    red, green, blue = colorsys.hls_to_rgb(
        hue, min(1.0, max(0.0, lightness + shift)), saturation
    )
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def _squircle(half: float) -> str:
    """A superellipse of the given half-width, centred on the canvas."""
    centre = CANVAS / 2
    power = 2.0 / SQUIRCLE_EXPONENT
    points: list[str] = []
    for step in range(SQUIRCLE_STEPS):
        angle = 2.0 * math.pi * step / SQUIRCLE_STEPS
        cosine, sine = math.cos(angle), math.sin(angle)
        x = centre + half * math.copysign(abs(cosine) ** power, cosine)
        y = centre + half * math.copysign(abs(sine) ** power, sine)
        points.append(f"{x:.2f} {y:.2f}")
    return "M" + "L".join(points) + "Z"


def build_master_svg(artwork: Artwork, *, compact: bool = False) -> str:
    """The 1024px master a raster size is rendered from.

    The compact one serves the sizes at or below COMPACT_MAX_SIZE; it drops the
    shadow, which at 16px is a grey smear over an eighth of the canvas.
    """
    top = _shade(artwork.background, TILE_TOP_SHIFT)
    bottom = _shade(artwork.background, TILE_BOTTOM_SHIFT)
    scale = BODY / artwork.extent
    body = textwrap.indent(artwork.compact_body if compact else artwork.body, "      ")
    shadow = "" if compact else ' filter="url(#shadow)"'
    shadow_filter = (
        ""
        if compact
        else f"""
    <filter id="shadow" x="-15%" y="-15%" width="130%" height="130%">
      <feDropShadow dx="0" dy="{SHADOW_OFFSET}" stdDeviation="{SHADOW_BLUR}"
                    flood-color="{SHADOW_COLOR}" flood-opacity="{SHADOW_OPACITY}" />
    </filter>"""
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS}" height="{CANVAS}"
     viewBox="0 0 {CANVAS} {CANVAS}" role="img" aria-label="mail-archive">
  <title>mail-archive</title>
  <defs>
    <linearGradient id="{TILE_GRADIENT}" gradientUnits="userSpaceOnUse"
                    x1="0" y1="{MARGIN:g}" x2="0" y2="{MARGIN + BODY:g}">
      <stop offset="0" stop-color="{top}" />
      <stop offset="1" stop-color="{bottom}" />
    </linearGradient>
    <!-- The same gradient expressed in the mark's coordinates, so paint the
         source took from the tile still matches the tile underneath it. -->
    <linearGradient id="{MARK_GRADIENT}" gradientUnits="userSpaceOnUse"
                    x1="0" y1="0" x2="0" y2="{artwork.extent:g}">
      <stop offset="0" stop-color="{top}" />
      <stop offset="1" stop-color="{bottom}" />
    </linearGradient>
    <linearGradient id="rim" gradientUnits="userSpaceOnUse"
                    x1="0" y1="{MARGIN:g}" x2="0" y2="{MARGIN + BODY * 0.45:g}">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity="0.28" />
      <stop offset="1" stop-color="#FFFFFF" stop-opacity="0" />
    </linearGradient>{shadow_filter}
  </defs>
  <path d="{_squircle(BODY / 2)}"
        fill="url(#{TILE_GRADIENT})"{shadow} />
  <path d="{_squircle(BODY / 2 - RIM_WIDTH / 2)}"
        fill="none" stroke="url(#rim)" stroke-width="{RIM_WIDTH:g}" />
  <g transform="translate({MARGIN:g} {MARGIN:g}) scale({scale:.6g})">
{body}
  </g>
</svg>
"""


class Masters(BaseModel):
    """The two rendered-from files: the full mark, and the small-size one."""

    model_config = ConfigDict(frozen=True)

    full: Path
    compact: Path

    def pick(self, size: int) -> Path:
        return self.compact if size <= COMPACT_MAX_SIZE else self.full


def find_renderer() -> Path:
    """The SVG rasteriser, or a message saying how to get one."""
    found = shutil.which("rsvg-convert")
    if found is None:
        msg = (
            "rsvg-convert not found — the app icons are rendered from "
            "docs/public/favicon.svg at build time. Install it with "
            "`brew install librsvg`."
        )
        raise RuntimeError(msg)
    return Path(found)


def _run(command: list[str], failure: str) -> None:
    result = subprocess.run(  # noqa: S603 - resolved binaries, local paths only
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        msg = f"{failure}: {result.stderr.strip()}"
        raise RuntimeError(msg)


def render(renderer: Path, master: Path, size: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(renderer),
            "--width",
            str(size),
            "--height",
            str(size),
            "--background-color",
            "none",
            str(master),
            "--output",
            str(target),
        ],
        f"rsvg-convert failed on {target.name}",
    )
    logger.debug("rendered %s at %dpx", target.name, size)


def write_icons(source: Path, output_dir: Path) -> None:
    renderer = find_renderer()
    output_dir.mkdir(parents=True, exist_ok=True)

    artwork = load_artwork(source)
    masters = Masters(
        full=output_dir / "icon.svg",
        compact=output_dir / "icon-small.svg",
    )
    masters.full.write_text(build_master_svg(artwork), encoding="utf-8")
    masters.compact.write_text(
        build_master_svg(artwork, compact=True), encoding="utf-8"
    )
    logger.info(
        "wrote %s and %s from %s", masters.full.name, masters.compact.name, source
    )

    for name, size in PNG_SIZES.items():
        render(renderer, masters.pick(size), size, output_dir / name)
    logger.info("wrote %d PNGs", len(PNG_SIZES))

    if sys.platform == "darwin":
        _write_icns(renderer, masters, output_dir)


def _write_icns(renderer: Path, masters: Masters, output_dir: Path) -> None:
    iconset = output_dir / "icon.iconset"
    for name, size in ICONSET_SIZES.items():
        render(renderer, masters.pick(size), size, iconset / name)

    _run(
        [
            "/usr/bin/iconutil",
            "-c",
            "icns",
            str(iconset),
            "-o",
            str(output_dir / "icon.icns"),
        ],
        "iconutil failed",
    )

    for child in iconset.iterdir():
        child.unlink()
    iconset.rmdir()
    logger.info("wrote icon.icns (%d representations)", len(ICONSET_SIZES))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    write_icons(args.source, args.output)
    logger.info("Icons ready in %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
