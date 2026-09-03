"""Every statement the derived layer runs, named, parameterised and enumerated.

**No free Cypher from outside.** A statement is a module-level constant here or
it does not exist: caller input reaches the graph as a bound ``$parameter``,
never as a formatted string, so no address, subject or label can ever change
what a statement *does*. Phase 6's MCP server serves a model from this same
file for the same reason — a query catalogue is the only shape in which
"let something else ask the archive questions" is safe. **That argument never
depended on the statements being strings**, and it is if anything stronger now:
a parameter is *declared* on the statement rather than spelled into its text,
``statement.parameter_names()`` reads the declaration back, and a binding that
leaves one out raises ``ValueError: statement is missing values for declared
parameter(s): …`` instead of quietly passing a null. A value the statement
fixed about *itself* — the empty string every canonical-id filter compares
against — is auto-bound as ``$p0`` and deliberately does **not** appear in
``parameter_names()``: the boundary counts what a caller may supply, and a
caller may supply nothing there.

Naming them also makes them reviewable. Four of these statements are §6 and §12
of the spec with the model's real properties put back: ``Address`` has no
``address`` property, the key is ``id``; ``Group``'s key field is ``id``, not
``key``; and the group query's thresholds are parameters rather than the
literals ``> 2`` and ``> 5``, or the configuration would be decorative.

What the statements are now
---------------------------

:mod:`mailarc_core.archive.repository` states the house rule that graph reads
go through runic's query builder, and this file used to be the argument for an
exception: it listed four things the builder could not express — the
``[:SENT_TO|COPIED_TO]`` alternation A1 is *defined* as, ``DELETE``, ``MERGE``,
and ``$token IN m.refs``. **runic 0.5 closed all four**, so the exception is
spent and the statements are builder objects checked against the mapped models.
``traverse(types=[…])`` emits the alternation as one pattern, ``delete()`` and
``unwind().merge().set()`` exist, and ``.any_of(param(…))`` asks the list
question the way ``.in_()`` never could. A misspelled property is now a
``ty`` error rather than a null column, and the ``MERGE``-key/``SET``-property
split that idempotence depends on is enforced by ``merge()`` instead of trusted
to whoever wrote the string.

**Three kinds of exception remain, and none is an oversight: the index read,
the procedure calls and the reply chain.**

:data:`VECTOR_INDEX_OPTIONS` is still raw Cypher, because 0.6's replacement for
reading indexes — ``IndexOperations.describe()`` — returns
``IndexSpec(label, property, index_type)`` and nothing else. It cannot report
the live vector index's *dimension*, which is the only thing that read exists
to learn, and FalkorDB stores a wrong-length vector silently and never indexes
it. Replacing it with ``describe()`` would compile, pass every test that checks
a label, and delete the guard that catches a mismatched index. Its docstring
names the condition for removing it.

The six statements in
:mod:`mailarc_analytics.queries.statements.algorithms` are raw Cypher for a
structural reason rather than a missing feature: **runic 0.6 cannot start a
statement with ``CALL``**. ``select()`` always opens with ``MATCH (n:Label)``
and ``.call()`` is a *mid-pipeline* clause, so the closest a builder gets to
``CALL algo.pageRank(...)`` is ``MATCH (m:Message) CALL algo.pageRank(...)`` —
which runs a whole-graph algorithm once per matched message. There is no
builder form of these to prefer. What the builder would have given them is
replaced in kind: the AST test proves each is a plain literal with nothing
formatted into it, the ``graph_local`` sweep runs every one against the
vendored FalkorDB, and each projects ``node.id AS id`` rather than handing back
an entity the driver would decode into a row.

:data:`REPLY_CHAIN` is the third, and its reason is narrower than either: runic
*can* write the ``*1..3`` it walks — ``hops=(1, 3)`` compiles to exactly that —
but it refuses to bind an ``edge`` variable on a variable-length pattern,
because such a pattern binds a *list* of relationships its mapper cannot decode.
A reachable set of messages with nothing saying how they are joined is not a
subgraph, so the explorer's one chain read is written out and covered by the
same two rules the procedure calls are.

Two things every caller has to know
-----------------------------------

**Bind, do not build.** A statement carrying ``param()`` goes through
``session.all_rows(statement, params)`` — which runs writes as happily as reads
— and never through ``session.execute(*statement.build())``: the declared
parameters are not in the auto-bound dict the builder returns, and the store
answers with a parse error rather than with a missing value.

**Read a statement, never extend one.** A builder mutates in place and returns
itself, so ``catalog.COUNT_GROUPS.limit(5)`` does not derive a narrowed query,
it narrows the catalogue entry for the rest of the process. Binding leaves a
statement untouched and is the whole calling convention; anything that needs a
different shape declares a different constant. For the same reason one
statement object may only be *executed* by one thread at a time — see
:mod:`mailarc_analytics.queries.statements.embedding`, where that was measured,
and :func:`~mailarc_analytics.queries.rows.rows_of`, which is where every
catalogue statement is run and therefore where the lock that enforces it sits.

A ``$rows`` payload is still not mapped: an ``UNWIND`` payload never passes
through runic's converters, so a row bound to one of the ``MERGE_`` statements
goes through :func:`runic.ogm.encode_rows` first —
:func:`~mailarc_analytics.derived.writes.merge_rows` is where every one of them
does, under the model the rows describe — or carries its timestamps as ISO-8601
strings by hand (:func:`as_graph_datetime`, which now has no caller and says
so). And ``Message.simhash`` comes back as the *signed* 64-bit integer the
writer had to store: run it through
:func:`~mailarc_core.archive.model.to_unsigned_64` before banding, comparing or
rendering it.

Where they live
---------------

The statements and the docstrings that explain each one's shape are some
sixteen hundred lines, well past the house limit for one file, so they sit in
:mod:`mailarc_analytics.queries.statements` — one module per family — and this
module is their public surface. Nothing above imports from that package:
``from mailarc_analytics.queries import catalog`` and then ``catalog.NAME``, as
it always was.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any

from runic.ogm import QueryBuilder

from mailarc_analytics.queries.statements.algorithms import (
    ADDRESS_BETWEENNESS,
    LABEL_PROPAGATION,
    MESSAGE_PAGERANK,
    NEIGHBOURHOOD,
    PROCEDURES,
    SHORTEST_PATHS,
)
from mailarc_analytics.queries.statements.analysis import (
    CO_RECIPIENTS,
    COUNT_CO_ADDRESSED,
    COUNT_COMMUNITIES,
    COUNT_GROUPS,
    COUNT_TEMPLATES,
    COUNT_TOPICS,
    GROUPS_OF_MESSAGES,
    RECURRING_GROUPS,
    SUGGESTION_COUNTS,
    TAG_SUGGESTIONS,
    TOP_CO_ADDRESSED,
    TOP_COMMUNITIES,
    TOP_IMPORTANT,
    TOP_TEMPLATES,
    TOPIC_BREAKDOWN,
    TOPIC_KEYWORDS,
    TOPICS_OF_MESSAGES,
)
from mailarc_analytics.queries.statements.embedding import (
    CLEAR_EMBEDDINGS,
    COUNT_NEEDING_EMBEDDING,
    MESSAGES_NEEDING_EMBEDDING,
    VECTOR_COVERAGE,
    WRITE_EMBEDDINGS,
)
from mailarc_analytics.queries.statements.graph import (
    ADDRESS_MESSAGES,
    ADDRESS_NEIGHBOURS,
    COMMUNITY_MEMBERS,
    COMMUNITY_MESSAGES,
    MESSAGE_ADDRESSES,
    MESSAGE_CIRCLE,
    MESSAGE_TAGS,
    MESSAGE_TOPICS,
    OVERVIEW_COMMUNITIES,
    OVERVIEW_TAG_TOPIC,
    OVERVIEW_TAGS,
    OVERVIEW_TOPIC_CIRCLE,
    OVERVIEW_TOPICS,
    REPLY_CHAIN,
    TAG_MEMBERS,
    THREAD_SIBLINGS,
    TOPIC_MEMBERS,
    TOPIC_PARTICIPANTS,
)
from mailarc_analytics.queries.statements.reads import (
    ACCOUNT_ADDRESSES,
    ARCHIVED_PER_DAY,
    COUNT_MESSAGES,
    COUNT_UNIDENTIFIED,
    MESSAGE_BODIES,
    MESSAGE_PROPERTIES,
    MESSAGE_RELATIONS,
    MESSAGE_REPLIES,
    MESSAGE_SIGNALS,
    MESSAGE_TEXTS,
    TAGGED_MEMBERSHIP,
)
from mailarc_analytics.queries.statements.search import (
    CREATE_VECTOR_INDEX,
    DROP_VECTOR_INDEX,
    FULLTEXT_MESSAGES,
    SEMANTIC_NEIGHBOURS,
    SEMANTIC_TOPIC_PAIRS,
    VECTOR_INDEX_OPTIONS,
)
from mailarc_analytics.queries.statements.writes import (
    CLEAR_ADDRESS_RANKS,
    CLEAR_IMPORTANCE,
    DELETE_CO_ADDRESSED,
    DELETE_COMMUNITIES,
    DELETE_GROUPS,
    DELETE_SUGGESTED,
    DELETE_TEMPLATES,
    DELETE_TOPICS,
    MERGE_ABOUT,
    MERGE_ADDRESSED_GROUP,
    MERGE_CO_ADDRESSED,
    MERGE_COMMUNITIES,
    MERGE_GROUPS,
    MERGE_IN_CIRCLE,
    MERGE_INSTANCE_OF,
    MERGE_MEMBER_OF,
    MERGE_SUGGESTED,
    MERGE_TEMPLATES,
    MERGE_TOPIC_KEYWORDS,
    MERGE_TOPICS,
    WRITE_ADDRESS_RANKS,
    WRITE_IMPORTANCE,
)

type Statement = QueryBuilder[Any] | str
"""What a catalogue entry is: a builder statement, or — once — raw Cypher.

