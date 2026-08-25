"""The catalogue's statements, split by the question each family asks.

:mod:`mailarc_analytics.queries.catalog` is still the public surface — a
statement is read as ``catalog.MESSAGE_PROPERTIES`` and never imported from
here — and everything that module's docstring says about why the statements are
named, parameterised and enumerated holds for every module below. This package
exists for one reason: the statements and the docstrings that explain them run
to some sixteen hundred lines together, and the house limit is a thousand per
file.

``reads``
    The six plain reads a rebuild opens with, and the two counts that frame it.
``writes``
    The four batched deletions a rebuild starts with, and the seven upserts the
    three analyses finish with.
``analysis``
    What the reports ask: A1 off the ground truth and off the materialised
    edge, the group, template and topic listings, and the four derived counts.
``embedding``
    What the embed job counts, reads and writes, plus the coverage number every
    semantic answer carries.
``search``
    The two vector searches, the full-text search, the vector-index DDL, and
    the one read that is still raw Cypher.

Split by the question asked rather than by Cypher keyword, so a statement and
the one it is the complement of stay in the same file: ``COUNT_UNIDENTIFIED``
sits beside the read whose filter it inverts, and both counts of the archive's
population sit beside the paged read that defines it. A split by keyword would
have put the two halves of every cross-check in different modules.

**Nothing is re-exported here.** The surface is ``catalog.py``, which imports
each name from its family module and writes it out again in
:data:`~mailarc_analytics.queries.catalog.CATALOG`. A third hand-written list
in this file would be a third copy of the same truth with nothing checking it
against the other two — and the two that exist are checked against each other
by a test.
"""
