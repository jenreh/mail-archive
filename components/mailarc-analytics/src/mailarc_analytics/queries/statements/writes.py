"""Everything the derived layer writes — six deletes, eleven upserts, five sets.

A rebuild is a delete before it is anything else, and an upsert afterwards:
these statements are that whole cycle. Idempotence is the phase's contract, so
every write here is a ``MERGE`` and every delete is batched and counted so the
caller can loop it.

**Four of them are not merges and not deletes**, and they are the exception
this file has to name. ``Message.importance``, ``importance_reasons``,
``importance_version`` and ``Address.rank``/``rank_version`` are properties on
*ground-truth* nodes: the import never writes them, a rebuild computes them,
nulls them and computes them again. The pattern is
:data:`~mailarc_analytics.queries.catalog.WRITE_EMBEDDINGS`' exactly — ``MATCH``
the node and ``SET`` the properties, never ``MERGE`` it, and clear with
``SET … = NULL`` rather than with a ``DELETE`` that would take a real node down
to reset a number computed from it. The delete guards in
:mod:`mailarc_analytics.derived.rebuild` never see these four; they are not
nodes and not edges.

What changed, and what did not
------------------------------

The statements stopped being strings and nothing they *do* moved. That was
verified rather than assumed: each of the original eleven was run beside the
string it replaces, on two isolated graphs of one embedded FalkorDB over the
same planted data, and the whole graph was dumped from each and compared node
for node, edge for edge and property for property. All eleven came back
identical, including the batch-by-batch ``removed`` sequences and a second
identical write leaving every count where it was. The eleven §5.3 adds were
born as builders and have no string to be compared against; what checks them is
``test_queries_catalog_local.py``, which runs every entry twice and counts.

The builder's ``merge()`` puts only the key in the pattern and everything else
in a following ``SET``, which is the rule a hand-written ``MERGE`` had to be
*trusted* to follow — a changed property inside the pattern makes ``MERGE``
miss the existing node and grow a second one beside it, and the derived labels
carry no unique constraint to catch that. ``add()`` still compiles to
``CREATE`` and is still never what an upsert wants.

Two things every caller has to know
-----------------------------------

**Bind, do not build.** A statement carrying ``param()`` cannot be run as
``session.execute(*statement.build())`` — the declared parameters are not in
the auto-bound dict, and FalkorDB answers with a parse error rather than a
missing value. Statement objects go through
``session.all_rows(statement, params)``, which runs writes as happily as reads
and returns ``[]`` for a write with no ``RETURN``. A binding that leaves a
declared parameter out raises ``ValueError: statement is missing values for
declared parameter(s): …``, so the catalogue's security property is not
weakened by the move — measured on both :data:`DELETE_GROUPS` and
:data:`MERGE_CO_ADDRESSED`.

**Encode the rows.** The old advice — "a row binds ``value.isoformat()`` or
``None``, never a ``datetime``" — was right, and is now
:func:`runic.ogm.encode_rows`' job: it applies the model's own converters
across the ``$rows`` payload before it is bound. Handing a bare ``datetime``
through without it still fails, loudly: ``ResponseError: Failed to parse query
parameter 'rows' value``. **All seven merges here are fully covered by
``encode_rows``** — checked field by field against the three node models and
the four edge models. ``Group``, ``Topic``, ``Template`` and ``CoAddressed``
each declare ``first_seen`` and ``last_seen``, so every date these statements
carry sits on a declared field; ``AddressedGroup``, ``About`` and ``InstanceOf``
carry no date at all. Keys the models do not declare — ``left``, ``right``,
``message_id``, ``group_id``, ``topic_id``, ``template_id`` — are passed
through untouched, which is exactly what an edge row needs. A payload built
with :func:`~mailarc_analytics.queries.catalog.as_graph_datetime` is still
accepted byte-identically, which is what makes the switch safe to make one call
site at a time.

The same holds for the four merges §5.3 adds. ``Community`` declares both
dates; ``MemberOf``, ``InCircle`` and ``Suggested`` carry none, and their
``address_id``, ``community_id``, ``message_id`` and ``tag_id`` keys are not
fields of the edge models and pass through as they are.
"""

from runic.ogm import alias, count, param, select, unwind
from runic.ogm.query.values import row