The union is the honest type and the ``str`` half is not a leftover. Every
statement in this project is a :class:`~runic.ogm.QueryBuilder` except
:data:`VECTOR_INDEX_OPTIONS`, which reads the live vector index's dimension and
has no builder equivalent, and the six procedure calls, which cannot be
builders at all because runic 0.6 will not let a statement begin with ``CALL``
(see this module's docstring, and each statement's). Annotating the mapping
``Mapping[str, QueryBuilder[Any]]`` and
leaving the one string in it would be a lie a type checker cannot see through;
annotating it ``Mapping[str, Any]`` would say nothing at all. Both halves
support what a caller does with an entry — :func:`parameters_of` reads either,
and both run — so the union costs a consumer nothing except knowing that the
exception exists, which is the point of naming it.
"""

CATALOG: Mapping[str, Statement] = MappingProxyType(
    {
        "ACCOUNT_ADDRESSES": ACCOUNT_ADDRESSES,
        "MESSAGE_PROPERTIES": MESSAGE_PROPERTIES,
        "MESSAGE_RELATIONS": MESSAGE_RELATIONS,
        "COUNT_UNIDENTIFIED": COUNT_UNIDENTIFIED,
        "COUNT_MESSAGES": COUNT_MESSAGES,
        "MESSAGE_BODIES": MESSAGE_BODIES,
        "ARCHIVED_PER_DAY": ARCHIVED_PER_DAY,
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
        "VECTOR_INDEX_OPTIONS": VECTOR_INDEX_OPTIONS,
        "MESSAGE_REPLIES": MESSAGE_REPLIES,
        "MESSAGE_SIGNALS": MESSAGE_SIGNALS,
        "MESSAGE_TEXTS": MESSAGE_TEXTS,
        "TAGGED_MEMBERSHIP": TAGGED_MEMBERSHIP,
        "DELETE_COMMUNITIES": DELETE_COMMUNITIES,
        "DELETE_SUGGESTED": DELETE_SUGGESTED,
        "MERGE_COMMUNITIES": MERGE_COMMUNITIES,
        "MERGE_MEMBER_OF": MERGE_MEMBER_OF,
        "MERGE_IN_CIRCLE": MERGE_IN_CIRCLE,
        "MERGE_SUGGESTED": MERGE_SUGGESTED,
        "MERGE_TOPIC_KEYWORDS": MERGE_TOPIC_KEYWORDS,
        "WRITE_IMPORTANCE": WRITE_IMPORTANCE,
        "CLEAR_IMPORTANCE": CLEAR_IMPORTANCE,
        "WRITE_ADDRESS_RANKS": WRITE_ADDRESS_RANKS,
        "CLEAR_ADDRESS_RANKS": CLEAR_ADDRESS_RANKS,
        "COUNT_COMMUNITIES": COUNT_COMMUNITIES,
        "TOP_COMMUNITIES": TOP_COMMUNITIES,
        "TOP_IMPORTANT": TOP_IMPORTANT,
        "TOPIC_KEYWORDS": TOPIC_KEYWORDS,
        "SUGGESTION_COUNTS": SUGGESTION_COUNTS,
        "TAG_SUGGESTIONS": TAG_SUGGESTIONS,
        "TOPICS_OF_MESSAGES": TOPICS_OF_MESSAGES,
        "GROUPS_OF_MESSAGES": GROUPS_OF_MESSAGES,
        "PROCEDURES": PROCEDURES,
        "LABEL_PROPAGATION": LABEL_PROPAGATION,
        "MESSAGE_PAGERANK": MESSAGE_PAGERANK,
        "ADDRESS_BETWEENNESS": ADDRESS_BETWEENNESS,
        "SHORTEST_PATHS": SHORTEST_PATHS,
        "NEIGHBOURHOOD": NEIGHBOURHOOD,
        "MESSAGE_ADDRESSES": MESSAGE_ADDRESSES,
        "THREAD_SIBLINGS": THREAD_SIBLINGS,
        "REPLY_CHAIN": REPLY_CHAIN,
        "MESSAGE_TOPICS": MESSAGE_TOPICS,
        "MESSAGE_TAGS": MESSAGE_TAGS,
        "MESSAGE_CIRCLE": MESSAGE_CIRCLE,
        "TOPIC_MEMBERS": TOPIC_MEMBERS,
        "TOPIC_PARTICIPANTS": TOPIC_PARTICIPANTS,
        "ADDRESS_NEIGHBOURS": ADDRESS_NEIGHBOURS,
        "ADDRESS_MESSAGES": ADDRESS_MESSAGES,
        "TAG_MEMBERS": TAG_MEMBERS,
        "COMMUNITY_MEMBERS": COMMUNITY_MEMBERS,
        "COMMUNITY_MESSAGES": COMMUNITY_MESSAGES,
        "OVERVIEW_TOPICS": OVERVIEW_TOPICS,
        "OVERVIEW_COMMUNITIES": OVERVIEW_COMMUNITIES,
        "OVERVIEW_TAGS": OVERVIEW_TAGS,
        "OVERVIEW_TOPIC_CIRCLE": OVERVIEW_TOPIC_CIRCLE,
        "OVERVIEW_TAG_TOPIC": OVERVIEW_TAG_TOPIC,
    }
)
"""Every statement in this catalogue, by name.

