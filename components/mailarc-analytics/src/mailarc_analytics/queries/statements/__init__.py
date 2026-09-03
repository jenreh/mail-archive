"""The catalogue's statements, split by the question each family asks.

:mod:`mailarc_analytics.queries.catalog` is still the public surface — a
statement is read as ``catalog.MESSAGE_PROPERTIES`` and never imported from
here — and everything that module's docstring says about why the statements are
named, parameterised and enumerated holds for every module below. This package
exists for one reason: the statements and the docstrings that explain them run
to some sixteen hundred lines together, and the house limit is a thousand per
file.

``reads``
    The plain reads a rebuild opens with, the two counts that frame it, the
    four §5.3 adds — the reply table, the importance signals, the keyword texts
    and the tag memberships — and the one read that answers a page instead:
    ``ARCHIVED_PER_DAY``, which buckets the provenance edge by the day a copy
    was archived on.
``writes``
    The batched deletions a rebuild starts with, the upserts the analyses
    finish with, and the five statements that set and clear the derived
    properties on two ground-truth nodes.
``analysis``
    What the reports ask: A1 off the ground truth and off the materialised
    edge, the group, template, topic, community, importance, keyword and
    suggestion listings, and the five derived counts.
``algorithms``
    FalkorDB's own procedures — the capability probe, label propagation, the
    reply PageRank, betweenness, the shortest paths and the BFS neighbourhood.
    The one family that is **raw Cypher**, because runic 0.6 cannot start a
    statement with ``CALL`` and a builder version would run a whole-graph
    algorithm once per matched row.
``graph``
    What the explorer draws: the eighteen small reads
    :class:`~mailarc_analytics.queries.graphs.GraphReader` composes a subgraph
    out of. Small on purpose — one statement per hop rather than one per view —
    because a single read carrying a topic's messages *and* their addresses
    *and* their tags cross-multiplies exactly the way ``MESSAGE_RELATIONS``'
    five optional expansions do. One of them, ``REPLY_CHAIN``, is raw Cypher:
    the builder writes a variable-length pattern but will not name the edge on
    one, and a walk with no edges is not a picture.
``embedding``
    What the embed job counts, reads and writes, plus the coverage number every
    semantic answer carries.
``search``
    The two vector searches, the full-text search, the vector-index DDL, and
    the index read that is raw Cypher for a reason of its own —
    ``describe()`` cannot report a vector index's dimension.

Split by the question asked rather than by Cypher keyword, so a statement and
the one it is the complement of stay in the same file: ``COUNT_UNIDENTIFIED``
sits beside the read whose filter it inverts, and both counts of the archive's
population sit beside the paged read that defines it. A split by keyword would
have put the two halves of every cross-check in different modules. It is also
why ``ARCHIVED_PER_DAY`` is in ``reads`` although a page and not a rebuild asks
it: it reads the same ground truth the rebuild's reads do, and grouping it with
the report listings would have split "what the archive holds" across two files.

**Nothing is re-exported here.** The surface is ``catalog.py``, which imports
each name from its family module and writes it out again in
:data:`~mailarc_analytics.queries.catalog.CATALOG`. A third hand-written list
in this file would be a third copy of the same truth with nothing checking it
against the other two — and the two that exist are checked against each other
by a test.
"""
