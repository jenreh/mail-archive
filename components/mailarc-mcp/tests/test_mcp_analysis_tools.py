"""The four tools the analysis phase added, as a model on the wire sees them.

``important_messages`` and ``topic_messages`` answer for B2 — what matters and
what one piece of work is made of — and ``tags``/``tagged_messages`` for B1, the
annotation layer a person builds by hand. All four are read-only and none of
them lets a model write a tag: promoting a cluster is a decision somebody makes
in the application, and §3.2 keeps a model on the reading side of it.

The same stubs and the same in-memory client as ``test_mcp_server.py`` — see
``mcp_doubles.py`` for why they live in a module of their own. What is at stake
here is the same thing: a tool's name, the sentence a failure puts on the wire,
and that a caller's limit is clamped rather than refused.

``topic_messages`` is the one with a read of its own, and it is split in two.
What this module proves is what that read *makes of* rows — one member per
message however many signals joined it, the topic's own count deciding whether
the listing was cut — over a session that answers from a list. That the
statement itself orders by importance and that the backend runs it at all is
``test_mcp_server_local.py``'s, against a real graph.
"""

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

import pytest
from fastmcp import Client
from mcp_doubles import (
    AUGUST,
    MARCH,
    StubAccess,
    StubAnalytics,
    StubArchive,
    StubTags,
    client_over,
    failure_text,
    unbuilt,
)
from runic.ogm import Session

from mailarc_analytics.queries import catalog
from mailarc_analytics.queries.model import ArchiveTotals, ImportantMessageRow
from mailarc_core.archive.model import MessageSummary, TagOrigin, TagSummary
from mailarc_mcp.server.model import TopicMessage, TopicMessages
from mailarc_mcp.server.reads import ArchiveAccess
from mailarc_mcp.server.server import (
    MAX_ROWS,
    NO_MESSAGES,
    NOT_DERIVED,
    STALE_TOPIC,
    WHERE_TAGS_COME_FROM,
)

SCORED = ImportantMessageRow(
    id="m1",
    subject="Angebot 4711",
    sent_at=AUGUST,
    sender="anna@kunde.example",
    importance=0.82,
    reasons=("replied by you", "addressed directly"),
)

TAG = TagSummary(
    id="tag:nord-42",
    name="NORD-42",
    origin=TagOrigin.TOPIC,
    created_at=MARCH,
    message_count=2,
)


def summary(message_id: str, subject: str = "Angebot 4711") -> MessageSummary:
    return MessageSummary(
        id=message_id,
        sender_name="Anna Meier",
        sender_address="anna@kunde.example",
        subject=subject,
        preview="Hallo Jens, anbei das Angebot.",
        sent_at=AUGUST,
    )


@pytest.fixture
async def bare() -> AsyncIterator[Client]:
    """A client over the default stubs — enough for schema-level assertions."""
    async with client_over(StubAccess()) as connected:
        yield connected


class TestImportantMessages:
    """B2 as a model reads it: a score it can argue with, or an explanation."""

    async def test_the_reasons_travel_with_the_score(self) -> None:
        """A ranking nobody can question is what §1.1 refused to build: the
        vocabulary is the whole difference between this and an opinion."""
        analytics = StubAnalytics(important=(SCORED,))
        async with client_over(StubAccess(analytics=analytics)) as connected:
            answer = await connected.call_tool("important_messages", {"limit": 5})

        assert answer.is_error is False
        assert [one.message_id for one in answer.data] == ["m1"]
        assert answer.data[0].importance == 0.82
        assert answer.data[0].reasons == ["replied by you", "addressed directly"]
        assert answer.data[0].sent_at == AUGUST
        assert analytics.asked == [("important", 5)]

    async def test_the_limit_is_clamped_at_both_ends(self) -> None:
        """A model cannot know this archive's size and will ask for ten
        thousand; zero would render as an archive with nothing in it."""
        analytics = StubAnalytics(important=(SCORED,))
        async with client_over(StubAccess(analytics=analytics)) as connected:
            await connected.call_tool("important_messages", {"limit": 5_000})
            await connected.call_tool("important_messages", {"limit": 0})

        assert analytics.asked == [("important", MAX_ROWS), ("important", 1)]

    async def test_an_unscored_archive_is_explained_rather_than_answered_empty(
        self,
    ) -> None:
        """Every message gets a score, so nothing scored means no rebuild ran.

        Answering with an empty list would tell a reader this archive holds
        nothing worth their attention, which is a claim no analysis made.
        """
        analytics = StubAnalytics(totals=ArchiveTotals(messages=41, topics=3))
        async with client_over(StubAccess(analytics=analytics)) as connected:
            answer = await connected.call_tool(
                "important_messages", {}, raise_on_error=False
            )

        assert answer.is_error is True
        assert failure_text(answer) == NOT_DERIVED

    async def test_an_empty_archive_is_named_as_such(self) -> None:
        """The other half of the ambiguity: nothing has been imported yet."""
        async with client_over(StubAccess(analytics=StubAnalytics())) as connected:
            answer = await connected.call_tool(
                "important_messages", {}, raise_on_error=False
            )

        assert answer.is_error is True
        assert failure_text(answer) == NO_MESSAGES


