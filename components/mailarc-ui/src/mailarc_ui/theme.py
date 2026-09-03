"""The archive's Mantine theme — warm grays, one coral accent, Inter.

Every value here is transcribed from the design spec the redesign works to: a
warm-gray canvas (``#F4F4F2``) with white surfaces and hairline borders, ink
text on muted gray, and a single coral-orange accent (``#ED5A2D``) that owns
primary buttons, active states and focus. The stylesheet
(``assets/css/mail-archive.css``) states the same palette as ``--ma-*`` custom
properties for the classes; this module states it where Mantine reads colours —
palettes, component defaults, radius and shadow scales — so a stock ``mn.*``
component and a classed one land on the same design.

Usage
-----
Call :func:`set_mailarc_theme` **before** constructing ``rx.App()``::

    from mailarc_ui.theme import set_mailarc_theme

    set_mailarc_theme()
    app = rx.App(...)

Component authors reference the Mantine palette by key rather than by hex::

    mn.button("Search")  # primary — coral, from the theme
    mn.text("hint", c="gray.6")  # muted #8B8B88
    mn.box(bg="gray.1")  # the canvas tint

Palettes
--------
- ``coral`` — the 10-shade accent ramp; shade 5 is the primary ``#ED5A2D``.
- ``gray``  — Mantine's neutral key, overridden with the archive's warm grays
  so no component ever falls back to a stock blue-gray: inputs, borders,
  placeholder text and disabled states all pick these up automatically.
"""

from __future__ import annotations

import appkit_mantine as mn
from appkit_mantine.theme import ThemeDict

from mailarc_ui.styles import FONT_FAMILY, MONO_FONT_FAMILY


class COLORS:
    """Named design tokens, mirrored by ``--ma-*`` in the stylesheet."""

    CANVAS = "#F4F4F2"
    """The page ground — warm gray, never white."""

    SURFACE = "#FFFFFF"
    """Every raised thing: cards, the reading pane, the active rail item."""

    BORDER = "#E9E9E6"
    """Every hairline."""

    INK = "#1A1A18"
    """Primary text."""

    MUTED = "#8B8B88"
    """Secondary text — senders' times, previews, field labels."""

    ACCENT = "#ED5A2D"
    """The coral-orange that owns primary buttons, active states and focus."""


class Palette:
    """The graph canvas' colours, as concrete hexes rather than token names.

    Every other colour in this application reaches the screen as a class or a
    ``var(--ma-…)``, and this is the one place that cannot: cytoscape paints
    into a ``<canvas>`` element, so nothing it draws is a DOM node a stylesheet
    can match and no custom property is resolvable from inside it. The colours
    have to be handed to it as values, from Python.

    Six kinds and four pieces of chrome, in both colour schemes. A kind's hue is
    the same idea in both — coral is always a message — at the lightness the
    ground it sits on needs.

    The six kinds exist twice: here, and as ``--ma-graph-<kind>`` in
    ``assets/css/mail-archive.css``, which is what the legend dot beside the
    canvas wears. The three canvas-only colours — the edge, the label ink, the
    selection ring — are here *only*: a ``--ma-*`` token nothing paints reads as
    a design decision that was made and is not one, and
    ``tests/test_stylesheets.py`` fails a run over it. ``surface`` is the fourth
    and is not a colour of its own: every node is ringed in it so two touching
    circles stay apart, which only works while it equals ``--ma-surface``, the
    ground ``.ma-graph-canvas`` is painted with.

    ``test_ui_graph_model.TestThePaletteHasTwoHomes`` parses the stylesheet and
    holds both agreements.
    """

    LIGHT: dict[str, str] = {
        "message": "#ED5A2D",  # the accent: the archive is made of these
        "address": "#5F7A8C",  # slate
        "thread": "#7A7871",  # graphite, as `--ma-meter-cool`
        "topic": "#2E8B7A",  # teal
        "tag": "#C08A2E",  # amber — the one thing a person named
        "community": "#8B5E9C",  # plum
        "edge": "#D5D4CF",
        "text": "#1A1A18",
        "selected": "#1A1A18",  # an ink ring, so it reads over any fill
        "surface": "#FFFFFF",  # the halo, and so also `--ma-surface`
    }
    """On the warm-gray canvas."""

    DARK: dict[str, str] = {
        "message": "#F07144",
        "address": "#8FA9BA",
        "topic": "#4FB8A2",
        "thread": "#9C998F",
        "tag": "#DCA84A",
        "community": "#B084C4",
        "edge": "#4A4741",
        "text": "#F2F1EF",
        "selected": "#F2F1EF",
        "surface": "#1F1E1C",
    }
    """On the near-black one — the same hues, lifted."""

    @classmethod
    def of(cls, *, dark: bool) -> dict[str, str]:
        """The palette for the scheme the reader is in."""
        return cls.DARK if dark else cls.LIGHT


#: The accent ramp. Shade 5 is the primary; 6 is its hover.
_CORAL_PALETTE: list[str] = [
    "#FEF1EB",  # 0 — the soft tint a relevance chip sits on
    "#FCDFD2",  # 1
    "#F9C4AC",  # 2
    "#F5A480",  # 3
    "#F17E54",  # 4 — the primary shade on a dark scheme
    "#ED5A2D",  # 5 — the primary
    "#D74B20",  # 6 — hover
    "#B43C18",  # 7
    "#8F2E12",  # 8
    "#6A220D",  # 9
]

