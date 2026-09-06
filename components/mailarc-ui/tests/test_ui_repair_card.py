"""What the repair card draws, read off the render the browser is handed.

Its own file beside ``test_ui_repair_state.py`` for the split every panel in
this package keeps: the state's tests say what the archive is asked and what a
job does, and this one says what a person actually sees — which sentence, which
control, and which of them are wired to the state at all.

The claims are small and each one is a thing that was decided rather than
typed. The count leads and the explanation follows it, because a number like
"735 messages sit in no conversation" without a reason reads as data loss. The
card carries no cancel, because the work it starts is over before a person
could press one. And it stops following the job when it is unmounted, which is
the difference between a page somebody left and a background task that outlives
the session.
"""

import json
from typing import Any

from mailarc_ui.repair import repair_card
from mailarc_ui.repair.model import EXPLANATION, REPAIR_ICON, TITLE

WAND = "LucideWandSparkles"
"""``rx.icon("wand-sparkles")`` as the render spells it."""


def _taken(node: Any) -> Any:
    """One render tree with every constant condition already resolved."""
    if isinstance(node, list):
        return [_taken(one) for one in node]
    if not isinstance(node, dict):
        return node
    if (state := node.get("cond_state")) in ("true", "false"):
        return _taken(node["true_value" if state == "true" else "false_value"])
    return {key: _taken(value) for key, value in node.items()}


def _drawn(component: Any) -> str:
    """One component's whole render tree, as something searchable."""
    return json.dumps(_taken(component.render()), default=str, ensure_ascii=False)


class TestWhatTheCardSays:
    def test_it_leads_with_the_count_and_then_the_reason(self) -> None:
        drawn = _drawn(repair_card())

        assert "outside_message" in drawn
        assert EXPLANATION[:40] in drawn
        assert drawn.index("outside_message") < drawn.index(EXPLANATION[:40])

    def test_it_names_itself(self) -> None:
        assert TITLE in _drawn(repair_card())

    def test_the_result_and_the_error_are_both_wired(self) -> None:
        """A repair that failed says why, and one that worked says what it did;
        neither is a sentence the card can produce on its own."""
        drawn = _drawn(repair_card())

        assert "result_message" in drawn
        assert "job.error" in drawn or "error" in drawn


class TestWhatTheCardOffers:
    def test_the_button_starts_a_repair(self) -> None:
        drawn = _drawn(repair_card())

        assert "start_repair" in drawn
        assert WAND in drawn

    def test_the_button_is_gated_on_the_state_and_not_always_live(self) -> None:
        assert "can_repair" in _drawn(repair_card())

    def test_there_is_no_cancel(self) -> None:
        """The worker's handler makes the same argument from the other side: a
        repair is idempotent and over in seconds, so a stop button would be a
        control for work that has already finished."""
        assert "cancel" not in _drawn(repair_card()).lower()

    def test_leaving_the_page_stops_the_follow(self) -> None:
        """Without this, a person who navigates away mid-repair leaves a
        background task reading the job row for the life of the session.

        Read off ``event_triggers`` and not off the render: a lifecycle
        handler is wired into the component and never reaches the props a
        render carries, so searching the tree for it would pass whether or not
        it is there.
        """
        card = repair_card()

        assert "on_unmount" in card.event_triggers
        assert "stop_polling" in str(card.event_triggers["on_unmount"])

    def test_the_card_wears_the_icon_its_model_names(self) -> None:
        """One place decides what this card is called and what it looks like."""
        assert REPAIR_ICON.replace("-", "").lower() in _drawn(repair_card()).lower()
