"""Every tool's happy path, against a real graph with real mail in it.

The stubs in ``test_mcp_server.py`` prove the contract: names, schemas, limits,
and what a failure reads like. They cannot prove that a tool's answer is the
archive's answer — a stub returns whatever the test handed it, so a projection
that dropped a field, a statement that never ran, or a conversation read that
does not compile would all pass. That is what this module is for: one vendored
FalkorDB, six real messages written through the real
:class:`~mailarc_core.archive.writer.MessageArchiver`, one real rebuild of the
derived layer, and then the tools called over the wire.

Skipped unless ``task tauri:vendor`` has produced the runtime, so a fresh
checkout still runs green. The server is session-scoped and torn down
explicitly: a per-test server would leave a ``redis-server`` per test to be
reaped at interpreter exit, which turns a suite that runs in seconds into one
that hangs for minutes after the last assertion.

The corpus is deliberately tiny and every message is here for one assertion:

===== ========================================== ==============================
key   what it is                                 proves
===== ========================================== ==============================
p1-p3 one conversation, three people, one thread thread, topics, co_recipients,
                                                 topic_messages
s1-s3 the same text sent three times to one      templates (direction ``sent``),
      partner                                    tags and tagged_messages
===== ========================================== ==============================

The rebuild scores every message it sees, so ``important_messages`` needs no
corpus of its own; the annotation layer does need one thing the import cannot
write, and ``tagged`` puts one tag on two of the reports through the real
:class:`~mailarc_core.archive.tags.TagStore`.
"""

import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.tools.base import TextContent
from runic.ogm import Session

from mailarc_analytics import AnalyticsConfig, AnalyticsReader, rebuild_derived
from mailarc_analytics.semantic import SemanticConfig, SemanticSearch
from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource, TagOrigin, TagSource
from mailarc_core.archive.reader import ArchiveReader
from mailarc_core.archive.tags import TagStore
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph.client import session as graph_session
from mailarc_core.graph.config import GraphConfig
from mailarc_core.graph.model import GraphServerMode
from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR
from mailarc_core.graph.server import FalkorDBServer
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message
from mailarc_mcp.server.reads import ArchiveAccess
from mailarc_mcp.server.server import build_server

pytestmark = pytest.mark.graph_local

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()

VERSION = "1.0.0"
"""What a client would see in ``serverInfo``. A literal for the reason
``test_mcp_server.py`` gives: the version belongs to the application, and this
component may not know which one installed it."""

ACCOUNT_ID = "1"
OWN = "jens@nordlicht.example"
ANNA = "anna@kunde.example"
BOB = "bob@kunde.example"
CARL = "carl@partner.example"

MARCH = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

REPORT = """\
Hallo Carl,

hier der Monatsbericht zum Hafenprojekt. Die Migration der Bestandsdaten
laeuft nach Plan, die Abnahme durch den Fachbereich ist terminiert und die
Testfaelle liegen vor. Offene Punkte sind unveraendert die Anbindung des
Rechnungssystems und die Schulung der Sachbearbeitung. Ich melde mich, sobald
die Rueckmeldung der Fachabteilung vorliegt.
"""
"""The recurring text. Identical in all three copies on purpose: identical
fingerprints are joined whatever the comparison budget allows, so the template
this corpus is measured on cannot fail for a reason that has nothing to do with
the tools."""


def _free_port() -> int:
    """A port nothing is listening on, in a range no other suite claims.

    6500-6600 and 6600-6700 belong to ``mailarc-core``'s tests and 6700-6800 to
    ``mailarc-analytics``'s; a repository-wide run starts all of them, and two
    session-scoped servers agreeing on a number before either has bound it is a
    failure that only shows up under ``-n auto``.
    """
    for candidate in range(6800, 6850):
        with socket.socket() as probe:
            probe.settimeout(0.05)
            if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                return candidate
    raise RuntimeError("no free port in 6800-6850 for the test FalkorDB")


