"""What one corner of the graph looks like — the explorer's eighteen reads.

Every other family in this package answers a *question*: how many, which are
the heaviest, what should be automated. These answer a **picture**: the nodes
and edges around one thing a user clicked, in a shape a canvas can draw.

Three rules run through all of them, and each is here because the alternative
was tried:

* **Small, and composed by the reader.** :class:`~mailarc_analytics.queries.graphs.GraphReader`
  runs several of these per view and folds the rows together. One statement
  carrying a topic's messages *and* their addresses *and* their tags would
  cross-multiply exactly the way
  :data:`~mailarc_analytics.queries.statements.reads.MESSAGE_RELATIONS`'s five
  optional expansions do — a topic of forty messages with five people on each
  is two hundred rows to draw forty nodes — and every column would then have to
  be de-duplicated in Python anyway.
* **Ids, never entities.** A projected node comes back as a
  ``falkordb.node.Node`` that no value object can read, so every statement
  projects ``n.id`` and the properties a canvas paints with. The rule is the
  procedure statements' and it holds for the same reason one file over.
* **The seed's own columns ride along.** ``topic_label``, ``thread_subject``,
  ``message_subject``, ``address_domain`` and their siblings repeat in every
  row of the read that carries them. They are how the node a user *asked
  about* gets a label without a second round trip for one string, and they are
  also what tells "this topic has no members" from "this topic is gone": no
  rows at all means no node, which is the answer R7 wants for a stale link.

Two shapes here are the builder being taken at its word rather than fought:

**A traversal source carries no label.** ``(o)-[:IN_THREAD]->(t:Thread)`` in
:data:`THREAD_SIBLINGS` and ``(t)<-[:ABOUT]-(m:Message)`` in
:data:`MESSAGE_TOPICS` name the far end without a label, because the builder
labels a pattern's *target* and leaves a source bare. It is exact all the same
— only a ``Message`` has an outgoing ``IN_THREAD`` and only a ``Topic`` has an
incoming ``ABOUT`` — and the alternative is worse: reversing a traversal with
``direction="INCOMING"`` labels the new target from the relation's *declared*
target class, so ``(t)<-[:IN_THREAD]-(o:Thread)`` is what asking for the
siblings the other way round actually compiles to. Measured, and silently
wrong rather than empty.

**One filter sits on a traversed variable.** :data:`ADDRESS_MESSAGES` is rooted
at the ``Message`` and filtered on the ``Address`` it walks to, which is the
opposite of every other read here and is not a preference: ``Address`` declares
no relation back to ``Message`` — ground truth does not describe a message from
an address — so the only builder shape that keeps ``(m:Message)`` labelled
starts there. runic emits such a predicate after the whole pipeline, so it is
the store's planner that decides to seek ``a.id`` first; the property is
indexed, which is what makes that available to it.

:data:`REPLY_CHAIN` is the one raw statement in this module and the third in the
package. runic *can* write a variable-length pattern — ``hops=(1, 3)`` compiles
to ``*1..3`` — but it cannot bind an ``edge`` variable on one, by design: a
variable-length pattern binds a *list* of relationships that the mapper does
not decode. A set of reachable messages with no edges between them is not a
subgraph, so this one is written out.
"""

from runic.ogm import QueryBuilder, alias, count, fn, param, select, var

from mailarc_analytics.derived.model import (
    About,
    CoAddressed,
    Community,
    InCircle,
    MemberOf,
    Topic,
)
from mailarc_core.archive.model import Address, Message, Tag, Tagged, Thread

_ON_A_MESSAGE = ["SENT_FROM", "SENT_TO", "COPIED_TO"]
"""The three header edges an explorer draws, and the one it does not.

``BLIND_COPIED_TO`` is left out for the reason
:data:`~mailarc_analytics.queries.statements.reads.MESSAGE_RELATIONS` keeps it
in a column of its own and A1 never pairs on it: a Bcc recipient was written to
*without* the other recipients knowing, and a canvas that drew them on one
message would put that fact on screen beside the people it was kept from. The
address is still reachable — it is on its own mail — but nothing here draws the
line that discloses it.
"""


