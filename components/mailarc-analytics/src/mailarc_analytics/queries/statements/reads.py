"""What a rebuild reads, and the two counts that frame what it read.

Six statements: the account's own addresses, the two halves of every message's
facts, the bodies A3 comes back for, and the pair of counts that make "what the
reader stepped over" and "what a capped rebuild could have seen" numbers rather
than absences. They are the first thing a rebuild runs and the last thing its
job row reports.

Three measured properties of runic 0.5's builder shape everything here, and
each is repeated at the statement it bites:

* A **bare field** in ``project()`` names its own column (``m.id`` → ``id``),
  but an aggregate or expression keys the row by its raw Cypher text unless it
  carries ``.as_()``. Every computed column below spells the old name out,
  because a consumer reading ``row["senders"]`` is the contract.
* ``.order_by().limit()`` written **before** a ``traverse()`` compiles to a
  ``WITH`` stage ahead of the ``OPTIONAL MATCH``. Written order is compiled
  order, which is what lets :data:`MESSAGE_RELATIONS` page before it expands.
* Consecutive ``traverse()`` calls **chain** — the second leaves from the
  first one's target, not from the root — and the result is an empty collected
  column, not an error. Every expansion after the first passes ``from_=``.

A projected field does not go through runic's converter: ``project()`` returns
values rather than entities, so ``sent_at`` arrives as the ISO-8601 string the
mapper wrote and ``simhash`` as the *signed* 64-bit integer the writer had to
store. :func:`~mailarc_analytics.queries.rows.as_datetime` and
:func:`~mailarc_core.archive.model.to_unsigned_64` are still what decode them.
"""

from runic.ogm import QueryBuilder, alias, collect, count, param, select, var

from mailarc_core.archive.model import (
    Account,
    Address,
    Attachment,
    Message,
    Thread,
)

_account = alias(Account, "a")

ACCOUNT_ADDRESSES: QueryBuilder[Account] = (
    select(_account)
    .where(_account.address.is_not_null())
    .distinct()
    .project(_account.address)
)
"""Every address this archive imports from — what "sent by me" means.

Read once at the start of a rebuild and compared, lowercased, against each
message's sender. A template is only worth automating if the user writes it.

``.distinct()`` sits on the statement rather than on the column: it compiles to
``RETURN DISTINCT``, which is where the old string had it, and not to a
``collect(DISTINCT …)`` over one address.
"""


_properties = alias(Message, "m")

MESSAGE_PROPERTIES: QueryBuilder[Message] = (
    select(_properties)
    .where(_properties.id.is_not_null() & (_properties.id > param("after")))
    .project(
        _properties.id,
        _properties.sent_at,
        _properties.subject_norm,
        _properties.participant_key,
        _properties.simhash,
        _properties.refs,
    )
    .order_by(_properties.id)
    .limit(param("limit"))
)
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

**A cursor and not an offset.** ``.skip(param("offset"))`` reads correctly and
costs quadratically: a graph store has no way to reach row twenty thousand
except by matching, expanding and sorting the twenty thousand before it, so
every page re-does the whole archive and reading it in pages costs
``O(n² / PAGE_SIZE)``. Measured on the vendored FalkorDB, sixteen times the
messages cost sixty-five times the time. Carrying the last id forward turns
each page into an index seek, and it is what makes the range index on
``Message.id`` pay. The builder offers ``skip()`` and it stays unused here on
purpose.

Every column is a bare field, so each names itself — ``m.sent_at`` returns as
``sent_at``, not as ``m.sent_at``. Nothing here needs an explicit ``.as_()``;
:data:`MESSAGE_RELATIONS`' collected columns do.

A projected field does **not** go through runic's converter, even though the
builder knows the field's type: ``sent_at`` comes back as the ISO-8601
*string* the mapper wrote, exactly as the raw statement returned it. Measured
on the vendored FalkorDB, old and new side by side — ``project()`` returns
values, not entities, so :func:`~mailarc_analytics.derived.reader._as_datetime`
is still the thing that parses it and still earns its docstring. (It already
accepts a ``datetime`` too, so it would have survived either answer.) For the
same reason ``simhash`` is still the *signed* 64-bit integer the writer had to
store — run it through
:func:`~mailarc_core.archive.model.to_unsigned_64` before banding, comparing or
rendering it.
"""


_relations = alias(Message, "m")
_sender = alias(Address, "s")
_recipient = alias(Address, "r")
_blind = alias(Address, "b")
_thread = alias(Thread, "t")
_attachment = alias(Attachment, "f")

MESSAGE_RELATIONS: QueryBuilder[Message] = (
    select(_relations)
    .where(_relations.id.is_not_null() & (_relations.id > param("after")))
    .order_by(_relations.id)
    .limit(param("limit"))
    .traverse(Message.sender, to=_sender, optional=True)
    .traverse(
        Message.recipients,
        types=["SENT_TO", "COPIED_TO"],
        from_=_relations,
        to=_recipient,
        optional=True,
    )
    .traverse(Message.blind_copied_to, from_=_relations, to=_blind, optional=True)
    .traverse(Message.thread, from_=_relations, to=_thread, optional=True)
    .traverse(Message.attachments, from_=_relations, to=_attachment, optional=True)
    .project(
        _relations.id,
        collect(_sender.id, distinct=True).as_("senders"),
        collect(_recipient.id, distinct=True).as_("addressed"),
        collect(_blind.id, distinct=True).as_("blind_copied"),
        collect(_thread.id, distinct=True).as_("threads"),
        collect(_attachment.id, distinct=True).as_("attachments"),
    )
    .order_by(var("id"))
)
"""The set half of the facts, joined to :data:`MESSAGE_PROPERTIES` by id.