def _mail(
    *,
    key: str,
    sender: str,
    to: tuple[str, ...],
    subject: str,
    body: str,
    sent: datetime,
    cc: tuple[str, ...] = (),
    reply_to: str | None = None,
) -> bytes:
    """One message as the bytes a provider would have handed over.

    Real RFC 5322 rather than a hand-built value, because every field the tools
    end up showing — the subject, the preview, the fingerprint the templates
    are found by — is computed by the real parser on the way in.
    """
    message = EmailMessage()
    message["Message-ID"] = f"<{key}@nordlicht.example>"
    message["From"] = sender
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = format_datetime(sent)
    if reply_to:
        message["In-Reply-To"] = f"<{reply_to}@nordlicht.example>"
        message["References"] = f"<{reply_to}@nordlicht.example>"
    message.set_content(body)
    return message.as_bytes()


def _corpus() -> tuple[tuple[str, bytes, str | None], ...]:
    """Six messages: a thread of three, and a text sent three times."""
    offer = "Angebot 4711 fuer das Hafenprojekt"
    return (
        (
            "p1",
            _mail(
                key="p1",
                sender=ANNA,
                to=(OWN,),
                cc=(BOB,),
                subject=offer,
                body="Hallo Jens,\n\nanbei unser Angebot fuer das Hafenprojekt.\n",
                sent=MARCH,
            ),
            "T1",
        ),
        (
            "p2",
            _mail(
                key="p2",
                sender=OWN,
                to=(ANNA,),
                cc=(BOB,),
                subject=f"Re: {offer}",
                body="Hallo Anna,\n\nvielen Dank, wir pruefen das Angebot.\n",
                sent=MARCH + timedelta(hours=2),
                reply_to="p1",
            ),
            "T1",
        ),
        (
            "p3",
            _mail(
                key="p3",
                sender=ANNA,
                to=(OWN,),
                cc=(BOB,),
                subject=f"Re: {offer}",
                body="Hallo Jens,\n\ngern, melden Sie sich bei Rueckfragen.\n",
                sent=MARCH + timedelta(days=1),
                reply_to="p1",
            ),
            "T1",
        ),
        *(
            (
                f"s{index}",
                _mail(
                    key=f"s{index}",
                    sender=OWN,
                    to=(CARL,),
                    subject=f"Monatsbericht {month}",
                    body=REPORT,
                    sent=MARCH + timedelta(days=30 * index),
                ),
                None,
            )
            for index, month in enumerate(("April", "Mai", "Juni"), start=1)
        ),
    )


def _source(key: str, thread: str | None) -> ArchiveSource:
    """Where this message came from — one account, so "sent by me" resolves.

    Without an ``Account`` node carrying :data:`OWN`, every template would be
    classified ``received`` and the direction split under test would be
    vacuously true.
    """
    return ArchiveSource(
        account_id=ACCOUNT_ID,
        account_address=OWN,
        provider=MailProvider.GMAIL,
        provider_message_id=f"g-{key}",
        provider_thread_id=thread,
        folder="INBOX",
        labels=(
            (LabelInfo(provider_label_id="L1", name="Kunden", kind=LabelKind.USER),)
            if key == "p1"
            else ()
        ),
    )


def failure_text(answer: CallToolResult) -> str:
    """The sentence a caller reads off a failed call.

    A copy of the helper in ``test_mcp_server.py`` rather than an import: a
    test module that imports another test module ties the two together for six
    lines, and pytest's collection order is not a dependency worth taking on.
    """
    block = answer.content[0]
    assert isinstance(block, TextContent)
    return block.text


