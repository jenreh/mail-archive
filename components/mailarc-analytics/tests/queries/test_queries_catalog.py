"""The catalogue's rules, read off the statements themselves.

Three of them, and each has a way of rotting quietly. *No free Cypher from
outside* holds only if every statement really does take its input as a bound
parameter — one ``f"…{name}…"`` and an address decides what a statement does.
*Never write ground truth* holds only if no ``MERGE`` in the file names a label
the import owns; a read of ``Message`` is fine and a write of one is the bug
this whole package exists to make impossible. And the four statements the spec
prints do not run against the real model at all — ``Address`` has no
``address`` property and ``Group`` has no ``key`` — so the corrections are
asserted here rather than trusted to a comment.

Whether the statements *execute* is a different question and belongs to
``test_queries_catalog_local.py``, which binds each one's parameters and runs
the lot against a real backend. A string constant is only checked by a server.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.catalog import CATALOG, as_graph_datetime, parameters_of

SOURCE = Path(catalog.__file__)

DERIVED_LABELS = frozenset({"Group", "Topic", "Template"})
GROUND_TRUTH_LABELS = frozenset(
    {"Message", "Address", "Thread", "Label", "Attachment", "Account"}
)

_MERGE_LABEL = re.compile(r"MERGE \(\w*:(\w+)")

_CREATE_PATTERN = re.compile(r"\bCREATE\s*\(", re.IGNORECASE)
"""``CREATE`` followed by a pattern — a node or an edge, never an index."""

_PLACEHOLDER = re.compile(r"\{[A-Za-z_0-9.\[\]]*\}|%[sdr]\b|%\(")
"""A ``str.format`` field or a percent placeholder.

Deliberately not "a brace": Cypher map literals are written ``{id: row.id}``
and are the normal way to name a node's key. A format field has no colon in it,
which is exactly what tells the two apart.
"""


def _assigned_statements() -> dict[str, ast.expr]:
    """Every module-level upper-case assignment in the catalogue, unevaluated.

    Read from the source rather than from the imported values, because the
    question is how the string was *built*: an f-string and a plain literal are
    indistinguishable once they are a ``str``.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.isupper():
                found[target.id] = node.value
    return found


def test_the_catalogue_lists_every_statement_in_the_module() -> None:
    """Hand-written, so an omission shows up in a diff — and so this test can
    bind every statement's parameters and run the lot against a backend."""
    constants = {
        name
        for name, value in vars(catalog).items()
        if name.isupper() and isinstance(value, str)
    }

    assert constants == set(CATALOG)


@pytest.mark.parametrize("name", sorted(CATALOG))
class TestEveryStatement:
    """The rules that have to hold for all thirty-three of them."""

    def test_it_is_a_plain_literal_and_not_a_built_string(self, name: str) -> None:
        """Read off the syntax tree, so an f-string cannot hide as a ``str``.

        Phase 6 serves a model from this same file, so "a subject cannot change
        what a statement does" is the property that makes handing a query
        catalogue to something else safe — and the only way to keep it is for
        every statement to be a constant nobody assembles.
        """
        assigned = _assigned_statements()[name]

        assert isinstance(assigned, ast.Constant)
        assert isinstance(assigned.value, str)

    def test_it_carries_no_format_placeholder(self, name: str) -> None:
        """The other half: a literal with a ``{}`` in it is one ``.format``
        call away from being interpolated by a caller."""
        assert not _PLACEHOLDER.search(CATALOG[name])

    def test_its_parameters_are_named_and_lower_case(self, name: str) -> None:
        """Positional parameters would make a catalogue entry unreadable at the
        call site, which is half of why the statements are named at all."""
        for parameter in parameters_of(CATALOG[name]):
            assert parameter.islower()

    def test_it_never_creates_a_node_or_an_edge(self, name: str) -> None:
        """``CREATE`` on a derived label would grow a second node on the second
        rebuild — the labels carry no unique constraint — and every edge
        written afterwards would hang off both. So a pattern is merged, never
        created.

        Index DDL is deliberately outside that: ``CREATE VECTOR INDEX`` makes
        no node, and the reason the rule exists does not reach it. The vector
        index's length follows a setting a human picks on the embedder page, so
        it has to be buildable at run time — a migration is a versioned
        statement about the schema every installation shares, and this is one
        installation choosing a length. What is still banned is what the rule
        was written for: ``CREATE`` immediately followed by a pattern.
        """
        assert not _CREATE_PATTERN.search(CATALOG[name]), (
            "a catalogue statement creates a node or an edge; derived labels "
            "carry no unique constraint, so a rebuild would grow a second one"
        )

    def test_it_only_ever_merges_a_derived_label(self, name: str) -> None:
        """A ground-truth label may be matched and may never be merged.

        ``MERGE (m:Message {id: …})`` would invent an empty message wherever a
        row named one that is not there, which is how a derived layer starts
        writing ground truth without anybody deciding to.
        """
        merged = set(_MERGE_LABEL.findall(CATALOG[name]))

        assert merged <= DERIVED_LABELS
        assert merged.isdisjoint(GROUND_TRUTH_LABELS)


