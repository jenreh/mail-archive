"""What a rebuild reads, the two counts that frame what it read, and when.

Eleven statements. Six of them are the original rebuild's: the account's own
addresses, the two halves of every message's facts, the bodies A3 comes back
for, and the pair of counts that make "what the reader stepped over" and "what
a capped rebuild could have seen" numbers rather than absences — the first
thing a rebuild runs and the last thing its job row reports.
:data:`ARCHIVED_PER_DAY` answers a page instead: it reads the same two things
the others do and buckets them by the day the copy was archived on.

The four at the end are §5.3's, and each is a *separate* read on purpose.
:data:`MESSAGE_REPLIES` and :data:`MESSAGE_SIGNALS` could both have been more
columns on :data:`MESSAGE_RELATIONS`, and that is exactly what must not happen:
that statement already carries five optional expansions which cross-multiply
per message, and two more would multiply the reply chain and the label set into
the same rows. :data:`MESSAGE_TEXTS` is A2's late read for the same reason
:data:`MESSAGE_BODIES` is A3's — the text is only wanted for the members of an
actual finding. :data:`TAGGED_MEMBERSHIP` reads the annotation layer, which no
other statement in this package touches at all.

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

from runic.ogm import (
    QueryBuilder,
    alias,
    collect,
    count,
    left,
    param,
    select,
    sum_,
    var,
)

from mailarc_core.archive.model import (
    Account,
    Address,
    ArchivedFrom,
    Attachment,
    Label,
    Message,
    Tag,
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


_archived = alias(Message, "m")
_provenance = alias(ArchivedFrom, "r")

ARCHIVED_PER_DAY: QueryBuilder[Message] = (
    select(_archived)
    .traverse(Message.archived_from, from_=_archived, edge=_provenance)
    .where(_provenance.archived_at.is_not_null())
    .project(
        left(_provenance.archived_at, 10).as_("day"),
        count(_archived).as_("messages"),
        sum_(_archived.size_bytes).as_("bytes"),
    )
    .order_by(var("day"), desc=True)
    .limit(param("limit"))
)
"""When the archive grew, and by how much — one row per day, newest first.

The odd one out in this module: every other statement here is something a
rebuild runs, and this one answers a page. It sits with them because it reads
the same two things they do — ``Message`` and the provenance edge — and because
the counts it is the sibling of are here.

**``left()`` and not a bucketing loop, and that was measured rather than
assumed.** ``ArchivedFrom.archived_at`` is a ``datetime`` field, so a
``left()`` over it is only a day key if runic's mapper stored the value as an
ISO-8601 string and FalkorDB's ``left`` cuts characters off that string —
neither of which ``build()`` can show, because a compiled statement shows the
call and not what the store makes of the property. Run against the vendored
FalkorDB with five copies archived at named instants, it answers
``{'day': '2026-03-01', 'messages': 2, 'bytes': 3139.0}``: midnight and one
second to midnight fold into one bucket, and the key is exactly the first ten
characters of ``2026-03-01T08:00:00+00:00``. So the fallback the spec
authorised — projecting the raw stamp under the ``MAX_ROWS`` ceiling and
bucketing in Python — is not needed, and
``tests/queries/test_queries_archived_per_day_local.py`` is what would say so
if a FalkorDB upgrade changed its mind.

**The day key carries the offset the stamp was written with, which is why the
reader can call it UTC.** :class:`~mailarc_core.archive.writer.MessageArchiver`
stamps ``datetime.now(UTC)`` whenever ``ArchiveSource.archived_at`` is unset,
and nothing in this repository sets it — a sync run always archives in the
present. Every stored stamp is therefore UTC, and cutting ten characters off
it yields the UTC calendar day. A caller that one day hands the writer a stamp
in another zone would get *that* zone's date here, which is a property of what
was written and not something this statement could correct.

**Newest first, and it is the one departure from every sibling's ordering.**
The other listings are ordered by the number that matters, so cutting them
keeps the interesting rows; this one is ordered by time, and a chart of the
last week wants the newest days. Ascending under the same ``LIMIT`` would hand
back the oldest days in the archive and draw an empty week.
:meth:`~mailarc_analytics.queries.reports.AnalyticsReader.archived_per_day`
turns the window round again.

**A row is one archiving event, not one message.** ``count(m)`` counts pattern
rows and ``r`` is bound, so the same mail reaching two accounts is two
``ARCHIVED_FROM`` edges and counts twice — measured, two edges and
``messages: 2``. That is what the column means: the chart is of what the
archive did on a day, and importing a mail into a second account is work the
archive did.

