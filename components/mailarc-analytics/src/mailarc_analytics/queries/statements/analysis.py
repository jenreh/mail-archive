"""What the reports ask — §6 and §12, with the model's real properties put back.

Nine statements: A1 read twice, once straight off the ground truth and once off
the edge that materialises it, the three listings a page renders, and the four
counts that turn "a second rebuild changes nothing" into a test rather than a
claim. Between them they are what
:class:`~mailarc_analytics.queries.reports.AnalyticsReader` runs.

The handles below are module-private and shared on purpose — ``a`` and ``b``
are the same two ``Address`` variables in all three ``CO_ADDRESSED`` statements,
and a reader comparing them should not have to check whether they are. An
:func:`~runic.ogm.alias` is a naming handle and holds no query state, so sharing
one across statements is safe; the aggregate expressions are not shared,
because each names its own output column.

**Read a statement, never extend one.** A builder mutates in place and returns
itself, so ``CATALOG["COUNT_GROUPS"].limit(5)`` does not derive a narrowed
query — it appends ``LIMIT 5`` to the catalogue entry for the rest of the
process, and every later caller gets the narrowed one. Measured: the constant's
own ``build()`` changed. Binding is safe and is the whole calling convention —
``session.all_rows(statement, params)`` leaves the statement untouched, which is
why one constant serves ``$direction`` "sent" and "received" in the same
session — but a statement out of this package is read-only to everything above
it. Anything that needs a different shape declares a different constant here.
"""

from runic.ogm import alias, count, param, select

from mailarc_analytics.derived.model import About, CoAddressed, Group, Template, Topic
from mailarc_core.archive.model import Address, Message

_message = alias(Message, "m")
_left = alias(Address, "a")
_right = alias(Address, "b")
_pair = alias(CoAddressed, "r")
_group = alias(Group, "g")
_template = alias(Template, "t")
_topic = alias(Topic, "t")
_about = alias(About, "r")

_messages_together = count(_message).as_("together")
"""``count(m) AS together`` — projected and then ordered by, so the ``ORDER BY``
names the result column the way the string did rather than repeating the
aggregate."""

CO_RECIPIENTS = (
    select(_message)
    .traverse(Message.recipients, types=["SENT_TO", "COPIED_TO"], to=_left)
    .traverse(
        Message.recipients, types=["SENT_TO", "COPIED_TO"], from_=_message, to=_right
    )
    .where(_message.id.is_not_null() & (_message.id != "") & (_left.id < _right.id))
    .project(_left.id.as_("left_id"), _right.id.as_("right_id"), _messages_together)
    .order_by(_messages_together, desc=True)
    .limit(param("limit"))
)
"""A1 straight off the ground truth, no derived edge involved — §6.1.

The self-join gets expensive somewhere around a hundred thousand messages,
which is what
:data:`~mailarc_analytics.queries.catalog.MERGE_CO_ADDRESSED` materialises it
for. It stays here
because it is the definition: if the edge and this query ever disagree, the
edge is wrong.

``a.id < b.id`` is what makes an unordered pair appear once. The sender is not
in the pattern on purpose — they are the one addressing, not one of the
addressed, and including them would make the heaviest edge in every archive
"the user, and everyone the user has ever mailed".

The canonical-id filter is
:data:`~mailarc_analytics.queries.catalog.MESSAGE_PROPERTIES`'s, and it is
here because
a cross-check is only worth anything if both sides count the same population. A
rebuild skips a ``Message`` with no id or an empty one — that is what
:data:`~mailarc_analytics.queries.catalog.COUNT_UNIDENTIFIED` counts — so the
edge can never represent it, and
counting it here made the truth side higher than the edge by construction.
Measured on a graph with one readable message and two id-less ones: the edge
said 1, this said 3, and the cross-check called it a disagreement whose stated
causes did not include the real one. No rebuild could ever have cleared it.

**Both hops leave from the message**, which is why the second one spells
``from_=_message`` out. The builder keeps a cursor and advances it to each
traversal's target, so two traversals written one after the other walk a
*chain*: without ``from_`` the second pattern reads
``(a)-[:SENT_TO|COPIED_TO]->(b)`` — which addresses address each other — and
the statement returns nothing at all. Measured, and silent, because an empty
self-join is also what an archive with no co-addressing looks like.

The pattern is two ``MATCH`` clauses off ``m`` where the string was one path
``(a)<-[…]-(m)-[…]->(b)``, and that is the only shape change here. It is safe
for the reason the single path was safe: **the statement references neither
relationship**, so the backend collapses the duplicate ``(a, m, b)`` binding
that a message which both ``SENT_TO`` and ``COPIED_TO`` the same address would
otherwise produce. Measured on exactly that graph — one message, ``SENT_TO``
a, ``SENT_TO`` b, ``COPIED_TO`` b — with the pair filter applied:

===================================== =========
shape                                 ``count``
===================================== =========
one path, relationships referenced            2
one path, no relationship referenced          1
two clauses, relationships referenced         2
two clauses, no relationship referenced       1
===================================== =========

Referenced, not merely named: a pattern that binds ``r1``/``r2`` and never
returns them collapses too. The builder emits no relationship variable here at
all, which is one step further from the hazard than the string was. On the
two-clause shape ``count(m)``, ``count(*)`` and ``count(DISTINCT m)`` all
answer 1, which is the equivalence ``CoAddressedAgreement``'s docstring claims
— and claims *for this statement* rather than in general, because referencing
either edge is all it takes to break it.
"""

