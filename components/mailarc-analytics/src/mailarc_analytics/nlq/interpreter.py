"""The service the search page holds: a question in, a grounded filter out.

Thin on purpose. The reading is
:mod:`~mailarc_analytics.nlq.prompt`'s, the request is an adapter's, the
grounding is :mod:`~mailarc_analytics.nlq.resolve`'s — what is left here is the
sequence and, more importantly, the **error boundary**.

That boundary is the reason this class is not a function. The adapters raise
:mod:`mailarc_core.mail.errors`, which is the right taxonomy for an HTTP call
inside a job: retry, re-consent, or skip. It is the wrong taxonomy at the top
of an interactive search, and letting one out would be a real regression rather
than an untidiness — the search page catches
:class:`~mailarc_analytics.semantic.errors.SemanticError` and ``ValueError`` as
notices and everything else as *"the archive could not be searched"*. A wrong
API key would arrive there as
:class:`~mailarc_core.mail.errors.MailAuthError`, be shown as a fault, and name
no remedy at all for what is a one-line configuration fix.

So every call out of here is caught and re-raised as
:class:`~mailarc_analytics.nlq.errors.NlqUnavailable` or
:class:`~mailarc_analytics.nlq.errors.NlqRefused`. That includes pydantic's
:class:`~pydantic.ValidationError`, which **is** a ``ValueError`` — uncaught, a
model that sent one wrong field would put pydantic's multi-line internal report
on the page as though it were a sentence written for a reader.
"""

import logging
from datetime import date

from pydantic import ValidationError

from mailarc_analytics.nlq.chat import intent_json
from mailarc_analytics.nlq.config import LlmConfig
from mailarc_analytics.nlq.errors import (
    NO_LLM,
    NlqRefused,
    NlqUnavailable,
    refused,
)
from mailarc_analytics.nlq.model import (
    QueryIntent,
    ResolvedQuery,
    Vocabulary,
    strict_schema,
)
from mailarc_analytics.nlq.ports import ChatPort
from mailarc_analytics.nlq.prompt import system_prompt
from mailarc_analytics.nlq.resolve import resolve
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)

logger = logging.getLogger(__name__)

_SCHEMA = strict_schema(QueryIntent)
"""Computed once at import: it is a pure function of a frozen model, and paying
for the walk on every question would be paying for the same answer."""


class QueryInterpreter:
    """Reads a question against one archive's vocabulary. Registered by type.

    **Built even when no model is configured**, exactly as
    :class:`~mailarc_analytics.semantic.SemanticSearch` is. The page has to be
    able to ask :attr:`available` before it draws the mode selector, and a
    composition root that published this only when a provider existed would
    make "no interpreter configured" — an ordinary state — indistinguishable
    from "this build is broken", which is the one thing the page cannot
    diagnose on its own.
    """

    def __init__(self, config: LlmConfig, client: ChatPort | None = None) -> None:
        self._config = config
        self._client = client

    @property
    def available(self) -> bool:
        """Whether a question can be interpreted at all.

        In memory and touching nothing: the mode selector is drawn on every
        page load, and an answer that needed an HTTP round trip would make the
        form wait for a model server that may not be running.
        """
        return self._client is not None

    @property
    def model(self) -> str:
        """The model's name, for a page that wants to say which one read it."""
        return self._client.model if self._client is not None else ""

    async def interpret(
        self,
        question: str,
        vocabulary: Vocabulary,
        *,
        today: date | None = None,
    ) -> ResolvedQuery:
        """One question as a filter this archive can answer.

        ``today`` defaults to the **local** wall-clock day, not UTC.
        ``Message.sent_at`` is stored as the header's own wall clock and the
        bounds are compared against it as naive ISO strings, so a UTC day would
        name yesterday for anybody east of Greenwich in the small hours — and
        "heute" would quietly search the wrong day.

        Raises rather than returning an empty filter for anything it cannot
        do. An empty :class:`~mailarc_core.archive.search.SearchFilters` is not
        "nothing matched" — it is the browse-the-whole-archive read, so a
        swallowed failure would answer a narrow question with every message
        there is and look like it had worked.
        """
        if self._client is None:
            raise NlqUnavailable(NO_LLM)
        asked = question.strip()
        if not asked:
            raise NlqRefused(
                "Natural-language search: ask a question — there is nothing to read."
            )
        day = today or date.today()  # noqa: DTZ011 — local wall clock, see above
        answer = await self._answer(asked, day)
        intent = self._intent(answer)
        logger.debug("The model read the question as %s", intent.model_dump_json())
        return resolve(
            intent,
            vocabulary,
            today=day,
            max_addresses=self._config.max_resolved_addresses,
        )

    async def _answer(self, question: str, day: date) -> str:
        """The model's text, with the adapter's taxonomy translated into ours.

        The mapping is the point of this method. A credential the endpoint
        refused and a provider that is unreachable are both
        :class:`~mailarc_analytics.nlq.errors.NlqUnavailable` — a person has to
        change or start something, and asking a different question will not
        help. Everything else is a fault of *this* request.

        A transient failure is reported rather than retried. There is a person
        waiting on this call, and a retry loop behind a **Search** button turns
        one slow answer into several before saying so.
        """
        assert self._client is not None  # noqa: S101 — guarded by interpret()
        try:
            return await self._client.complete(
                system=system_prompt(day),
                question=question,
                schema=_SCHEMA,
            )
        except MailAuthError as error:
            raise NlqUnavailable(
                f"Natural-language search: the model refused the credentials. {error}"
            ) from error
        except MailTransientError as error:
            raise NlqUnavailable(
                f"Natural-language search: the model did not answer. {error}"
            ) from error
        except MailPermanentError as error:
            raise NlqRefused(f"Natural-language search: {error}") from error

    def _intent(self, answer: str) -> QueryIntent:
        """The answer as a validated reading, or the sentence saying why not.

        Both failures land on :func:`~mailarc_analytics.nlq.errors.refused`,
        which quotes what came back. That is deliberate: prose, a refusal and
        an object with one wrong field look identical from here, and the reader
        can tell them apart at a glance while this method cannot.
        """
        try:
            return QueryIntent.model_validate(intent_json(answer))
        except MailPermanentError as error:
            raise NlqRefused(refused(answer)) from error
        except ValidationError as error:
            logger.debug("The model's object did not validate: %s", error)
            raise NlqRefused(refused(answer)) from error

    async def aclose(self) -> None:
        """Release the client's connection pool. Safe to call twice (§7.1)."""
        if self._client is not None:
            await self._client.aclose()