_addressed = alias(Message, "m")
_participant = alias(Address, "a")

MESSAGE_ADDRESSES: QueryBuilder[Message] = (
    select(_addressed)
    .where(_addressed.id == param("id"))
    .traverse(
        Message.recipients,
        types=_ON_A_MESSAGE,
        from_=_addressed,
        to=_participant,
        edge="r",
    )
    .project(
        _addressed.subject.as_("message_subject"),
        _addressed.importance.as_("message_importance"),
        _participant.id,
        _participant.domain,
        _participant.rank,
        fn("type", var("r")).as_("kind"),
    )
)
"""Who is on one message, and in which line — the ego view's anchor read.

``type(r)`` is the column that makes this one read instead of three: the sender,
the To line and the Cc line are one pattern with an alternation, and the edge's
own type is what tells the canvas which arrow to draw. Without it the statement
would have to be run once per relationship type, and a message with one sender
and forty recipients would be three round trips to learn what one already knows.

The two ``message_*`` columns are the seed's own, repeated per row. Every
message in the archive has a sender, so this is the read that always answers
for a message that exists — which makes it the right one to hang the anchor's
label on.
"""


_seed = alias(Message, "m")
_thread = alias(Thread, "t")
_sibling = alias(Message, "o")

THREAD_SIBLINGS: QueryBuilder[Message] = (
    select(_seed)
    .where(_seed.id == param("id"))
    .traverse(Message.thread, from_=_seed, to=_thread)
    .traverse(Message.thread, from_=_sibling, to=_thread)
    .project(
        _thread.id.as_("thread_id"),
        _thread.subject.as_("thread_subject"),
        _sibling.id.as_("id"),
        _sibling.subject.as_("subject"),
        _sibling.sent_at.as_("sent_at"),
        _sibling.importance.as_("importance"),
    )
    .order_by(var("id"))
    .limit(param("limit"))
)
"""The rest of the provider's conversation, plus the seed itself.

The seed comes back among the siblings and is not filtered out, which is
deliberate: the thread node is drawn as a hub with an ``IN_THREAD`` edge per
member, and the seed's own edge to it is one of them. Filtering it would draw a
message sitting beside its own conversation.

A provider's thread groups the copies **one** account holds, so the same
exchange imported from two mailboxes is two threads and this read finds one of
them. :data:`REPLY_CHAIN` is the half that crosses accounts.
"""


REPLY_CHAIN = """\
MATCH path = (m:Message)-[:REPLIES_TO*1..3]-(other:Message)
WHERE m.id = $id
RETURN [one IN nodes(path) | one.id] AS ids,
       [one IN nodes(path) | one.subject] AS subjects,
       [one IN nodes(path) | one.importance] AS importances,
       [one IN relationships(path) | startNode(one).id] AS sources,
       [one IN relationships(path) | endNode(one).id] AS targets
LIMIT $limit
"""
"""Who answered whom around one message, out to three hops in both directions.

Raw Cypher, and the third statement in this package that is. The reason is not
the ``*1..3`` — runic writes that as ``hops=(1, 3)`` — it is that a
variable-length pattern binds a **list** of relationships, which the builder
refuses to name (``TypeError: an edge variable on a variable-length pattern
binds a list of relationships``) because its mapper cannot decode one. What a
builder version can hand back is therefore the reachable messages and nothing
about how they are joined, and a set of nodes with no edges between them is not
a subgraph.

**Both directions on purpose.** ``(reply)-[:REPLIES_TO]->(parent)`` is the one
genuinely directed edge in this graph, and a conversation is what you get by
ignoring the arrow: the answers to a message matter as much as what it answered.
The arrows are not lost — ``startNode``/``endNode`` project each step's real
ends, so the canvas draws the direction the header stated while the walk itself
is undirected.

**The bound is fixed and the depth is not a parameter.** Cypher takes a
variable-length quantifier as syntax rather than as a value, so ``*1..$depth``
does not parse; three is what the statement walks, and
:meth:`~mailarc_analytics.queries.graphs.GraphReader.message` cuts the returned
edges to the depth a user asked for. That is the same division of labour as
everywhere else here: the store answers with what it holds, the reader decides
what to draw.

``$limit`` is a path ceiling and not a node one. A message in a busy thread is
on many paths, and five columns of lists per path is what an unbounded walk
would multiply.

Every column is a list of scalars in path order, ``nodes(path)`` starting at the
seed. Five of them rather than two, because ``ids`` alone would leave every
message in the chain labelled by its id.
"""


