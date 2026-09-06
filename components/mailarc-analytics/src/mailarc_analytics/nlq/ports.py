"""The third seam in this project where more than one implementation exists.

:mod:`mailarc_analytics.semantic.ports` states the bar and this clears it the
same way, on the day it is written: :class:`~mailarc_analytics.nlq.chat.
OllamaChat`, :class:`~mailarc_analytics.nlq.chat.OpenAIChat` and
:class:`~mailarc_analytics.nlq.chat.AzureOpenAIChat` all exist already, and the
choice between them is a decision about privacy and about who holds the
endpoint rather than about a backend. None of them may be wired in above.

The port hands back **text**, not a parsed object, and that is deliberate.
Getting JSON out of a model is two separate problems: one is the provider's —
which parameter asks for a schema, which field carries a refusal, how a
truncated answer is reported — and the other is the model's, which answers
prose around its JSON however it was trained to. The first belongs in each
adapter, the second is one function every adapter shares
(:func:`~mailarc_analytics.nlq.chat.intent_json`). A port returning a decoded
object would have forced the second into all three.

Async for the reason the mail and embedder ports are: this is an HTTP round
trip on the path between a person pressing **Search** and a list appearing, and
the graph work around it is blocking and reached through ``asyncio.to_thread``.
"""

from collections.abc import Mapping
from typing import Any, Protocol


class ChatPort(Protocol):
    """A model that answers a question with JSON, as far as this package cares.

    :attr:`model` is part of the contract because every failure message names
    it. Unlike the embedder's, it is written nowhere and stored nowhere — an
    interpretation produces no node — so changing it costs the next question
    and never a rebuild.
    """

    model: str
    """The model's name, as the provider knows it — a deployment name on Azure."""

    async def complete(
        self,
        *,
        system: str,
        question: str,
        schema: Mapping[str, Any],
    ) -> str:
        """The model's answer to one question, as text.

        ``schema`` is the JSON Schema the answer has to satisfy, and every
        adapter is expected to *ask* its provider to enforce it — Ollama
        through ``format``, the two OpenAI-shaped ones through
        ``response_format``. None of them may rely on it: a schema-constrained
        model still emits prose around its object often enough that the caller
        parses defensively either way.

        Raises from :mod:`mailarc_core.mail.errors` and nothing else. No
        ``httpx`` exception may escape an implementation, for the reason
        :class:`~mailarc_analytics.semantic.ports.EmbedderPort` gives: a caller
        catching :class:`~mailarc_core.mail.errors.MailTransientError` will not
        catch a ``ConnectError``, and an adapter that lets one through has not
        decided whether the work should be retried.
        """
        ...

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call twice (§7.1)."""
        ...