#: Warm neutrals, canvas (1) to ink (9). Replaces Mantine's stock ``gray``.
_WARM_GRAY_PALETTE: list[str] = [
    "#FAFAF8",  # 0
    "#F4F4F2",  # 1 — the canvas
    "#EFEFEC",  # 2
    "#E9E9E6",  # 3 — the hairline
    "#DBDBD7",  # 4
    "#B8B8B4",  # 5
    "#8B8B88",  # 6 — muted text
    "#6A6A67",  # 7
    "#40403E",  # 8
    "#1A1A18",  # 9 — ink
]

#: Soft shadows: an ink so dilute it reads as depth, never as an outline.
_SHADOWS: dict[str, str] = {
    "xs": "0 1px 2px rgba(26, 26, 24, 0.04)",
    "sm": "0 1px 3px rgba(26, 26, 24, 0.05)",
    "md": ("0 6px 20px -8px rgba(26, 26, 24, 0.08), 0 2px 6px rgba(26, 26, 24, 0.04)"),
    "lg": (
        "0 14px 36px -12px rgba(26, 26, 24, 0.12), 0 4px 10px rgba(26, 26, 24, 0.05)"
    ),
    "xl": (
        "0 28px 70px -18px rgba(26, 26, 24, 0.22), "
        "0 8px 20px -8px rgba(26, 26, 24, 0.10)"
    ),
}


def create_mailarc_theme() -> ThemeDict:
    """Return the Mantine ThemeOverride dict for the archive's design system.

    One theme for both colour schemes: Mantine derives its dark values from
    the same palettes, and the surfaces that genuinely change with the scheme
    are the stylesheet's business (``--ma-*`` has a dark block).

    The component defaults exist so kit call sites stay prop-free — a kit
    input never re-passes ``radius`` or ``size``, which is what let fourteen
    hand-built cards drift the last time defaults lived at call sites.
    """
    return mn.create_theme(
        # ── Palette ──────────────────────────────────────────────────────
        primary_color="coral",
        primary_shade={"light": 5, "dark": 4},
        white=COLORS.SURFACE,
        black=COLORS.INK,
        colors={
            "coral": _CORAL_PALETTE,
            # Replace Mantine's neutral gray so components referencing
            # gray-N (input borders, SegmentedControl track, placeholder
            # and disabled text, …) pick up warm values, never stock ones.
            "gray": _WARM_GRAY_PALETTE,
        },
        auto_contrast=True,
        # ── Typography ───────────────────────────────────────────────────
        font_family=FONT_FAMILY,
        font_family_monospace=MONO_FONT_FAMILY,
        font_smoothing=True,
        headings={
            "fontFamily": FONT_FAMILY,
            "fontWeight": "700",
            "sizes": {
                "h1": {"fontSize": "1.5rem", "lineHeight": "1.25"},
                "h2": {"fontSize": "1.25rem", "lineHeight": "1.3"},
                "h3": {"fontSize": "1.0625rem", "lineHeight": "1.35"},
                "h4": {"fontSize": "0.9375rem", "lineHeight": "1.4"},
                "h5": {"fontSize": "0.875rem", "lineHeight": "1.4"},
                "h6": {"fontSize": "0.8125rem", "lineHeight": "1.4"},
            },
        },
        font_sizes={
            "xs": "0.6875rem",  # 11px — the mono field-label scale
            "sm": "0.8125rem",  # 13px — metadata, previews
            "md": "0.875rem",  # 14px — the default body
            "lg": "1rem",  # 16px
            "xl": "1.125rem",  # 18px
        },
        line_heights={
            "xs": "1.4",
            "sm": "1.45",
            "md": "1.5",
            "lg": "1.55",
            "xl": "1.6",
        },
        # ── Shape ────────────────────────────────────────────────────────
        default_radius="md",
        radius={
            "xs": "8px",  # chips
            "sm": "10px",
            "md": "12px",  # controls, panels
            "lg": "16px",
            "xl": "20px",
        },
        # ── Shadows ──────────────────────────────────────────────────────
        shadows=_SHADOWS,
        # ── Interaction ──────────────────────────────────────────────────
        cursor_type="pointer",
        focus_ring="auto",
        # ── Component defaults ───────────────────────────────────────────
        components={
            "Button": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "ActionIcon": {
                "defaultProps": {"radius": "md", "size": "md"},
            },
            "TextInput": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "Textarea": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "PasswordInput": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "NumberInput": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "Select": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "MultiSelect": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "DateInput": {
                "defaultProps": {"radius": "md", "size": "sm"},
            },
            "SegmentedControl": {
                "defaultProps": {"radius": "xl", "size": "sm"},
            },
            "Badge": {
                "defaultProps": {"radius": "xs", "variant": "light", "size": "sm"},
            },
            "Paper": {
                "defaultProps": {"radius": "md", "shadow": "none"},
            },
            "Tooltip": {
                "defaultProps": {"radius": "sm", "openDelay": 200},
            },
        },
        # ── Semantic tokens forwarded to theme.other ─────────────────────
        other={
            "canvas": COLORS.CANVAS,
            "surface": COLORS.SURFACE,
            "border": COLORS.BORDER,
            "ink": COLORS.INK,
            "muted": COLORS.MUTED,
            "accent": COLORS.ACCENT,
        },
    )


def set_mailarc_theme() -> None:
    """Register the archive's theme as the app-wide Mantine theme.

    Call once **before** constructing ``rx.App()`` — the theme is forwarded
    to the root ``MantineProvider`` that wraps every page automatically.
    """
    mn.set_app_theme(create_mailarc_theme())