Written out rather than scraped off the modules, so adding a statement without
listing it here is visible in a diff — and so a test can bind each one's
parameters and run the lot against a real backend, which is the only way a
statement ever gets checked. That sweep is why the mapping holds *runnable*
things and only runnable things: every entry takes
``session.all_rows(entry, parameters_of(entry) bound)``, the one ``str``
included, and :func:`parameters_of` answers for either kind.

**Two names left the mapping, and it is worth saying which and why.**
``CREATE_VECTOR_INDEX`` and ``DROP_VECTOR_INDEX`` are functions now, not
statements: runic 0.5 emits vector-index DDL through ``IndexOperations`` rather
than as Cypher a caller can hold. They are still exported from this module,
still named the way every call site spells them, and they were already excluded
from the bind-and-run sweep — running a drop in it would take away the index
the KNN statements in the same sweep need. Keeping a callable in a mapping of
statements would have cost the mapping its one useful property to preserve the
appearance of completeness.
"""

__all__ = [
    "ACCOUNT_ADDRESSES",
    "ADDRESS_BETWEENNESS",
    "ADDRESS_MESSAGES",
    "ADDRESS_NEIGHBOURS",
    "ARCHIVED_PER_DAY",
    "CATALOG",
    "CLEAR_ADDRESS_RANKS",
    "CLEAR_EMBEDDINGS",
    "CLEAR_IMPORTANCE",
    "COMMUNITY_MEMBERS",
    "COMMUNITY_MESSAGES",
    "COUNT_COMMUNITIES",
    "COUNT_CO_ADDRESSED",
    "COUNT_GROUPS",
    "COUNT_MESSAGES",
    "COUNT_NEEDING_EMBEDDING",
    "COUNT_TEMPLATES",
    "COUNT_TOPICS",
    "COUNT_UNIDENTIFIED",
    "CO_RECIPIENTS",
    "CREATE_VECTOR_INDEX",
    "DELETE_COMMUNITIES",
    "DELETE_CO_ADDRESSED",
    "DELETE_GROUPS",
    "DELETE_SUGGESTED",
    "DELETE_TEMPLATES",
    "DELETE_TOPICS",
    "DROP_VECTOR_INDEX",
    "FULLTEXT_MESSAGES",
    "GROUPS_OF_MESSAGES",
    "LABEL_PROPAGATION",
    "MERGE_ABOUT",
    "MERGE_ADDRESSED_GROUP",
    "MERGE_COMMUNITIES",
    "MERGE_CO_ADDRESSED",
    "MERGE_GROUPS",
    "MERGE_INSTANCE_OF",
    "MERGE_IN_CIRCLE",
    "MERGE_MEMBER_OF",
    "MERGE_SUGGESTED",
    "MERGE_TEMPLATES",
    "MERGE_TOPICS",
    "MERGE_TOPIC_KEYWORDS",
    "MESSAGES_NEEDING_EMBEDDING",
    "MESSAGE_ADDRESSES",
    "MESSAGE_BODIES",
    "MESSAGE_CIRCLE",
    "MESSAGE_PAGERANK",
    "MESSAGE_PROPERTIES",
    "MESSAGE_RELATIONS",
    "MESSAGE_REPLIES",
    "MESSAGE_SIGNALS",
    "MESSAGE_TAGS",
    "MESSAGE_TEXTS",
    "MESSAGE_TOPICS",
    "NEIGHBOURHOOD",
    "OVERVIEW_COMMUNITIES",
    "OVERVIEW_TAGS",
    "OVERVIEW_TAG_TOPIC",
    "OVERVIEW_TOPICS",
    "OVERVIEW_TOPIC_CIRCLE",
    "PROCEDURES",
    "RECURRING_GROUPS",
    "REPLY_CHAIN",
    "SEMANTIC_NEIGHBOURS",
    "SEMANTIC_TOPIC_PAIRS",
    "SHORTEST_PATHS",
    "SUGGESTION_COUNTS",
    "TAGGED_MEMBERSHIP",
    "TAG_MEMBERS",
    "TAG_SUGGESTIONS",
    "THREAD_SIBLINGS",
    "TOPICS_OF_MESSAGES",
    "TOPIC_BREAKDOWN",
    "TOPIC_KEYWORDS",
    "TOPIC_MEMBERS",
    "TOPIC_PARTICIPANTS",
    "TOP_COMMUNITIES",
    "TOP_CO_ADDRESSED",
    "TOP_IMPORTANT",
    "TOP_TEMPLATES",
    "VECTOR_COVERAGE",
    "VECTOR_INDEX_OPTIONS",
    "WRITE_ADDRESS_RANKS",
    "WRITE_EMBEDDINGS",
    "WRITE_IMPORTANCE",
    "Statement",
    "as_graph_datetime",
    "parameters_of",
]
"""This module's surface — every statement, and the six things that are not one.

