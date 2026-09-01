"""The second seam in this project where more than one implementation exists.

:mod:`mailarc_core.mail.ports` states the bar: a port earns its place when a
second implementation is already there, and everything else stays straight
layering. This one clears it on the day it is written — §7.4 names both
:class:`~mailarc_analytics.semantic.embedder.OllamaEmbedder` and
:class:`~mailarc_analytics.semantic.embedder.OpenAIEmbedder`, and they are not
variations of each other: one is a local process with no account and no key,
the other is a paid API that message text is uploaded to. A user picking
between those two is making a decision about their privacy, not about a
backend, and neither side may be wired in above.

Async for the same reason the mail port is: a first embedding run is one HTTP
call per batch over the whole archive, and the graph work around it is
blocking and reached through ``asyncio.to_thread``.

Errors are the taxonomy in :mod:`mailarc_core.mail.errors`, reused rather than
reinvented. It is a strange name for an embedder — nothing about mail failed —
but its three answers are exactly the three an embedding loop has: ask for
credentials again, wait and retry the same call, or give up on this batch and
carry on. A fourth taxonomy meaning the same three things would be a second
thing for the job to catch and the first one it forgot.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from runic.ogm import Vector


class EmbedPurpose(StrEnum):
    """Whether a text is being *stored* or being *searched with*.

    Not decoration, and not symmetry for its own sake: instruction-tuned
    embedding models — ``nomic-embed-text`` is the one this project ships
    against — are trained with a different prefix for a corpus document than
    for a query, and produce different vectors accordingly. A port without this
    argument could only ever embed both halves the same way, and the adapter
    would have no place to put the difference.

    Defaulted everywhere, so a provider that has no such distinction
    (OpenAI has none) ignores it and no caller has to know which is which.
    """

    DOCUMENT = "document"
    """Text going into the index — a message body."""

    QUERY = "query"
    """Text being searched with — what a person typed."""


class EmbedderPort(Protocol):
    """A model that turns text into vectors, as far as this package cares.

    :attr:`model` and :attr:`dimension` are part of the contract rather than
    implementation detail, and both are read before a single vector is written.
    The name is stored on every node as ``Message.embedding_model``, which is
    what makes a later change of model detectable and its recomputation
    targeted; the dimension is checked against the live vector index, because
    the store accepts a wrong-length vector and then silently declines to index
    it.
    """

    model: str
    """The embedding model's name, exactly as it will be stored on the node."""

    dimension: int
    """Floats in every vector this embedder returns. Fixed, and equal to the
    dimension the graph's vector index was migrated with."""

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,
    ) -> Sequence[Vector]:
        """One vector per text, in the order the texts were given.

        Positional association is the contract, because the two APIs do not
        agree on how to express it: Ollama answers with a bare list of vectors
        and OpenAI answers with entries carrying an ``index`` field precisely
        because *its* array order is not contractual. Sorting that out is the
        adapter's job, so that nothing above ever has to ask which provider it
        is holding.

        Raises from :mod:`mailarc_core.mail.errors` and nothing else — no
        ``httpx`` exception may escape an implementation, for the reason
        :class:`~mailarc_google.source.client.GmailClient` gives: a caller that
        catches :class:`~mailarc_core.mail.errors.MailTransientError` will not
        catch a ``ConnectError``, and an adapter that lets one through has not
        decided whether the work should be retried.
        """
        ...

    async def aclose(self) -> None:
        """Release the HTTP client. Safe to call twice (§7.1)."""
        ...