_stored_together = _pair.count.as_("together")
"""``r.count AS together`` — the edge's own count, named so the ``ORDER BY``
reads the result column."""

TOP_CO_ADDRESSED = (
    select(_left)
    .traverse(_left.co_addressed, to=_right, edge=_pair, direction="BOTH")
    .where((_left.id < _right.id) & _pair.count.is_not_null())
    .project(
        _left.id.as_("left_id"),
        _right.id.as_("right_id"),
        _stored_together,
        _pair.first_seen.as_("first_seen"),
        _pair.last_seen.as_("last_seen"),
    )
    .order_by(_stored_together, desc=True)
    .limit(param("limit"))
)
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

``direction="BOTH"`` is how the missing arrow is spelled now.
``Address.co_addressed`` is declared ``OUTGOING`` — the only direction
FalkorDB will ``MERGE`` — and this override turns *this pattern* back into the
undirected one without changing anything that is stored. Left off, the builder
compiles ``(a)-[r:CO_ADDRESSED]->(b)``, which happens to return the same rows
today only because the writer orders every pair smaller id first; reading
without the arrow is what keeps this listing from depending on that. It is
also what makes ``a.id < b.id`` load-bearing rather than decorative: an
undirected pattern matches every stored edge from both ends, so dropping the
filter lists every pair twice.
"""

_group_messages = _group.message_count.as_("message_count")
"""The group's own count, projected under the name the readers use and reused
in the ``ORDER BY`` so the clause names the column and not ``g.message_count``.
Identical rows either way; the alias is what the string said."""

RECURRING_GROUPS = (
    select(_group)
    .where(
        (_group.size >= param("min_size"))
        & (_group.message_count >= param("min_messages"))
    )
    .project(
        _group.id,
        _group.size,
        _group_messages,
        _group.first_seen,
        _group.last_seen,
    )
    .order_by(_group_messages, desc=True)
    .limit(param("limit"))
)
"""Which *groups* write repeatedly, rather than which pairs — §6.1.

The spec's version walks in from ``(m:Message)-[:ADDRESSED_GROUP]->(g)`` and
then returns the group's properties, which yields one identical row per
message. The count is already on the node; the message is not needed to read
it.