from mailarc_analytics.derived.model import (
    About,
    AddressedGroup,
    CoAddressed,
    Community,
    Group,
    InCircle,
    InstanceOf,
    MemberOf,
    Suggested,
    Template,
    Topic,
)
from mailarc_core.archive.model import Address, Message, Tag

# ---------------------------------------------------------------------------
# Deletions — a rebuild is a delete before it is anything else
# ---------------------------------------------------------------------------

DELETE_GROUPS = (
    select(Group)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""Drop a batch of ``Group`` nodes; loop until ``removed`` is zero.

``DETACH DELETE`` on a derived label takes that node and the edges incident to
it and nothing else, so the ``ADDRESSED_GROUP`` edges go with their group and
no ``Message`` is touched. Batched because FalkorDB has no
``CALL … IN TRANSACTIONS`` and one unbounded delete over a large archive is a
single long stall on a store the UI is also reading.

``with_("n", limit=param("batch"))`` is that batching: a ``WITH`` stage written
before the delete, which is what bounds the delete rather than the result.
Written order is compiled order, so the stage lands where the string put it.
``count("n")`` carries an explicit ``.as_("removed")`` because an aggregate
without one keys the row by its raw Cypher text — the caller reads
``row["removed"]`` and a bare ``count(n)`` would hand it ``row["count(n)"]``.

Emits ``MATCH (n:Group) WITH n LIMIT $batch DETACH DELETE n
RETURN count(n) AS removed``. Over five groups at ``batch=2`` the observed
sequence is 2, 2, 1, 0 — the same one the string produced.
"""

DELETE_TOPICS = (
    select(Topic)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""Drop a batch of ``Topic`` nodes, their ``ABOUT`` edges with them."""

DELETE_TEMPLATES = (
    select(Template)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""Drop a batch of ``Template`` nodes, their ``INSTANCE_OF`` edges with them."""

_CO_ADDRESSED_EDGE = alias(CoAddressed, "r")
"""The edge handle ``DELETE_CO_ADDRESSED`` deletes, named once.

Bound to a handle rather than to the string ``"r"`` so the variable that is
carried through the ``WITH`` stage and the variable that is deleted cannot
drift apart in an edit.
"""

DELETE_CO_ADDRESSED = (
    select(alias(Address, "a"))
    .traverse(Address.co_addressed, to="b", edge=_CO_ADDRESSED_EDGE)
    .with_(_CO_ADDRESSED_EDGE, limit=param("batch"))
    .delete(_CO_ADDRESSED_EDGE)
    .returning(count("r").as_("removed"))
)
"""Drop a batch of ``CO_ADDRESSED`` edges, keeping both addresses.

The one derived thing that lives *between* two ground-truth nodes, which is
why it needs its own statement and why it is ``DELETE r`` and never
``DETACH DELETE``: detaching here would take the addresses down and with them
every ``SENT_TO`` in the archive. ``delete()`` defaults to ``detach=False`` and
this statement never passes the flag — the one place in the catalogue where
that default is load-bearing rather than incidental. Verified on a planted
graph: after this statement has looped to zero, the three ``Address`` nodes,
the three ``Message`` nodes and all nine ``SENT_TO`` edges are still there.

Written with an arrow although the edge is undirected in meaning. Both ends
carry the same label, so a directed pattern still matches every edge — exactly
once, which an undirected one would not, and the count would then be double.
Measured on three stored pairs: the directed pattern counts 3, the undirected
one counts 6. The builder makes that choice for us — ``traverse()`` follows the
declared direction of the relation and always emits the arrow — so the shape
this statement had by argument is now the shape it has by construction.

The pattern is expressible at all because ``Address`` declares
``co_addressed`` as an ``OUTGOING`` ``Relation`` to ``Address``. That
declaration lives in **mailarc-core** and is never written there: like
``Message.embedding`` and ``Message.embedding_model``, it is a field the import
leaves alone and the analytics phase fills in. ``traverse()`` needs the
declaration to build the pattern, so without it these four ``CO_ADDRESSED``
statements would have had to stay raw Cypher.

Emits ``MATCH (a:Address) MATCH (a)-[r:CO_ADDRESSED]->(b:Address)
WITH r LIMIT $batch DELETE r RETURN count(r) AS removed``. Two differences from
the string, both cosmetic: the endpoints are named ``a`` and ``b`` instead of
being anonymous, and the pattern arrives as two ``MATCH`` clauses instead of
one. Over six stored edges at ``batch=2`` the observed sequence is 2, 2, 2, 0 —
the same one the string produced, on the same data.
"""

# ---------------------------------------------------------------------------
# Node upserts — MERGE, never add(), because idempotence is the contract
# ---------------------------------------------------------------------------

MERGE_GROUPS = (
    unwind(param("rows"))
    .merge(Group, key=Group.id, alias="g")
    .set(Group.size, Group.message_count, Group.first_seen, Group.last_seen)
)
"""Upsert groups. ``$rows``: ``id``, ``size``, ``message_count``,
``first_seen``, ``last_seen``.

The dates are no longer the caller's problem to spell: bind
``encode_rows(Group, rows)`` and a ``datetime`` on either field becomes the
ISO-8601 string the graph stores. Both fields are declared on ``Group``, so
this merge needs nothing from ``as_graph_datetime``.

Only ``id`` is in the ``MERGE`` pattern and every other property is in the
``SET`` — which is not a style choice. A changed ``message_count`` inside the
pattern would make ``MERGE`` miss the existing node and create a second one
beside it, and the derived labels carry no unique constraint to catch that.
``merge()`` enforces the split; the string had to be trusted to keep it.

A bare field descriptor in ``set()`` assigns from the same-named row key, so
``set(Group.size)`` is ``SET g.size = row.size`` and the row keys stay the
contract they were.
"""

MERGE_TOPICS = (
    unwind(param("rows"))
    .merge(Topic, key=Topic.id, alias="t")
    .set(
        Topic.label,
        Topic.method,
        Topic.score,
        Topic.message_count,
        Topic.first_seen,
        Topic.last_seen,
    )
)
"""Upsert topics. ``$rows``: ``id``, ``label``, ``method``, ``score``,
``message_count``, ``first_seen``, ``last_seen``.

Bind ``encode_rows(Topic, rows)``; both dates are declared fields.
"""

MERGE_TEMPLATES = (
    unwind(param("rows"))
    .merge(Template, key=Template.id, alias="t")
    .set(
        Template.sample_text,
        Template.occurrences,
        Template.automation_score,
        Template.direction,
        Template.first_seen,
        Template.last_seen,
    )
)
"""Upsert templates. ``$rows``: ``id``, ``sample_text``, ``occurrences``,
``automation_score``, ``direction`` (``"sent"`` or ``"received"``),
``first_seen``, ``last_seen``.

Bind ``encode_rows(Template, rows)``; both dates are declared fields, and so is
``direction``, so a caller may now hand over the
:class:`~mailarc_analytics.derived.model.TemplateDirection` member itself
instead of its ``.value``. A caller that keeps passing ``.value`` is writing
the identical string — this is a relaxation, not a change.
"""

# ---------------------------------------------------------------------------
# Edge upserts — MATCH both endpoints, never MERGE them
# ---------------------------------------------------------------------------

MERGE_ADDRESSED_GROUP = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("message_id")}, alias="m")
    .match(Group, key={Group.id: row("group_id")}, alias="g")
    .merge_edge("m", AddressedGroup, "g")
)
"""Attach messages to their group. ``$rows``: ``message_id``, ``group_id``.

``match()`` and not ``merge()`` on the two nodes: a group that is not there yet
is a bug in the caller's ordering, and merging it would paper over that with an
empty node instead of writing no edge. The two methods sit side by side on the
same builder, so the distinction is now one word rather than one keyword buried
in a string — and ``match()``'s own contract says the same thing.

``merge_edge`` takes the ``AddressedGroup`` class rather than the string
``"ADDRESSED_GROUP"``, so the relationship type comes from the model that
declares it and cannot be misspelled into a second edge type. No edge alias,
because ``AddressedGroup`` is deliberately propertyless and there is nothing to
``SET``: membership is not a judgement with a confidence behind it.

Emits ``UNWIND $rows AS row MATCH (m:Message {id: row.message_id})
MATCH (g:Group {id: row.group_id}) MERGE (m)-[:ADDRESSED_GROUP]->(g)``. The one
difference from the string is that the two endpoint matches arrive as two
``MATCH`` clauses rather than one comma-separated clause.
"""

MERGE_CO_ADDRESSED = (
    unwind(param("rows"))
    .match(Address, key={Address.id: row("left")}, alias="a")
    .match(Address, key={Address.id: row("right")}, alias="b")
    .merge_edge("a", CoAddressed, "b", alias="r")
    .set(CoAddressed.count, CoAddressed.first_seen, CoAddressed.last_seen, on="r")
)
"""Upsert co-addressing. ``$rows``: ``left``, ``right`` (the smaller id first),
``count``, ``first_seen``, ``last_seen``.

**THE CALLER ORDERING THE PAIR — SMALLER ID FIRST — IS WHAT KEEPS ONE PAIR TO
ONE EDGE.** Nothing in this statement enforces it. Hand the same two addresses
in reversed and the graph grows a second edge for them: measured, three pairs
written smaller-first give three edges, the same three written reversed give
six. That invariant used to be a convenience and is now the whole mechanism.

It moved because the arrow-less ``MERGE`` is gone. FalkorDB rejects an
undirected ``MERGE`` outright, and runic refuses to emit one there rather than
sending Cypher the server cannot parse:

    NotImplementedError: FalkorDB does not support an undirected MERGE
    ((a)-[r:T]-(b)). Express the query without it, or drop to a
    backend-specific statement via session.execute().

So the edge is written directed, and **no data migration is needed to do it**.
The old arrow-less ``MERGE`` was already storing every edge smaller→larger,
because the caller was already ordering the pair — read back off a graph the
old string wrote, the stored arrows are ``a→b``, ``a→c``, ``b→c`` and nothing
else. A directed ``merge_edge`` over the same rows writes byte-identical data;
run side by side on two graphs, the node sets, the edge sets and every property
compared equal, and a second identical write left both untouched.

The caller does order the pair, and not by accident:
:func:`~mailarc_analytics.derived.correspondents.build_correspondents` takes
``combinations`` over ``MessageFacts.addressed``, which is sorted and
deduplicated by construction, so every pair it produces is already
smaller-first.

What the readers must do is unchanged in result and changed in reason.
``COUNT_CO_ADDRESSED`` keeps its arrow and keeps counting each edge once — the
directed count is 3 where the undirected one is 6. ``TOP_CO_ADDRESSED``'s
arrow-less pattern plus ``a.id < b.id`` returns the same three rows it always
did, but ``a.id < b.id`` is no longer *deduplicating* two directions: every
stored edge is already smaller→larger, so it is a corruption guard for a graph
some earlier build wrote the other way round.

Bind ``encode_rows(CoAddressed, rows)``. ``count``, ``first_seen`` and
``last_seen`` are all declared on the edge model, so the two dates are
converted; ``left`` and ``right`` are not fields of it and are passed through
untouched, which is what the two ``MATCH`` keys need.

**One behaviour this statement no longer has.** The arrow-less ``MERGE``
*tolerated* a reversed pair — handed ``(b, a)`` after ``(a, b)``, it found the
existing edge and updated it. The directed one cannot, and creates a second
edge instead. Measured: forwards then backwards gives ``[1, 9]`` through the
old string and ``[2, 9]`` through this one. No rebuild reaches that path, for
the reason above, and a caller that constructs a
:class:`~mailarc_analytics.derived.model.CoAddressedPair` by hand cannot reach
it either: that value object now swaps ``left`` and ``right`` in a validator
rather than documenting the order and hoping — which is where the tolerance
the ``MERGE`` used to provide has gone.
"""

MERGE_ABOUT = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("message_id")}, alias="m")
    .match(Topic, key={Topic.id: row("topic_id")}, alias="t")
    .merge_edge("m", About, "t", alias="r")
    .set(About.score, About.method, on="r")
)
"""Attach messages to their topic. ``$rows``: ``message_id``, ``topic_id``,
``score``, ``method``.

The score and method are per *message*, not per topic: a message pulled in by a
ticket token and one pulled in by a shared attachment sit in the same cluster
and should not claim the same confidence.

``on="r"`` puts the two ``SET`` assignments on the edge rather than on the node
the builder is otherwise pointed at — ``About.score`` names a field of the edge
model, and the alias says which variable it lands on. The row carries no date,
so ``encode_rows(About, rows)`` has nothing to convert and every key passes
through; binding the rows unencoded works identically today, and encoding them
is what keeps that true if the edge ever gains a date.
"""

MERGE_INSTANCE_OF = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("message_id")}, alias="m")
    .match(Template, key={Template.id: row("template_id")}, alias="t")
    .merge_edge("m", InstanceOf, "t", alias="r")
    .set(InstanceOf.distance, on="r")
)
"""Attach messages to their template. ``$rows``: ``message_id``,
``template_id``, ``distance`` (bits from the group's representative).

Like :data:`MERGE_ABOUT`: both endpoints matched rather than merged, the
property set on the edge alias, and no date on the row.
"""

DELETE_COMMUNITIES = (
    select(Community)
    .with_("n", limit=param("batch"))
    .delete(detach=True)
    .returning(count("n").as_("removed"))
)
"""Drop a batch of ``Community`` nodes; loop until ``removed`` is zero.

:data:`DELETE_GROUPS`' shape, and ``DETACH DELETE`` is right here for the same
reason: ``MEMBER_OF`` and ``IN_CIRCLE`` both hang off the community and have
nowhere to be without it, while the ``Address`` and ``Message`` at their other
ends are ground truth and a detach on a *derived* label never reaches them.

The fourth derived label, so the rebuild's ``_DERIVED_NODE_DELETE`` guard has
to name it too — the regex is what stops a delete statement from ever reaching
``Message``, and a label missing from it is a delete nothing checks.
"""

_SUGGESTED_EDGE = alias(Suggested, "r")
"""The edge handle :data:`DELETE_SUGGESTED` deletes, named once.

Bound to a handle rather than to the string ``"r"`` for the reason
:data:`_CO_ADDRESSED_EDGE` is: the variable carried through the ``WITH`` stage
and the variable deleted cannot drift apart in an edit.
"""

DELETE_SUGGESTED = (
    select(alias(Tag, "t"))
    .traverse(Tag.suggested, to="m", edge=_SUGGESTED_EDGE)
    .with_(_SUGGESTED_EDGE, limit=param("batch"))
    .delete(_SUGGESTED_EDGE)
    .returning(count("r").as_("removed"))
)
"""Drop a batch of ``SUGGESTED`` edges, keeping the tag and the message.

The second derived edge that lives between two nodes this package may not
delete, and the more dangerous of the two: ``DETACH DELETE`` here would take
the ``Tag`` — a node :mod:`mailarc_core.archive.tags` owns, that no rebuild may
touch and that survives an account clear-out — and the ``Message`` with it.
``delete()`` defaults to ``detach=False`` and this statement never passes the
flag, exactly as :data:`DELETE_CO_ADDRESSED` does not.

**Rooted at the tag and not at the message**, which is the finding that made
:attr:`~mailarc_core.archive.model.Tag.suggested` a declared relation in the
first place: runic emits a predicate naming a *traversed* variable after the
whole pipeline, so a statement walked in from the message end puts its ``WITH``
stage — and anything narrowing it — behind the ``DELETE`` it was meant to bound.
Starting at the ``Tag`` puts the batching ``WITH`` where the loop needs it,
which the compiled text shows and ``test_queries_catalog.py`` asserts.

``Tag.suggested`` carries no ``edge_model`` on the core side (the precedent is
``Address.co_addressed``: ground truth does not describe a derived thing), so
the handle above supplies the edge class here.

Emits ``MATCH (t:Tag) MATCH (t)<-[r:SUGGESTED]-(m:Message) WITH r LIMIT $batch
DELETE r RETURN count(r) AS removed``.
"""

MERGE_COMMUNITIES = (
    unwind(param("rows"))
    .merge(Community, key=Community.id, alias="c")
    .set(
        Community.size,
        Community.message_count,
        Community.label,
        Community.method,
        Community.first_seen,
        Community.last_seen,
    )
)
"""Upsert circles. ``$rows``: ``id``, ``size``, ``message_count``, ``label``,
``method``, ``first_seen``, ``last_seen``.

Bind ``encode_rows(Community, rows)``; both dates are declared fields, so the
merge needs nothing from ``as_graph_datetime``.

Only ``id`` is in the pattern, for the reason :data:`MERGE_GROUPS` states: the
derived labels carry no unique constraint, so a changed ``size`` inside the
``MERGE`` would grow a second community beside the first and every
``MEMBER_OF`` written afterwards would hang off both.
"""

MERGE_MEMBER_OF = (
    unwind(param("rows"))
    .match(Address, key={Address.id: row("address_id")}, alias="a")
    .match(Community, key={Community.id: row("community_id")}, alias="c")
    .merge_edge("a", MemberOf, "c", alias="r")
    .set(MemberOf.rank, on="r")
)
"""Put addresses in their circle. ``$rows``: ``address_id``, ``community_id``,
``rank``.

``match()`` on both ends and never ``merge()``: a community that is not there
yet is a bug in the caller's ordering, and an address that is not there is a
partition over a graph that moved underneath it. Merging either would invent an
empty node — and one of them would be a *ground-truth* node carrying nothing
but a rank.

The rank on the edge is the archive-wide centrality
:data:`WRITE_ADDRESS_RANKS` has already written to the node, copied here so a
subgraph read can size a member without a second hop. That is why the stage
order puts ``CENTRALITY`` before ``COMMUNITIES``: an edge written before the
ranks exist carries a null and nothing recomputes it.
"""

MERGE_IN_CIRCLE = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("message_id")}, alias="m")
    .match(Community, key={Community.id: row("community_id")}, alias="c")
    .merge_edge("m", InCircle, "c", alias="r")
    .set(InCircle.score, InCircle.method, on="r")
)
"""Place messages in the circle they circulate in. ``$rows``: ``message_id``,
``community_id``, ``score``, ``method``.

``score`` is the share of *this message's* participants that are members of the
community, so it belongs on the edge and not on either node: the same community
holds a mail everybody on it is a member of and a mail with one member on it,
and the two are not the same statement.

A message joins at most one circle — the largest share wins, ties going to the
smaller community id — because "which circle is this mail in" has one answer or
none. Nothing in the statement enforces that; the caller does, the way it
enforces :data:`MERGE_CO_ADDRESSED`'s pair order.
"""

MERGE_SUGGESTED = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("message_id")}, alias="m")
    .match(Tag, key={Tag.id: row("tag_id")}, alias="t")
    .merge_edge("m", Suggested, "t", alias="r")
    .set(Suggested.score, Suggested.method, on="r")
)
"""Offer messages to a tag. ``$rows``: ``message_id``, ``tag_id``, ``score``,
``method``.

**The one statement in this package that touches the annotation layer, and it
only ever points at it.** ``Tag`` is matched, never merged — the catalogue test
puts it in the ground-truth set for exactly this — so a row naming a tag a
human deleted writes nothing at all, which is the right answer rather than a
resurrection.

The edge it writes is derived and disposable: :data:`DELETE_SUGGESTED` takes
every one of them at the start of the next rebuild, and ``TAGGED`` — the human
decision — is untouched by both. A user accepting a suggestion goes through
:meth:`~mailarc_core.archive.tags.TagRepository.tag_messages`, in the other
component, which is the only thing that may write a membership.
"""

_keyworded = alias(Topic, "t")

MERGE_TOPIC_KEYWORDS = (
    unwind(param("rows"))
    .match(Topic, key={Topic.id: row("topic_id")}, alias="t")
    .set({Topic.keywords: row("keywords")}, on=_keyworded)
    .returning(count("t").as_("written"))
)
"""Attach keywords to their topic. ``$rows``: ``topic_id``, ``keywords``.

A ``MATCH`` and a ``SET`` rather than a merge, although the name matches its
siblings: the topic was written by :data:`MERGE_TOPICS` one stage earlier and
the keywords are a second pass over the same node. Merging it here would create
an empty ``Topic`` for a keyword row whose cluster had gone — which can only
happen if the two stages disagree, and an empty node is a worse way to find
that out than a row that wrote nothing.

Separate from :data:`MERGE_TOPICS` because the two have different *inputs*.
Clustering needs no message text at all; the TF-IDF needs the text of twenty
members of every topic, which is a read the clustering must not have to wait
for. Splitting them is what keeps the expensive read inside its own stage,
where what it cost is a number in the job row.

``keywords`` is a list of strings and passes through ``encode_rows`` untouched:
it is a declared field on ``Topic`` with no converter, so the payload the
caller builds is the payload the store receives.
"""

_importance = alias(Message, "m")

WRITE_IMPORTANCE = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("id")}, alias="m")
    .set(
        {
            Message.importance: row("importance"),
            Message.importance_reasons: row("reasons"),
            Message.importance_version: param("version"),
        },
        on=_importance,
    )
    .returning(count("m").as_("written"))
)
"""Score messages in place. ``$rows``: ``id``, ``importance``, ``reasons``.

The second statement in this catalogue that writes a *ground-truth* node, and
it is :data:`~mailarc_analytics.queries.catalog.WRITE_EMBEDDINGS`' argument
word for word: three properties the import deliberately never writes (see
:mod:`mailarc_core.archive.writer`, which leaves an existing ``Message``
untouched precisely so a later phase can fill them in), set on a node that is
matched and never merged. A row naming a message that is not there writes
nothing; merging it would invent an empty ``Message`` carrying a score and no
mail, which no import can ever reconcile and every listing would happily
return.

``$version`` is a parameter of the *statement* rather than a key of each row,
because it is one value for the whole run — the same shape ``$model`` has on
the embedding write. A changed formula therefore leaves a visible mark on every
message it touched, and a message carrying an older version is one this
rebuild did not reach.

The reasons are not decoration and not optional. A score with no vocabulary
behind it is a ranking a user cannot argue with, and the whole reason this is
arithmetic over headers rather than a model is that every term can be named.
"""

CLEAR_IMPORTANCE = (
    select(_importance)
    .where(
        _importance.importance.is_not_null()
        | _importance.importance_version.is_not_null()
    )
    .set(
        {
            Message.importance: None,
            Message.importance_reasons: None,
            Message.importance_version: None,
        },
        on=_importance,
    )
    .returning(count("m").as_("cleared"))
)
"""Forget every score, because the next rebuild computes them all again.

:data:`~mailarc_analytics.queries.catalog.CLEAR_EMBEDDINGS`' shape, and needed
for a sharper reason than tidiness: a rebuild deletes and recomputes the
derived layer, and a message that dropped out of the archive's scoring — its
template gone, its replies purged with an account — would otherwise keep the
number the *previous* run gave it forever. Nulling first is what makes
``importance`` mean "this run's answer".

``None`` compiles to the Cypher literal ``NULL`` rather than to a parameter
bound to null, which runic states outright: ``SET n.p = $x`` with a null ``x``
does remove the property, but it reads as assigning a value and not every
backend treats the two the same.

Ground truth is untouched by it. These three properties are this phase's own,
declared on the node and left empty by the import — the same arrangement
``embedding`` has, and the delete guards in
:mod:`mailarc_analytics.derived.rebuild` are unaffected because nothing here is
a node or an edge.

The filter is what keeps it off the whole archive: a message that was never
scored matches neither condition, so a first rebuild clears nothing and a
later one touches only what it wrote. Takes **no parameters**, like its
embedding counterpart.
"""

_ranked = alias(Address, "a")

WRITE_ADDRESS_RANKS = (
    unwind(param("rows"))
    .match(Address, key={Address.id: row("id")}, alias="a")
    .set(
        {
            Address.rank: row("rank"),
            Address.rank_version: param("version"),
        },
        on=_ranked,
    )
    .returning(count("a").as_("written"))
)
"""Rank addresses in place. ``$rows``: ``id``, ``rank``.

:data:`WRITE_IMPORTANCE` on the other ground-truth node, and the same rules
apply: matched and never merged, versioned by a statement parameter, cleared by
its own statement rather than deleted.

The number is computed in **Python**, not by ``algo.pageRank``, and that is not
an implementation detail: ``CO_ADDRESSED`` is stored with the smaller id first,
so a PageRank over that arrow would rank an address by where its id sorts.
:mod:`mailarc_analytics.derived.centrality` runs a weighted, undirected power
iteration over the pair counts instead — deterministic, and about the graph
rather than about the alphabet.
"""

CLEAR_ADDRESS_RANKS = (
    select(_ranked)
    .where(_ranked.rank.is_not_null() | _ranked.rank_version.is_not_null())
    .set({Address.rank: None, Address.rank_version: None}, on=_ranked)
    .returning(count("a").as_("cleared"))
)
"""Forget every rank. :data:`CLEAR_IMPORTANCE`'s argument, on ``Address``.

An address that stopped being written to — every message it was on purged with
its account — would otherwise keep the centrality it had when it still was, and
a page sizing nodes by rank would go on showing it as a hub of an archive it is
no longer in.

Takes no parameters, and its filter keeps it off every address the rebuild has
not touched.
"""