Two things a consumer has to know about the numbers. ``sum()`` comes back as a
**float** (``3139.0``), so
:func:`~mailarc_analytics.queries.rows.as_int` is what makes ``bytes`` a whole
number — the same coercion every other count in this package needs and for a
different reason. And a day whose copies all lack ``size_bytes`` sums to ``0``
rather than to null, which is the answer a chart wants anyway.

The traversal is an inner match on purpose. A ``Message`` with no
``ARCHIVED_FROM`` edge was never archived from anywhere and has no day to be
placed on; ``optional=True`` would give it a null key and collect every one of
them into a bucket no calendar has. The ``IS NOT NULL`` filter closes the same
hole from the other side, for an edge written before the field existed.
"""


_reply = alias(Message, "m")
_parent = alias(Message, "p")

MESSAGE_REPLIES: QueryBuilder[Message] = (
    select(_reply)
    .where(_reply.id.is_not_null() & (_reply.id > param("after")))
    .traverse(Message.replies_to, from_=_reply, to=_parent)
    .project(_reply.id, _parent.id.as_("parent"))
    .order_by(var("id"))
    .limit(param("limit"))
)
"""Who answered whom — one row per reply, ``(id, parent)``.

The second half of what makes a *conversation*. A provider's thread id groups
the copies **one** account holds, so the same exchange imported from two
mailboxes is two threads; the ``In-Reply-To`` header is the sender's own
statement that this message answers that one and crosses accounts. Union-find
over both is signal 7,
:attr:`~mailarc_analytics.derived.model.TopicSignal.CONVERSATION`.

**The page is cut after the match, not before it**, which is the opposite of
:data:`MESSAGE_RELATIONS` and is right for the opposite reason. That statement
expands every message into many rows, so its ``LIMIT`` has to come first or the
page pays for the whole archive's expansion. This one *narrows*: an inner match
keeps only the messages that have a parent, which is a small minority of any
archive. A ``WITH … LIMIT`` above the match would window two thousand messages
and hand back the five of them that are replies — and
:func:`~mailarc_analytics.derived.reader._paged` stops on a short page, so the
walk would end at the first sparse window. Written this way, a page holds two
thousand replies and the cursor is the last one's id.

That in turn is why the match is **not** optional. An optional one would keep
every message with a null parent, which is exactly the wide read this statement
exists to avoid, and the caller would filter in Python what the store can
filter for free.

One row per message, because the writer produces at most one ``REPLIES_TO``
edge per message — the parent is a single header. A graph carrying two would
put both rows on the same page and the reader keeps the first; nothing in this
project can produce one.
"""


_signals = alias(Message, "m")
_addressee = alias(Address, "t")
_answer = alias(Message, "q")
_answerer = alias(Address, "ra")
_label = alias(Label, "l")

MESSAGE_SIGNALS: QueryBuilder[Message] = (
    select(_signals)
    .where(_signals.id.is_not_null() & (_signals.id > param("after")))
    .order_by(_signals.id)
    .limit(param("limit"))
    .traverse(Message.recipients, from_=_signals, to=_addressee, optional=True)
    .traverse(
        Message.replies_to,
        from_=_signals,
        to=_answer,
        direction="INCOMING",
        optional=True,
    )
    .traverse(Message.sender, from_=_answer, to=_answerer, optional=True)
    .traverse(Message.labels, from_=_signals, to=_label, optional=True)
    .project(
        _signals.id,
        collect(_addressee.id, distinct=True).as_("to"),
        count(_answer, distinct=True).as_("reply_count"),
        collect(_answerer.id, distinct=True).as_("replied_by"),
        collect(_label.name, distinct=True).as_("label_names"),
        _signals.has_attachments,
    )
    .order_by(var("id"))
)
"""What a message says about its own importance — a read of its own.

**Not four more columns on :data:`MESSAGE_RELATIONS`, and that is the point.**
That statement already carries five optional expansions which cross-multiply
per message — fifty recipients and twenty attachments are a thousand
intermediate rows — and the two hops below would multiply the reply chain and
the label set into the same product. It also answers a different question:
``addressed`` there is To *and* Cc folded into one set, which is what
co-addressing is defined over, while "addressed directly" is a claim about the
To line alone. One statement cannot mean both.

Four columns and four reasons:

* ``to`` is ``SENT_TO`` **only**. Cc folded in would make "addressed directly"
  true of every mail sent to a department.
* ``reply_count`` counts the archive's own answers to this message, which is
  the evidence that somebody engaged with it. ``count(DISTINCT q)`` and not
  ``count(q)``, because the label and recipient expansions multiply each reply
  into as many rows as the message has recipients.