_filed = alias(Message, "m")
_topic = alias(Topic, "t")
_about = alias(About, "r")

MESSAGE_TOPICS: QueryBuilder[Message] = (
    select(_filed)
    .where(_filed.id == param("id"))
    .traverse(Topic.messages, from_=_topic, to=_filed, edge=_about)
    .project(
        _topic.id,
        _topic.label,
        _topic.message_count,
        _about.method.as_("method"),
    )
)
"""Which pieces of work one message belongs to, and by which signal.

``method`` travels with the row for :data:`~mailarc_analytics.queries.statements.analysis.TOPIC_BREAKDOWN`'s
reason: a ticket token and a shared attachment are not the same claim, and the
edge is where the difference is recorded. It becomes the edge's label on the
canvas.

Rooted at the message rather than at the topic, so the one seek is on the
indexed primary key. The cost is the bare ``(t)`` the builder emits for a
traversal source; only a ``Topic`` is on the other end of an ``ABOUT`` edge, so
the pattern is exact.
"""


_tagged_message = alias(Message, "m")
_tag = alias(Tag, "g")

MESSAGE_TAGS: QueryBuilder[Message] = (
    select(_tagged_message)
    .where(_tagged_message.id == param("id"))
    .traverse(Message.tags, from_=_tagged_message, to=_tag)
    .project(_tag.id, _tag.name, _tag.color)
)
"""What a human has filed one message under — the annotation layer, read.

Read and never written: ``TAGGED`` records a decision a person made, and this
package's whole boundary is that it may match ``Tag`` and never merge one.

``color`` comes back because a tag's colour is a property of the tag rather
than of the page showing it, and a chip on a canvas that disagreed with the
chip on the insights card would be two answers to one question.
"""


_circling = alias(Message, "m")
_community = alias(Community, "c")
_in_circle = alias(InCircle, "r")

MESSAGE_CIRCLE: QueryBuilder[Message] = (
    select(_circling)
    .where(_circling.id == param("id"))
    .traverse(Community.messages, from_=_community, to=_circling, edge=_in_circle)
    .project(
        _community.id,
        _community.label,
        _community.size,
        _community.message_count,
        _in_circle.score.as_("score"),
    )
)
"""Which circle of correspondents one message circulated in.

``score`` is the share of the message's participants that are in the circle,
which is what the rebuild wrote the edge with — so a canvas can draw a mail
that is squarely inside a circle differently from one that merely brushes it.
"""


_member_topic = alias(Topic, "t")
_member = alias(Message, "m")
_member_about = alias(About, "r")

TOPIC_MEMBERS: QueryBuilder[Topic] = (
    select(_member_topic)
    .where(_member_topic.id == param("topic"))
    .traverse(Topic.messages, from_=_member_topic, to=_member, edge=_member_about)
    .project(
        _member_topic.label.as_("topic_label"),
        _member_topic.message_count.as_("topic_messages"),
        _member.id,
        _member.subject,
        _member.sent_at,
        _member.importance,
        _member_about.method.as_("method"),
    )
    .order_by(_member.importance, desc=True)
    .order_by(_member.id)
    .limit(param("limit"))
)
"""The mail one topic is made of, the most important first.

Two orderings and both are load-bearing. ``importance`` first is what makes a
cut listing keep the messages a user would look at; ``id`` second is what makes
the cut *stable*, because an archive that has never been analysed has a null
importance on every message and would otherwise be cut wherever the store
happened to walk. A null sorts first on this backend's ``DESC``, so the
unanalysed case is exactly "in id order" rather than "in no order".
"""


