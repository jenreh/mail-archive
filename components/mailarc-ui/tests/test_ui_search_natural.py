"""The third mode's two seams: what the model may see, and what it may write.

Both are pure functions, so both are checkable without a graph, without a model
and without instantiating Reflex —
:mod:`mailarc_ui.search.natural` exists in order to keep them that way.

The one claim worth stating twice is that :data:`~mailarc_ui.search.model
.MODE_NATURAL` is deliberately not a
:class:`~mailarc_analytics.semantic.model.SearchKind`. The asymmetry looks like
an oversight and is not, so it is pinned here as well as argued in the
constant's docstring: a later reader meeting it should find the reason rather
than re-derive the rule it appears to break.
"""

import pytest

from mailarc_analytics.nlq import NO_LLM, ResolvedQuery, Vocabulary
from mailarc_analytics.semantic import SearchKind
from mailarc_core.archive.search import AddressDirectory, AddressEntry, KnownAddresses
from mailarc_ui.search import reads
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MODE_FULLTEXT,
    MODE_NATURAL,
    MODE_SEMANTIC,
)
from mailarc_ui.search.natural import (
    attachment_segment,
    understood,
    vocabulary_of,
)

ANNA = "anna@example.com"
CARL = "carl@firma.de"
BOB = "bob@example.com"

DIRECTORY = AddressDirectory(
    entries=(
        AddressEntry(address=ANNA, names=("Anna Bauer",), domain="example.com"),
        AddressEntry(address=BOB, domain="example.com"),
        AddressEntry(address=CARL, names=("Carl Winter",), domain="firma.de"),
    )
)

OFFERED = KnownAddresses(senders=(ANNA, CARL), recipients=(BOB,))


class TestTheModeIsNotASearchKind:
    def test_it_is_not_a_member_of_the_enum(self) -> None:
        """``SearchKind`` names which *index* answered, and an interpreted
        question is answered by the ordinary full-text or structured path — so
        there is no third path for it to grow.

        Adding one would break three things quietly: ``SearchResult.kind``
        could report a path no code produces, ``SemanticSearch.search``
        dispatches ``SEMANTIC`` and otherwise full text so a third member falls
        through, and the MCP server's ``search_messages(semantic: bool)`` is a
        boolean over exactly this two-valued question.
        """
        assert MODE_NATURAL not in {one.value for one in SearchKind}

    def test_the_other_two_still_are(self) -> None:
        """The asymmetry is the point. Those two name an index and this one
        names how a question was composed."""
        assert {MODE_FULLTEXT, MODE_SEMANTIC} == {one.value for one in SearchKind}


class TestWhatTheModelIsShown:
    """The privacy boundary, from the side that could break it."""

    def test_a_role_carries_the_names_behind_its_addresses(self) -> None:
        """Without them a question naming a company resolves to nothing —
        ``SearchFilters.senders`` is matched exactly."""
        found = vocabulary_of(DIRECTORY, OFFERED, (), ())

        assert [one.address for one in found.senders] == [ANNA, CARL]
        assert found.senders[0].names == ("Anna Bauer",)

    def test_the_roles_come_from_what_the_pickers_offer(self) -> None:
        """Not from the directory, which is one flat scan and does not know who
        sent what. A "from" resolving to an address that has only ever
        *received* mail would be a filter that cannot match."""
        found = vocabulary_of(DIRECTORY, OFFERED, (), ())

        assert [one.address for one in found.recipients] == [BOB]

    def test_an_address_with_no_name_is_still_offered(self) -> None:
        """Reachable by address even though no question can name it."""
        found = vocabulary_of(DIRECTORY, OFFERED, (), ())

        assert found.recipients[0].names == ()

    def test_the_pickers_options_cross_as_they_are(self) -> None:
        """Grounding: the interpreter matches against exactly the options the
        form can display, so a reading it produces is one the form can show."""
        found = vocabulary_of(
            DIRECTORY,
            OFFERED,
            [{"value": "pdf", "label": "PDF"}],
            [{"value": "1", "label": "Work"}],
        )

        assert found.families[0].value == "pdf"
        assert found.families[0].label == "PDF"
        assert found.accounts[0].label == "Work"

    def test_an_option_that_grew_a_key_does_not_break_the_reading(self) -> None:
        """These dicts are what Reflex holds for a Mantine picker, so their
        shape is a UI concern rather than this module's."""
        found = vocabulary_of(
            DIRECTORY, OFFERED, [{"value": "pdf", "label": "PDF", "group": "files"}], ()
        )

        assert found.families[0].value == "pdf"

    def test_an_empty_archive_shows_the_model_nothing(self) -> None:
        found = vocabulary_of(AddressDirectory(), KnownAddresses(), (), ())

        assert found.senders == ()
        assert found.recipients == ()


