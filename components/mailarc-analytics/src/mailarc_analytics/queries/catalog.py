"""Every Cypher statement the derived layer runs, named and parameterised.

**No free Cypher from outside.** A statement is a module-level constant here or
it does not exist: caller input reaches the graph as a bound ``$parameter``,
never as a formatted string, so no address, subject or label can ever change
what a statement *does*. Phase 6's MCP server serves a model from this same
file for the same reason — a query catalogue is the only shape in which
"let something else ask the archive questions" is safe.

Naming them also makes them reviewable. These four statements are §6 and §12 of
the spec with the model's real properties put back: ``Address`` has no
``address`` property, the key is ``id``; ``Group``'s key field is ``id``, not
``key``; and the group query's thresholds are parameters rather than the
literals ``> 2`` and ``> 5``, or the configuration would be decorative.

:mod:`mailarc_core.archive.repository` states the house rule that graph reads
go through runic's query builder, so a file full of strings has to argue for
itself. Four things the builder cannot do, all of them load-bearing here:

``[:SENT_TO|COPIED_TO]``
    ``traverse()`` takes one relation descriptor, so an alternation in a single
    pattern is not expressible — and A1 is *defined* as the walk over both.
``DELETE``
    The builder has none. ``rebuild-derived`` is a delete before it is anything
    else.
``MERGE``
    ``session.add()`` compiles to ``CREATE``. Adding a ``Group`` whose key is
    already there silently makes a second node — derived labels carry no unique
    constraint — and every edge written afterwards is then written twice.
    Idempotence is the phase's contract, so the writes are ``MERGE`` by hand.
``$token IN m.refs``
    ``.in_()`` on a list property compiles to ``m.refs IN $p0``, asks whether
    the whole list is an element of the parameter, and returns nothing at all.

Two things every caller has to know, because a raw statement gets none of
runic's mapping. A ``datetime`` is stored as an ISO-8601 **string**, so a row
handed to one of the ``MERGE_`` statements binds ``value.isoformat()`` or
``None`` — never a ``datetime``. And ``Message.simhash`` comes back as the
*signed* 64-bit integer the writer had to store: run it through
:func:`~mailarc_core.archive.model.to_unsigned_64` before banding, comparing or
rendering it.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

ACCOUNT_ADDRESSES = """\
MATCH (a:Account)
WHERE a.address IS NOT NULL
RETURN DISTINCT a.address AS address
"""
"""Every address this archive imports from — what "sent by me" means.

Read once at the start of a rebuild and compared, lowercased, against each
message's sender. A template is only worth automating if the user writes it.
"""

MESSAGE_PROPERTIES = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id > $after
RETURN m.id AS id,
       m.sent_at AS sent_at,
       m.subject_norm AS subject_norm,
       m.participant_key AS participant_key,
       m.simhash AS simhash,
       m.refs AS refs
ORDER BY m.id
LIMIT $limit
"""
"""The scalar half of :class:`~mailarc_analytics.derived.model.MessageFacts`.

Nodes without a canonical id are skipped rather than defaulted, the same way
:class:`~mailarc_core.archive.repository.MessageRepository` skips them: the
writer does not produce one, but a graph that has been around — a smoke test,
an older schema — can hold one, and a rebuild that tripped over it would take
the whole job down. An id that is the empty string is not a canonical id
either, and ``m.id > $after`` leaves it behind for the same reason; the cursor
starts at ``""``, so :data:`COUNT_UNIDENTIFIED` is exactly this filter's
complement and the two still add up to every ``Message`` node.

Ordered by id, always, not only when a ceiling is set. Paging without an order
is undefined in Cypher, and two rebuilds reading different pages would cluster
differently and mint different topic ids.

**A cursor and not an offset.** ``SKIP $skip`` reads correctly and costs
quadratically: a graph store has no way to reach row twenty thousand except by
matching, expanding and sorting the twenty thousand before it, so every page
re-does the whole archive and reading it in pages costs ``O(n² / PAGE_SIZE)``.
Measured on the vendored FalkorDB, sixteen times the messages cost sixty-five
times the time. Carrying the last id forward turns each page into an index
seek, and it is what makes the range index on ``Message.id`` pay.
"""

MESSAGE_RELATIONS = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id > $after
WITH m ORDER BY m.id LIMIT $limit
OPTIONAL MATCH (m)-[:SENT_FROM]->(s:Address)
OPTIONAL MATCH (m)-[:SENT_TO|COPIED_TO]->(r:Address)
OPTIONAL MATCH (m)-[:BLIND_COPIED_TO]->(b:Address)
OPTIONAL MATCH (m)-[:IN_THREAD]->(t:Thread)
OPTIONAL MATCH (m)-[:HAS_ATTACHMENT]->(f:Attachment)
RETURN m.id AS id,
       collect(DISTINCT s.id) AS senders,
       collect(DISTINCT r.id) AS addressed,
       collect(DISTINCT b.id) AS blind_copied,
       collect(DISTINCT t.id) AS threads,
       collect(DISTINCT f.id) AS attachments
