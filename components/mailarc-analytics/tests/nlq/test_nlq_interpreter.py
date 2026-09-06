"""The error boundary, mostly — which is what this class exists for.

The adapters raise ``mailarc_core.mail.errors``, the right taxonomy for an HTTP
call inside a job and the wrong one at the top of an interactive search. The
search page catches ``SemanticError`` and ``ValueError`` as notices and
everything else as *"the archive could not be searched"*, so a wrong API key
escaping this class would be shown to a user as a fault, naming no remedy, for
what is a one-line configuration fix. Every branch of that translation is
below.

The second thing under test is the rule the whole package is built around:
**an unanswerable question raises rather than returning an empty filter.** An
empty ``SearchFilters`` is not "nothing matched" — it is the browse-the-whole
-archive read, so a swallowed failure would answer a narrow question with every
message there is and look like it had worked.

No HTTP here: the port is a Protocol, so a fake is four lines and every failure
mode is reachable directly. ``test_nlq_chat.py`` is where real requests are
made, against a local server.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from mailarc_analytics.nlq.config import LlmConfig, LlmProvider
from mailarc_analytics.nlq.errors import NO_LLM, NlqRefused, NlqUnavailable
from mailarc_analytics.nlq.interpreter import QueryInterpreter
from mailarc_analytics.nlq.model import DirectoryEntry, Order, Vocabulary
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)

TODAY = date(2026, 9, 6)

VOCABULARY = Vocabulary(
    senders=(
        DirectoryEntry(
            address="service@telekom.de",
            names=("Telekom Kundenservice",),
            domain="telekom.de",
        ),
    )
)


class FakeChat:
    """One canned answer, or one raise. Everything the port promises."""

    def __init__(self, answer: str = "{}", error: Exception | None = None) -> None:
        self.model = "fake-model"
        self._answer = answer
        self._error = error
        self.asked: list[tuple[str, str]] = []
        self.closed = 0

    async def complete(
        self, *, system: str, question: str, schema: Mapping[str, Any]
    ) -> str:
        self.asked.append((system, question))
        if self._error is not None:
            raise self._error
        return self._answer

    async def aclose(self) -> None:
        self.closed += 1


def interpreter(answer: str = "{}", error: Exception | None = None) -> QueryInterpreter:
    """An interpreter over a fake model, configured as a real one would be."""
    return QueryInterpreter(
        LlmConfig(provider=LlmProvider.OLLAMA), FakeChat(answer, error)
    )


class TestWhetherItCanAnswerAtAll:
    """Asked on every page load, so it may not touch anything."""

    def test_no_client_is_unavailable(self) -> None:
        assert QueryInterpreter(LlmConfig()).available is False

    def test_a_client_is_available(self) -> None:
        assert interpreter().available is True

    def test_the_model_name_is_readable_for_a_page_that_names_it(self) -> None:
        assert interpreter().model == "fake-model"
        assert QueryInterpreter(LlmConfig()).model == ""

    async def test_asking_with_no_model_raises_the_shared_sentence(self) -> None:
        """One constant rather than one sentence per caller — the page, the
        selector and any tool all reach this state."""
        with pytest.raises(NlqUnavailable, match="no interpreter model"):
            await QueryInterpreter(LlmConfig()).interpret("was auch immer", VOCABULARY)
        assert NO_LLM.startswith("Natural-language search")


class TestReadingAQuestion:
    """The happy path, and the two things it has to get right about the day."""

    async def test_it_grounds_what_the_model_read(self) -> None:
        found = await interpreter(
            '{"from_mentions": ["Telekom"], "text": "Aufladung"}'
        ).interpret("Nachrichten von Telekom über Aufladung", VOCABULARY, today=TODAY)
        assert found.senders == ("service@telekom.de",)
        assert found.text == "Aufladung"

    async def test_the_prompt_carries_the_day_it_was_asked_on(self) -> None:
        """The model has to date "letzte Woche" against something, and the
        weekday is there because a question can name one."""
        client = FakeChat("{}")
        await QueryInterpreter(
            LlmConfig(provider=LlmProvider.OLLAMA), client
        ).interpret("was gibt es neues", VOCABULARY, today=TODAY)
        system, question = client.asked[0]
        assert "2026-09-06" in system
        assert "Sunday" in system
        assert question == "was gibt es neues"

    async def test_a_missing_field_is_its_empty_value_and_not_an_error(self) -> None:
        """The schema puts every property in ``required``, but a model that
        sends fewer must still be readable — pydantic's defaults are what makes
        a half-answer a narrow filter rather than a failure."""
        found = await interpreter('{"text": "Rechnung"}').interpret(
            "Rechnungen", VOCABULARY, today=TODAY
        )
        assert found.text == "Rechnung"
        assert found.order is Order.RELEVANCE

    async def test_a_blank_question_is_refused_before_a_request_is_made(self) -> None:
        """An empty filter is the browse-everything read, so this must not fall
        through to it — and there is nothing to spend a round trip on."""
        client = FakeChat("{}")
        one = QueryInterpreter(LlmConfig(provider=LlmProvider.OLLAMA), client)
        with pytest.raises(NlqRefused, match="ask a question"):
            await one.interpret("   ", VOCABULARY)
        assert client.asked == []


class TestWhenTheModelWillNotAnswer:
    """Every way the taxonomy is translated, and why each lands where it does."""

    async def test_a_refused_credential_is_a_setting_to_change(self) -> None:
        """Unavailable and not refused: asking a different question will not
        help, and the page must not show this as a fault."""
        with pytest.raises(NlqUnavailable, match="refused the credentials"):
            await interpreter(error=MailAuthError("401")).interpret("x", VOCABULARY)

    async def test_an_unreachable_model_is_unavailable_too(self) -> None:
        """Reported rather than retried: there is a person waiting behind a
        button, and a retry loop turns one slow answer into several."""
        with pytest.raises(NlqUnavailable, match="did not answer"):
            await interpreter(error=MailTransientError("down")).interpret(
                "x", VOCABULARY
            )

    async def test_a_permanent_failure_is_this_requests_own(self) -> None:
        with pytest.raises(NlqRefused, match="too old"):
            await interpreter(error=MailPermanentError("too old")).interpret(
                "x", VOCABULARY
            )

    async def test_prose_instead_of_a_filter_is_quoted_back(self) -> None:
        with pytest.raises(NlqRefused, match="I am a language model"):
            await interpreter("I am a language model and cannot search").interpret(
                "x", VOCABULARY
            )

    async def test_an_object_that_does_not_validate_is_quoted_back(self) -> None:
        """Pydantic's ``ValidationError`` **is** a ``ValueError``, so uncaught
        it would reach the page as a notice — and a reader would be shown
        pydantic's multi-line internal report as though it were a sentence."""
        with pytest.raises(NlqRefused) as raised:
            await interpreter('{"order": "sideways"}').interpret("x", VOCABULARY)
        assert "sideways" in str(raised.value)
        assert "validation error" not in str(raised.value).lower()

    async def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        """``extra="forbid"``. A model inventing a field has misread the schema,
        and the rest of what it sent is not to be trusted either."""
        with pytest.raises(NlqRefused):
            await interpreter('{"sender_email": "a@b.c"}').interpret("x", VOCABULARY)

    async def test_every_failure_is_one_exception_a_caller_can_catch(self) -> None:
        """One base, so a page turns the pair into a single readable failure
        with one ``except`` — the shape ``SemanticError`` already has."""
        from mailarc_analytics.nlq.errors import NlqError

        assert issubclass(NlqUnavailable, NlqError)
        assert issubclass(NlqRefused, NlqError)


class TestClosing:
    async def test_it_closes_the_client_it_was_given(self) -> None:
        client = FakeChat()
        await QueryInterpreter(LlmConfig(), client).aclose()
        assert client.closed == 1

    async def test_closing_without_a_client_is_safe(self) -> None:
        await QueryInterpreter(LlmConfig()).aclose()