_topic_of = alias(Topic, "t")
_topic_message = alias(Message, "m")
_topic_participant = alias(Address, "a")
_participation = count(_topic_message, distinct=True).as_("messages")

TOPIC_PARTICIPANTS: QueryBuilder[Topic] = (
    select(_topic_of)
    .where(_topic_of.id == param("topic"))
    .traverse(Topic.messages, from_=_topic_of, to=_topic_message)
    .traverse(
        Message.recipients,
        types=_ON_A_MESSAGE,
        from_=_topic_message,
        to=_topic_participant,
    )
    .project(
        _topic_participant.id,
        _topic_participant.domain,
        _topic_participant.rank,
        _participation,
    )
    .order_by(_participation, desc=True)
    .order_by(var("id"))
    .limit(param("limit"))
)
"""Who is on a topic's mail, and on how much of it — one row per person.

An aggregate rather than the join it is computed from, which is the whole
argument for this module's shape. The rows a canvas would need to draw every
message-to-address edge inside a topic are ``members × people``; what a reader
looks at is "these five worked on this", and one weighted line per person says
that in as many rows as there are people.

``count(DISTINCT m)`` because the alternation matches a message twice where the
same address is on both the To and the Cc line.
"""


_neighbour_of = alias(Address, "a")
_neighbour = alias(Address, "b")
_pair = alias(CoAddressed, "r")
_together = _pair.count.as_("together")

ADDRESS_NEIGHBOURS: QueryBuilder[Address] = (
    select(_neighbour_of)
    .where(_neighbour_of.id == param("address"))
    .traverse(_neighbour_of.co_addressed, to=_neighbour, edge=_pair, direction="BOTH")
    .project(
        _neighbour.id,
        _neighbour.domain,
        _neighbour.rank,
        _together,
    )
    .order_by(_together, desc=True)
    .order_by(var("id"))
    .limit(param("limit"))
)
"""Who this address is written to together with, heaviest pair first.

``direction="BOTH"`` for the reason
:data:`~mailarc_analytics.queries.statements.analysis.TOP_CO_ADDRESSED` passes
it: the edge is stored smaller-id-first because that is the only way one pair
is one edge, and a read that kept the arrow would list only the half of an
address's neighbours whose ids sort after it. There is no ``a.id < b.id`` filter
here and there must not be — this read is rooted at one address, so the
undirected pattern matches each of its edges once.
"""


_writer = alias(Message, "m")
_written_to = alias(Address, "a")

ADDRESS_MESSAGES: QueryBuilder[Message] = (
    select(_writer)
    .traverse(
        Message.recipients,
        types=_ON_A_MESSAGE,
        from_=_writer,
        to=_written_to,
        edge="r",
    )
    .where(_written_to.id == param("address"))
    .project(
        _written_to.domain.as_("address_domain"),
        _written_to.rank.as_("address_rank"),
        _writer.id,
        _writer.subject,
        _writer.sent_at,
        _writer.importance,
        fn("type", var("r")).as_("kind"),
    )
    .order_by(_writer.importance, desc=True)
    .order_by(_writer.id)
    .limit(param("limit"))
)
"""One correspondent's mail, most important first, with the line they were on.

The one read in this module rooted at something other than the node it is
about, and the module docstring says why: ``Address`` declares no relation back
to ``Message``, and reversing the message's own relation would label the far
end ``:Address``. So the predicate lands on the traversed variable, after the
pipeline, and the planner is left to seek the indexed ``Address.id`` first.

``kind`` is :data:`MESSAGE_ADDRESSES`' column read from the other end, and it
means the same thing: which line of the header this address was on.
"""


