"""The catalogue's rules, read off the statements themselves.

Three of them, and each has a way of rotting quietly. *No free Cypher from
outside* holds only if every statement really does take its input as a bound
parameter — one ``f"…{name}…"`` and an address decides what a statement does.
*Never write ground truth* holds only if no ``MERGE`` in the catalogue names a
label the import owns; a read of ``Message`` is fine and a write of one is the
bug this whole package exists to make impossible. And the four statements the
spec prints do not run against the real model at all — ``Address`` has no
``address`` property and ``Group`` has no ``key`` — so the corrections are
asserted here rather than trusted to a comment.

**Two readings, and the difference between them is the point.** A statement is
a :class:`~runic.ogm.QueryBuilder` now rather than a string, so a rule about
what the *store* will run is asserted against :func:`_cypher` — the statement
compiled — and a rule about how the statement was *built* is asserted against
the syntax tree of the module that assigns it. The second is where the security
argument lives: a compiled statement cannot tell you whether the text it came
from was interpolated, and interpolation is the one failure a query catalogue
exists to make impossible.

Whether the statements *execute* is a different question and belongs to
``test_queries_catalog_local.py``, which binds each one's parameters and runs
the lot against a real backend. A statement is only checked by a server.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from runic.ogm import QueryBuilder

from mailarc_analytics.queries import catalog, statements
from mailarc_analytics.queries.catalog import (
    CATALOG,
    Statement,
    as_graph_datetime,
    parameters_of,
)

SOURCE = Path(catalog.__file__)
STATEMENT_SOURCES = tuple(
    sorted(Path(statements.__file__).parent.glob("*.py"), key=lambda one: one.name)
)
"""Every module a statement can be assigned in.

``catalog.py`` is still the surface and still holds one statement — the raw one
— while the other thirty-four live one family per module under
``queries/statements/``. The rules below are about statements and not about
files, so they are read off all six together.
"""

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


def _cypher(statement: Statement) -> str:
    """What the store will run, for a statement of either kind.

    ``build()`` compiles a builder without a session and hands back the text
    plus the literals it auto-bound. It is enough for every rule below, which
    are about labels, patterns and clauses — the one thing it does *not* show
    is a dialect-supplied function, so ``WRITE_EMBEDDINGS``' ``vecf32`` wrap
    appears only when the statement is compiled through a session and is
    checked where that happens, in ``test_queries_catalog_local.py``.

    The compiler's backticks are dropped on the way out. runic quotes every
    identifier it emits — ``m.`id```, ``AS `removed``` — so that a model may
    declare a field named after a Cypher keyword. That is the compiler's
    escaping, not the statement's shape, and the rules below are about the
    shape: they stay written as the Cypher a reader would type. A backtick can
    only wrap an identifier, so removing it cannot make a statement look like
    one it is not.
    """
    cypher = statement if isinstance(statement, str) else statement.build()[0]
    return cypher.replace("`", "")


def _built(statement: Statement) -> QueryBuilder[Any]:
    """*statement* as the builder it is, so its own ``build()`` can be asked.

    An assertion about what a statement auto-bound is an assertion about a
    builder; the one raw entry has nothing to answer with. Narrowed here rather
    than suppressed, so a name that stopped being a builder fails loudly.
    """
    assert not isinstance(statement, str), "this rule is about a builder statement"
    return statement


def _assigned_statements() -> dict[str, ast.expr]:
    """Every module-level upper-case assignment in the catalogue, unevaluated.

    Read from the source rather than from the imported values, because the
    question is how the statement was *built*: an f-string and a plain literal
    are indistinguishable once they are a ``str``, and a builder chain assembled
    out of caller-supplied text is indistinguishable from an honest one once it
    is a ``QueryBuilder``.

    All six modules at once, and keyed by name, so a statement that moved
    between families is still covered and one assigned in two places would show
    up as the wrong expression under a familiar name.
    """
    found: dict[str, ast.expr] = {}
    for path in (SOURCE, *STATEMENT_SOURCES):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            assigned = _assigned(node)
            if assigned is not None and assigned[0].isupper():
                found[assigned[0]] = assigned[1]
    return found


def _assigned(node: ast.stmt) -> tuple[str, ast.expr] | None:
    """The name a module-level assignment binds and the expression it binds it
    to, annotated or not.

    ``ACCOUNT_ADDRESSES: QueryBuilder[Account] = select(…)`` is an
    ``AnnAssign``; the same line without the annotation is an ``Assign``. Both
    are how a statement is written in this catalogue, and a check that only saw
    one of them would silently stop covering half the file.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        return (target.id, node.value) if isinstance(target, ast.Name) else None
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return (
            (node.target.id, node.value) if isinstance(node.target, ast.Name) else None
        )
    return None


