"""What a search asks, plus the vector index it needs to be asked at all.

Six names, three shapes, and the third one is the interesting one.

:data:`SEMANTIC_NEIGHBOURS`, :data:`SEMANTIC_TOPIC_PAIRS` and
:data:`FULLTEXT_MESSAGES` are ordinary statement objects: named, unbound,
compiled once and executed through ``session.all_rows(statement, params)``.
Caller input still only ever arrives as a bound ``$parameter`` — the builder
declares them rather than binding them, and ``statement.parameter_names()``
reads that declaration back, which is what
:func:`~mailarc_analytics.queries.catalog.parameters_of` used to read off the
string. A missing binding **raises** rather than passing a silent null.

:func:`DROP_VECTOR_INDEX` and :func:`CREATE_VECTOR_INDEX` are no longer
statements at all. Vector-index DDL in runic 0.5 lives on
:class:`~runic.ogm.schema.runtime_index.IndexOperations`, which takes a model
class and a field descriptor and emits the DDL itself, so the two names are
**functions** here — same names, called rather than executed, and
:func:`CREATE_VECTOR_INDEX`'s five former ``$parameters`` are now five keyword
arguments the type checker enforces instead of the store. Nothing about *why*
they are in this package rather than in a graph migration changed, and nothing
about what they emit changed either — keeping that true is what the second
private reach below buys.

:data:`VECTOR_INDEX_OPTIONS` **stays raw Cypher, deliberately** —
``IndexOperations.describe()`` cannot answer the question it is asked. See its
docstring; it is the one exception in this package and it is not an oversight.

Private-attribute reaches, in one place. Getting from a
:class:`~runic.ogm.Session` to the driver ``IndexOperations`` needs means
``session._driver``, and carrying the index's tuning constants through needs
``IndexOperations._adapter``: 0.5 exposes a public accessor for neither.
:func:`_index_operations` and :func:`CREATE_VECTOR_INDEX` are the only two
places in this package that do it, so the day runic grows public equivalents
there are two call sites to change and no others.
"""

from typing import Any, Final

from runic.ogm import (
    Session,
    alias,
    fulltext_search,
    param,
    score,
    select,
    var,
    vector_search,
)
from runic.ogm.schema.runtime_index import IndexOperations

from mailarc_core.archive.model import Address, Message

_NODE: Final = alias(Message, "node")
"""The message a search procedure yields — the same variable in all three.

A handle passed to :func:`~runic.ogm.vector_search` or
:func:`~runic.ogm.fulltext_search` names the yielded variable as well as the
class, so ``CALL … YIELD node`` reads exactly as it did. In
:data:`SEMANTIC_TOPIC_PAIRS` the handle is never given to ``select()`` or
``traverse()`` and therefore adds no pattern of its own: it is only how a
property is read off a variable the ``CALL`` introduced, which is what
``col("node", Message.id)`` does with more punctuation. Naming the variable
the same way in all three is worth something on its own — it is what let the
old strings and the new statements be diffed line for line.
"""

_SENDER: Final = alias(Address, "s")
"""The optional ``SENT_FROM`` target both hit-listing statements project."""

_LEFT: Final = alias(Message, "m")
"""The message :data:`SEMANTIC_TOPIC_PAIRS` walks, one row per stored vector."""

_EMBEDDING: Final[Any] = Message.embedding
"""The field descriptor the index DDL is built from.

``IndexOperations`` wants the descriptor itself — it reads ``field_name`` off
it — where every query in this file wants an :func:`~runic.ogm.alias` handle's
property reference, and the two are different objects. Named once and typed
``Any`` because the model annotates the attribute with the *value* type it
carries (``Vector | None``) while class access yields the descriptor, so the
checker sees a mismatch that the runtime does not have. One named exception
beats the same suppression on both call sites.
"""