ORDER BY id
"""
"""The set half of the facts, joined to :data:`MESSAGE_PROPERTIES` by id.

The page is cut **before** the optional matches, not after. Five expansions
that cross-multiply — a message with fifty recipients and twenty attachments is
a thousand intermediate rows — are the expensive half of this statement, and a
``LIMIT`` at the end would pay for the whole archive's expansion to keep two
thousand messages. ``WITH m ORDER BY m.id LIMIT $limit`` picks the page off the
index first and expands only that.

Two statements rather than one because the grouping key of an aggregating
``RETURN`` is every non-aggregated item in it, and ``m.refs`` is a list — a
list as a grouping key is asking a graph store for trouble it has no reason to
give. Grouping on ``m.id`` alone is a string comparison and always safe.

Bcc comes back in its own column and is deliberately kept out of ``addressed``:
a Bcc recipient was written to *without* the others knowing, so a
``CO_ADDRESSED`` edge between them would materialise exactly the confidentiality
the header exists to protect. It belongs in the participant set all the same,
because ``participant_key`` was hashed over it.

The optional matches multiply rows before ``collect(DISTINCT …)`` folds them
back; a message with ten recipients and three attachments costs thirty rows,
which is the price of reading each message once instead of five times.
"""

COUNT_UNIDENTIFIED = """\
MATCH (m:Message)
WHERE m.id IS NULL OR m.id = ''
RETURN count(m) AS total
"""
"""``Message`` nodes with no canonical id — what the reader silently steps over.

The complement of :data:`MESSAGE_PROPERTIES`'s filter, asked separately so the
skipping is a number in the job row rather than an absence in the result. A
non-zero answer means the graph holds something the writer cannot produce, and
that is worth seeing before it is worth explaining.

The empty string is in here rather than in the read because it is the same
kind of node: :func:`~mailarc_core.mail.identity.canonical_id` always answers
something, so an id of ``''`` is a property that was written by something
else. Counting it here is what keeps "read plus unidentified" equal to every
``Message`` node in the graph.
"""

COUNT_MESSAGES = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id <> ''
RETURN count(m) AS total
"""
"""How many messages a rebuild *could* have read — asked only under a ceiling.

:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.max_messages` is the
one omission in this package that nothing else counts: ``unidentified``,
``wide_messages``, ``unhashable_messages`` and both dropped-bucket numbers all
reach the job row, while a rebuild capped at a thousand messages of a hundred
thousand reported the same shape as one on a small archive. This is the total
that turns that into a number.

Exactly :data:`MESSAGE_PROPERTIES`'s filter without the cursor, so the
subtraction is against the same population the read walks and not against every
node wearing the label.
"""

MESSAGE_BODIES = """\
MATCH (m:Message)
WHERE m.id IN $ids
RETURN m.id AS id, m.body_clean AS body_clean
"""
"""The cleaned bodies of named messages — A3's second read.

Only the members of an actual template need their text, for the sample and for
the word count the brevity factor uses. Reading a hundred thousand bodies to
keep a few hundred puts the archive's text next to an in-process FalkorDB for
no gain; ``$ids`` is what keeps a rebuild's memory bounded by its findings.
"""

DELETE_GROUPS = """\
MATCH (n:Group)
WITH n LIMIT $batch
DETACH DELETE n
RETURN count(n) AS removed
"""
"""Drop a batch of ``Group`` nodes; loop until ``removed`` is zero.

``DETACH DELETE`` on a derived label takes that node and the edges incident to
it and nothing else, so the ``ADDRESSED_GROUP`` edges go with their group and
no ``Message`` is touched. Batched because FalkorDB has no
``CALL … IN TRANSACTIONS`` and one unbounded delete over a large archive is a
single long stall on a store the UI is also reading.
"""

DELETE_TOPICS = """\
MATCH (n:Topic)
WITH n LIMIT $batch
DETACH DELETE n
RETURN count(n) AS removed
"""
"""Drop a batch of ``Topic`` nodes, their ``ABOUT`` edges with them."""

DELETE_TEMPLATES = """\
MATCH (n:Template)
WITH n LIMIT $batch
DETACH DELETE n
RETURN count(n) AS removed
"""
"""Drop a batch of ``Template`` nodes, their ``INSTANCE_OF`` edges with them."""