def test_the_catalogue_lists_every_statement_in_the_module() -> None:
    """Hand-written, so an omission shows up in a diff — and so this test can
    bind every statement's parameters and run the lot against a backend.

    ``QueryBuilder`` *or* ``str``, which is what :data:`Statement` says: the
    two DDL names on this module are functions and belong to neither, which is
    why they are not in the mapping and why nothing in the sweep tries to run
    one.
    """
    constants = {
        name
        for name, value in vars(catalog).items()
        if name.isupper() and isinstance(value, QueryBuilder | str)
    }

    assert constants == set(CATALOG)


def test_the_surface_is_the_catalogue_plus_the_six_things_that_are_not_one() -> None:
    """``__all__`` is written out because ruff refuses a computed one, so the
    relationship between the two hand-written lists is stated and checked.

    Anything else means a statement was added to one and not the other, which
    is exactly the drift that made writing them both out defensible.
    """
    not_statements = {
        "CATALOG",
        "CREATE_VECTOR_INDEX",
        "DROP_VECTOR_INDEX",
        "Statement",
        "as_graph_datetime",
        "parameters_of",
    }

    assert set(catalog.__all__) - not_statements == set(CATALOG)
    assert not_statements <= set(catalog.__all__)


@pytest.mark.parametrize("name", sorted(CATALOG))
class TestEveryStatement:
    """The rules that have to hold for all thirty-five of them."""

    def test_it_is_assembled_from_the_builder_and_never_from_text(
        self, name: str
    ) -> None:
        """Read off the syntax tree, so an f-string cannot hide as a statement.

        Phase 6 serves a model from this same catalogue, so "a subject cannot
        change what a statement does" is the property that makes handing a
        query catalogue to something else safe — and the only way to keep it is
        for no statement to be assembled out of text at all. A builder is
        checked for the same thing a string was: no f-string, no ``%``, no
        ``.format``, no concatenation anywhere in the expression that produces
        it. The one raw statement is still a plain literal.
        """
        assigned = _assigned_statements()[name]

        if isinstance(assigned, ast.Constant):
            assert isinstance(assigned.value, str)
            return
        for node in ast.walk(assigned):
            assert not isinstance(node, ast.JoinedStr), "an f-string builds this"
            assert not (
                isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod | ast.Add)
            ), "text is concatenated or percent-formatted into this"
            assert not (
                isinstance(node, ast.Attribute)
                and node.attr in {"format", "format_map", "join"}
            ), "text is formatted into this"

    def test_it_carries_no_format_placeholder(self, name: str) -> None:
        """The other half: compiled Cypher with a ``{}`` in it is one
        ``.format`` call away from being interpolated by a caller."""
        assert not _PLACEHOLDER.search(_cypher(CATALOG[name]))

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

        Index DDL used to be the stated exception here and no longer needs to
        be: ``CREATE VECTOR INDEX`` is emitted by ``IndexOperations`` now, from
        a function that is not in this mapping, so no entry in it can contain a
        ``CREATE`` of any kind. What the rule was written for is unchanged —
        ``CREATE`` immediately followed by a pattern.
        """
        assert not _CREATE_PATTERN.search(_cypher(CATALOG[name])), (
            "a catalogue statement creates a node or an edge; derived labels "
            "carry no unique constraint, so a rebuild would grow a second one"
        )

    def test_it_only_ever_merges_a_derived_label(self, name: str) -> None:
        """A ground-truth label may be matched and may never be merged.

        ``MERGE (m:Message {id: …})`` would invent an empty message wherever a
        row named one that is not there, which is how a derived layer starts
        writing ground truth without anybody deciding to. The four edge upserts
        merge a *relationship* between two variables the statement matched
        first, which names no label at all and is the shape this rule wants.
        """
        merged = set(_MERGE_LABEL.findall(_cypher(CATALOG[name])))

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
            ".address" in _cypher(one)
            for name, one in CATALOG.items()
            if name != "ACCOUNT_ADDRESSES"
        )

    def test_the_account_is_the_one_node_that_does_have_an_address(self) -> None:
        """Which is what makes "sent by me" answerable at all."""
        assert "a.address" in _cypher(CATALOG["ACCOUNT_ADDRESSES"])

    def test_no_statement_reads_a_group_key_property(self) -> None:
        """The spec's group query returns ``g.key``. The field is ``id``; the
        *value* is the participant key, which is what the table meant."""
        assert not any(".key" in _cypher(one) for one in CATALOG.values())

    def test_the_co_recipient_query_pairs_recipients_and_not_the_sender(
        self,
    ) -> None:
        """The sender is the one addressing, not one of the addressed.

        Including them makes the heaviest edge in every archive "the user, and
        everyone the user has ever mailed" — already available as the degree of
        ``SENT_FROM``, and enough to bury the finding.
        """
        statement = _cypher(CATALOG["CO_RECIPIENTS"])

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

    def test_the_upsert_pattern_is_directed_and_the_pair_is_ordered(self) -> None:
        """The arrow is new and the guarantee behind it is not.

        The statement used to carry no arrow and relied on the caller ordering
        the pair; FalkorDB refuses an undirected ``MERGE`` outright — runic 0.5
        raises ``NotImplementedError`` rather than emitting one — so the arrow
        has to be there and the ordering is now the *only* thing that keeps one
        pair to one edge. It is enforced where a pair is made, in
        ``CoAddressedPair``, which is asserted next door in
        ``test_derived_model.py`` and measured against a real graph in
        ``test_derived_writer_local.py``.

        Nothing in an existing archive moved: the undirected ``MERGE`` had
        already stored every edge smaller-id-first, because the caller ordered
        the pair — measured on a planted graph, arrow for arrow.
        """
        assert re.search(
            r"MERGE \(a\)-\[r:CO_ADDRESSED\]->\(b\)",
            _cypher(CATALOG["MERGE_CO_ADDRESSED"]),
        )

    def test_the_report_reads_it_without_an_arrow_too(self) -> None:
        """Which way round it was stored is an accident of who was written to
        first, so a directed read would miss half the archive's pairs."""
        statement = _cypher(CATALOG["TOP_CO_ADDRESSED"])

        assert re.search(r"MATCH \(a\)-\[r:CO_ADDRESSED\]-\(b:Address\)", statement)
        assert "->" not in statement
        assert "a.id < b.id" in statement

    def test_the_count_and_the_delete_are_directed(self) -> None:
        """Both ends carry the same label, so an arrow costs no matches and
        saves visiting each edge twice — which would double the count and make
        the delete loop run twice as long."""
        for name in ("COUNT_CO_ADDRESSED", "DELETE_CO_ADDRESSED"):
            assert "-[r:CO_ADDRESSED]->" in _cypher(CATALOG[name])

    def test_the_delete_removes_the_relationship_and_not_its_endpoints(
        self,
    ) -> None:
        """The one derived thing that lives between two ground-truth nodes.

        ``DETACH DELETE r`` here would take both addresses down and every
        ``SENT_TO`` in the archive with them.
        """
        statement = _cypher(CATALOG["DELETE_CO_ADDRESSED"])

        assert "DELETE r" in statement
        assert "DETACH" not in statement