class TestTopicMessages:
    """The §3.2-compliant way to have a model summarise one piece of work.

    It reads the members — subject, sender, date, preview — and writes its
    summary in its own answer. Nothing it concludes comes back into the graph.
    """

    async def test_the_members_come_back_in_the_order_the_read_gave(self) -> None:
        found = TopicMessages(
            topic_id="topic:abc",
            label="Angebot 4711",
            messages=(
                TopicMessage(
                    message_id="m2",
                    subject="Re: Angebot 4711",
                    sender="jens@nordlicht.example",
                    sent_at=AUGUST,
                    preview="Danke, passt.",
                    importance=0.9,
                ),
                TopicMessage(message_id="m1", subject="Angebot 4711", importance=0.4),
            ),
            truncated=True,
        )
        access = StubAccess(topic=found)
        async with client_over(access) as connected:
            answer = await connected.call_tool(
                "topic_messages", {"topic_id": "topic:abc", "limit": 5}
            )

        assert [one.message_id for one in answer.data.messages] == ["m2", "m1"]
        assert answer.data.messages[0].preview == "Danke, passt."
        assert answer.data.messages[0].importance == 0.9
        assert answer.data.truncated is True
        assert access.asked_topic == [("topic:abc", 5)]

    async def test_the_limit_is_clamped_at_both_ends(self) -> None:
        access = StubAccess(topic=TopicMessages(topic_id="topic:abc"))
        async with client_over(access) as connected:
            await connected.call_tool(
                "topic_messages", {"topic_id": "topic:abc", "limit": 9_000}
            )
            await connected.call_tool(
                "topic_messages", {"topic_id": "topic:abc", "limit": 0}
            )

        assert access.asked_topic == [("topic:abc", MAX_ROWS), ("topic:abc", 1)]

    async def test_a_stale_topic_id_says_topics_are_recomputed(self) -> None:
        """R7: a topic id is a hash of its members and is a different string
        after every rebuild. "No such topic" alone would read as "that work
        does not exist", which is the wrong thing to tell a reader."""
        access = StubAccess(topic=None)
        async with client_over(access) as connected:
            answer = await connected.call_tool(
                "topic_messages", {"topic_id": "topic:gone"}, raise_on_error=False
            )

        assert answer.is_error is True
        assert "'topic:gone'" in failure_text(answer)
        assert failure_text(answer).endswith(STALE_TOPIC)

    async def test_a_topic_id_reaches_the_read_exactly_as_it_arrived(self) -> None:
        """The id is a key and is bound, never concatenated — and never
        cleaned up either: an id silently altered reads the wrong topic."""
        hostile = "topic:x' OR 1=1 -- {$ne: null}"
        access = StubAccess(topic=None)
        async with client_over(access) as connected:
            await connected.call_tool(
                "topic_messages", {"topic_id": hostile}, raise_on_error=False
            )

        assert access.asked_topic == [(hostile, 20)]


class TestTags:
    """The annotation layer, which is the one thing here a rebuild cannot
    invent: a tag is what a person decided, and it outlives every topic."""

    async def test_it_lists_what_a_person_filed(self) -> None:
        tags = StubTags(tags=(TAG,))
        async with client_over(StubAccess(tags=tags)) as connected:
            answer = await connected.call_tool("tags", {})

        assert [one.tag_id for one in answer.data] == ["tag:nord-42"]
        assert answer.data[0].name == "NORD-42"
        assert answer.data[0].origin == "topic"
        assert answer.data[0].messages == 2
        assert tags.listed == 1

    async def test_an_archive_with_no_tags_answers_empty(self) -> None:
        """An honest empty, unlike a derived listing: tags are made by hand, so
        "there are none" is a state a user is in and not a job that has not
        run."""
        async with client_over(StubAccess(tags=StubTags())) as connected:
            answer = await connected.call_tool("tags", {}, raise_on_error=False)

        assert answer.is_error is False
        assert answer.data == []