Written out because ruff refuses an ``__all__`` that is computed, and it would
otherwise be derived from :data:`CATALOG`, which is what it is: the same
statement names, plus :func:`CREATE_VECTOR_INDEX` and
:func:`DROP_VECTOR_INDEX` — which are functions now, not statements — plus
:data:`CATALOG` itself, :data:`Statement`, :func:`parameters_of` and
:func:`as_graph_datetime`. Two hand-written lists in one file is one too many,
so the relationship between them is exact and a test can check it: the surface
minus those six names is ``set(CATALOG)``, always.
"""


_PARAMETER = re.compile(r"\$([a-z_][a-z0-9_]*)")
"""``$name`` in raw Cypher.

All that is left of the regex the whole catalogue was read with, and it has
seven customers now rather than one: :data:`VECTOR_INDEX_OPTIONS` and the six
procedure calls, which are the entries that are strings. A builder statement
declares its parameters and hands them back through ``parameter_names()``, so
reading them off the compiled text would be both slower and less true — an
auto-bound literal is a ``$p0`` in the text and is *not* caller input. What the
regex reads instead is text nobody interpolated into: the AST test proves each
of those seven is a plain literal, so a ``$name`` in one was written there by
the statement's author and is exactly the caller-supplied set.
"""


def parameters_of(statement: Statement) -> tuple[str, ...]:
    """The parameter names *statement* binds, sorted and deduplicated.

    Read off the statement instead of maintained beside it: a hand-written list
    is a second copy of the truth and drifts the first time a statement gains a
    ``LIMIT``. It exists so a test can bind every entry in :data:`CATALOG` and
    run the lot, which is the only thing that ever checks a statement against
    the backend it was written for — so it has to answer for both kinds of
    entry, and it does.

    A builder statement is asked: ``parameter_names()`` returns what the
    statement *declared*, which is exactly the caller-supplied set. Values the
    statement fixed about itself are auto-bound as ``$p0``, ``$p1`` and are
    correctly absent — binding one is neither possible nor needed, and a
    ``$p``-name in the compiled text is not a hole in the boundary. The result
    is re-sorted here rather than trusted to the builder, because "sorted and
    deduplicated" is this function's contract and not runic's.

    Raw Cypher is read with :data:`_PARAMETER`, the way every entry used to be.
    """
    if isinstance(statement, str):
        return tuple(sorted(set(_PARAMETER.findall(statement))))
    return tuple(sorted(set(statement.parameter_names())))


def as_graph_datetime(value: datetime | None) -> str | None:
    """A timestamp for a ``$rows`` key that :func:`runic.ogm.encode_rows` will
    not convert.

    **Its job is narrower than it was, and nothing in this catalogue needs it
    any more.** It used to be mandatory: a statement run as raw Cypher got none
    of runic's converters, so a ``datetime`` anywhere in a ``$rows`` entry
    reached the driver as an object it had no encoding for. That gap is now
    ``encode_rows(Model, rows)``, which applies the model's own converters
    across the payload — and measured field by field, all seven ``MERGE_``
    statements are covered by it. ``Group``, ``Topic``, ``Template`` **and**
    ``CoAddressed`` each declare ``first_seen`` and ``last_seen``, so every date
    the merges carry sits on a declared field; ``AddressedGroup``, ``About`` and
    ``InstanceOf`` carry no date at all.

    What is left is the case ``encode_rows`` cannot reach: a ``datetime`` under
    a key the model does **not** declare is passed through untouched, and the
    store then refuses the whole payload with ``ResponseError: Failed to parse
    query parameter 'rows' value``. This function is what a caller reaches for
    there.

    **It has no caller left in this repository**, and that is the end of the
    move its previous docstring described rather than a sign it was forgotten.
    The three analyses in :mod:`mailarc_analytics.derived` used to call it while
    they built their rows by hand; they hand the ``datetime`` over as it is now
    and :func:`~mailarc_analytics.derived.writes.merge_rows` encodes each batch
    under the model it describes. Measured on all seven merges: the payload is
    byte-identical either way, because encoding an already-converted value is a
    no-op — which is what let those call sites move one at a time. Kept because
    the case above is real and a caller building a ``$rows`` entry the model
    does not describe has nowhere else to turn; delete it the day nothing can
    be in that position.

    It lives beside the statements rather than beside the analyses because it
    is a property of what a ``$rows`` payload may contain, not of anything the
    analyses compute.
    """
    return None if value is None else value.isoformat()