class TestTheReads:
    """What the reader depends on, stated where the statements are."""

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_paged_read_is_ordered(self, name: str) -> None:
        """A ``LIMIT`` without an ``ORDER BY`` is an arbitrary subset in Cypher.

        Two capped rebuilds would then read different messages, cluster them
        differently and mint different topic ids — precisely what the
        idempotence contract forbids.
        """
        statement = _cypher(CATALOG[name])

        assert "ORDER BY" in statement
        assert parameters_of(CATALOG[name]) == ("after", "limit")

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_paged_read_carries_a_cursor_and_never_an_offset(self, name: str) -> None:
        """``SKIP`` is correct and quadratic.

        A graph store reaches row twenty thousand by matching, expanding and
        sorting the twenty thousand before it, so an offset walk re-does the
        whole archive per page — measured on the vendored FalkorDB at sixty-five
        times the time for sixteen times the messages. The last id carried
        forward makes each page an index seek instead. The builder offers
        ``skip()`` and both statements leave it unused.
        """
        statement = _cypher(CATALOG[name])

        assert "SKIP" not in statement
        assert "m.id > $after" in statement

    def test_the_relations_read_pages_before_it_expands(self) -> None:
        """Five ``OPTIONAL MATCH`` clauses cross-multiply per message, so a
        ``LIMIT`` after them would pay for the whole archive's expansion to
        keep one page of it.

        Written order is compiled order in the builder, which is what makes
        this checkable at all: ``.order_by().limit()`` written above the
        traversals compiles to a ``WITH`` stage ahead of the first
        ``OPTIONAL MATCH``, and moving those two calls below them is not a
        formatting choice — it is the whole archive.
        """
        statement = _cypher(CATALOG["MESSAGE_RELATIONS"])

        assert statement.index("LIMIT $limit") < statement.index("OPTIONAL MATCH")

    def test_every_expansion_after_the_first_leaves_from_the_message(self) -> None:
        """Consecutive traversals *chain*, and the result is silently empty.

        Written without ``from_=``, the recipients hop leaves from the sender
        node rather than from the message, the Bcc hop leaves from that, and
        every collected column comes back empty for every row with no error at
        all. Measured, and the reason each of the five patterns below has to
        start at ``(m)``.
        """
        statement = _cypher(CATALOG["MESSAGE_RELATIONS"])

        assert statement.count("OPTIONAL MATCH (m)-") == 5

    @pytest.mark.parametrize("name", ["MESSAGE_PROPERTIES", "MESSAGE_RELATIONS"])
    def test_a_message_without_a_canonical_id_is_skipped(self, name: str) -> None:
        """The writer cannot produce one, but a graph that has been around can
        hold one, and a rebuild that tripped over it would take the job down."""
        assert "m.id IS NOT NULL" in _cypher(CATALOG[name])

    def test_the_skipped_messages_are_countable(self) -> None:
        """Asked for rather than inferred from a shortfall: a caller comparing
        counts would only learn that something was missing, not what."""
        assert "m.id IS NULL" in _cypher(CATALOG["COUNT_UNIDENTIFIED"])

    def test_the_count_is_the_read_s_exact_complement(self) -> None:
        """The cursor starts at ``""``, so an id of ``''`` is left behind by
        the read as surely as a missing one — and the two statements only add
        up to every ``Message`` node if the count says so too.

        The empty string is an auto-bound literal now rather than a quoted
        value in the text, which is a hardening: it never reaches the parser as
        text, and — as the two assertions below say — it is bound by the
        statement itself and is deliberately absent from
        :func:`parameters_of`, so no caller can reach it.
        """
        unidentified = _built(CATALOG["COUNT_UNIDENTIFIED"])
        everything = _built(CATALOG["COUNT_MESSAGES"])

        assert _cypher(CATALOG["COUNT_UNIDENTIFIED"]).count("m.id = $p0") == 1
        assert _cypher(CATALOG["COUNT_MESSAGES"]).count("m.id <> $p0") == 1
        assert unidentified.build()[1] == {"p0": ""}
        assert everything.build()[1] == {"p0": ""}
        assert parameters_of(unidentified) == ()
        assert parameters_of(everything) == ()

    def test_the_archive_s_total_is_countable_for_a_capped_rebuild(self) -> None:
        """``max_messages`` is the one omission nothing else in this package
        counts; this is the total that makes it a number."""
        assert parameters_of(CATALOG["COUNT_MESSAGES"]) == ()
        assert "count(m) AS total" in _cypher(CATALOG["COUNT_MESSAGES"])

    def test_the_blind_copies_come_back_in_a_column_of_their_own(self) -> None:
        """Two different questions out of one read.

        A Bcc belongs in the participant set, because ``participant_key`` was
        hashed over it, and nowhere near the co-addressing, because those two
        were never visibly on a message together.
        """
        statement = _cypher(CATALOG["MESSAGE_RELATIONS"])

        assert "collect(DISTINCT b.id) AS blind_copied" in statement
        assert "collect(DISTINCT r.id) AS addressed" in statement

    def test_the_bodies_are_fetched_by_id_and_not_all_at_once(self) -> None:
        """``body_clean`` is uncapped, and only the members of an actual
        template need their words."""
        assert parameters_of(CATALOG["MESSAGE_BODIES"]) == ("ids",)
        assert "m.id IN $ids" in _cypher(CATALOG["MESSAGE_BODIES"])


