"""Everything the search page *reads*, and nothing that holds a state lock.

The seam :mod:`mailarc_ui.search.reads` describes, one layer further out. That
module is where a service is looked up; this one is where a service is *asked*,
and the difference is what the two of them together keep out of
:mod:`mailarc_ui.search.state`: every function here is module-level, writes no
var, takes no lock, and is callable from a test with no Reflex state at all.

What crosses back is one frozen
:class:`~mailarc_ui.search.model.SearchAnswer` per read rather than six
assignments a later edit could leave half-applied — which is the reason the
state's handlers are background handlers, and the reason none of them may await
anything while holding the lock.

Two rules the failures here follow, and both are deliberate.

**A configuration state is a sentence, never an empty list.** ``notice`` is a
statement about the question or the installation and is shown as written;
``error`` is a fault and never says whose. An empty result list is a valid
answer to a search, and a reader who meets one concludes their archive holds
nothing on the subject — so :func:`_answer` turns the two error taxonomies that
mean "this cannot be asked" into notices and everything else into
:data:`~mailarc_ui.search.model.SEARCH_FAILED`.

**A read that only adds completeness swallows its own failure.**
:func:`_whole_conversations` and :func:`_memberships_of` return an empty
mapping rather than raising: a list of the messages that did match is a worse
answer than a whole one and a far better one than a fault.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from mailarc_analytics.nlq import (
    CONFIGURE_LLM,
    NO_LLM,
    NlqError,
    ResolvedQuery,
    Vocabulary,
)
from mailarc_analytics.semantic import (
    NO_EMBEDDER,
    SearchKind,
    SearchRequest,
    SemanticError,
)
from mailarc_core.archive.search import (
    AddressDirectory,
    KnownAddresses,
    KnownAttachments,
    SearchFilters,
)
from mailarc_ui.search import reads
from mailarc_ui.search.memberships import read_memberships
from mailarc_ui.search.model import (
    MODE_SEMANTIC,
    SEARCH_FAILED,
    Grouping,
    Membership,
    ResultRow,
    SearchAnswer,
    partial_conversations,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
"""How many rows one read brings in. The list scrolls; "more" appends a page."""

SEMANTIC_HITS = PAGE_SIZE
"""How deep the KNN goes. One ranked answer, not a paged one: a nearest-
neighbour search takes a ``k`` and no offset, so asking again for "the next
fifty" would mean re-ranking the first fifty to throw them away."""


def _semantic_offer() -> tuple[bool, str]:
    """Whether the semantic segment can be offered, and why not when it cannot.

    Deliberately touches no graph. Both questions — is there a service, does
    it have an embedder — are answered in memory, so the page can say
    "semantic search is off" on an installation whose graph is not even
    running. The sentence is
    :data:`~mailarc_analytics.semantic.errors.NO_EMBEDDER`, which names the
    setting to change.
    """
    try:
        search = reads.semantic_search()
    except RuntimeError as error:
        logger.warning("The search page found no search service")
        return False, str(error)
    return search.available, "" if search.available else NO_EMBEDDER


def _llm_offer() -> tuple[bool, str]:
    """Whether the natural segment can be offered, and why not when it cannot.

    :func:`_semantic_offer`'s twin, in memory for the same reason: it is asked
    on every page load, and an answer that needed an HTTP round trip would make
    the mode selector wait for a model server that may not be running.

    The remedy is spelled out here where the embedder's is not, and the
    difference is that the embedder has a settings page to send a reader to.
    This one does not yet, so a sentence naming only a variable would be a
    remedy a desktop reader could not act on — see
    :data:`~mailarc_analytics.nlq.errors.CONFIGURE_LLM`.
    """
    try:
        interpreter = reads.query_interpreter()
    except RuntimeError as error:
        logger.warning("The search page found no query interpreter")
        return False, str(error)
    if interpreter.available:
        return True, ""
    return False, f"{NO_LLM} {CONFIGURE_LLM}"


async def _directory() -> AddressDirectory:
    """Every address with the names it signs itself, or an empty directory.

    The bargain :func:`_addresses` strikes. Losing this costs the natural
    segment its grounding — a question naming a company would resolve to
    nothing and be reported as unresolved — and must not cost the listing
    beside it. Blocking, so it goes to a thread.
    """
    try:
        return await asyncio.to_thread(reads.archive_reader().address_directory)
    except Exception:
        logger.exception("Could not read the address directory")
        return AddressDirectory()


