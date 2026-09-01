import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import make_icons  # noqa: E402

# The project mark: a tile, a title, two stroked paths and the message node.
FAVICON = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img">
  <title>mail-archive</title>
  <rect width="32" height="32" rx="7" fill="#3B5BA5" />
  <path d="M6.5 10.5h19v11.5a1.5 1.5 0 0 1-1.5 1.5H8a1.5 1.5 0 0 1-1.5-1.5z"
        fill="none" stroke="#FFFFFF" stroke-width="2" stroke-linejoin="round" />
  <circle cx="16" cy="18" r="2.6" fill="#E0F7FA" stroke="#3B5BA5" stroke-width="1.6" />
</svg>
"""

#: Half-width of the real macOS icon silhouette at a given height, both as a
#: fraction of the half-side, measured off Mail.app's ApplicationIcon.icns.
#: A superellipse fits these to an rms of 0.007; a plain rounded rect to 0.012.
MACOS_PROFILE = ((0.5, 0.9951), (0.7, 0.9709), (0.9, 0.8301), (0.95, 0.7549))
PROFILE_TOLERANCE = 0.015


@pytest.fixture
def favicon(tmp_path: Path) -> Path:
    source = tmp_path / "favicon.svg"
    source.write_text(FAVICON, encoding="utf-8")
    return source


def test_load_artwork_takes_its_colour_from_the_tile_it_drops(favicon: Path) -> None:
    artwork = make_icons.load_artwork(favicon)

    assert artwork.background == "#3B5BA5"
    assert artwork.extent == 32
    assert "<rect" not in artwork.body
    assert "<title" not in artwork.body
    assert "<path" in artwork.body


def test_load_artwork_repaints_tile_coloured_marks_with_the_gradient(
    favicon: Path,
) -> None:
    """The node's ring was the tile colour; on a shaded tile it has to track it."""
    artwork = make_icons.load_artwork(favicon)

    assert f'stroke="url(#{make_icons.MARK_GRADIENT})"' in artwork.body
    assert "#3B5BA5" not in artwork.body
    assert 'fill="#E0F7FA"' in artwork.body  # untouched: not the tile colour


def test_compact_body_drops_sub_pixel_detail_and_thickens_strokes(
    favicon: Path,
) -> None:
    artwork = make_icons.load_artwork(favicon)

    assert "<circle" in artwork.body
    assert "<circle" not in artwork.compact_body
    assert 'stroke-width="2"' in artwork.body
    assert 'stroke-width="2.8"' in artwork.compact_body


def test_master_places_the_mark_on_apples_grid(favicon: Path) -> None:
    svg = make_icons.build_master_svg(make_icons.load_artwork(favicon))

    assert 'width="1024" height="1024"' in svg
    # 824px body, 100px margin all round, so the 32-unit mark scales by 25.75.
    assert "translate(100 100) scale(25.75)" in svg
    assert 'filter="url(#shadow)"' in svg


def test_compact_master_carries_no_shadow(favicon: Path) -> None:
    """At 16px the shadow is a grey smear over an eighth of the canvas."""
    svg = make_icons.build_master_svg(make_icons.load_artwork(favicon), compact=True)

    assert "shadow" not in svg
    assert "<filter" not in svg
    assert "translate(100 100) scale(25.75)" in svg


@pytest.mark.parametrize(
    ("size", "expected"),
    [(16, "compact"), (32, "compact"), (33, "full"), (512, "full")],
)
def test_masters_pick_the_compact_one_only_for_small_sizes(
    size: int, expected: str
) -> None:
    masters = make_icons.Masters(full=Path("icon.svg"), compact=Path("icon-small.svg"))

    assert masters.pick(size) == getattr(masters, expected)


def test_shade_moves_lightness_in_both_directions() -> None:
    assert make_icons._shade("#3B5BA5", 0.0) == "#3B5BA5"
    assert make_icons._shade("#3B5BA5", 0.06) != "#3B5BA5"
    lighter = make_icons._shade("#3B5BA5", 0.06)
    darker = make_icons._shade("#3B5BA5", -0.08)
    assert sum(int(lighter[i : i + 2], 16) for i in (1, 3, 5)) > sum(
        int(darker[i : i + 2], 16) for i in (1, 3, 5)
    )


def _half_widths(path: str) -> dict[float, float]:
    """Sample the generated silhouette the way the real icons were measured."""
    points = [
        (float(x), float(y)) for x, y in re.findall(r"(-?[\d.]+) (-?[\d.]+)", path)
    ]
    centre = make_icons.CANVAS / 2
    half = make_icons.BODY / 2
    sampled: dict[float, float] = {}
    for height, _ in MACOS_PROFILE:
        target = centre + height * half
        nearest = min(points, key=lambda p: abs(p[1] - target))
        band = [p for p in points if abs(p[1] - nearest[1]) < 1.0]
        sampled[height] = max(abs(p[0] - centre) for p in band) / half
    return sampled


def test_silhouette_tracks_the_measured_macos_shape() -> None:
    """A plain rounded rect is visibly wrong beside the system icons."""
    sampled = _half_widths(make_icons._squircle(make_icons.BODY / 2))

    for height, expected in MACOS_PROFILE:
        assert sampled[height] == pytest.approx(expected, abs=PROFILE_TOLERANCE), (
            f"half-width at {height} of the half-side"
        )


def test_squircle_is_a_closed_path_on_the_canvas() -> None:
    path = make_icons._squircle(make_icons.BODY / 2)

    assert path.startswith("M")
    assert path.endswith("Z")
    coordinates = re.findall(r"(-?[\d.]+) (-?[\d.]+)", path)
    assert len(coordinates) == make_icons.SQUIRCLE_STEPS
    assert all(
        make_icons.MARGIN - 0.01
        <= float(value)
        <= make_icons.CANVAS - make_icons.MARGIN + 0.01
        for pair in coordinates
        for value in pair
    )