class LocalAccess(ArchiveAccess):
    """The real readers, pointed at the graph this module planted.

    Only the *wiring* differs from what ``app/mcp_server.py`` hands to
    :class:`~mailarc_mcp.server.reads.ArchiveAccess` in production — the
    readers, the statements and the conversation read are the application's
    own, which is the whole point of coming this far for a test.

    Five factories and no overridden accessor, deliberately: this is the one
    place the base class's own storing-and-asking is exercised against a real
    graph, and a subclass that answered from its own methods would leave those
    five lines untested against anything but a stub.
    """

    def __init__(self, config: GraphConfig, store: Path) -> None:
        self._config = config
        self._store = store
        super().__init__(
            graph_session=self._open,
            analytics=self._analytics_reader,
            archive=self._archive_reader,
            search=self._semantic_search,
            tags=self._tag_store,
        )

    def _open(self) -> AbstractContextManager[Session]:
        return graph_session(self._config)

    def _analytics_reader(self) -> AnalyticsReader:
        return AnalyticsReader(graph_session=self._open)

    def _archive_reader(self) -> ArchiveReader:
        return ArchiveReader(
            graph_session=self._open,
            blobs=BlobStore(ArchiveConfig(store_dir=self._store)),
        )

    def _tag_store(self) -> TagStore:
        return TagStore(graph_session=self._open)

    def _semantic_search(self) -> SemanticSearch:
        return SemanticSearch(
            graph_session=self._open, config=SemanticConfig(), embedder=None
        )


@pytest.fixture(scope="module")
def endpoint(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GraphConfig]:
    """One vendored server for this module, stopped explicitly at the end."""
    if not (RUNTIME_DIR / "falkordb.so").is_file():
        pytest.skip(f"vendored FalkorDB runtime not present at {RUNTIME_DIR}")
    config = GraphConfig(
        mode=GraphServerMode.LOCAL,
        host="127.0.0.1",
        port=_free_port(),
        graph_name="mcp-probe",
        data_dir=tmp_path_factory.mktemp("mcp-falkordb"),
        runtime_dir=RUNTIME_DIR,
        startup_timeout=30.0,
    )
    server = FalkorDBServer(config)
    server.start()
    try:
        yield config
    finally:
        server.stop()


@pytest.fixture(scope="module")
def archived(
    endpoint: GraphConfig, tmp_path_factory: pytest.TempPathFactory
) -> LocalAccess:
    """The corpus, imported and then derived — the state a user's archive is in.

    Both halves are needed and they are different halves: the import writes the
    ground truth every read here walks, and the rebuild writes the derived
    nodes half the tools answer from — topics, templates, correspondent pairs
    and the importance score. A test module that only imported would find the
    derived tools raising "nothing has been derived yet", which is a correct
    answer to the wrong question.

    Module-scoped because every test below is a read: one graph and one
    rebuild for all ten tools rather than one apiece. The one thing written
    afterwards is a tag, which ``tagged`` puts on through the real store —
    nothing on this server can.
    """
    store = tmp_path_factory.mktemp("mcp-mailstore")
    archiver = MessageArchiver(ArchiveConfig(store_dir=store))
    with graph_session(endpoint) as graph:
        for key, raw, thread in _corpus():
            archiver.archive(graph, parse_message(raw), _source(key, thread))
        # The baseline migration creates this index; a graph planted by hand
        # has no migrations, and `db.idx.fulltext.queryNodes` against a label
        # without one returns *no rows* rather than raising — the exact silent
        # emptiness the search path refuses to pass on. Creating it here is
        # what makes the full-text assertion below about the tool.
        graph.execute(
            "CALL db.idx.fulltext.createNodeIndex('Message', 'subject', 'body_text')"
        )
    with graph_session(endpoint) as graph:
        rebuild_derived(graph, AnalyticsConfig())
    return LocalAccess(endpoint, store)


@pytest.fixture
async def client(archived: LocalAccess) -> AsyncIterator[Client]:
    """A client over the real tools, reading the planted archive."""
    async with Client(build_server(archived, version=VERSION)) as connected:
        yield connected


async def _message_id(client: Client, subject: str) -> str:
    """The archive's own id for a planted message, the way a model finds it.

    Through ``timeline`` rather than computed with
    :func:`~mailarc_core.mail.identity.canonical_id`: a caller of this server
    has no other way to get an id, so taking the same route proves the two
    tools agree about what an id is.
    """
    entries = (await client.call_tool("timeline", {"limit": 50})).data
    return next(one.message_id for one in entries if one.subject == subject)