async def _accounts() -> list[dict[str, str]]:
    """The account picker's options, or an empty picker.

    A failure here is not a failure of the page: the account filter is one
    optional field, and losing the database must not cost the listing beside
    it. Logged with its traceback, shown as nothing.
    """
    try:
        return await reads.account_options()
    except Exception:
        logger.exception("Could not read the accounts for the search picker")
        return []


async def _addresses() -> KnownAddresses:
    """What the two address pickers offer, or two empty pickers.

    The same bargain :func:`_accounts` strikes, for the same reason: the
    address filters are optional fields, and a graph that cannot answer them
    must not cost the listing beside them. Blocking, so it goes to a thread.
    """
    try:
        return await asyncio.to_thread(reads.archive_reader().known_addresses)
    except Exception:
        logger.exception("Could not read the addresses for the search pickers")
        return KnownAddresses()


async def _attachment_catalogue() -> KnownAttachments:
    """What the file picker offers, or an empty picker.

    The bargain :func:`_addresses` strikes, for the same reason: the file
    filter is one optional field, and a graph that cannot answer it must not
    cost the listing beside it. Blocking, so it goes to a thread.

    Both halves or neither: the picker is built from the two lists together,
    and half a catalogue would offer families whose files it could not then
    find.
    """
    try:
        return await asyncio.to_thread(reads.archive_reader().known_attachments)
    except Exception:
        logger.exception("Could not read the files for the search picker")
        return KnownAttachments()


async def _interpreted(
    question: str, vocabulary: Vocabulary
) -> ResolvedQuery | SearchAnswer:
    """One question read into a filter, or the answer to show instead.

    The union is the contract rather than a shortcut: on this path there is no
    third outcome. Either the question became a filter the form can be filled
    from, or nothing runs and the page shows a sentence — because the filter a
    failure would fall back to is the *empty* one, which is not "nothing
    matched" but the browse-the-whole-archive read. Answering a narrow question
    with every message in the archive is the one failure here that looks like a
    success.

    :class:`~mailarc_analytics.nlq.errors.NlqError` becomes a ``notice``: both
    of its subclasses are statements about the question or the configuration,
    which is what a notice is for and what an ``error`` — a fault that never
    says whose — is not. Anything else is a fault.
    """
    try:
        return await reads.query_interpreter().interpret(question, vocabulary)
    except (NlqError, RuntimeError) as error:
        logger.info("The archive cannot read this question: %s", error)
        return SearchAnswer(notice=str(error))
    except Exception:
        logger.exception("The question could not be interpreted")
        return SearchAnswer(error=SEARCH_FAILED)


async def _answer(
    mode: str, filters: SearchFilters, offset: int, grouping: Grouping
) -> SearchAnswer:
    """One search, with every way it can fail turned into a sentence.

    Two clauses, and they are the taxonomy this page needs. A
    :class:`~mailarc_analytics.semantic.errors.SemanticError` — or the
    ``ValueError`` the core's own sanitizer raises for the same reason — was
    written for the person reading it: it names the setting to change, or says
    the question holds no searchable words. Those are shown verbatim as a
    notice, because they are statements about the ask rather than faults.
    Anything else is a fault: the graph went away, the embedder's host refused
    the connection, a driver raised. Those are logged with a traceback and
    shown as :data:`~mailarc_ui.search.model.SEARCH_FAILED`, because a
    driver's words can name a path inside this installation.
    """
    try:
        if mode == MODE_SEMANTIC:
            return await _nearest(filters.text, grouping)
        return await asyncio.to_thread(_filtered, filters, offset, grouping)
    except (SemanticError, ValueError) as error:
        logger.info("The archive cannot answer this search: %s", error)
        return SearchAnswer(notice=str(error))
    except Exception:
        logger.exception("The archive could not be searched")
        return SearchAnswer(error=SEARCH_FAILED)


def _filtered(filters: SearchFilters, offset: int, grouping: Grouping) -> SearchAnswer:
    """One page of whatever the form asked for. Blocking — hence the thread.

    The membership read rides in the same thread as the page, so one frozen
    answer still crosses the state lock — and it is not made at all for a
    grouping that needs none. A failure in it is not one this page has to
    survive specially: it comes out of the same archive the page came out of,
    so the caller's own handler catches it and the list reports one fault
    rather than half an answer.
    """
    page = reads.archive_reader().search_messages(
        filters, limit=PAGE_SIZE, offset=offset
    )
    memberships = read_memberships([one.summary.id for one in page.hits], grouping)
    now = datetime.now(UTC)
    rows = tuple(
        ResultRow.from_summary(one.summary, now, one.relevance) for one in page.hits
    )
    return SearchAnswer(
        rows=rows,
        total=page.total or 0,
        memberships=memberships,
        whole=_whole_conversations(rows, memberships, grouping, now),
        grouping=grouping.value,
    )


