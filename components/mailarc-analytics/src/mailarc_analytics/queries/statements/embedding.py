"""What the embed job counts, reads and writes — §7.4.

Five statements: the total a progress bar divides by, the page the job walks,
the write that attaches the vectors, the coverage number every semantic answer
carries, and the clear that a resized index makes necessary.

Two mechanics the builder now owns, and both used to be caveats in the
docstrings below:

``vecf32()``
    ``Message.embedding`` is declared ``Vector``, so its converter carries
    ``cypher_fn = "vecf32"`` and every ``set()`` of that property is wrapped on
    the way in automatically. The wrap is no longer hand-written, and it is
    still never applied on the way out.
``NULL``
    ``set({field: None})`` compiles to the literal ``SET n.p = NULL`` rather
    than to a parameter bound to null — runic states the clear outright,
    because some backends treat the two differently.

**Read the Cypher through a session, never off a bare ``build()``.** The
wrapping function comes from the *dialect*, and a module-level statement has no
session until one executes it, so ``WRITE_EMBEDDINGS.build()`` prints
``SET m.embedding = row.vector`` — no ``vecf32`` — while the statement that
actually runs is ``SET m.embedding = vecf32(row.vector)``. Measured both ways on
the vendored FalkorDB. Anything inspecting these statements as text (a test
asserting an invariant, a reviewer reading a diff) has to compile them bound:
``with statement._bound_to(session) as bound: bound.build()``, which is what
``session.all_rows`` does internally. A bare ``build()`` is not what the store
sees, and here the difference is a vector stored unindexed and never found.

**One statement object, one thread at a time — and something enforces that.**
``_bound_to`` binds the session onto the statement itself and resets it in a
``finally``, so two threads sharing a module-level constant can have one clear
the other's session mid-flight: measured at 1 failure in 200 executions across
four threads and again at 1 in 3200 across eight, raising ``AttributeError:
'NoneType' object has no attribute 'mapper'`` out of the decoder. Building a
fresh statement per call in the same harness raised nothing. Neither run ever
returned a *wrong* row — ``bind()`` builds a new dict per call, so no thread
can see another's parameters; the whole failure is the crash.

This is a property of runic 0.5 and applies to every catalogue constant, not
only to this family — it is written here because this is where it was measured.
It is also new with the migration, a Cypher *string* being immutable and shared
safely, which is why it is closed rather than merely noted:
:data:`~mailarc_analytics.queries.rows._IN_USE` gives each statement object a
lock and :func:`~mailarc_analytics.queries.rows.rows_of` holds it for the
execution. Every catalogue statement in this project is executed there and
nowhere else, so the guarantee is the package's rather than each caller's — but
it *is* ``rows_of``'s, so a caller that reached past it to
``session.all_rows(catalog.SOMETHING, …)`` would be back in the window.

One mechanic the builder still does **not** own: a ``$rows`` payload is not
mapped. :func:`runic.ogm.encode_rows` converts *declared* fields and passes
every other key through untouched, and :data:`WRITE_EMBEDDINGS`' rows carry
``id`` and ``vector`` — ``vector`` is not a field name (the property is
``embedding``), so a plain list of floats is exactly what has to arrive, and
``EmbeddedMessage.as_row()`` is still right to build one.
"""

from typing import Any

from runic.ogm import (
    QueryBuilder,
    alias,
    count,
    left,
    param,
    row,
    select,
    unwind,
    when,
)

from mailarc_core.archive.model import Message

_m = alias(Message, "m")
"""The Cypher variable every statement here matches ``Message`` through.

Named ``m`` because that is what the strings these replaced were written with,
so a diff of the emitted Cypher against the catalogue's history reads as a
change of *builder*, not of query.
"""


COUNT_NEEDING_EMBEDDING: QueryBuilder[Any] = (
    select(_m)
    .where(
        _m.id.is_not_null()
        & (_m.id != "")
        & _m.body_clean.is_not_null()
        & (_m.body_clean != "")
        & (
            _m.embedding.is_null()
            | _m.embedding_model.is_null()
            | (_m.embedding_model != param("model"))
        )
    )
    .project(count("m").as_("total"))
)
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

``$model`` is the only parameter, and the empty strings are auto-bound literals
(``$p0``, ``$p1``): they are values the statement fixed about itself, not
caller input, which is why they are absent from ``parameter_names()``.
"""


MESSAGES_NEEDING_EMBEDDING: QueryBuilder[Any] = (
    select(_m)
    .where(
        _m.id.is_not_null()
        & (_m.id > param("after"))
        & _m.body_clean.is_not_null()
        & (_m.body_clean != "")
        & (
            _m.embedding.is_null()
            | _m.embedding_model.is_null()
            | (_m.embedding_model != param("model"))
        )
    )
    .project(
        _m.id,
        _m.subject,
        left(_m.body_clean, param("max_chars")).as_("body"),
    )
    .order_by(_m.id)
    .limit(param("limit"))
)
"""One page of messages to embed, with the text already cut to length.