The other four columns are bare fields, and the builder names each one after
its property — ``g.id AS id``, ``g.size AS size`` — so every ``row["id"]`` a
reader already writes keeps working. Only a computed column would need an
explicit ``.as_()``; there is none here, and ``message_count`` carries one
only so the ``ORDER BY`` can name it.
"""

_template_score = _template.automation_score.as_("automation_score")
"""Named for the same reason :data:`_group_messages` is: the ``ORDER BY``
names the result column."""

TOP_TEMPLATES = (
    select(_template)
    .where(_template.direction == param("direction"))
    .project(
        _template.id,
        _template.occurrences,
        _template_score,
        _template.sample_text,
        _template.first_seen,
        _template.last_seen,
    )
    .order_by(_template_score, desc=True)
    .limit(param("limit"))
)
"""What is worth automating, best first — §12, with the direction put back.

§6.3 requires sent and received to be reported separately, so a listing that
mixes them is not the one the spec asks for: the scores are only comparable
within one direction, and only the sent ones are anybody's to automate.

``$direction`` is compared against the property exactly as it was stored — a
plain string. ``Template.direction`` is declared as
:class:`~mailarc_analytics.derived.model.TemplateDirection`, but the field's
converter runs on the way into a *node property*, not over a bound parameter:
``bind()`` hands the driver whatever the caller passed, measured, so the reader
goes on binding ``direction.value``. An enum member happens to match anyway,
because ``TemplateDirection`` is a ``StrEnum`` and the member *is* the string —
which is exactly why binding the member is not proof of anything and the
``.value`` stays. ``"SENT"`` matches nothing at all.
"""

_topic_messages = count(_message).as_("messages")
"""``count(m) AS messages`` — one topic's size within one method."""

TOPIC_BREAKDOWN = (
    select(_topic)
    .traverse(_topic.messages, to=_message, edge=_about)
    .project(
        _topic.id,
        _topic.label,
        _about.method.as_("method"),
        _topic_messages,
    )
    .order_by(_topic_messages, desc=True)
    .limit(param("limit"))
)
"""Topics by size, split by the signal that drew each edge — §12.

``method`` in the grouping key rather than off the node, because that is the
column a reader has to look at before believing the row: the same topic can
hold messages joined by a ticket token and messages joined by nothing stronger
than a shared attachment.

Walked from the ``Topic`` rather than from the ``Message``, which is the same
pattern read from the other end. ``ABOUT`` is declared on
:class:`~mailarc_analytics.derived.model.Topic` as an ``INCOMING`` relation —
ground truth does not describe derived things — so the builder emits
``(t)<-[r:ABOUT]-(m:Message)`` for it, and there is no relation on ``Message``
to walk in the other direction. The match is not optional, so a ``Topic``
nothing points at is still absent from the listing, exactly as it was when the
pattern started at the message.
"""

COUNT_GROUPS = select(_group).project(count(_group).as_("total"))
"""How many ``Group`` nodes there are."""

COUNT_TOPICS = select(_topic).project(count(_topic).as_("total"))
"""How many ``Topic`` nodes there are."""

COUNT_TEMPLATES = select(_template).project(count(_template).as_("total"))
"""How many ``Template`` nodes there are."""

COUNT_CO_ADDRESSED = (
    select(_left)
    .traverse(_left.co_addressed, to=_right, edge=_pair)
    .project(count(_pair).as_("total"))
)
"""How many ``CO_ADDRESSED`` edges there are.

Directed for the same reason
:data:`~mailarc_analytics.queries.catalog.DELETE_CO_ADDRESSED` is: both ends
are
addresses, so an arrow costs no matches and saves counting each edge twice.
These four counts are what makes "a second rebuild changes nothing" a test
rather than a claim.

Which is why this is the one ``CO_ADDRESSED`` read that does *not* pass
``direction="BOTH"``: here the relation's declared ``OUTGOING`` is the arrow,
and adding the override would report twice as many edges as the graph holds —
measured, 6 against a graph carrying 3.
"""