The page is cut **before** the optional matches, not after. Five expansions
that cross-multiply — a message with fifty recipients and twenty attachments is
a thousand intermediate rows — are the expensive half of this statement, and a
``LIMIT`` at the end would pay for the whole archive's expansion to keep two
thousand messages. ``.order_by().limit()`` written *above* the traversals picks
the page off the index first and expands only that: written order is compiled
order in the builder, and this one compiles to ``WITH m ORDER BY m.id ASC
LIMIT $limit`` ahead of the first ``OPTIONAL MATCH``. Moving those two calls
below the traversals is not a formatting choice — it is the whole archive.

**Every expansion after the first passes ``from_=``, and that is load-bearing.**
Consecutive ``traverse()`` calls walk a *path*: written without ``from_``, the
recipients hop leaves from the sender node rather than from the message, the
Bcc hop leaves from that, and the collected columns come back **empty for every
row with no error at all**. Measured, not feared. The first traversal needs no
``from_`` because the message is where it already starts.

``optional=True`` is spelled out on all five. It defaults to ``False`` in runic
0.5 — it did not in 0.4.6 — and an inner match would drop every message that
has no attachment, no thread, or no Bcc, which is nearly all of them.

The alternation is one pattern and not two: ``types=["SENT_TO", "COPIED_TO"]``
compiles to ``(m)-[:SENT_TO|COPIED_TO]->(r:Address)``, and A1 is *defined* as
the walk over both. (The catalogue's module docstring lists this alternation as
one of the four things the builder could not express. That was true of 0.4.6
and is no longer true.)

Two statements rather than one because the grouping key of an aggregating
``RETURN`` is every non-aggregated item in it, and ``m.refs`` is a list — a
list as a grouping key is asking a graph store for trouble it has no reason to
give. Grouping on ``m.id`` alone is a string comparison and always safe.

Bcc comes back in its own column and is deliberately kept out of ``addressed``:
a Bcc recipient was written to *without* the others knowing, so a
``CO_ADDRESSED`` edge between them would materialise exactly the confidentiality
the header exists to protect. It belongs in the participant set all the same,
because ``participant_key`` was hashed over it. That separation is why
``blind_copied_to`` is its own traversal and is *not* folded into the
``types=`` alternation above.

The optional matches multiply rows before ``collect(DISTINCT …)`` folds them
back; a message with ten recipients and three attachments costs thirty rows,
which is the price of reading each message once instead of five times.

Each collected column carries an explicit ``.as_()``. A bare field names its
own column, but an aggregate without one is keyed by its raw Cypher text —
``collect(DISTINCT s.id)`` — and every consumer reads ``row["senders"]``. The
trailing ``order_by(var("id"))`` orders by that returned alias, the way the old
string's ``ORDER BY id`` did, not by ``m.id`` again.
"""


_unidentified = alias(Message, "m")

COUNT_UNIDENTIFIED: QueryBuilder[Message] = (
    select(_unidentified)
    .where(_unidentified.id.is_null() | (_unidentified.id == ""))
    .project(count(_unidentified).as_("total"))
)
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

That ``''`` is a **literal the statement fixed**, not caller input. The builder
binds it as ``$p0`` instead of inlining it, which is a hardening and not a
loosening: it never reaches the parser as text. It is auto-bound at
``build()`` time and deliberately absent from ``parameter_names()``, which
stays ``()`` — the security boundary counts what a caller may supply, and a
caller may supply nothing here.
"""


_messages = alias(Message, "m")

COUNT_MESSAGES: QueryBuilder[Message] = (
    select(_messages)
    .where(_messages.id.is_not_null() & (_messages.id != ""))
    .project(count(_messages).as_("total"))
)
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

``count(m)`` and not ``count(*)``: the column has to be ``total``, so the
aggregate carries ``.as_("total")`` — without it the row would be keyed by the
string ``count(m)``. As in :data:`COUNT_UNIDENTIFIED`, the ``''`` is an
auto-bound literal and ``parameter_names()`` is ``()``.
"""


_bodies = alias(Message, "m")

MESSAGE_BODIES: QueryBuilder[Message] = (
    select(_bodies)
    .where(_bodies.id.in_(param("ids")))
    .project(_bodies.id, _bodies.body_clean)
)
"""The cleaned bodies of named messages — A3's second read.

Only the members of an actual template need their text, for the sample and for
the word count the brevity factor uses. Reading a hundred thousand bodies to
keep a few hundred puts the archive's text next to an in-process FalkorDB for
no gain; ``$ids`` is what keeps a rebuild's memory bounded by its findings.

``.in_()`` is right here and wrong two statements away, which is worth being
explicit about. ``Message.id`` is a **scalar** property, so ``m.id IN $ids``
asks whether this message's id is one of the caller's — the intended question.
The banned use is ``.in_()`` on a *list* property: ``Message.refs.in_([…])``
compiles to ``m.refs IN $p0``, asks whether the whole list is an element of the
parameter, and returns nothing at all. That case wants
``Message.refs.any_of(param("token"))``.
"""