class TestTheAttachmentTriState:
    def test_either_is_a_real_answer_and_not_an_absent_one(self) -> None:
        """Which is why the segment has three positions and not a checkbox."""
        assert attachment_segment(None) == ATTACH_ANY

    def test_with_and_without_are_both_filters(self) -> None:
        assert attachment_segment(True) == ATTACH_WITH
        assert attachment_segment(False) == ATTACH_WITHOUT


class TestWhatTheFormCannotShow:
    """``understood``: everything a filled-in form leaves unsaid."""

    def test_a_clean_reading_says_nothing(self) -> None:
        """The fields already show what was searched. A sentence restating them
        is noise a reader learns to skip — and then misses the one that
        matters."""
        assert understood(ResolvedQuery(text="Rechnung", senders=(ANNA,))) == ""

    def test_an_unresolved_name_is_named(self) -> None:
        """A form that merely looks narrower than expected cannot show that a
        name went unmatched."""
        said = understood(ResolvedQuery(unresolved=("Frau Schmidt",)))

        assert "Frau Schmidt" in said
        assert "searched for the words instead" in said

    def test_several_unresolved_names_are_named_together(self) -> None:
        said = understood(ResolvedQuery(unresolved=("Schmidt", "Fastmail")))

        assert "Schmidt" in said
        assert "Fastmail" in said

    def test_a_note_from_the_reading_is_carried_as_written(self) -> None:
        """The words dropped to honour a "wann war die letzte", a date refused
        for being outside the archive's range — each is a way the question was
        not honoured as asked."""
        said = understood(ResolvedQuery(notes=("Sorted newest first.",)))

        assert said == "Sorted newest first."

    def test_notes_and_unresolved_names_are_both_said(self) -> None:
        said = understood(
            ResolvedQuery(notes=("Sorted newest first.",), unresolved=("Schmidt",))
        )

        assert "Sorted newest first." in said
        assert "Schmidt" in said

    def test_an_empty_vocabulary_object_is_usable(self) -> None:
        """The shape a lost graph read produces — the page goes on working with
        the grounding gone, and every mention comes back unresolved."""
        assert Vocabulary().senders == ()


class TestReachingTheServices:
    """The lookups, and the one sentence a user should never meet.

    ``mailarc-ui`` may not import ``app``, so everything the composition root
    built arrives through the registry — and a lookup that finds nothing means
    the build is half wired, never that a person has not configured something.
    Both messages therefore have to be distinguishable at a glance.
    """

    def test_a_missing_interpreter_is_a_developers_error(self) -> None:
        from appkit_commons.registry import service_registry

        registry = service_registry()
        saved = registry.snapshot()
        registry.restore(registry.__class__().snapshot())
        try:
            with pytest.raises(RuntimeError, match=r"app\.composition"):
                reads.query_interpreter()
        finally:
            registry.restore(saved)

    def test_it_is_not_the_sentence_for_an_unconfigured_model(self) -> None:
        """That is an ordinary state with a one-line remedy, and it belongs to
        ``NO_LLM`` — which is why the composition root publishes an interpreter
        whether or not a provider exists."""
        assert NO_LLM not in reads.NO_INTERPRETER
        assert "QueryInterpreter is registered" in reads.NO_INTERPRETER