:data:`COUNT_NEEDING_EMBEDDING`'s condition plus
:data:`~mailarc_analytics.queries.catalog.MESSAGE_PROPERTIES`'s
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

``id`` and ``subject`` are bare fields and name their own columns; ``body`` is
an expression and would otherwise key the row by its raw Cypher text
(``left(m.body_clean, $max_chars)``), so the ``.as_("body")`` is not decoration
— it is the column name :class:`~mailarc_analytics.semantic.model.PendingMessage`
reads.
"""


WRITE_EMBEDDINGS: QueryBuilder[Any] = (
    unwind(param("rows"))
    .match(Message, key={Message.id: row("id")}, alias="m")
    .set(
        {
            Message.embedding: row("vector"),
            Message.embedding_model: param("model"),
        },
        on=_m,
    )
    .returning(count("m").as_("written"))
)
"""Attach computed vectors to their messages. ``$rows``: ``id``, ``vector``.

The one statement in this catalogue that writes a *ground-truth* node, and the
one the package docstring's exception is about: an embedder only ever adds a
vector, and this sets exactly two properties that the import deliberately never
writes (see :mod:`mailarc_core.archive.writer`, which leaves an existing
``Message`` untouched precisely so this phase can fill them in).

``match`` and never ``merge``: a row naming a message that is not there is a
bug in the caller, and merging it would invent an empty ``Message`` carrying
nothing but a vector — a node that no import can ever reconcile and that every
search would happily return.

``vecf32(row.vector)`` on the way in, and never on the way out. The function
turns a list into a vector; applying it to a stored property raises ``Type
mismatch: expected List or Null but was Vectorf32`` — re-measured, and still
the reason nothing reads a vector back through it.

**The wrap is no longer hand-written**, and that is the one change here worth
distrusting. ``Message.embedding`` is declared ``Vector``, its converter carries
``cypher_fn = "vecf32"``, and ``set()`` applies the dialect's wrapping function
to that property on its own. But the *dialect* is what supplies it, so an
unbound ``build()`` shows ``SET m.embedding = row.vector`` and looks like the
wrap was lost — see the module docstring. Compiled through a session it is
``SET m.embedding = vecf32(row.vector)``.

Why neither reading proves anything on its own: FalkorDB accepts a vector of the
wrong length or the wrong type, stores it as a property, and declines to index
it — no error, no log line, ``indexingFailures`` stays at zero. Measured here as
the control: a two-float vector into a four-dimension index returned
``written: 1`` and was then absent from every KNN. So this statement is verified
by writing a real vector through it and watching
:data:`~mailarc_analytics.queries.catalog.SEMANTIC_NEIGHBOURS` come back with
it at distance ``0.0``, and by
nothing less.

The vector still arrives as a plain list of floats, and that is now a statement
about ``$rows`` rather than about raw Cypher: values inside an ``UNWIND``
payload never pass through the mapper, and ``encode_rows`` converts only keys
that are *declared field names* — ``vector`` is not one (the property is
``embedding``), so it would be passed through untouched even if the caller ran
it. ``EmbeddedMessage.as_row()`` building a plain list is therefore still
correct, and its docstring's reason ("a raw statement goes past runic's
converters") should be restated rather than deleted: the converter is not
skipped any more, it simply never sees a key it does not know.

The model is bound once for the whole batch rather than per row: a batch is one
embedder's answer by construction, and a row that claimed a different model
would be a vector in one space labelled as another.
"""


VECTOR_COVERAGE: QueryBuilder[Any] = (
    select(_m)
    .where(_m.id.is_not_null() & (_m.id != ""))
    .project(
        count("m").as_("total"),
        count(when(_m.embedding_model == param("model"), 1)).as_("embedded"),
        count(when(_m.body_clean.is_null() | (_m.body_clean == ""), 1)).as_(
            "unembeddable"
        ),
    )
)
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

``when(...)`` is the ``CASE WHEN … THEN 1 END`` this used to spell by hand, and
``count()`` skips a null branch, which is why no ``ELSE`` is emitted and none is
wanted. The ``1`` is auto-bound (``THEN $pN``) instead of being a literal; it is
a constant of the statement, so it does not appear in ``parameter_names()`` and
nothing about the three counts changes.
"""


CLEAR_EMBEDDINGS: QueryBuilder[Any] = (
    select(_m)
    .where(_m.embedding.is_not_null() | _m.embedding_model.is_not_null())
    .set(
        {Message.embedding: None, Message.embedding_model: None},
        on=_m,
    )
    .returning(count("m").as_("cleared"))
)
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

``None`` in ``set()`` is a clear, and it compiles to the Cypher literal
``NULL`` rather than to a parameter bound to null. runic states it outright on
purpose: ``SET n.p = $x`` with ``x`` null does remove the property, but it
reads as assigning a value, and not every backend treats the two the same. The
property that matters — ``embedding IS NULL`` afterwards, so
:data:`MESSAGES_NEEDING_EMBEDDING` offers the message again — was measured, not
inferred.

Takes **no parameters**, which is why it is the one statement here that
``rows_of`` calls without a binding at all.
"""