DELETE_CO_ADDRESSED = """\
MATCH (:Address)-[r:CO_ADDRESSED]->(:Address)
WITH r LIMIT $batch
DELETE r
RETURN count(r) AS removed
"""
"""Drop a batch of ``CO_ADDRESSED`` edges, keeping both addresses.

The one derived thing that lives *between* two ground-truth nodes, which is
why it needs its own statement and why it is ``DELETE r`` and never
``DETACH DELETE``: detaching here would take the addresses down and with them
every ``SENT_TO`` in the archive.

Written with an arrow although the edge is undirected in meaning. Both ends
carry the same label, so a directed pattern still matches every edge — exactly
once, which an undirected one would not, and the count would then be double.
"""

MERGE_GROUPS = """\
UNWIND $rows AS row
MERGE (g:Group {id: row.id})
SET g.size = row.size,
    g.message_count = row.message_count,
    g.first_seen = row.first_seen,
    g.last_seen = row.last_seen
"""
"""Upsert groups. ``$rows``: ``id``, ``size``, ``message_count``,
``first_seen``, ``last_seen`` (the last two ISO-8601 strings or ``None``)."""

MERGE_ADDRESSED_GROUP = """\
UNWIND $rows AS row
MATCH (m:Message {id: row.message_id}), (g:Group {id: row.group_id})
MERGE (m)-[:ADDRESSED_GROUP]->(g)
"""
"""Attach messages to their group. ``$rows``: ``message_id``, ``group_id``.

``MATCH`` and not ``MERGE`` on the two nodes: a group that is not there yet is
a bug in the caller's ordering, and merging it would paper over that with an
empty node instead of writing no edge.
"""

MERGE_CO_ADDRESSED = """\
UNWIND $rows AS row
MATCH (a:Address {id: row.left}), (b:Address {id: row.right})
MERGE (a)-[r:CO_ADDRESSED]-(b)
SET r.count = row.count,
    r.first_seen = row.first_seen,
    r.last_seen = row.last_seen
"""
"""Upsert co-addressing. ``$rows``: ``left``, ``right`` (the smaller id first),
``count``, ``first_seen``, ``last_seen``.

The ``MERGE`` pattern carries no arrow, so the same pair handed in either order
finds the same edge instead of growing a second one. Every read has to match it
the same way — ``(a:Address)-[r:CO_ADDRESSED]-(b:Address)``, no arrow — because
which way round the edge was physically stored is an accident of who was
written to first.
"""

MERGE_TOPICS = """\
UNWIND $rows AS row
MERGE (t:Topic {id: row.id})
SET t.label = row.label,
    t.method = row.method,
    t.score = row.score,
    t.message_count = row.message_count,
    t.first_seen = row.first_seen,
    t.last_seen = row.last_seen
"""
"""Upsert topics. ``$rows``: ``id``, ``label``, ``method``, ``score``,
``message_count``, ``first_seen``, ``last_seen``."""

MERGE_ABOUT = """\
UNWIND $rows AS row
MATCH (m:Message {id: row.message_id}), (t:Topic {id: row.topic_id})
MERGE (m)-[r:ABOUT]->(t)
SET r.score = row.score, r.method = row.method
"""
"""Attach messages to their topic. ``$rows``: ``message_id``, ``topic_id``,
``score``, ``method``.

The score and method are per *message*, not per topic: a message pulled in by a
ticket token and one pulled in by a shared attachment sit in the same cluster
and should not claim the same confidence.
"""

MERGE_TEMPLATES = """\
UNWIND $rows AS row
MERGE (t:Template {id: row.id})
SET t.sample_text = row.sample_text,
    t.occurrences = row.occurrences,
    t.automation_score = row.automation_score,
    t.direction = row.direction,
    t.first_seen = row.first_seen,
    t.last_seen = row.last_seen
"""
"""Upsert templates. ``$rows``: ``id``, ``sample_text``, ``occurrences``,
``automation_score``, ``direction`` (``"sent"`` or ``"received"``),
``first_seen``, ``last_seen``."""

MERGE_INSTANCE_OF = """\
UNWIND $rows AS row
MATCH (m:Message {id: row.message_id}), (t:Template {id: row.template_id})
MERGE (m)-[r:INSTANCE_OF]->(t)
SET r.distance = row.distance
"""
"""Attach messages to their template. ``$rows``: ``message_id``,
``template_id``, ``distance`` (bits from the group's representative)."""