* ``replied_by`` is who those answers came from, so "replied by you" is this
  set meeting the archive's own addresses — the strongest signal a mailbox
  carries and the one no provider flag can fake.
* ``label_names`` is the provider's own filing. Only Gmail brings ``IMPORTANT``
  and ``STARRED`` into the graph (``mailarc_google.source.mapping``); IMAP's
  ``\\Flagged`` and M365's flags are not imported, so a reason drawn from this
  column is honest only where a label really says it.

The reply hop is the one traversal here that is **incoming** and the one that
deliberately *chains*. ``(m)<-[:REPLIES_TO]-(q)`` finds the answers, and the
next hop leaves from ``q`` rather than from ``m`` — without ``from_=_answer``
it would leave from the message and collect the message's own sender. Every
other expansion passes ``from_=_signals`` for the reason
:data:`MESSAGE_RELATIONS`' do: consecutive traversals walk a path, and a
chained one comes back empty with no error at all.

The page is cut **before** the expansions, the way that statement's is, and one
message is exactly one row after the aggregation — so
:func:`~mailarc_analytics.derived.reader._paged`'s short-page rule holds.

``has_attachments`` is a property and not a traversal: the import already
stores it, and a fifth hop to count attachment nodes would answer the same
question at the cost of another product.
"""


_texts = alias(Message, "m")

MESSAGE_TEXTS: QueryBuilder[Message] = (
    select(_texts)
    .where(_texts.id.in_(param("ids")))
    .project(
        _texts.id,
        _texts.subject,
        left(_texts.body_clean, param("max_chars")).as_("body"),
    )
)
"""The words of named messages, already cut to length — the keyword read.

:data:`MESSAGE_BODIES` with a ceiling and a subject, and the two are separate
because the ceiling is the whole difference. A3 needs a template member's
*whole* cleaned body: the sample text a human recognises it by and the word
count the brevity factor divides. The keyword pass needs only enough text to
count terms in, over twenty members of every topic in the archive, so it cuts
in the store — ``left(m.body_clean, $max_chars)`` — for the reason
:data:`~mailarc_analytics.queries.catalog.MESSAGES_NEEDING_EMBEDDING` does:
``body_clean`` is uncapped, and sending it whole would move tens of megabytes
per page to count the first two thousand characters of each.

The subject comes back beside the body because a subject line is where a piece
of work is usually named — "Angebot Datenmigration" is the keyword, and the
body says "anbei unser Angebot". Both go through the same tokeniser; nothing
here decides how they are weighed.

``$ids`` bounds the read to the members of actual clusters, and
``topic_keyword_members`` × ``topic_keyword_chars`` is what bounds it in total.
``.in_()`` is right on ``Message.id`` for the reason :data:`MESSAGE_BODIES`'
docstring gives, and would be wrong on a list property like ``refs``.

``body`` is an expression, so it carries an explicit ``.as_()``; without one
the row would be keyed by ``left(m.body_clean, $max_chars)``.
"""


_membership_tag = alias(Tag, "t")
_tagged = alias(Message, "m")

TAGGED_MEMBERSHIP: QueryBuilder[Tag] = (
    select(_membership_tag)
    .traverse(Tag.messages, from_=_membership_tag, to=_tagged)
    .where(_tagged.id.is_not_null() & (_tagged.id != ""))
    .project(_membership_tag.id.as_("tag_id"), _tagged.id.as_("message_id"))
    .order_by(var("tag_id"))
    .order_by(var("message_id"))
)
"""Who wears what, right now — the suggestion pass's only look at ground truth.

The annotation layer is **read** here and written nowhere in this package: a
suggestion is an analysis talking and ``TAGGED`` records what a human decided,
which is the line
``test_queries_catalog.py::test_it_only_ever_merges_a_derived_label`` draws by
putting ``Tag`` in the ground-truth set. What this read is for is the other
half of that: a suggestion may only be made for a message that is *not* already
tagged, and the score is a share of the members that are — so the pass needs
the memberships as they stand after every earlier stage.

Rooted at the ``Tag`` and walked to the message, which is not a matter of
taste. runic emits a predicate naming a *traversed* variable after the whole
pipeline, so a statement that started at the message would have its filter land
behind the clause it was meant to narrow — the same argument
:attr:`~mailarc_core.archive.model.Tag.messages` was declared for.

Not paged, and that is a bounded choice rather than an oversight: a membership
row is two ids, and the population is the messages a human has tagged by hand —
thousands, in an archive of a hundred thousand. Both columns are ordered so
that two rebuilds fold the same mapping in the same order.

The canonical-id filter is :data:`MESSAGE_PROPERTIES`'s, so the ids in here are
ids the rest of the rebuild has also seen.
"""
