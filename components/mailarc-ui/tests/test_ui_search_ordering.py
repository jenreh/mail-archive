"""Tests for :mod:`mailarc_ui.search.ordering` — the order every picker keeps.

One rule over five controls, so the claims here are about the rule and the
claims in the state, files and form suites are about each control obeying it.

Two of these are worth naming. Sorting by ``label`` and not by ``value`` is
the whole of the account picker's correctness — its values are row ids, and an
ordering by those is a list of mailboxes in the order somebody added them,
under names that say nothing of the sort. And folding accents is what puts
``Ärzte`` beside ``Anwalt`` rather than past ``Zoll``, which is where the raw
code point puts it and where nobody on a German archive would look for it.
"""

from mailarc_ui.search.model import GROUPING_OPTIONS, Grouping
from mailarc_ui.search.ordering import alphabetical, by_label, sort_key


class TestPlainOptions:
    def test_it_orders_addresses_by_word(self) -> None:
        assert alphabetical(["zoe@example.com", "anna@example.com"]) == [
            "anna@example.com",
            "zoe@example.com",
        ]

    def test_case_does_not_decide(self) -> None:
        """An archive holds both spellings of an address book, and upper case
        sorting ahead of every lower-case entry is two lists, not one."""
        assert alphabetical(["beta@x.de", "Alpha@x.de", "gamma@x.de"]) == [
            "Alpha@x.de",
            "beta@x.de",
            "gamma@x.de",
        ]

    def test_an_accent_sorts_under_its_letter(self) -> None:
        """``Ä`` is a code point past ``z``; a German address book in which
        every umlaut sits after ``Zoll`` is one nobody can look up in."""
        assert alphabetical(["zoll@x.de", "ärzte@x.de", "anwalt@x.de"]) == [
            "anwalt@x.de",
            "ärzte@x.de",
            "zoll@x.de",
        ]

    def test_the_source_list_is_left_alone(self) -> None:
        """The reads behind these lists keep their own order — busiest first
        is what the file catalogue is cut by and what the interpreter reads a
        mention against. This is the last step before a dropdown, not a
        rewrite of what any of them mean."""
        held = ["zoe@example.com", "anna@example.com"]

        alphabetical(held)

        assert held == ["zoe@example.com", "anna@example.com"]

    def test_nothing_offered_stays_nothing(self) -> None:
        assert alphabetical([]) == []


class TestMantineOptions:
    def test_it_orders_by_the_word_and_not_by_the_value(self) -> None:
        """The account picker's whole correctness: values are row ids."""
        assert by_label(
            [
                {"value": "1", "label": "Work"},
                {"value": "2", "label": "Archive"},
            ]
        ) == [
            {"value": "2", "label": "Archive"},
            {"value": "1", "label": "Work"},
        ]

    def test_an_option_that_grew_a_key_still_sorts(self) -> None:
        """Read through ``get``, the way every other reader of these dicts
        does: their shape is Mantine's and may carry more than two keys."""
        ordered = by_label(
            [
                {"value": "b", "label": "Beta", "disabled": "true"},
                {"value": "a", "label": "Alpha"},
            ]
        )

        assert [one["label"] for one in ordered] == ["Alpha", "Beta"]

    def test_an_option_with_no_word_is_not_a_crash(self) -> None:
        """A picker that lost its labels must still draw."""
        assert by_label([{"value": "b", "label": "Beta"}, {"value": "a"}]) == [
            {"value": "a"},
            {"value": "b", "label": "Beta"},
        ]


class TestTheKeyItself:
    def test_two_spellings_that_fold_together_keep_a_fixed_order(self) -> None:
        """Otherwise ``Muller`` and ``Müller`` sit in whichever order the read
        happened to hand them over in, and the list moves between page
        loads."""
        assert sorted(["Müller", "Muller"], key=sort_key) == sorted(
            ["Muller", "Müller"], key=sort_key
        )


class TestTheGroupingDropdown:
    def test_none_is_pinned_in_front(self) -> None:
        """It is the off position rather than a grouping among the others: a
        list whose *off* switch sits between *Conversation* and *Receiver* is
        one nobody can turn off at a glance."""
        assert GROUPING_OPTIONS[0]["value"] == Grouping.NONE.value

    def test_the_rest_are_alphabetical(self) -> None:
        labels = [one["label"] for one in GROUPING_OPTIONS[1:]]

        assert labels == sorted(labels, key=sort_key)

    def test_every_grouping_is_offered_exactly_once(self) -> None:
        """The sort must not be able to drop one — see the exhaustiveness rule
        the archive keeps for every map across two modules."""
        assert sorted(one["value"] for one in GROUPING_OPTIONS) == sorted(
            one.value for one in Grouping
        )