CO_RECIPIENTS = """\
MATCH (a:Address)<-[:SENT_TO|COPIED_TO]-(m:Message)-[:SENT_TO|COPIED_TO]->(b:Address)
WHERE m.id IS NOT NULL AND m.id <> '' AND a.id < b.id
RETURN a.id AS left_id, b.id AS right_id, count(m) AS together
ORDER BY together DESC
LIMIT $limit
"""
"""A1 straight off the ground truth, no derived edge involved — §6.1.

The self-join gets expensive somewhere around a hundred thousand messages,
which is what :data:`MERGE_CO_ADDRESSED` materialises it for. It stays here
because it is the definition: if the edge and this query ever disagree, the
edge is wrong.

``a.id < b.id`` is what makes an unordered pair appear once. The sender is not
in the pattern on purpose — they are the one addressing, not one of the
addressed, and including them would make the heaviest edge in every archive
"the user, and everyone the user has ever mailed".

The canonical-id filter is :data:`MESSAGE_PROPERTIES`'s, and it is here because
a cross-check is only worth anything if both sides count the same population. A
rebuild skips a ``Message`` with no id or an empty one — that is what
:data:`COUNT_UNIDENTIFIED` counts — so the edge can never represent it, and
counting it here made the truth side higher than the edge by construction.
Measured on a graph with one readable message and two id-less ones: the edge
said 1, this said 3, and the cross-check called it a disagreement whose stated
causes did not include the real one. No rebuild could ever have cleared it.
"""

TOP_CO_ADDRESSED = """\
MATCH (a:Address)-[r:CO_ADDRESSED]-(b:Address)
WHERE a.id < b.id AND r.count IS NOT NULL
RETURN a.id AS left_id,
       b.id AS right_id,
       r.count AS together,
       r.first_seen AS first_seen,
       r.last_seen AS last_seen
ORDER BY together DESC
LIMIT $limit
"""
"""The same answer off the materialised edge — and the worked example of
reading it without an arrow. The ``a.id < b.id`` filter is what turns the two
directions the undirected pattern matches into one row.

``r.count IS NOT NULL`` guards the *sort*, not the arithmetic. Under this
backend a NULL sorts **first** on ``ORDER BY … DESC``, so an edge that somehow
carries no count would take the top slot of a listing ordered by weight — and
:func:`~mailarc_analytics.queries.rows.as_int` decodes it to 0, which is also
the value ``CoAddressedAgreement`` reads as "this listing was never cut". A
single countless edge therefore both stole a row from a real pair and told the
cross-check that its silence about every other pair was proof. The writer
always sets ``r.count``, so this is a guard against a corrupted or
hand-migrated graph — on the one comparison whose whole job is to notice one.
"""

RECURRING_GROUPS = """\
MATCH (g:Group)
WHERE g.size >= $min_size AND g.message_count >= $min_messages
RETURN g.id AS id,
       g.size AS size,
       g.message_count AS message_count,
       g.first_seen AS first_seen,
       g.last_seen AS last_seen
ORDER BY message_count DESC
LIMIT $limit
"""
"""Which *groups* write repeatedly, rather than which pairs — §6.1.

The spec's version walks in from ``(m:Message)-[:ADDRESSED_GROUP]->(g)`` and
then returns the group's properties, which yields one identical row per
message. The count is already on the node; the message is not needed to read
it.
"""

TOP_TEMPLATES = """\
MATCH (t:Template)
WHERE t.direction = $direction
RETURN t.id AS id,
       t.occurrences AS occurrences,
       t.automation_score AS automation_score,
       t.sample_text AS sample_text,
       t.first_seen AS first_seen,
       t.last_seen AS last_seen
ORDER BY automation_score DESC
LIMIT $limit
"""
"""What is worth automating, best first — §12, with the direction put back.

§6.3 requires sent and received to be reported separately, so a listing that
mixes them is not the one the spec asks for: the scores are only comparable
within one direction, and only the sent ones are anybody's to automate.
"""

TOPIC_BREAKDOWN = """\
MATCH (m:Message)-[r:ABOUT]->(t:Topic)
RETURN t.id AS id, t.label AS label, r.method AS method, count(m) AS messages
ORDER BY messages DESC
LIMIT $limit
"""
"""Topics by size, split by the signal that drew each edge — §12.

``method`` in the grouping key rather than off the node, because that is the
column a reader has to look at before believing the row: the same topic can
hold messages joined by a ticket token and messages joined by nothing stronger
than a shared attachment.
"""

COUNT_GROUPS = "MATCH (n:Group) RETURN count(n) AS total"
"""How many ``Group`` nodes there are."""

COUNT_TOPICS = "MATCH (n:Topic) RETURN count(n) AS total"
"""How many ``Topic`` nodes there are."""