async def test_the_timeline_reads_the_archive_newest_first(client: Client) -> None:
    """Six planted messages, in the order a person would page through them."""
    answer = await client.call_tool("timeline", {"limit": 50})

    assert len(answer.data) == 6
    dates = [one.sent_at for one in answer.data]
    assert dates == sorted(dates, reverse=True)
    labelled = [one for one in answer.data if one.labels]
    assert labelled[0].labels == ["Kunden"]
    assert labelled[0].sender == ANNA
    assert "Angebot" in labelled[0].preview


async def test_the_timeline_pages(client: Client) -> None:
    """``offset`` skips the newest, so a model can walk backwards in time."""
    first = (await client.call_tool("timeline", {"limit": 2})).data
    second = (await client.call_tool("timeline", {"limit": 2, "offset": 2})).data

    assert {one.message_id for one in first}.isdisjoint(
        one.message_id for one in second
    )


async def test_full_text_search_finds_the_planted_words(client: Client) -> None:
    """The path that needs no embedder, over the index the baseline creates."""
    answer = await client.call_tool("search_messages", {"query": "Hafenprojekt"})

    assert answer.is_error is False
    assert answer.data.kind == "fulltext"
    assert len(answer.data.hits) >= 3
    assert all(0.0 <= one.score <= 1.0 for one in answer.data.hits)
    assert answer.data.notice == ""


async def test_search_operators_never_reach_the_store(client: Client) -> None:
    """A model may write ``-Hafenprojekt`` meaning "not this".

    RediSearch would read the leading minus as a negation and answer with
    everything *else*. The words are tokenised before the archive sees them, so
    the query narrows rather than inverts — and a query with nothing but
    operators in it is an error, never "no matches".
    """
    negated = (
        await client.call_tool("search_messages", {"query": "-Hafenprojekt"})
    ).data
    assert {one.message_id for one in negated.hits} == {
        one.message_id
        for one in (
            await client.call_tool("search_messages", {"query": "Hafenprojekt"})
        ).data.hits
    }

    empty = await client.call_tool(
        "search_messages", {"query": "@@@ ---"}, raise_on_error=False
    )
    assert empty.is_error is True
    assert "no searchable words" in failure_text(empty)


async def test_a_search_that_matches_nothing_answers_with_no_hits(
    client: Client,
) -> None:
    """The one empty answer this server is allowed to give: it really is empty.

    Everything else that could produce an empty list — no embedder, no index,
    nothing derived — raises instead, which is what makes this list readable as
    "your archive holds nothing about that".
    """
    answer = await client.call_tool("search_messages", {"query": "Trampolin"})

    assert answer.is_error is False
    assert answer.data.hits == []


async def test_semantic_search_over_a_real_archive_still_refuses(
    client: Client,
) -> None:
    """A graph full of messages and no embedder is still an error, not a blank.

    Worth asserting against a *populated* archive: the tempting shortcut is a
    KNN that returns nothing because no message carries a vector, and that
    would look exactly like a working search over an archive with nothing to
    say.
    """
    answer = await client.call_tool(
        "search_messages",
        {"query": "Hafenprojekt", "semantic": True},
        raise_on_error=False,
    )

    assert answer.is_error is True
    assert "no embedder is configured" in failure_text(answer)


async def test_reading_the_planted_conversation(client: Client) -> None:
    """The thread tool over the provider's own grouping, oldest first."""
    anchor = await _message_id(client, "Angebot 4711 fuer das Hafenprojekt")

    answer = await client.call_tool("thread", {"message_id": anchor})

    assert answer.data.thread_id == f"{ACCOUNT_ID}:T1"
    assert len(answer.data.messages) == 3
    assert answer.data.truncated is False
    dates = [one.sent_at for one in answer.data.messages]
    assert dates == sorted(dates)
    assert answer.data.messages[0].message_id == anchor
    assert answer.data.messages[0].sender == ANNA
    assert answer.data.messages[1].sender == OWN
    assert "Angebot" in answer.data.messages[0].preview