_wearing = alias(Tag, "g")
_worn_by = alias(Message, "m")
_membership = alias(Tagged, "r")

TAG_MEMBERS: QueryBuilder[Tag] = (
    select(_wearing)
    .where(_wearing.id == param("tag"))
    .traverse(Tag.messages, from_=_wearing, to=_worn_by, edge=_membership)
    .project(
        _wearing.name.as_("tag_name"),
        _worn_by.id,
        _worn_by.subject,
        _worn_by.sent_at,
        _worn_by.importance,
        _membership.source.as_("source"),
    )
    .order_by(_worn_by.importance, desc=True)
    .order_by(_worn_by.id)
    .limit(param("limit"))
)
"""What one tag holds — the durable reference, unlike a topic id (R7).

Rooted at the tag, which is what :attr:`~mailarc_core.archive.model.Tag.messages`
was declared for: runic emits a predicate naming a traversed variable after the
whole pipeline, so a walk from the message end would filter behind the traversal
it was meant to narrow.

``source`` says how the membership came about — by hand, by accepting a
suggestion, or automatically — and becomes the edge's label. A user looking at
a tag on a canvas should be able to see which of its messages they chose and
which a threshold did.
"""


_circle = alias(Community, "c")
_circle_member = alias(Address, "a")
_membership_of = alias(MemberOf, "r")
_member_rank = _membership_of.rank.as_("rank")

COMMUNITY_MEMBERS: QueryBuilder[Community] = (
    select(_circle)
    .where(_circle.id == param("community"))
    .traverse(Community.members, from_=_circle, to=_circle_member, edge=_membership_of)
    .project(
        _circle.label.as_("community_label"),
        _circle_member.id,
        _circle_member.domain,
        _member_rank,
    )
    .order_by(_member_rank, desc=True)
    .order_by(var("id"))
    .limit(param("limit"))
)
"""The people in one circle, the most central first.

The rank is read off the ``MEMBER_OF`` edge rather than off the address,
although the rebuild writes the same number to both. The edge is what the
circle was written with, so a circle read back after a partial rebuild is
consistent with itself.
"""


_circle_of = alias(Community, "c")
_circulated = alias(Message, "m")
_circulation = alias(InCircle, "r")
_share = _circulation.score.as_("score")

COMMUNITY_MESSAGES: QueryBuilder[Community] = (
    select(_circle_of)
    .where(_circle_of.id == param("community"))
    .traverse(Community.messages, from_=_circle_of, to=_circulated, edge=_circulation)
    .project(
        _circulated.id,
        _circulated.subject,
        _circulated.sent_at,
        _circulated.importance,
        _share,
    )
    .order_by(_share, desc=True)
    .order_by(_circulated.id)
    .limit(param("limit"))
)
"""The mail that circulates inside one circle, most of it inside first.

Ordered by the edge's share rather than by the message's importance, which is
the one listing here that is: a circle is a claim about who a mail was between,
so the mail that is *most* a circle's own is what a cut listing should keep.
"""


_overview_topic = alias(Topic, "t")

OVERVIEW_TOPICS: QueryBuilder[Topic] = (
    select(_overview_topic)
    .where(_overview_topic.message_count.is_not_null())
    .project(
        _overview_topic.id,
        _overview_topic.label,
        _overview_topic.message_count,
    )
    .order_by(_overview_topic.message_count, desc=True)
    .order_by(_overview_topic.id)
    .limit(param("limit"))
)
"""The biggest pieces of work, for the map of the whole archive.

``message_count IS NOT NULL`` guards the sort rather than the arithmetic, the
way :data:`~mailarc_analytics.queries.statements.analysis.TOP_CO_ADDRESSED`'s
count filter does: a null sorts *first* on this backend's ``DESC``, so a topic
written by an interrupted rebuild would take the top of the map.
"""


_overview_circle = alias(Community, "c")