SEMANTIC_NEIGHBOURS = (
    vector_search(_NODE.embedding, vector=param("vector"), k=param("k"))
    .where(
        _NODE.id.is_not_null()
        & (_NODE.id != "")
        & (_NODE.embedding_model == param("model"))
    )
    .traverse(Message.sender, from_=_NODE, to=_SENDER, optional=True)
    .project(
        _NODE.id,
        _NODE.subject,
        _NODE.sent_at,
        _SENDER.id.as_("sender"),
        score().as_("distance"),
    )
    .limit(param("limit"))
)
"""The ``$k`` nearest messages to a vector, cut to ``$limit`` after filtering.

Two parameters for what looks like one number, and the difference is the whole
usable shape of this statement. **The procedure cannot be filtered before the
fact**: a ``MATCH`` above it does not narrow it, and binding its output to an
already-matched variable returns nothing at all — measured. So ``$k`` is how
wide the index search goes and ``$limit`` is what the caller sees, and every
row dropped in between (a node with no canonical id, and whatever a caller
filters further) has to be paid for up front. Asking for ``k = limit`` and
filtering leaves a short page that looks like a small archive. The builder
keeps the two apart by construction: ``k=`` goes into the procedure call and
``.limit()`` into the trailing ``LIMIT``, so they cannot be collapsed by
accident.

``score()`` is a **distance**: cosine gives ``1 - similarity``, lower is
better, and an exact match can come back very slightly *negative* — measured
``-1.19e-07`` on a normalised 768-dimensional vector. Anything converting it to
a similarity has to clamp, or a UI shows 100.00001 %. It is the same
``score()`` :data:`FULLTEXT_MESSAGES` projects and it means the opposite there,
which is why both name their column rather than leaving it ``score``.

A message with no vector is not ranked low here, it is absent from the index
entirely, and nothing in the result says so. That is what ``VECTOR_COVERAGE``
is for, and why every semantic answer in this project carries it.

``$model`` is the half that keeps the coverage notice honest, and it is not
optional politeness. The index holds one vector per message and says nothing
about which model produced it, so a half-finished re-embed leaves two spaces in
one index — measured on a real server: twelve messages embedded by one model,
five re-embedded by another, and a search under the new one returned six
confidently ranked hits, at least one of them a comparison between two spaces,
under a notice saying seven messages could not be found. Filtering here rather
than trusting the index means a changed embedder degrades to *fewer* hits
instead of to wrong ones, and ``VECTOR_COVERAGE`` and this statement then mean
the same thing by "embedded". The rows dropped are paid for by ``$k``, which is
what the over-fetch is for.

**The ordering is no longer written down, and that is not an omission.** The
old string ended ``ORDER BY distance`` and had to; a KNN statement built by
runic always compiles ``ORDER BY __score ASC`` — closest first — ahead of
anything the caller adds, so spelling it out here emits ``ORDER BY __score ASC,
distance ASC``: the same column ordered twice. Measured. The guarantee is the
builder's now, and it is the stronger one, because it cannot be lost by editing
the projection.

``node.id <> ''`` is now an auto-bound literal (``<> $p0``) rather than a
quoted empty string. The comparison is the same one; the difference is that the
value is fixed by the statement, so it does **not** appear in
``parameter_names()`` and no caller can reach it.
"""


SEMANTIC_TOPIC_PAIRS = (
    select(_LEFT)
    .where(
        _LEFT.id.is_not_null()
        & (_LEFT.id != "")
        & (_LEFT.embedding_model == param("model"))
    )
    .call(
        "db.idx.vector.queryNodes",
        "Message",
        "embedding",
        param("k"),
        _LEFT.embedding,
        yields=["node", "score"],
    )
    .where(
        _NODE.id.is_not_null()
        & (_NODE.id > _LEFT.id)
        & (_NODE.embedding_model == param("model"))
        & (var("score") <= param("max_distance"))
    )
    .project(
        _LEFT.id.as_("left"),
        _NODE.id.as_("right"),
        var("score").as_("distance"),
    )
    .order_by(var("score").as_("distance"))
    .limit(param("limit"))
)
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

This is the one statement here that still names the procedure by hand, through
``.call(…, yields=["node", "score"])``, and it has to:
:func:`~runic.ogm.vector_search` searches for **one** vector handed in as a
parameter, and the whole point of this shape is a vector read off each matched
node. The procedure name is a literal in this file, never a value from a
caller. ``node`` and ``score`` are variables no model declares, so neither can
be reached as a plain field: ``score`` is :func:`~runic.ogm.var`, and ``node``
is the :data:`_NODE` handle, which renders ``node.id`` without matching a
pattern of its own. ``var("node").id`` does *not* work — measured; ``var()``
yields a bare reference with no attribute access — and ``col("node",
Message.id)`` is the equivalent one-off form, used here as a handle only
because the class-level descriptor is annotated with the value type it carries
and the type checker rejects it.
"""


FULLTEXT_MESSAGES = (
    fulltext_search(_NODE, query=param("text"))
    .where(_NODE.id.is_not_null() & (_NODE.id != ""))
    .traverse(Message.sender, from_=_NODE, to=_SENDER, optional=True)
    .project(
        _NODE.id,
        _NODE.subject,
        _NODE.sent_at,
        _SENDER.id.as_("sender"),
        score().as_("relevance"),
    )
    .order_by(score().as_("relevance"), desc=True)
    .limit(param("limit"))
)
"""Full-text search over ``subject`` and ``body_text`` — the path that always
works, embedder or not.