class TestTheArchivingHistory:
    """The one read here that answers a page rather than a rebuild.

    What it *returns* on a real backend — whether ``left()`` over a
    datetime-converted column yields a ``YYYY-MM-DD`` key at all — is
    ``test_queries_archived_per_day_local.py``'s question and cannot be
    answered here. These are the claims the compiled text can carry.
    """

    def test_a_caller_may_only_supply_the_row_ceiling(self) -> None:
        """One day is one row, so the ceiling is a number of *days* — and it is
        the only thing a caller reaches. The ten characters a day key is cut to
        are the statement's own, auto-bound as ``$p0`` the way every fixed
        literal in this catalogue is."""
        statement = _built(CATALOG["ARCHIVED_PER_DAY"])

        assert parameters_of(statement) == ("limit",)
        assert statement.build()[1] == {"p0": 10}

    def test_the_day_key_is_cut_out_of_the_stored_timestamp(self) -> None:
        """``left(r.archived_at, 10)`` — the store cuts it, not Python, because
        the alternative is reading every edge in the archive to group it."""
        assert "left(r.archived_at, $p0) AS day" in _cypher(CATALOG["ARCHIVED_PER_DAY"])

    def test_it_counts_and_sums_under_the_names_the_reader_uses(self) -> None:
        """An aggregate without an ``.as_()`` is keyed by its raw Cypher, and
        every consumer reads ``row["messages"]``."""
        statement = _cypher(CATALOG["ARCHIVED_PER_DAY"])

        assert "count(m) AS messages" in statement
        assert "sum(m.size_bytes) AS bytes" in statement

    def test_an_edge_without_a_stamp_is_left_out_rather_than_bucketed(
        self,
    ) -> None:
        """``archived_at`` is nullable on the edge, and a null day key would
        collect every undated copy into one bucket the chart cannot place."""
        assert "r.archived_at IS NOT NULL" in _cypher(CATALOG["ARCHIVED_PER_DAY"])

    def test_the_ceiling_keeps_the_newest_days_and_not_the_oldest(self) -> None:
        """The one place this statement departs from its siblings, and the
        reason is the ``LIMIT``.

        Every other listing is ordered by the number that matters, so cutting
        it keeps the interesting rows. This one is ordered by *time*, and a
        chart of the last week wants the newest days — an ascending order under
        the same ceiling would hand back the oldest days in the archive and an
        empty chart. So the store orders newest first and the reader turns the
        window round again.
        """
        statement = _cypher(CATALOG["ARCHIVED_PER_DAY"])

        assert "ORDER BY day DESC" in statement
        assert statement.index("ORDER BY day DESC") < statement.index("LIMIT $limit")


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
        assert "AS removed" in _cypher(statement)