def _whole_conversations(
    rows: Sequence[ResultRow],
    memberships: Mapping[str, Membership],
    grouping: Grouping,
    now: datetime,
) -> dict[str, tuple[ResultRow, ...]]:
    """The full membership of every conversation this answer cut into.

    **A thread is shown whole or not at all.** A search matches messages, and
    the half of an exchange a person wrote themselves matches almost nothing
    they type: their own replies carry neither the sender they filtered on nor,
    usually, the words they searched for. Grouping the remainder under a
    heading that says "2 of 3" is true and useless — it names a message the
    list then refuses to show. So the missing members are read here, in the
    same thread as the page, and travel with it.

    One read for the page, not one per group: see
    :meth:`~mailarc_core.ArchiveReader.conversation_members`. Nothing is read
    when the list is grouped some other way, and nothing when every group is
    already whole — the common case on an archive nobody has filtered.

    A failure is swallowed, and that is the difference between this read and
    the page's own: what it adds is completeness, and a list of the messages
    that *did* match is a worse answer than a whole one but a far better one
    than a fault. The groups then draw as they used to, chip and button and
    all.
    """
    if grouping != Grouping.CONVERSATION:
        return {}
    wanted = partial_conversations(rows, memberships)
    if not wanted:
        return {}
    try:
        found = reads.archive_reader().conversation_members(wanted)
    except Exception:
        logger.exception("Could not read the whole of %d conversations", len(wanted))
        return {}
    return {
        conversation: tuple(ResultRow.from_summary(one, now) for one in members)
        for conversation, members in found.items()
    }


async def _memberships_of(ids: list[str], grouping: Grouping) -> dict[str, Membership]:
    """The memberships of rows already on screen — what a switch needs.

    A failure leaves the list in one bucket rather than emptied: the rows are
    right, only the sections are missing, and a page that threw them away to
    report a fault would be worse than one that quietly draws what it has.
    """
    try:
        return await asyncio.to_thread(read_memberships, list(ids), grouping)
    except Exception:
        logger.exception("Could not read the %s of the result list", grouping.value)
        return {}


async def _conversation_rows(
    conversation_id: str,
) -> tuple[tuple[ResultRow, ...], str]:
    """One whole conversation as rows, or the sentence to show instead.

    The rows carry no relevance: a member the search never returned was not
    ranked, and printing a score for it would invent one.
    """
    try:
        summaries = await asyncio.to_thread(
            reads.archive_reader().conversation_messages, conversation_id
        )
    except Exception:
        logger.exception("Could not read conversation %s", conversation_id)
        return (), SEARCH_FAILED
    now = datetime.now(UTC)
    return tuple(ResultRow.from_summary(one, now) for one in summaries), ""


async def _nearest(question: str, grouping: Grouping) -> SearchAnswer:
    """The KNN's ranking, hydrated into rows in exactly that order.

    Two round trips on purpose. The embedding is HTTP and is awaited; the
    hydration is a graph read and goes to a thread. A message the ranking
    names but the graph no longer holds is left out rather than answered with
    a hole, which is :meth:`~mailarc_core.ArchiveReader.messages_by_ids`'s
    contract — so the row count can be shorter than the hit count.

    ``notice`` carries the result's coverage sentence: a semantic answer over
    a half-embedded archive is short for a reason the rows cannot show.
    """
    result = await reads.semantic_search().semantic(
        SearchRequest(text=question, kind=SearchKind.SEMANTIC, limit=SEMANTIC_HITS)
    )
    scores = {hit.message_id: hit.score for hit in result.hits}
    summaries = await asyncio.to_thread(
        reads.archive_reader().messages_by_ids, list(scores)
    )
    memberships = await asyncio.to_thread(
        read_memberships, [one.id for one in summaries], grouping
    )
    now = datetime.now(UTC)
    rows = tuple(
        ResultRow.from_summary(one, now, scores.get(one.id)) for one in summaries
    )
    whole = await asyncio.to_thread(
        _whole_conversations, rows, memberships, grouping, now
    )
    return SearchAnswer(
        rows=rows,
        total=len(rows),
        memberships=memberships,
        whole=whole,
        grouping=grouping.value,
        notice=result.notice,
    )
