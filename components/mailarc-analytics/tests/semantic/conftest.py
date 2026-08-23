"""The schema the migrations create, applied by hand for the local tests.

A component test may not run the repository's migration chain: ``runic`` reads
the *composed* application configuration to find out which graph to talk to,
and a component is not allowed to know that configuration exists. So the two
index kinds a semantic test needs are issued here directly, and the numbers
they are issued with are deliberately small — a four-dimensional index makes a
fixture a readable four floats instead of seven hundred and sixty-eight.

That the *shipped* dimension matches the migration's is asserted in
``test_semantic_config.py``, by reading the migration's source. The two tests
are complementary: this one exercises the behaviour, that one the number.

The DDL is written out rather than taken from the catalogue on purpose.
``catalog.py`` may not hold a ``CREATE`` — a test there asserts exactly that,
because a statement that can create an index is a statement that can create a
node — so schema belongs to a migration or, here, to a fixture.
"""

from collections.abc import Iterator

import pytest
from runic.ogm import Session

from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig

TEST_DIMENSION = 4
"""Floats per vector in these tests. Not the shipped 768 — see the module
docstring; the shipped number is checked where it is declared."""

VECTOR_INDEX = (
    "CREATE VECTOR INDEX FOR (n:`Message`) ON (n.`embedding`) "
    "OPTIONS {dimension: %d, similarityFunction: 'cosine', "
    "M: 16, efConstruction: 400, efRuntime: 512}"
)
"""What the migration compiles to, with the dimension left open.

Percent-formatted and not an f-string because the dimension is the only thing
that varies and the rest has to stay readable as the Cypher it is.

Byte for byte what runic's FalkorDB adapter really sends, backticks included —
``tests/test_graph_migrations_vector_local.py`` holds this string against the
adapter's own output for exactly the migration's constants. It used to be a
transcription of what the revision was *believed* to produce, which meant a
renamed or reordered option inside runic would leave every test here green
while the shipped index was built with different parameters. Per the
migration's docstring, a wrong index does not raise: it silently stops
indexing.
"""

FULLTEXT_INDEX = (
    "CALL db.idx.fulltext.createNodeIndex('Message', 'subject', 'body_text')"
)
"""What the baseline migration creates, and the reason full-text search works
without any of the semantic configuration."""


def install_schema(
    config: GraphConfig, *, dimension: int = TEST_DIMENSION, fulltext: bool = True
) -> GraphConfig:
    """Give this graph the indexes the migrations would have given it."""
    with client.session(config) as graph:
        graph.execute(VECTOR_INDEX % dimension, {})
        if fulltext:
            graph.execute(FULLTEXT_INDEX, {})
    return config


@pytest.fixture
def migrated(config: GraphConfig) -> GraphConfig:
    """An empty graph of this test's own, carrying both indexes."""
    return install_schema(config)


@pytest.fixture
def unmigrated(config: GraphConfig) -> GraphConfig:
    """A graph with no indexes at all — what an un-upgraded archive looks like.

    Worth a fixture rather than an omission: "the migrations have not been
    applied" is a state a real user reaches by upgrading the application and
    not the graph, and every surface has to say so rather than fail obscurely.
    """
    return config


@pytest.fixture
def migrated_archive(archived: GraphConfig) -> GraphConfig:
    """The planted corpus, archived by the real writer, plus both indexes."""
    return install_schema(archived)


@pytest.fixture
def graph(migrated: GraphConfig) -> Iterator[Session]:
    """One open session on the migrated graph, for the read-only tests."""
    with client.session(migrated) as session:
        yield session