``score()`` is a **relevance**: higher is better, the opposite convention to
:data:`SEMANTIC_NEIGHBOURS`'s distance, which is why this one orders
descending. The two are not comparable and must never be sorted into one list
without a stated normalisation, or the merge invents a ranking neither index
produced.

``$text`` is a bound parameter, so no Cypher can be injected through it — but
it reaches **RediSearch**, which is a second query language with operators of
its own: ``|`` is OR, a leading ``-`` negates, ``@subject:`` selects a field,
``*`` truncates, and a lone ``(`` raises a syntax error. A caller here may be a
model reading through MCP, so the words are tokenised in
:mod:`mailarc_analytics.semantic.search` before they arrive — this docstring is
the reason that tokeniser exists and is not optional politeness. **Nothing
about that changed with the builder**: ``fulltext_search(…, query=param("text"))``
parameterises the Cypher, not the RediSearch expression inside it.

The builder chooses which properties the index covers by leaving ``fields``
unset, which emits the two-argument ``queryNodes('Message', $text)`` the string
used — the label's whole full-text index, so ``subject`` and ``body_text``
both, as the baseline migration built it.
"""


def _index_operations(session: Session) -> IndexOperations:
    """The index façade for *session*'s graph.

    **One of the two private-attribute reaches in this package**, the other
    being :func:`CREATE_VECTOR_INDEX`'s, which needs what this returns.
    :class:`~runic.ogm.schema.runtime_index.IndexOperations` wants a driver and
    :class:`~runic.ogm.Session` publishes no accessor for its own, so
    ``session._driver`` it is. Isolated here rather than repeated at each call
    site: it is exactly the kind of thing a minor release turns into a public
    property, and then this is one line to change instead of three.
    """
    return IndexOperations.from_driver(session._driver)  # noqa: SLF001


def DROP_VECTOR_INDEX(session: Session) -> None:  # noqa: N802 - see below
    """Take the vector index away, so one of a different length can be built.

    The one piece of DDL in this catalogue, and it is here rather than in a
    graph migration because the dimension is a *setting* now: a human picks the
    embedder on the settings page, and the length follows the model they
    picked. A migration is a versioned statement about the schema every
    installation shares; this is one installation choosing a length, and
    re-running the same revision with a different constant is not something a
    migration chain can express.

    Paired with :func:`CREATE_VECTOR_INDEX` and never used alone — a graph left
    without the index answers every semantic search with an opaque driver
    error.

    **No longer a string, and therefore no longer a ``CATALOG`` entry.**
    Vector-index DDL in runic 0.5 is
    :meth:`~runic.ogm.schema.runtime_index.IndexOperations.drop_vector_index`,
    which takes the model class and the field descriptor and emits
    ``DROP VECTOR INDEX FOR (n:Message) ON (n.embedding)`` itself — the
    string this replaced, bar the pattern variable's name and the backquotes
    runic puts round every label and property it writes. Emitting it needs a
    session, so the name is a function: ``DROP_VECTOR_INDEX(session)`` where it
    used to be ``session.execute(DROP_VECTOR_INDEX, {})``. It takes no
    parameters, which is why it never had any, and ``parameters_of`` has
    nothing to read — a name that cannot carry caller input sits outside that
    security boundary rather than being exempted from it.

    Dropping an index that is not there raises ``ResponseError: Unable to drop
    index on :Message(embedding): no such index`` — measured, and **the same
    error the string raised**, so
    :func:`~mailarc_analytics.semantic.indexing.rebuild_index`'s
    ``if existing is not None`` guard is still doing the job it was written
    for.

    The name shouts because it is a catalogue name and the callers spell it
    that way; ruff's ``N802`` is answered here rather than by renaming, because
    a rename is a second, silent change to every call site in the same commit
    as a behaviour change.
    """
    _index_operations(session).drop_vector_index(Message, _EMBEDDING)


def CREATE_VECTOR_INDEX(  # noqa: N802 - see DROP_VECTOR_INDEX
    session: Session,
    *,
    dimension: int,
    similarity: str,
    m: int,
    ef_construction: int,
    ef_runtime: int,
) -> None:
    """Build the vector index at a chosen length.

    The settings are the migration's own — see
    ``graph_migrations/versions/5f4678dfc5a4``, which this deliberately mirrors
    so that an index rebuilt here is indistinguishable from one a fresh install
    migrated. Only the dimension is expected to differ, and that is the whole
    point.

    **The five ``$parameters`` became five keyword arguments, and none of them
    was dropped.** That is not the obvious translation.
    :meth:`~runic.ogm.schema.runtime_index.IndexOperations.create_vector_index`
    takes ``dimension`` and ``similarity`` and nothing else, and calls the
    adapter positionally, so the adapter's own defaults would decide the rest —
    measured on the vendored FalkorDB, the public method emits ``OPTIONS
    {dimension: 768, similarityFunction: 'cosine', M: 16, efConstruction: 200,
    efRuntime: 10}`` and the live index reports exactly that back through
    :data:`VECTOR_INDEX_OPTIONS`. ``M`` would have survived by coincidence; the
    other two would not. So this reaches the adapter directly, where the three
    are keyword arguments, and passes what the caller gave it.

    Keeping them is not tidiness. ``efRuntime`` is how long a candidate list a
    KNN keeps, and :data:`SEMANTIC_NEIGHBOURS` is *built* on over-fetching ``$k``
    and filtering afterwards — a candidate list of ten under a ``$k`` of fifty
    is a short page that looks exactly like a small archive, which is the one
    failure this whole family is shaped to avoid. ``efConstruction`` is the
    build-time half of the same thing. Neither changes what the index holds,
    both change what it finds, and no test that counts rows on a planted graph
    would ever notice.

    The cost is the second private reach in this package, and it is written
    down rather than done quietly: ``IndexOperations`` exposes its adapter only
    as ``_adapter``, and ``_resolve`` is what turns the model class and the
    field descriptor into the label and property name the adapter wants —
    asking the model rather than spelling ``"Message"`` twice more. Both go
    away the day ``create_vector_index`` grows the three keyword arguments its
    own adapter already has.

    **The ``OPTIONS`` map is interpolated now, not bound, and it is still safe
    — by a different argument.** The old statement passed all five as
    parameters, which FalkorDB's ``OPTIONS`` map accepts; runic's adapter
    formats them into the DDL text instead. What replaces the binding is a
    check: the four numbers are typed ``int``, and ``similarity`` is tested
    against a closed set *before* it is formatted, so ``similarity="'} DROP"``
    raises ``ValueError: unsupported vector similarity function "'} DROP";
    expected one of ['cosine', 'euclidean']`` — measured — rather than reaching
    the store. That is the stricter of the two: the parameter form would have
    carried any string the caller liked, and it was only ever FalkorDB that
    refused it.

    None of the five has a default. They are the same five values the string
    bound, they live where they always did — ``INDEX_SIMILARITY``, ``INDEX_M``,
    ``INDEX_EF_CONSTRUCTION``, ``INDEX_EF_RUNTIME`` in
    :mod:`mailarc_analytics.semantic.indexing` — and a call site that forgets
    one is a ``TypeError`` at the call rather than a quiet recall regression a
    year later.
    """
    operations = _index_operations(session)
    label, prop = operations._resolve(Message, _EMBEDDING)  # noqa: SLF001
    operations._adapter.create_vector_index(  # noqa: SLF001
        label,
        prop,
        dimension,
        similarity,
        m=m,
        ef_construction=ef_construction,
        ef_runtime=ef_runtime,
    )


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

**And the one statement in this file that stays raw Cypher — deliberately, not
because nobody got to it.** runic 0.5's replacement for reading indexes is
:meth:`~runic.ogm.schema.runtime_index.IndexOperations.describe`, and it
returns ``IndexSpec(label, property, index_type)``: three fields, measured, no
``options`` and therefore **no dimension**. It can say that ``Message.embedding``
carries a vector index; it cannot say how long a vector that index will accept,
which is the only thing this read is for. ``CALL DB.INDEXES()`` still has it —
``options: {'embedding': {'dimension': 4, 'similarityFunction': 'cosine', 'M':
16, 'efConstruction': 400, 'efRuntime': 512}}`` — so this string is what
:func:`~mailarc_analytics.semantic.search.vector_index` and
:func:`~mailarc_analytics.semantic.search.has_fulltext_index` keep reading,
through ``session.execute()`` and ``rows_of``, exactly as before. Replacing it
with ``describe()`` would compile, pass every test that checks a label, and
quietly delete the guard that catches a mismatched index. Take it out only when
``describe()`` reports the options.

It is also the reason ``rows_of`` and the four ``as_*`` coercions in
:mod:`mailarc_analytics.queries.rows` survive the migration: a raw statement
still comes back as a header plus a list of lists with the driver's own value
shapes, and this is the statement that still does.
"""