class TestTheSpecsOwnQueriesCorrected:
    """§6 and §12, with the real model's property names put back."""

    def test_no_statement_reads_an_address_property_that_does_not_exist(
        self,
    ) -> None:
        """The spec's A1 query writes ``a.address``. ``Address`` has no such
        property — the normalised address *is* the key — so the statement would
        compare two nulls and return the whole cross product."""
        assert not any(
            ".address" in one
            for name, one in CATALOG.items()
            if name != "ACCOUNT_ADDRESSES"
        )

    def test_the_account_is_the_one_node_that_does_have_an_address(self) -> None:
        """Which is what makes "sent by me" answerable at all."""
        assert "a.address" in CATALOG["ACCOUNT_ADDRESSES"]

    def test_no_statement_reads_a_group_key_property(self) -> None:
        """The spec's group query returns ``g.key``. The field is ``id``; the
        *value* is the participant key, which is what the table meant."""
        assert not any(".key" in one for one in CATALOG.values())

    def test_the_co_recipient_query_pairs_recipients_and_not_the_sender(
        self,
    ) -> None:
        """The sender is the one addressing, not one of the addressed.

        Including them makes the heaviest edge in every archive "the user, and
        everyone the user has ever mailed" — already available as the degree of
        ``SENT_FROM``, and enough to bury the finding.
        """
        statement = CATALOG["CO_RECIPIENTS"]

        assert "SENT_TO|COPIED_TO" in statement
        assert "SENT_FROM" not in statement
        assert "BLIND_COPIED_TO" not in statement
        assert "a.id < b.id" in statement

    def test_the_group_query_takes_its_thresholds_as_parameters(self) -> None:
        """The spec hard-codes ``g.size > 2 AND g.message_count > 5``, which
        makes half of ``AnalyticsConfig`` decorative."""
        assert parameters_of(CATALOG["RECURRING_GROUPS"]) == (
            "limit",
            "min_messages",
            "min_size",
        )

    def test_the_template_listing_takes_a_direction(self) -> None:
        """§6.3 reports sent and received separately, and the scores are only
        comparable within one direction."""
        assert "direction" in parameters_of(CATALOG["TOP_TEMPLATES"])


class TestTheUndirectedEdge:
    """``CO_ADDRESSED`` is undirected in meaning and directed in storage."""

    def test_the_upsert_pattern_carries_no_arrow(self) -> None:
        """So the same pair handed in either order finds the same edge instead
        of growing a second one."""
        assert re.search(
            r"MERGE \(a\)-\[r:CO_ADDRESSED\]-\(b\)", CATALOG["MERGE_CO_ADDRESSED"]
        )

    def test_the_report_reads_it_without_an_arrow_too(self) -> None:
        """Which way round it was stored is an accident of who was written to
        first, so a directed read would miss half the archive's pairs."""
        assert re.search(
            r"MATCH \(a:Address\)-\[r:CO_ADDRESSED\]-\(b:Address\)",
            CATALOG["TOP_CO_ADDRESSED"],
        )
        assert "a.id < b.id" in CATALOG["TOP_CO_ADDRESSED"]

    def test_the_count_and_the_delete_are_directed(self) -> None:
        """Both ends carry the same label, so an arrow costs no matches and
        saves visiting each edge twice — which would double the count and make
        the delete loop run twice as long."""
        for name in ("COUNT_CO_ADDRESSED", "DELETE_CO_ADDRESSED"):
            assert "-[r:CO_ADDRESSED]->" in CATALOG[name]

    def test_the_delete_removes_the_relationship_and_not_its_endpoints(
        self,
    ) -> None:
        """The one derived thing that lives between two ground-truth nodes.

        ``DETACH DELETE r`` here would take both addresses down and every
        ``SENT_TO`` in the archive with them.
        """
        assert "DELETE r" in CATALOG["DELETE_CO_ADDRESSED"]
        assert "DETACH" not in CATALOG["DELETE_CO_ADDRESSED"]


