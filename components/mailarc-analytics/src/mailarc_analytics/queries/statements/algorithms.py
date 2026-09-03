"""The six statements the query builder cannot express — FalkorDB's procedures.

**runic 0.6 cannot start a statement with ``CALL``.** ``select()`` always opens
with ``MATCH (n:Label)`` and ``.call()`` is a *mid-pipeline* clause, so the
nearest a builder gets to ``CALL algo.pageRank(...)`` is
``MATCH (m:Message) CALL algo.pageRank(...)`` — which asks the store to run a
whole-graph algorithm once per matched message. There is no version of these
six that is a builder object, so all six are raw Cypher, which is what the
catalogue's module docstring means when it says the exception is now the index
read *and* the procedure calls.

That costs the builder's two guarantees and replaces each of them:

* A misspelled property is no longer a ``ty`` error. What answers instead is
  ``tests/queries/test_queries_catalog_local.py``, which binds every entry's
  parameters and runs the lot against the vendored FalkorDB — the procedures
  included, against a graph planted with the labels and relationship types
  they name.
* The text is not assembled, it is *written*. Every one of these is a plain
  literal with no f-string, no ``%`` and no ``.format`` anywhere near it, and
  a caller's value can only reach the store as a bound ``$parameter``. The
  catalogue's AST test asserts exactly that, over the string entries as well as
  the builder ones.

**Every statement projects scalars, never a node.** ``YIELD node`` binds a
graph entity, and :func:`~mailarc_analytics.queries.rows.rows_of` zips the
driver's own value shapes into a row — so a bare ``RETURN node`` would hand a
``falkordb.node.Node`` to a value object that wanted an id. ``node.id AS id``
is the shape, on all of them.

**The procedures throw, and that is why §5.1 has a guard.** Measured on the
vendored FalkorDB 4.20.3: ``algo.labelPropagation`` over a label the graph does
not hold raises ``ResponseError: labelPropagation configuration, unknown label
Nope``, and over an unknown relationship type it raises the same way — which is
exactly the state a fresh archive is in before the first ``CO_ADDRESSED`` merge.
``algo.pageRank`` is the odd one out and answers with no rows instead. So every
caller runs :func:`~mailarc_analytics.derived.algorithms.graph_algorithms`
first, catches ``ResponseError`` around each call, and counts what it stepped
over in ``DerivedCounts.algorithms_skipped``.

Two more things this backend's procedures do that a reader would not guess,
both measured rather than assumed:

* ``algo.pageRank`` takes **two positional arguments** — ``('Message',
  'REPLIES_TO')`` — and not the configuration map its siblings take. Passing a
  map raises ``Procedure `algo.pageRank` requires 2 arguments, got 1``.
* ``algo.betweenness`` refuses a sampling size of zero (``'samplingSize'
  should be a positive integer``), which is why
  :attr:`~mailarc_analytics.derived.config.AnalyticsConfig.betweenness_sampling`
  means "skip the call" at zero rather than "sample nothing".
"""

PROCEDURES = """\
CALL dbms.procedures() YIELD name
RETURN name
"""
"""Which procedures this store actually has — §5.1's capability probe.

The one call that is safe on an empty graph, which is why the guard starts
here: it names no label and no relationship type, so there is nothing for it to
be unknown about. Everything else in this module is only run when the name it
needs came back from this one.

Names come back in the binary's own spelling — ``algo.WCC``, ``algo.BFS``,
``algo.labelPropagation`` — while the store's *error messages* lower-case them.
The probe's caller lower-cases the whole set for that reason, and asks for
``algo.labelpropagation``.
"""

LABEL_PROPAGATION = """\
CALL algo.labelPropagation({nodeLabels: ['Address'], relationshipTypes: ['CO_ADDRESSED'], maxIterations: $max_iterations})
YIELD node, communityId
RETURN node.id AS id, communityId AS community
"""
"""Which addresses form a circle — the partition ``Community`` is built from.

Over ``CO_ADDRESSED``, the edge A1 already materialised, because that is what
"these people are written to together" is stored as. The arrow the edge carries
is an artefact of the writer ordering each pair smaller-id-first, and label
propagation is undirected in effect — it spreads a label along every incident
edge — so the ordering does not bias the partition the way a PageRank over the
same edge would. ``tests/derived/test_derived_algorithms_local.py`` is where
that is measured rather than argued.

``$max_iterations`` is pinned by the caller instead of left to the procedure's
default, because FalkorDB's LPA takes **no seed**: an unconverged run is where
two rebuilds over an unchanged graph can label an ambiguous node differently.
The iteration count is the one thing this end can hold still, and
:func:`~mailarc_analytics.derived.model.community_id` absorbs what it cannot —
a changed partition writes a differently-keyed node rather than renaming one.

Throws on an archive that has never been analysed: no ``CO_ADDRESSED``
relationship type exists until A1 has run once. Guarded by its caller, counted,
and the stage reports zero.
"""