class TestTaggedMessages:
    """One tag's mail, hydrated through the reader that knows how to show a
    message — the tag store answers with ids and nothing else."""

    async def test_the_ids_are_hydrated_in_the_order_they_came_back(self) -> None:
        tags = StubTags(tags=(TAG,), members={"tag:nord-42": ("m2", "m1")})
        archive = StubArchive([summary("m1"), summary("m2", "Re: Angebot 4711")])
        access = StubAccess(tags=tags, archive=archive)
        async with client_over(access) as connected:
            answer = await connected.call_tool(
                "tagged_messages", {"tag": "tag:nord-42", "limit": 5}
            )

        assert [one.message_id for one in answer.data] == ["m2", "m1"]
        assert answer.data[0].subject == "Re: Angebot 4711"
        assert answer.data[0].sender == "anna@kunde.example"
        assert answer.data[0].preview == "Hallo Jens, anbei das Angebot."
        assert tags.asked == [("tag:nord-42", 5, 0)]
        assert archive.hydrated == [["m2", "m1"]]

    async def test_the_limit_is_clamped_at_both_ends(self) -> None:
        tags = StubTags(tags=(TAG,), members={"tag:nord-42": ("m1",)})
        archive = StubArchive([summary("m1")])
        async with client_over(StubAccess(tags=tags, archive=archive)) as connected:
            await connected.call_tool(
                "tagged_messages", {"tag": "tag:nord-42", "limit": 5_000}
            )
            await connected.call_tool(
                "tagged_messages", {"tag": "tag:nord-42", "limit": 0}
            )

        assert [one[1] for one in tags.asked] == [MAX_ROWS, 1]

    async def test_a_tag_that_holds_nothing_answers_empty(self) -> None:
        """A tag somebody made and has not used yet is a legitimate answer, and
        the count on the listing already said so."""
        tags = StubTags(tags=(TAG,))
        async with client_over(StubAccess(tags=tags)) as connected:
            answer = await connected.call_tool(
                "tagged_messages", {"tag": "tag:nord-42"}, raise_on_error=False
            )

        assert answer.is_error is False
        assert answer.data == []

    async def test_an_unknown_tag_says_where_tag_ids_come_from(self) -> None:
        """Unlike a topic id a tag id is permanent, so a wrong one is a typo —
        and the sentence has to send the caller to the listing, not to a job."""
        tags = StubTags(tags=(TAG,))
        async with client_over(StubAccess(tags=tags)) as connected:
            answer = await connected.call_tool(
                "tagged_messages", {"tag": "tag:nope"}, raise_on_error=False
            )

        assert answer.is_error is True
        assert "'tag:nope'" in failure_text(answer)
        assert failure_text(answer).endswith(WHERE_TAGS_COME_FROM)

    async def test_a_tags_name_is_answered_with_its_id(self) -> None:
        """The mistake a model actually makes, and it costs a round trip.

        Resolving the name silently would be worse: two names can slug to one
        id, and a tool that guesses which tag was meant answers a question
        nobody asked. Naming the id is the whole of the help.
        """
        tags = StubTags(tags=(TAG,))
        async with client_over(StubAccess(tags=tags)) as connected:
            answer = await connected.call_tool(
                "tagged_messages", {"tag": "nord-42"}, raise_on_error=False
            )

        assert answer.is_error is True
        assert "tag:nord-42" in failure_text(answer)

    async def test_a_tag_id_reaches_the_store_exactly_as_it_arrived(self) -> None:
        """The store binds it as a parameter; nothing here may tidy it up."""
        hostile = "tag:x' OR 1=1 -- "
        tags = StubTags(tags=(TAG,))
        async with client_over(StubAccess(tags=tags)) as connected:
            await connected.call_tool(
                "tagged_messages", {"tag": hostile}, raise_on_error=False
            )

        assert tags.asked == [(hostile, 20, 0)]