async def test_a_conversation_says_when_it_was_cut_short(client: Client) -> None:
    """``truncated`` is the difference between a page and a whole exchange."""
    anchor = await _message_id(client, "Angebot 4711 fuer das Hafenprojekt")

    answer = await client.call_tool("thread", {"message_id": anchor, "limit": 2})

    assert len(answer.data.messages) == 2
    assert answer.data.truncated is True


async def test_a_message_in_no_thread_is_a_conversation_of_one(
    client: Client,
) -> None:
    """Not every mail is an exchange, and that is an answer rather than a fault.

    The three reports were planted without a provider thread id, which is what
    IMAP and a first mail nobody answered both look like.
    """
    anchor = await _message_id(client, "Monatsbericht Mai")

    answer = await client.call_tool("thread", {"message_id": anchor})

    assert answer.data.thread_id == ""
    assert [one.message_id for one in answer.data.messages] == [anchor]
    assert answer.data.messages[0].sender == OWN


async def test_co_recipients_counts_the_planted_pairs(client: Client) -> None:
    """A1 off the derived edge: Bob was copied on all three offer messages."""
    answer = await client.call_tool("co_recipients", {})

    pairs = {
        frozenset((one.address_a, one.address_b)): one.messages_together
        for one in answer.data
    }
    assert pairs[frozenset((BOB, OWN))] == 2
    assert pairs[frozenset((ANNA, BOB))] == 1
    assert frozenset((CARL, OWN)) not in pairs, "one recipient makes no pair"


async def test_topics_report_the_signal_that_drew_them(client: Client) -> None:
    """Three messages, one conversation — and every signal here is a fact.

    The same topic comes back once per signal that joined it, which is why
    ``joined_by`` is a column and not a footnote.

    ``conversation`` is in the set since phase 2 gave A2 its seventh signal,
    and it displaces ``thread`` here rather than joining it: the union-find
    behind a conversation runs over the provider's thread id *and* the
    ``In-Reply-To`` chain, so every pair a thread joins a conversation joins as
    well — at a weight of 0.9 against the thread's 0.8, because "these messages
    answer each other" is a stronger statement than "one provider filed them
    together". A method is the strongest signal that joined the pair, so the
    stronger one wins.
    """
    answer = await client.call_tool("topics", {})

    assert answer.data
    assert {one.joined_by for one in answer.data} <= {
        "conversation",
        "thread",
        "subject",
    }
    assert all(one.is_suggestion is False for one in answer.data)
    assert max(one.messages for one in answer.data) == 3


async def test_templates_find_the_text_that_was_sent_three_times(
    client: Client,
) -> None:
    """A3, and the direction split that makes it useful.

    The report was written by the archive's owner, so it is automatable and
    lands under ``sent``; nothing was received three times, and asking for the
    other direction says so instead of returning the same rows.
    """
    answer = await client.call_tool("templates", {"direction": "sent"})

    assert len(answer.data) == 1
    assert answer.data[0].occurrences == 3
    assert answer.data[0].direction == "sent"
    assert "Monatsbericht" in answer.data[0].sample_text

    received = await client.call_tool(
        "templates", {"direction": "received"}, raise_on_error=False
    )
    assert received.is_error is False
    assert received.data == []


@pytest.fixture(scope="module")
def tagged(archived: LocalAccess) -> str:
    """One tag on two planted messages, written the way the pages write it.

    Through the real :class:`~mailarc_core.archive.tags.TagStore` rather than a
    hand-written ``MERGE``: the annotation layer is ground truth's neighbour
    and the tool has to read what the application actually stores, edge
    properties included.

    Module-scoped like the corpus, because it is written once and only read
    afterwards — no tool on this server can change it.
    """
    store = archived.tags()
    summary = store.create("NORD-42", origin=TagOrigin.TOPIC)
    members = [
        one.id
        for one in archived.archive().list_messages(limit=50)
        if one.subject.startswith("Monatsbericht")
    ]
    store.tag_messages(summary.id, members[:2], source=TagSource.ACCEPTED)
    return summary.id