COUNT_TEMPLATES = "MATCH (n:Template) RETURN count(n) AS total"
"""How many ``Template`` nodes there are."""

COUNT_CO_ADDRESSED = """\
MATCH (:Address)-[r:CO_ADDRESSED]->(:Address)
RETURN count(r) AS total
"""
"""How many ``CO_ADDRESSED`` edges there are.

Directed for the same reason :data:`DELETE_CO_ADDRESSED` is: both ends are
addresses, so an arrow costs no matches and saves counting each edge twice.
These four counts are what makes "a second rebuild changes nothing" a test
rather than a claim.
"""

COUNT_NEEDING_EMBEDDING = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id <> ''
  AND m.body_clean IS NOT NULL AND m.body_clean <> ''
  AND (m.embedding IS NULL OR m.embedding_model IS NULL
       OR m.embedding_model <> $model)
RETURN count(m) AS total
"""
"""How many messages the embed job still owes a vector — its ``total``.

Asked once, before the first page, because it is what a progress bar divides
by: recomputing it per page over an archive that is being imported at the same
time makes the bar go backwards, which reads as a fault rather than as news.

The three-part condition is one question — "not embedded *by this model*" —
and each part catches a different history. ``embedding IS NULL`` is a message
the import wrote and nothing has embedded yet. ``embedding_model IS NULL`` is a
vector written by something that did not say what produced it, which cannot be
trusted to match the index. ``embedding_model <> $model`` is the case §7.4
built the property for: the user changed embedder, and every old vector is now
a lie about which space it lives in.

A message with no ``body_clean`` is not pending, it is *unembeddable* — there
is no text to embed — so it is excluded here rather than counted and skipped,
or the job would end reporting failures for messages nothing could ever fix.
"""

MESSAGES_NEEDING_EMBEDDING = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id > $after
  AND m.body_clean IS NOT NULL AND m.body_clean <> ''
  AND (m.embedding IS NULL OR m.embedding_model IS NULL
       OR m.embedding_model <> $model)
RETURN m.id AS id,
       m.subject AS subject,
       left(m.body_clean, $max_chars) AS body
ORDER BY m.id
LIMIT $limit
"""
"""One page of messages to embed, with the text already cut to length.

:data:`COUNT_NEEDING_EMBEDDING`'s condition plus :data:`MESSAGE_PROPERTIES`'s
cursor, and both halves are load-bearing. The cursor is a cursor and not a
``SKIP`` for the reason that statement gives — an offset walk re-sorts the
whole archive per page and costs ``O(n² / page)`` — and it works here for the
extra reason that the pages *shrink behind it*: every page written stops
matching this pattern, so a re-read from the start would be correct too, but
only the ordered cursor guarantees the walk terminates while the set it walks
is changing under it.

``left(m.body_clean, $max_chars)`` truncates in the store rather than in
Python. ``body_clean`` is uncapped and a page is five hundred of them, so
sending them whole would move tens of megabytes per page to embed the first two
thousand characters of each — and the embedder would refuse the rest anyway.
"""

WRITE_EMBEDDINGS = """\
UNWIND $rows AS row
MATCH (m:Message {id: row.id})
SET m.embedding = vecf32(row.vector), m.embedding_model = $model
RETURN count(m) AS written
"""
"""Attach computed vectors to their messages. ``$rows``: ``id``, ``vector``.

The one statement in this catalogue that writes a *ground-truth* node, and the
one the package docstring's exception is about: an embedder only ever adds a
vector, and this sets exactly two properties that the import deliberately never
writes (see :mod:`mailarc_core.archive.writer`, which leaves an existing
``Message`` untouched precisely so this phase can fill them in).

``MATCH`` and never ``MERGE``: a row naming a message that is not there is a
bug in the caller, and merging it would invent an empty ``Message`` carrying
nothing but a vector — a node that no import can ever reconcile and that every
search would happily return.

``vecf32(row.vector)`` on the way in, and never on the way out. The function
turns a list into a vector; applying it to a stored property raises ``Type
mismatch: expected List or Null but was Vectorf32``. The vector arrives as a
plain list of floats because a raw statement gets none of runic's converters.

The model is bound once for the whole batch rather than per row: a batch is one
embedder's answer by construction, and a row that claimed a different model
would be a vector in one space labelled as another.
"""