async def test_every_new_tool_says_it_cannot_write(bare: Client) -> None:
    """The annotation layer is the one part of the graph a person edits, so a
    tool over it saying "read-only" is worth asserting rather than assuming."""
    tools = {tool.name: tool for tool in await bare.list_tools()}

    for name in ("important_messages", "topic_messages", "tags", "tagged_messages"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False


async def test_each_new_parameter_carries_its_own_explanation(bare: Client) -> None:
    """``Args:`` entries become per-parameter schema text — where a model reads
    them. A bound nobody wrote down gets guessed at, and an id whose origin is
    unstated gets invented."""
    schema = {tool.name: tool.inputSchema for tool in await bare.list_tools()}

    important = schema["important_messages"]["properties"]["limit"]
    assert "1..100" in important["description"]
    assert "topics" in schema["topic_messages"]["properties"]["topic_id"]["description"]
    assert "tags" in schema["tagged_messages"]["properties"]["tag"]["description"]
    assert schema["tags"]["properties"] == {}


class FakeGraph:
    """A session that answers whatever statement it is given with canned rows.

    Small on purpose: :meth:`ArchiveAccess.topic
    <mailarc_mcp.server.reads.ArchiveAccess.topic>` runs one catalogue
    statement through ``rows_of``, which for a builder statement is
    ``session.all_rows``. Everything else about a session is the driver's and
    is proved against a real one in ``test_mcp_server_local.py``.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.asked: list[tuple[Any, dict[str, Any]]] = []

    def all_rows(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.asked.append((statement, dict(params or {})))
        return list(self._rows)


def member_row(
    message_id: str, importance: float, method: str = "conversation"
) -> dict[str, Any]:
    """One row of ``TOPIC_MEMBERS`` — one ``ABOUT`` edge, not one message."""
    return {
        "topic_label": "Angebot 4711",
        "topic_messages": 3,
        "id": message_id,
        "subject": "Angebot 4711",
        "sent_at": AUGUST.isoformat(),
        "importance": importance,
        "method": method,
    }


def access_over(graph: FakeGraph, archive: StubArchive) -> ArchiveAccess:
    """The real access object, with a session that answers from *graph*.

    Not a :class:`StubAccess`: the point here is
    :meth:`ArchiveAccess.topic`'s own body — the dedup, the label, the
    truncation — and a subclass that answered it would test the subclass.
    """

    @contextmanager
    def session() -> Iterator[Session]:
        yield cast(Session, graph)

    return ArchiveAccess(
        graph_session=session,
        analytics=unbuilt,
        archive=lambda: archive,
        search=unbuilt,
        tags=unbuilt,
    )


class TestTheTopicRead:
    """What :meth:`ArchiveAccess.topic` makes of the statement's rows.

    Called directly rather than over the wire, because what is at stake is the
    read and not the tool: the ordering itself is the statement's and is proved
    against a real graph in ``test_mcp_server_local.py``.
    """

    def test_a_member_joined_by_two_signals_is_listed_once(self) -> None:
        """The statement returns one row per ``ABOUT`` edge, and a message can
        wear two — repeating it would spend a model's context saying the same
        thing twice, and would make the count of returned rows a lie."""
        graph = FakeGraph(
            [
                member_row("m2", 0.9),
                member_row("m2", 0.9, method="subject"),
                member_row("m1", 0.4),
            ]
        )
        archive = StubArchive([summary("m1"), summary("m2", "Re: Angebot 4711")])

        found = access_over(graph, archive).topic("topic:abc", 5)

        assert found is not None
        assert [one.message_id for one in found.messages] == ["m2", "m1"]
        assert archive.hydrated == [["m2", "m1"]]
        assert found.label == "Angebot 4711"
        assert found.messages[0].importance == 0.9
        assert found.messages[0].preview == "Hallo Jens, anbei das Angebot."

    def test_the_id_and_the_limit_are_bound_to_the_catalogue_statement(self) -> None:
        """One named constant, two bound parameters, and no string anywhere
        near the store — the rule the whole catalogue exists for."""
        graph = FakeGraph([member_row("m1", 0.4)])

        access_over(graph, StubArchive([summary("m1")])).topic("topic:abc", 7)

        statement, params = graph.asked[0]
        assert statement is catalog.TOPIC_MEMBERS
        assert params == {"topic": "topic:abc", "limit": 7}

    def test_the_topics_own_count_says_whether_the_listing_was_cut(self) -> None:
        """Read off ``Topic.message_count`` and not off the rows: a member the
        dedup dropped would otherwise make a whole topic look truncated."""
        graph = FakeGraph([member_row("m1", 0.4), member_row("m1", 0.4, "subject")])

        found = access_over(graph, StubArchive([summary("m1")])).topic("topic:abc", 5)

        assert found is not None
        assert len(found.messages) == 1
        assert found.truncated is True, "the topic holds three, one came back"

    def test_a_topic_the_graph_does_not_hold_is_none(self) -> None:
        """The caller turns it into the sentence about recomputed ids; the read
        itself says nothing, because "no rows" is all it knows."""
        assert access_over(FakeGraph([]), StubArchive()).topic("topic:gone", 5) is None