OVERVIEW_COMMUNITIES: QueryBuilder[Community] = (
    select(_overview_circle)
    .where(_overview_circle.message_count.is_not_null())
    .project(
        _overview_circle.id,
        _overview_circle.label,
        _overview_circle.size,
        _overview_circle.message_count,
    )
    .order_by(_overview_circle.message_count, desc=True)
    .order_by(_overview_circle.id)
    .limit(param("limit"))
)
"""The busiest circles, ordered the way the insights listing orders them.

By the mail that circulates rather than by how many people are in it: a circle
of forty who exchanged three mails is a directory, and a circle of five who
exchanged four hundred is where the work is.
"""


_overview_tag = alias(Tag, "g")
_counted_message = alias(Message, "m")
_tagged_count = count(_counted_message, distinct=True).as_("messages")

OVERVIEW_TAGS: QueryBuilder[Tag] = (
    select(_overview_tag)
    .traverse(Tag.messages, from_=_overview_tag, to=_counted_message, optional=True)
    .project(_overview_tag.id, _overview_tag.name, _tagged_count)
    .order_by(_tagged_count, desc=True)
    .order_by(var("id"))
    .limit(param("limit"))
)
"""Every tag and how much it holds — the durable half of the map.

The traversal is optional, so a tag nobody has filed anything under yet comes
back with a zero rather than being absent. That is the same distinction
:data:`~mailarc_analytics.queries.statements.analysis.SUGGESTION_COUNTS` makes
and it matters more here: a tag missing from a map reads as a tag that does not
exist.
"""


_joined_topic = alias(Topic, "t")
_joined_circle = alias(Community, "c")
_shared_message = alias(Message, "m")
_shared = count(_shared_message, distinct=True).as_("messages")

OVERVIEW_TOPIC_CIRCLE: QueryBuilder[Topic] = (
    select(_joined_topic)
    .traverse(Topic.messages, from_=_joined_topic, to=_shared_message)
    .traverse(Community.messages, from_=_joined_circle, to=_shared_message)
    .project(
        _joined_topic.id.as_("topic_id"),
        _joined_circle.id.as_("community_id"),
        _shared,
    )
    .order_by(_shared, desc=True)
    .order_by(var("topic_id"))
    .limit(param("limit"))
)
"""Where a piece of work and a circle of people meet, by how much they share.

The map's only edge between two derived collections, and it is an aggregate
this reader coins rather than anything the graph holds: ``(t)<-[:ABOUT]-(m)-
[:IN_CIRCLE]->(c)`` is a path through the mail, and what a map wants is the
one number at the end of it. Drawing the messages instead would put the whole
archive on the canvas to say that two circles overlap.

Both traversals leave from the *message*, which is why the second passes
``from_``: consecutive traversals chain, and a chained one would ask which
communities a topic is in — a pattern that matches nothing at all, silently.
"""


_shared_tag = alias(Tag, "g")
_shared_topic = alias(Topic, "t")
_tagged_shared = alias(Message, "m")
_overlap = count(_tagged_shared, distinct=True).as_("messages")

OVERVIEW_TAG_TOPIC: QueryBuilder[Tag] = (
    select(_shared_tag)
    .traverse(Tag.messages, from_=_shared_tag, to=_tagged_shared)
    .traverse(Topic.messages, from_=_shared_topic, to=_tagged_shared)
    .project(
        _shared_tag.id.as_("tag_id"),
        _shared_topic.id.as_("topic_id"),
        _overlap,
    )
    .order_by(_overlap, desc=True)
    .order_by(var("tag_id"))
    .limit(param("limit"))
)
"""Which topics a tag's mail also belongs to — the annotation layer on the map.

The one edge in the overview that crosses the line between what a person
decided and what an analysis found, which is exactly what makes it worth
drawing: a tag that lines up with a topic is a piece of work the archive
already knows about, and a tag scattered across ten of them is a label rather
than a project.
"""