class TestWhereAParameterComesFrom:
    """The security boundary, stated as the two kinds of ``$name`` there are."""

    def test_a_declared_parameter_is_read_back_off_the_statement(self) -> None:
        """Not maintained beside it: a hand-written list is a second copy of
        the truth and drifts the first time a statement gains a ``LIMIT``."""
        assert parameters_of(CATALOG["MESSAGES_NEEDING_EMBEDDING"]) == (
            "after",
            "limit",
            "max_chars",
            "model",
        )
        assert parameters_of(CATALOG["SEMANTIC_NEIGHBOURS"]) == (
            "k",
            "limit",
            "model",
            "vector",
        )

    def test_a_literal_the_statement_fixed_is_not_a_parameter_a_caller_can_reach(
        self,
    ) -> None:
        """``$p0`` in the compiled text is a value the *statement* bound.

        The boundary counts what a caller may supply, and a caller may supply
        nothing there — so an auto-bound literal is correctly absent from
        :func:`parameters_of` and its presence in the text is a hardening
        rather than a hole: the empty string never reaches the parser as text.
        """
        statement = _built(CATALOG["COUNT_UNIDENTIFIED"])
        cypher, bound = statement.build()

        assert "$p0" in cypher
        assert bound == {"p0": ""}
        assert parameters_of(statement) == ()

    def test_the_one_raw_statement_is_still_read_off_its_text(self) -> None:
        """:data:`~mailarc_analytics.queries.catalog.VECTOR_INDEX_OPTIONS` has
        no builder equivalent — ``describe()`` cannot report a vector index's
        dimension — so it stays a string, and ``parameters_of`` still answers
        for it. It happens to declare none, which is why the regex is exercised
        on statements of the same shape rather than only on that one.
        """
        assert isinstance(catalog.VECTOR_INDEX_OPTIONS, str)
        assert parameters_of(catalog.VECTOR_INDEX_OPTIONS) == ()
        assert parameters_of("MATCH (n) WHERE n.id = $id RETURN n LIMIT $limit") == (
            "id",
            "limit",
        )
        assert parameters_of("MATCH (n) WHERE n.a = $x AND n.b = $x RETURN n") == ("x",)
        assert parameters_of("MATCH (n) RETURN n") == ()