async def test_important_messages_scores_the_planted_corpus(client: Client) -> None:
    """B2 over a real rebuild: every message is scored, best first, with the
    reasons that produced the number — the archive's own arithmetic, not an
    opinion, which is the only reason it may be shown to a user."""
    answer = await client.call_tool("important_messages", {})

    assert answer.is_error is False
    assert len(answer.data) == 6
    scores = [one.importance for one in answer.data]
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= one <= 1.0 for one in scores)
    assert any(one.reasons for one in answer.data), "a score with no term behind it"
    assert all(one.subject for one in answer.data)


async def test_topic_messages_reads_one_topics_members(client: Client) -> None:
    """The §3.2 path: a model reads a topic's mail and summarises it itself.

    The id comes from the ``topics`` tool in the same session, which is the
    only place a caller can get one — and the reason the two are asserted
    together rather than against a computed digest.
    """
    topics = (await client.call_tool("topics", {})).data
    biggest = max(topics, key=lambda one: one.messages)

    answer = await client.call_tool("topic_messages", {"topic_id": biggest.topic_id})

    assert answer.data.topic_id == biggest.topic_id
    assert answer.data.label
    assert len(answer.data.messages) == 3, "one row per member, not per signal"
    assert answer.data.truncated is False
    scores = [one.importance for one in answer.data.messages]
    assert scores == sorted(scores, reverse=True)
    assert all(one.preview for one in answer.data.messages)
    assert {one.sender for one in answer.data.messages} == {ANNA, OWN}


async def test_a_cut_topic_says_it_was_cut(client: Client) -> None:
    """Read off the topic's own count, so a member listed once by two signals
    cannot make a whole topic look truncated or a cut one look complete."""
    topics = (await client.call_tool("topics", {})).data
    biggest = max(topics, key=lambda one: one.messages)

    answer = await client.call_tool(
        "topic_messages", {"topic_id": biggest.topic_id, "limit": 1}
    )

    assert len(answer.data.messages) == 1
    assert answer.data.truncated is True


async def test_a_topic_id_from_an_earlier_rebuild_is_explained(
    client: Client,
) -> None:
    """R7 as a caller meets it: the id is a digest of the members, so one from
    yesterday names nothing today and "no such topic" would be misleading."""
    answer = await client.call_tool(
        "topic_messages", {"topic_id": "topic:gone"}, raise_on_error=False
    )

    assert answer.is_error is True
    assert "recomputed" in failure_text(answer)


async def test_the_tags_a_person_made_and_the_mail_under_them(
    client: Client, tagged: str
) -> None:
    """B1 end to end: the annotation layer written through the store, read back
    through the two tools, and hydrated by the reader that shows a message."""
    listed = (await client.call_tool("tags", {})).data

    assert [one.tag_id for one in listed] == [tagged]
    assert listed[0].name == "NORD-42"
    assert listed[0].origin == "topic"
    assert listed[0].messages == 2

    answer = await client.call_tool("tagged_messages", {"tag": tagged})

    assert len(answer.data) == 2
    assert all(one.subject.startswith("Monatsbericht") for one in answer.data)
    assert all(one.sender == OWN for one in answer.data)
    assert all(one.preview for one in answer.data)
    dates = [one.sent_at for one in answer.data]
    assert dates == sorted(dates, reverse=True), "newest first"


async def test_an_unknown_tag_id_is_refused(client: Client, tagged: str) -> None:
    """A tag id is permanent, so an unknown one is a typo — and a name is the
    typo a model makes, which is answered with the id it belongs to."""
    unknown = await client.call_tool(
        "tagged_messages", {"tag": "tag:nope"}, raise_on_error=False
    )
    assert unknown.is_error is True
    assert "tag:<slug>" in failure_text(unknown)

    by_name = await client.call_tool(
        "tagged_messages", {"tag": "NORD-42"}, raise_on_error=False
    )
    assert by_name.is_error is True
    assert tagged in failure_text(by_name)