MESSAGE_PAGERANK = """\
CALL algo.pageRank('Message', 'REPLIES_TO') YIELD node, score
RETURN node.id AS id, score AS score
"""
"""Which messages sit at the centre of the archive's conversations.

**The one edge in this graph that is genuinely directed**, and the reason this
is the only PageRank that runs in the store. ``CO_ADDRESSED`` is written
smaller-id-first, so a PageRank over it would rank an address by where its id
sorts — a number that looks like centrality and is an artefact of a string
comparison. Address centrality is therefore power iteration in Python over the
undirected pair counts, in
:mod:`mailarc_analytics.derived.centrality`; a reply chain needs none of that,
because ``(reply)-[:REPLIES_TO]->(parent)`` means what an arrow is supposed to
mean.

Two positional arguments and not a configuration map, which is this backend's
own inconsistency rather than a choice made here: measured on FalkorDB 4.20.3,
the map form raises ``Procedure `algo.pageRank` requires 2 arguments, got 1``.
The label and the relationship type are literals the *statement* fixes and no
caller can reach, so the positional form costs the security boundary nothing.

Unlike its siblings it does not throw on a graph without ``REPLIES_TO`` — it
answers with no rows, measured — so the guard around it is a formality that
stays for symmetry rather than for safety.
"""

ADDRESS_BETWEENNESS = """\
CALL algo.betweenness({nodeLabels: ['Address'], relationshipTypes: ['CO_ADDRESSED'], samplingSize: $sampling_size, samplingSeed: $sampling_seed})
YIELD node, score
RETURN node.id AS id, score AS score
"""
"""Which addresses are the bridges between circles — optional, and off.

Betweenness is the one centrality that says something PageRank cannot: an
address on every path between two groups is a broker even when few people write
to it. It is also the most expensive procedure in the set, and nothing renders
the number yet, so
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.betweenness_sampling`
defaults to zero and the caller does not run it at all.

Zero means *skip*, not *sample nothing*: the procedure refuses a sampling size
of zero outright (``betweenness configuration, 'samplingSize' should be a
positive integer``), which is measured and is why the setting is a size rather
than a flag.

``$sampling_seed`` is bound for the same reason ``$max_iterations`` is on the
statement above. An unseeded sample is a second way for two rebuilds over an
unchanged graph to disagree, and this phase's contract is that they do not.
"""

SHORTEST_PATHS = """\
MATCH (a:Address), (b:Address)
WHERE a.id = $left AND b.id = $right
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['CO_ADDRESSED'], relDirection: 'both', maxLen: $max_len, pathCount: $path_count})
YIELD path
RETURN [one IN nodes(path) | one.id] AS ids, [edge IN relationships(path) | type(edge)] AS types
"""
"""How two people are connected — "show me the path" in the graph explorer.

The one statement here that *does* open with a ``MATCH``, because ``SPpaths``
takes its endpoints as bound nodes rather than as ids. It is raw Cypher all the
same: the ``CALL`` is still a clause the builder would put in a pipeline it
compiles its own way, and the projection below is a list comprehension the
builder has no expression for.

Over ``CO_ADDRESSED`` and ``relDirection: 'both'``, which is what makes the
answer readable: a path through the messages themselves would alternate person,
mail, person and report a hop count twice what a human would call it. Both ends
are addresses and every step is "these two were written to together".

Projected as two lists of scalars — the ids along the path and the type of each
step — because a ``path`` is an object the driver hands back whole and a
subgraph reader wants ids. An endpoint that does not exist yields no rows at
all rather than raising, measured, so a stale id from a bookmarked link is an
empty answer and not an error.

``$max_len`` and ``$path_count`` are both required. A path search with neither
is an unbounded walk over the densest edge in the archive.
"""

NEIGHBOURHOOD = """\
MATCH (m:Message)
WHERE m.id = $id
CALL algo.BFS(m, $depth, NULL) YIELD nodes, edges
RETURN [one IN nodes | one.id] AS ids, [edge IN edges | type(edge)] AS types
"""
"""What is around one message, out to a depth the caller sets.

The explorer's expansion step. ``NULL`` as the third argument means *every*
relationship type, which is what "what is around this" has to mean: a message's
neighbourhood is its sender, its recipients, its thread, its labels, its
attachments and — one hop further — the other mail those people are on.

``algo.BFS`` walks the **outgoing** edges of the node it starts from, measured,
and every edge a ``Message`` owns points away from it, so the walk covers the
message's own world without needing a direction override the procedure does not
offer. It also does *not* include the source node in ``nodes``; the caller
already has it, since the caller is the one that asked for it by id.

Both columns are lists of scalars for the reason
:data:`SHORTEST_PATHS`' are. A message id that is not in the archive yields no
rows rather than raising — measured — which is the answer a link from an older
rebuild should get.
"""