SEMANTIC_NEIGHBOURS = """\
CALL db.idx.vector.queryNodes('Message', 'embedding', $k, vecf32($vector))
YIELD node, score
WITH node, score
WHERE node.id IS NOT NULL AND node.id <> ''
  AND node.embedding_model = $model
OPTIONAL MATCH (node)-[:SENT_FROM]->(s:Address)
RETURN node.id AS id,
       node.subject AS subject,
       node.sent_at AS sent_at,
       s.id AS sender,
       score AS distance
ORDER BY distance
LIMIT $limit
"""
"""The ``$k`` nearest messages to a vector, cut to ``$limit`` after filtering.

Two parameters for what looks like one number, and the difference is the whole
usable shape of this statement. **The procedure cannot be filtered before the
fact**: a ``MATCH`` above it does not narrow it, and binding its output to an
already-matched variable returns nothing at all — measured. So ``$k`` is how
wide the index search goes and ``$limit`` is what the caller sees, and every
row dropped in between (a node with no canonical id, and whatever a caller
filters further) has to be paid for up front. Asking for ``k = limit`` and
filtering leaves a short page that looks like a small archive.

``score`` is a **distance**: cosine gives ``1 - similarity``, lower is better,
and an exact match can come back very slightly *negative* — measured
``-1.19e-07`` on a normalised 768-dimensional vector. Anything converting it to
a similarity has to clamp, or a UI shows 100.00001 %.

A message with no vector is not ranked low here, it is absent from the index
entirely, and nothing in the result says so. That is what
:data:`VECTOR_COVERAGE` is for, and why every semantic answer in this project
carries it.

``$model`` is the half that keeps the coverage notice honest, and it is not
optional politeness. The index holds one vector per message and says nothing
about which model produced it, so a half-finished re-embed leaves two spaces in
one index — measured on a real server: twelve messages embedded by one model,
five re-embedded by another, and a search under the new one returned six
confidently ranked hits, at least one of them a comparison between two spaces,
under a notice saying seven messages could not be found. Filtering here rather
than trusting the index means a changed embedder degrades to *fewer* hits
instead of to wrong ones, and :data:`VECTOR_COVERAGE` and this statement then
mean the same thing by "embedded". The rows dropped are paid for by ``$k``,
which is what the over-fetch is for.
"""

SEMANTIC_TOPIC_PAIRS = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id <> ''
  AND m.embedding_model = $model
CALL db.idx.vector.queryNodes('Message', 'embedding', $k, m.embedding)
YIELD node, score
WITH m, node, score
WHERE node.id IS NOT NULL AND node.id > m.id
  AND node.embedding_model = $model
  AND score <= $max_distance
RETURN m.id AS left, node.id AS right, score AS distance
ORDER BY distance
LIMIT $limit
"""
"""Signal 6: every pair of messages that landed close together, closest first.

The whole archive's neighbours in **one** round trip, which is the difference
between signal 6 being usable and being a per-message KNN over a hundred
thousand messages. Two things had to be measured before this shape was possible
and both were, against the vendored FalkorDB:
``db.idx.vector.queryNodes`` accepts the vector straight off a node matched
above it, and a ``WHERE`` after its ``YIELD`` really does narrow what it
produced. (That is not in tension with :data:`SEMANTIC_NEIGHBOURS`'s warning,
which is about binding ``node`` to an *already-matched* variable — a different
thing, and still true.)

``node.id > m.id`` does two jobs with one predicate. The KNN returns the query
node itself first for every message, and it is symmetric — *a* names *b* and *b*
names *a* — so an unordered comparison would offer one self-pair plus one
duplicate per edge, and §6.2's weak-pair budget would pay for all three.

``score`` is a cosine **distance**, so the caller's similarity floor arrives
here as ``$max_distance = 1 - minimum`` and is applied in the store. Filtering
in Python instead would move the archive's whole neighbour cross product over
the wire to throw most of it away.

``$model`` for the reason :data:`SEMANTIC_NEIGHBOURS` binds it: a half-finished
re-embed leaves two spaces in one index, and a topic joined across them is a
suggestion made from a comparison nobody computed.
"""

FULLTEXT_MESSAGES = """\
CALL db.idx.fulltext.queryNodes('Message', $text)
YIELD node, score
WITH node, score
WHERE node.id IS NOT NULL AND node.id <> ''
OPTIONAL MATCH (node)-[:SENT_FROM]->(s:Address)
RETURN node.id AS id,
       node.subject AS subject,
       node.sent_at AS sent_at,
       s.id AS sender,
       score AS relevance
ORDER BY relevance DESC
LIMIT $limit
"""
"""Full-text search over ``subject`` and ``body_text`` — the path that always
works, embedder or not.

``score`` is a **relevance**: higher is better, the opposite convention to
:data:`SEMANTIC_NEIGHBOURS`'s distance. The two are not comparable and must
never be sorted into one list without a stated normalisation, or the merge
invents a ranking neither index produced.