class TestTheIndexDDL:
    """The two names that are functions rather than statements."""

    def test_neither_is_in_the_catalogue(self) -> None:
        """A mapping of statements holds runnable things and only runnable
        things, which is what lets a test bind every entry and run the lot.
        Keeping a callable in it would cost the mapping its one useful property
        to preserve the appearance of completeness.
        """
        assert callable(catalog.CREATE_VECTOR_INDEX)
        assert callable(catalog.DROP_VECTOR_INDEX)
        assert "CREATE_VECTOR_INDEX" not in CATALOG
        assert "DROP_VECTOR_INDEX" not in CATALOG

    def test_the_index_settings_are_required_rather_than_defaulted(self) -> None:
        """All five, keyword-only and without defaults.

        ``IndexOperations.create_vector_index`` takes only ``dimension`` and
        ``similarity`` and lets its adapter default the rest, which would have
        moved ``efConstruction`` from 400 to 200 and ``efRuntime`` from 512 to
        10 — the second is the KNN's candidate list, and ``SEMANTIC_NEIGHBOURS``
        is *built* on over-fetching ``$k`` and filtering afterwards. A call site
        that forgets one has to be a ``TypeError`` here, not a quiet recall
        regression a year later that no row-counting test would notice.
        """
        import inspect

        signature = inspect.signature(catalog.CREATE_VECTOR_INDEX)
        settings = [
            one
            for one in signature.parameters.values()
            if one.kind is inspect.Parameter.KEYWORD_ONLY
        ]

        assert [one.name for one in settings] == [
            "dimension",
            "similarity",
            "m",
            "ef_construction",
            "ef_runtime",
        ]
        assert all(one.default is inspect.Parameter.empty for one in settings)


class TestTheTimestampHelper:
    """An ``UNWIND`` payload gets none of runic's mapping on its own."""

    def test_a_datetime_becomes_the_string_the_mapper_would_have_written(
        self,
    ) -> None:
        """ISO-8601, which is also what makes a derived timestamp comparable
        with an imported one — and what ``encode_rows`` produces for a declared
        field, so a payload built either way reaches the store identically."""
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
