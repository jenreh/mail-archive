"""The theme dict says what the design spec says — checked value by value.

``create_mailarc_theme`` is a dictionary handed to Mantine at app start, and
a wrong key in it fails like the silent stylesheet 404 the stylesheet test
exists for: no import error, no failing render — just stock Mantine blues and
grays where the design asks for coral on warm gray. So the anchors of the
spec are pinned here: the primary shade, the gray override, the two font
stacks, the radius remap, and the component defaults the kit relies on to
stay prop-free.
"""

import appkit_mantine as mn

from mailarc_ui.theme import create_mailarc_theme, set_mailarc_theme

THEME = create_mailarc_theme()


class TestPalettes:
    def test_coral_is_the_primary(self) -> None:
        assert THEME["primaryColor"] == "coral"

    def test_both_palettes_carry_the_ten_shades_mantine_requires(self) -> None:
        for name in ("coral", "gray"):
            assert len(THEME["colors"][name]) == 10, name

    def test_shade_five_is_the_accent(self) -> None:
        assert THEME["colors"]["coral"][5].upper() == "#ED5A2D"

    def test_the_gray_override_is_the_warm_ramp_not_mantines_stock(self) -> None:
        """Anchored at the four roles the spec names: canvas, hairline,
        muted text and ink. A stock Mantine gray at any of these is the
        override silently not applying."""
        gray = [shade.upper() for shade in THEME["colors"]["gray"]]

        assert gray[1] == "#F4F4F2"  # canvas
        assert gray[3] == "#E9E9E6"  # hairline
        assert gray[6] == "#8B8B88"  # muted
        assert gray[9] == "#1A1A18"  # ink


class TestTypography:
    def test_inter_leads_the_body_stack(self) -> None:
        assert THEME["fontFamily"].startswith("Inter")

    def test_roboto_mono_leads_the_mono_stack(self) -> None:
        assert "Roboto Mono" in THEME["fontFamilyMonospace"]

    def test_headings_are_inter_too(self) -> None:
        assert THEME["headings"]["fontFamily"].startswith("Inter")


class TestShape:
    def test_the_radius_scale_is_the_specs(self) -> None:
        assert THEME["radius"]["sm"] == "10px"
        assert THEME["radius"]["md"] == "12px"
        assert THEME["radius"]["lg"] == "16px"

    def test_every_shadow_step_is_stated(self) -> None:
        assert set(THEME["shadows"]) == {"xs", "sm", "md", "lg", "xl"}


class TestComponentDefaults:
    """What lets a kit call site pass no ``radius`` and no ``size``."""

    def test_every_control_the_kit_builds_has_radius_and_size(self) -> None:
        for name in (
            "TextInput",
            "Textarea",
            "Select",
            "MultiSelect",
            "DateInput",
            "Button",
            "ActionIcon",
        ):
            defaults = THEME["components"][name]["defaultProps"]
            assert "radius" in defaults, name
            assert "size" in defaults, name

    def test_the_segmented_control_is_a_pill(self) -> None:
        control = THEME["components"]["SegmentedControl"]["defaultProps"]

        assert control["radius"] == "xl"

    def test_badge_paper_and_tooltip_carry_defaults(self) -> None:
        for name in ("Badge", "Paper", "Tooltip"):
            assert THEME["components"][name]["defaultProps"], name


class TestRegistration:
    def test_set_mailarc_theme_hands_the_dict_to_the_app_provider(self) -> None:
        before = mn.get_app_theme()
        try:
            set_mailarc_theme()

            assert mn.get_app_theme() == create_mailarc_theme()
        finally:
            mn.set_app_theme(before)