``$text`` is a bound parameter, so no Cypher can be injected through it — but
it reaches **RediSearch**, which is a second query language with operators of
its own: ``|`` is OR, a leading ``-`` negates, ``@subject:`` selects a field,
``*`` truncates, and a lone ``(`` raises a syntax error. A caller here may be a
model reading through MCP, so the words are tokenised in
:mod:`mailarc_analytics.semantic.search` before they arrive — this docstring is
the reason that tokeniser exists and is not optional politeness.
"""

VECTOR_COVERAGE = """\
MATCH (m:Message)
WHERE m.id IS NOT NULL AND m.id <> ''
RETURN count(m) AS total,
       count(CASE WHEN m.embedding_model = $model THEN 1 END) AS embedded,
       count(CASE WHEN m.body_clean IS NULL OR m.body_clean = ''
                  THEN 1 END) AS unembeddable
"""
"""How much of the archive the current model has embedded — one scan, three
numbers.

Carried by every semantic answer for a reason the answer itself cannot show: a
KNN over a half-embedded archive returns a short, entirely plausible result
set, and it looks exactly like a complete search over a small archive. Without
this pair of numbers, "the embed job is only a third done" and "your archive
holds nothing about this" are the same output.

Counted against ``$model`` rather than against ``embedding IS NOT NULL``: a
vector produced by a different model is in a different space, and a search
under the current one cannot find it. It is un-embedded in every sense that
matters here.

The third number is what stops the warning becoming furniture. ``total`` is the
archive's own population — every message with a canonical id, the same one
every other count in this project uses — but :data:`COUNT_NEEDING_EMBEDDING`
deliberately does *not* offer a message with no ``body_clean`` to the job: there
is no text to embed, so it is unembeddable rather than pending. Counting the two
populations differently made ``complete`` permanently false on any real archive
— an attachment-only mail and a reply that is entirely quoted text both leave
``body_clean`` empty — so a finished job was followed by a "run the embed job"
notice on every answer forever. Reported as its own count rather than subtracted
from ``total`` so the sentence a user reads still divides by the number of
messages they think they have.
"""

DROP_VECTOR_INDEX = """\
DROP VECTOR INDEX FOR (m:Message) ON (m.embedding)
"""
"""Take the vector index away, so one of a different length can be built.

The one piece of DDL in this catalogue, and it is here rather than in a graph
migration because the dimension is a *setting* now: a human picks the embedder
on the settings page, and the length follows the model they picked. A migration
is a versioned statement about the schema every installation shares; this is one
installation choosing a length, and re-running the same revision with a
different constant is not something a migration chain can express.

Paired with :data:`CREATE_VECTOR_INDEX` and never used alone — a graph left
without the index answers every semantic search with an opaque driver error.
"""

CREATE_VECTOR_INDEX = """\
CREATE VECTOR INDEX FOR (m:Message) ON (m.embedding)
OPTIONS {dimension: $dimension, similarityFunction: $similarity,
         M: $m, efConstruction: $ef_construction, efRuntime: $ef_runtime}
"""
"""Build the vector index at a chosen length.

The parameters are the migration's own constants — see
``graph_migrations/versions/5f4678dfc5a4``, which this deliberately mirrors so
that an index rebuilt here is indistinguishable from one a fresh install
migrated. Only ``$dimension`` is expected to differ, and that is the whole
point.

Everything is bound, including the numbers: FalkorDB's OPTIONS map takes
parameters, so nothing here is built by interpolation and a caller cannot reach
the statement with anything but a value.
"""

CLEAR_EMBEDDINGS = """\
MATCH (m:Message)
WHERE m.embedding IS NOT NULL OR m.embedding_model IS NOT NULL
SET m.embedding = NULL, m.embedding_model = NULL
RETURN count(m) AS cleared
"""
"""Forget every stored vector, because a resized index cannot hold them.

Not tidiness. :data:`MESSAGES_NEEDING_EMBEDDING` selects on
``embedding_model <> $model``, so a message embedded by the *same* model at the
*old* length would be skipped by the very job that is supposed to replace it —
and the vector it kept is the wrong length, stored and never indexed. Clearing
is what makes "re-index, then re-embed" actually recompute everything.

Ground truth is untouched: these two properties are the semantic phase's own,
declared on the node and left empty by the import (see
``mailarc_core.archive.writer``, which never overwrites an existing node for
exactly this reason).
"""

VECTOR_INDEX_OPTIONS = """\
CALL DB.INDEXES() YIELD label, properties, types, options
RETURN label, properties, types, options
"""
"""Every index the graph actually has, with the options it was built with.

Read before an embed job writes anything, to answer one question no other
statement can: what dimension is the live vector index? The trap §7.4 names is
that FalkorDB accepts a vector of the wrong length, stores it as a property and
declines to index it — no exception, no log line, ``indexingFailures`` stays at
zero. A job run against a mismatched index therefore reports every message
embedded and leaves every one of them unfindable.

The one statement here that reads schema rather than data, which is why it
takes no parameters and returns the store's own column names unchanged.
"""

CATALOG: Mapping[str, str] = MappingProxyType(
    {
        "ACCOUNT_ADDRESSES": ACCOUNT_ADDRESSES,
        "MESSAGE_PROPERTIES": MESSAGE_PROPERTIES,
        "MESSAGE_RELATIONS": MESSAGE_RELATIONS,
        "COUNT_UNIDENTIFIED": COUNT_UNIDENTIFIED,
        "COUNT_MESSAGES": COUNT_MESSAGES,
        "MESSAGE_BODIES": MESSAGE_BODIES,
        "DELETE_GROUPS": DELETE_GROUPS,
        "DELETE_TOPICS": DELETE_TOPICS,
        "DELETE_TEMPLATES": DELETE_TEMPLATES,
        "DELETE_CO_ADDRESSED": DELETE_CO_ADDRESSED,
        "MERGE_GROUPS": MERGE_GROUPS,
        "MERGE_ADDRESSED_GROUP": MERGE_ADDRESSED_GROUP,
        "MERGE_CO_ADDRESSED": MERGE_CO_ADDRESSED,
        "MERGE_TOPICS": MERGE_TOPICS,
        "MERGE_ABOUT": MERGE_ABOUT,
        "MERGE_TEMPLATES": MERGE_TEMPLATES,
        "MERGE_INSTANCE_OF": MERGE_INSTANCE_OF,
        "CO_RECIPIENTS": CO_RECIPIENTS,
        "TOP_CO_ADDRESSED": TOP_CO_ADDRESSED,
        "RECURRING_GROUPS": RECURRING_GROUPS,
        "TOP_TEMPLATES": TOP_TEMPLATES,
        "TOPIC_BREAKDOWN": TOPIC_BREAKDOWN,
        "COUNT_GROUPS": COUNT_GROUPS,
        "COUNT_TOPICS": COUNT_TOPICS,
        "COUNT_TEMPLATES": COUNT_TEMPLATES,
        "COUNT_CO_ADDRESSED": COUNT_CO_ADDRESSED,
        "COUNT_NEEDING_EMBEDDING": COUNT_NEEDING_EMBEDDING,
        "MESSAGES_NEEDING_EMBEDDING": MESSAGES_NEEDING_EMBEDDING,
        "WRITE_EMBEDDINGS": WRITE_EMBEDDINGS,
        "SEMANTIC_NEIGHBOURS": SEMANTIC_NEIGHBOURS,
        "SEMANTIC_TOPIC_PAIRS": SEMANTIC_TOPIC_PAIRS,
        "FULLTEXT_MESSAGES": FULLTEXT_MESSAGES,
        "VECTOR_COVERAGE": VECTOR_COVERAGE,
        "CLEAR_EMBEDDINGS": CLEAR_EMBEDDINGS,
        "CREATE_VECTOR_INDEX": CREATE_VECTOR_INDEX,
        "DROP_VECTOR_INDEX": DROP_VECTOR_INDEX,
        "VECTOR_INDEX_OPTIONS": VECTOR_INDEX_OPTIONS,
    }
)
"""Every statement above, by name.

Written out rather than scraped off the module, so adding a statement without
listing it here is visible in a diff — and so a test can bind each one's
parameters and run the lot against a real backend, which is the only way a
string constant ever gets checked.
"""

_PARAMETER = re.compile(r"\$([a-z_][a-z0-9_]*)")


def parameters_of(statement: str) -> tuple[str, ...]:
    """The parameter names *statement* binds, sorted and deduplicated.

    Read off the text instead of maintained beside it: a hand-written list is
    a second copy of the truth and drifts the first time a statement gains a
    ``LIMIT``.
    """
    return tuple(sorted(set(_PARAMETER.findall(statement))))


def as_graph_datetime(value: datetime | None) -> str | None:
    """A timestamp in the form a bound parameter may carry.

    runic's mapper converts a ``datetime`` on its way to a node property; a
    statement run through ``Session.execute`` gets none of that, so a raw
    ``datetime`` in a ``$rows`` entry reaches the driver as an object it has no
    encoding for. ISO-8601 is what the mapper itself writes, which is also what
    makes a derived timestamp compare with an imported one.

    It lives beside the statements rather than beside the analyses because it
    is a property of *these strings*, not of anything the analyses compute.
    """
    return None if value is None else value.isoformat()