class TestTheReads:
    """What the reader depends on, stated where the statements are."""

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_paged_read_is_ordered(self, name: str) -> None:
        """A ``LIMIT`` without an ``ORDER BY`` is an arbitrary subset in Cypher.

        Two capped rebuilds would then read different messages, cluster them
        differently and mint different topic ids — precisely what the
        idempotence contract forbids.
        """
        statement = CATALOG[name]

        assert "ORDER BY" in statement
        assert parameters_of(statement) == ("after", "limit")

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_paged_read_carries_a_cursor_and_never_an_offset(self, name: str) -> None:
        """``SKIP`` is correct and quadratic.

        A graph store reaches row twenty thousand by matching, expanding and
        sorting the twenty thousand before it, so an offset walk re-does the
        whole archive per page — measured on the vendored FalkorDB at sixty-five
        times the time for sixteen times the messages. The last id carried
        forward makes each page an index seek instead.
        """
        statement = CATALOG[name]

        assert "SKIP" not in statement
        assert "m.id > $after" in statement

    def test_the_relations_read_pages_before_it_expands(self) -> None:
        """Five ``OPTIONAL MATCH`` clauses cross-multiply per message, so a
        ``LIMIT`` after them would pay for the whole archive's expansion to
        keep one page of it."""
        statement = CATALOG["MESSAGE_RELATIONS"]

        assert statement.index("LIMIT $limit") < statement.index("OPTIONAL MATCH")

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_message_without_a_canonical_id_is_skipped(self, name: str) -> None:
        """The writer cannot produce one, but a graph that has been around can
        hold one, and a rebuild that tripped over it would take the job down."""
        assert "m.id IS NOT NULL" in CATALOG[name]

    def test_the_skipped_messages_are_countable(self) -> None:
        """Asked for rather than inferred from a shortfall: a caller comparing
        counts would only learn that something was missing, not what."""
        assert "m.id IS NULL" in CATALOG["COUNT_UNIDENTIFIED"]

    def test_the_count_is_the_read_s_exact_complement(self) -> None:
        """The cursor starts at ``""``, so an id of ``''`` is left behind by
        the read as surely as a missing one — and the two statements only add
        up to every ``Message`` node if the count says so too."""
        assert "m.id = ''" in CATALOG["COUNT_UNIDENTIFIED"]
        assert "m.id <> ''" in CATALOG["COUNT_MESSAGES"]

    def test_the_archive_s_total_is_countable_for_a_capped_rebuild(self) -> None:
        """``max_messages`` is the one omission nothing else in this package
        counts; this is the total that makes it a number."""
        assert parameters_of(CATALOG["COUNT_MESSAGES"]) == ()
        assert "count(m) AS total" in CATALOG["COUNT_MESSAGES"]

    def test_the_blind_copies_come_back_in_a_column_of_their_own(self) -> None:
        """Two different questions out of one read.

        A Bcc belongs in the participant set, because ``participant_key`` was
        hashed over it, and nowhere near the co-addressing, because those two
        were never visibly on a message together.
        """
        statement = CATALOG["MESSAGE_RELATIONS"]

        assert "collect(DISTINCT b.id) AS blind_copied" in statement
        assert "collect(DISTINCT r.id) AS addressed" in statement

    def test_the_bodies_are_fetched_by_id_and_not_all_at_once(self) -> None:
        """``body_clean`` is uncapped, and only the members of an actual
        template need their words."""
        assert parameters_of(CATALOG["MESSAGE_BODIES"]) == ("ids",)
        assert "m.id IN $ids" in CATALOG["MESSAGE_BODIES"]


class TestTheDeletes:
    """Batched, counted, and each naming exactly one derived thing."""

    @pytest.mark.parametrize(
        "name",
        ["DELETE_GROUPS", "DELETE_TOPICS", "DELETE_TEMPLATES", "DELETE_CO_ADDRESSED"],
    )
    def test_each_delete_is_batched_and_reports_what_it_removed(
        self, name: str
    ) -> None:
        """The returned count is the loop condition: FalkorDB's own write
        statistics live on a private attribute of the raw result and come back
        as a float."""
        statement = CATALOG[name]

        assert parameters_of(statement) == ("batch",)
        assert "AS removed" in statement


def test_parameters_are_read_off_the_text_rather_than_listed_beside_it() -> None:
    """A hand-written list is a second copy of the truth and drifts the first
    time a statement gains a ``LIMIT``."""
    assert parameters_of("MATCH (n) WHERE n.id = $id RETURN n LIMIT $limit") == (
        "id",
        "limit",
    )
    assert parameters_of("MATCH (n) WHERE n.a = $x AND n.b = $x RETURN n") == ("x",)
    assert parameters_of("MATCH (n) RETURN n") == ()


class TestTheTimestampHelper:
    """A raw statement gets none of runic's mapping, including its converters."""

    def test_a_datetime_becomes_the_string_the_mapper_would_have_written(
        self,
    ) -> None:
        """ISO-8601, which is also what makes a derived timestamp comparable
        with an imported one."""
        assert as_graph_datetime(datetime(2026, 3, 4, 9, 15, tzinfo=UTC)) == (
            "2026-03-04T09:15:00+00:00"
        )

    def test_an_absent_timestamp_stays_absent(self) -> None:
        """A pair whose members are all undated keeps both ends empty rather
        than inventing one."""
        assert as_graph_datetime(None) is None


def test_every_counting_statement_is_on_the_packages_surface() -> None:
    """All six or none — an asymmetry reads as a deliberate exclusion.

    ``COUNT_MESSAGES`` was the one missing from ``queries/__init__``'s import
    list and ``__all__`` while sitting in ``CATALOG`` and behind
    ``AnalyticsReader.totals``, so the obvious sixth import raised
    ``ImportError`` and nothing said why.
    """
    from mailarc_analytics import queries

    counters = sorted(name for name in dir(catalog) if name.startswith("COUNT_"))

    assert counters, "the catalogue has to hold counting statements at all"
    assert [name for name in counters if name not in queries.__all__] == []
