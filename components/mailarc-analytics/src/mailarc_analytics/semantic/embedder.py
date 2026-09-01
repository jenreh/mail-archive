"""The two embedders, and the only place in this package that reads a status code.

Own code over two HTTP APIs. ``runic.rag`` ships an embedding stack and is
banned repository-wide (§3.2): pulling it in for two ``POST``s would import a
GraphRAG pipeline whose whole purpose — letting a model write nodes — is the
thing this archive is built not to do.

Three jobs and no fourth, the way
:class:`~mailarc_google.source.client.GmailClient` has three: send the request,
turn what came back into the taxonomy of §7.6, and close the pool.

**No ``httpx`` exception leaves this module**, and **it does not retry**. Both
rules are the Gmail client's and both arguments carry over unchanged: a caller
that catches :class:`~mailarc_core.mail.errors.MailTransientError` will not
catch a ``ConnectError``, and a retry loop under the job's own backoff
multiplies every wait by a number invisible from the outside.

The one real difference between the two adapters is how a batch's answers are
associated with its inputs, and it is worth stating twice because getting it
wrong is silent: **Ollama answers positionally** — a bare list of vectors in
input order, with no per-item key — while **OpenAI's entries carry an
``index``**, which exists precisely because the array's order is not
contractual. So one must not sort and the other must.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any

import httpx
from runic.ogm import Vector

from mailarc_analytics.semantic.config import SemanticConfig, SemanticProvider
from mailarc_analytics.semantic.errors import SETTINGS_PAGE, STORED_WINS
from mailarc_analytics.semantic.ports import EmbedderPort, EmbedPurpose
from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)

logger = logging.getLogger(__name__)

_SERVER_ERROR = 500
"""From here up the provider is broken, not the request."""

DEFAULT_BASE_URLS: Mapping[SemanticProvider, str] = MappingProxyType(
    {
        SemanticProvider.OLLAMA: "http://localhost:11434",
        SemanticProvider.OPENAI: "https://api.openai.com/v1",
    }
)
"""Where each provider lives when the configuration does not say.

The OpenAI root carries ``/v1`` because that is where its own SDK puts the
version — a proxy configured without it would send ``/embeddings`` to a server
that only answers ``/v1/embeddings``, and the 404 that follows looks like a
missing model rather than a missing path segment.
"""

DEFAULT_MODELS: Mapping[SemanticProvider, str] = MappingProxyType(
    {
        SemanticProvider.OLLAMA: "nomic-embed-text",
        SemanticProvider.OPENAI: "text-embedding-3-small",
    }
)
"""Each provider's default model — 768 native and 1536 native respectively.

Per provider and not one shared default, because a shared one is wrong for
whichever provider did not supply it: ``nomic-embed-text`` sent to OpenAI is a
404 for a model that does not exist there, and the user would be reading an
error about their key.
"""

NATIVE_DIMENSIONS: Mapping[str, int] = MappingProxyType(
    {
        "nomic-embed-text": 768,
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }
)
"""How many floats each known model produces when nobody asks for fewer.

Keyed by model rather than by provider, because the number is the model's and
not the vendor's: OpenAI ships 1536 and 3072 under one account. A model absent
from this table is not an error — it means nobody here knows its length, and
the caller keeps whatever length it already had rather than guessing one.

Only ``text-embedding-3-*`` can be asked for fewer: they are trained so that a
prefix of the vector is itself a usable embedding, which is why
:class:`OpenAIEmbedder` sends ``dimensions`` on every request and why a 768
index is a legitimate choice against a 1536-native model. ``nomic-embed-text``
has no such parameter, so for Ollama this table is the only length that works.
"""


def native_dimension(provider: SemanticProvider, model: str) -> int | None:
    """The length to offer for this provider, given the model named so far.

    Answers the question "somebody just chose this provider — what length
    should the form show?", so an unrecognised model falls back to the length
    of the provider's own default model rather than to nothing: picking OpenAI
    and typing a model this table has not heard of should still offer 1536, not
    leave the field at Ollama's 768.

    ``None`` only where the provider embeds nothing at all. A caller asking the
    narrower question — "does *this model* have a known length?" — reads
    :data:`NATIVE_DIMENSIONS` directly and gets ``None`` for an unknown name,
    which is what a model field should do: leave a length alone rather than
    invent one for it.

    Neither form may overrule a length somebody set on purpose. For OpenAI a
    shorter vector is a real choice, not a mistake — see
    :data:`NATIVE_DIMENSIONS` on why ``dimensions`` makes that work.
    """
    named = NATIVE_DIMENSIONS.get(model.strip())
    if named is not None:
        return named
    default = DEFAULT_MODELS.get(provider)
    return NATIVE_DIMENSIONS.get(default) if default else None


_TASK_PREFIXES: Mapping[EmbedPurpose, str] = MappingProxyType(
    {
        EmbedPurpose.DOCUMENT: "search_document: ",
        EmbedPurpose.QUERY: "search_query: ",
    }
)
"""The instructions ``nomic-embed-text`` is trained on, applied only when
:attr:`~mailarc_analytics.semantic.config.SemanticConfig.task_prefix` says so —
see that setting for why it is off until somebody measures it."""


class _HttpEmbedder:
    """What both adapters share: a client, a URL, and the error mapping.

    A base class rather than a mixin or a helper module, because the thing
    being shared *is* the object's lifetime — the ``httpx.AsyncClient`` is
    created here and closed here, and an adapter that owned its own would be
    one more place to forget :meth:`aclose`.

    Not the port. :class:`~mailarc_analytics.semantic.ports.EmbedderPort` is
    what the rest of the project holds; this is an implementation detail the
    two adapters happen to have in common, and nothing outside the module
    should name it.
    """

    def __init__(self, config: SemanticConfig, provider: SemanticProvider) -> None:
        self.model = config.model or DEFAULT_MODELS[provider]
        self.dimension = config.dimension
        self._config = config
        self._base_url = config.base_url or DEFAULT_BASE_URLS[provider]
        self._http = httpx.AsyncClient(timeout=config.request_timeout)
        self._closed = False

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call twice (§7.1)."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()

    async def _post(
        self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """One request, with the taxonomy already applied to whatever came back.

        Returns the decoded JSON object. A refusal, a dropped connection, a
        body that is not JSON — all of them leave as one of the three mail
        errors, so no caller of an embedder has to know what ``httpx`` is.
        """
        url = f"{self._base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            response = await self._http.post(
                url, json=dict(payload), headers=dict(headers)
            )
        except httpx.TimeoutException as error:
            raise MailTransientError(
                f"{self.model} timed out after {self._config.request_timeout}s: {error}"
            ) from error
        except httpx.RequestError as error:
            raise MailTransientError(
                f"the embedding endpoint {url} is unreachable: {error}"
            ) from error
        if response.status_code != httpx.codes.OK:
            raise self._refusal(url, response)
        return self._payload(url, response)

    def _refusal(self, url: str, response: httpx.Response) -> MailError:
        """Turn a non-200 into the error that says what to do about it.

        The *decision* comes off the status code, never off the message: both
        providers word their errors freely and one of them documents no error
        body at all, while a code is stable. The body is read afterwards and
        only to quote — so a provider that sends none costs the reader a
        sentence and nothing else.
        """
        status = response.status_code
        detail = _described(response)
        if status == httpx.codes.TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
            return MailTransientError(
                f"the embedding endpoint returned {status} for {url}{detail}",
                retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            return MailAuthError(
                f"the embedding endpoint refused the credentials with {status} "
                f"for {url}{detail} — check the API key on the "
                f"{SETTINGS_PAGE} page, or app_semantic_api_key if this "
                f"archive is configured from a file ({STORED_WINS})"
            )
        # 400, 404, 413, 422 and the rest: this batch will never embed. The job
        # records it and goes on, because one over-long body must not end a run
        # over a hundred thousand messages. A 404 for a model that was never
        # pulled would also land here — which is why the job probes the
        # embedder once before the loop rather than discovering it per batch.
        return MailPermanentError(
            f"the embedding endpoint answered {status} for {url}{detail}"
        )

    def _payload(self, url: str, response: httpx.Response) -> dict[str, Any]:
        """The body as a JSON object, or a transient error.

        A 200 that is not JSON is a proxy or a captive portal, never the model
        server: the same call a minute later is the thing most likely to work.
        """
        try:
            body = response.json()
        except ValueError as error:
            raise MailTransientError(
                f"{url} answered with something that is not JSON: {error}"
            ) from error
        if not isinstance(body, dict):
            raise MailTransientError(
                f"{url} answered with {type(body).__name__} rather than an object"
            )
        return body

    def _checked(
        self, vectors: list[list[float]], texts: Sequence[str]
    ) -> list[Vector]:
        """Refuse an answer that does not line up with the batch that asked.

        A count mismatch is permanent for this batch and not transient: it
        means the provider read the request differently than we wrote it, and
        the same request will be read the same way again. Retrying it would
        loop; zipping it would put the wrong vector on every message after the
        gap, and a wrong vector is a working, findable, confidently ranked lie.
        """
        if len(vectors) != len(texts):
            raise MailPermanentError(
                f"{self.model} answered {len(vectors)} vectors for {len(texts)} texts"
            )
        return [Vector(one) for one in vectors]


class OllamaEmbedder(_HttpEmbedder):
    """A local model server — ``POST /api/embed``, no account and no key.

    The path matters: ``/api/embeddings`` (plural) is the superseded endpoint,
    takes one ``prompt`` per call and answers a single flat ``embedding``.
    Supporting both would mean a second parser, a second fixture and a batch
    loop that degenerates into one HTTP call per message. ``/api/embed`` has
    shipped since Ollama 0.3, so this adapter requires it and a 404 from an
    older server is reported as the permanent failure it is.

    Answers are associated **positionally** — the response carries no per-item
    key — so nothing here sorts, and the count check in
    :meth:`_HttpEmbedder._checked` is the only thing standing between a short
    answer and a batch of mismatched vectors.
    """

    def __init__(self, config: SemanticConfig) -> None:
        super().__init__(config, SemanticProvider.OLLAMA)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,
    ) -> Sequence[Vector]:
        """One vector per text, in order.

        ``truncate`` is left at the API's default of true on purpose. The
        bodies are already cut to
        :attr:`~mailarc_analytics.semantic.config.SemanticConfig.max_body_chars`
        before they get here, so truncation should never trigger — and if a
        subject plus a body ever does exceed the model's context, a shortened
        embedding is a better answer than a failed batch.
        """
        if not texts:
            return []
        prepared = [self._prefixed(one, purpose) for one in texts]
        body = await self._post(
            "/api/embed",
            {"model": self.model, "input": prepared},
            {},
        )
        return self._checked(_float_lists(body.get("embeddings"), self.model), texts)

    def _prefixed(self, text: str, purpose: EmbedPurpose) -> str:
        """The text as the model wants to be given it, if we are prefixing."""
        if not self._config.task_prefix:
            return text
        return f"{_TASK_PREFIXES[purpose]}{text}"


class OpenAIEmbedder(_HttpEmbedder):
    """OpenAI's ``POST /v1/embeddings``, with a bearer token and a bill.

    Two details that are not interchangeable with the local adapter. Its
    ``data`` entries carry an ``index`` and are **sorted by it here**, because
    that field exists precisely to say that the array order is not contractual
    — trusting the order would be correct almost always, which is the worst
    kind of wrong.

    And every request asks for ``dimensions`` explicitly. ``text-embedding-3-*``
    supports shortening, and the graph's vector index is migrated to one fixed
    length, so asking is the only way to be sure that what comes back can be
    indexed at all — a longer vector would be stored and silently skipped by
    the index. The cost is that a model without that parameter (the older
    ``ada-002``) answers 400, which the start-of-job probe surfaces as a clear
    permanent failure before anything is written.
    """

    def __init__(self, config: SemanticConfig) -> None:
        super().__init__(config, SemanticProvider.OPENAI)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbedPurpose = EmbedPurpose.DOCUMENT,  # noqa: ARG002
    ) -> Sequence[Vector]:
        """One vector per text, re-sorted into the order the texts came in.

        ``purpose`` is accepted and ignored: OpenAI's embedding models are not
        instruction-tuned and take no task prefix, so a document and a query
        embed identically. The argument stays in the signature because it is
        the port's, and a caller must not have to ask which provider it holds.
        """
        if not texts:
            return []
        body = await self._post(
            "/embeddings",
            {
                "model": self.model,
                "input": list(texts),
                "encoding_format": "float",
                "dimensions": self.dimension,
            },
            self._headers(),
        )
        return self._checked(_indexed_vectors(body.get("data"), self.model), texts)

    def _headers(self) -> dict[str, str]:
        """The bearer token, or nothing at all.

        A missing key is not refused here. The endpoint answers 401 and the
        adapter reports it as an auth failure naming the setting — one message
        for "no key" and "wrong key", which are the same problem from the
        user's side, and one fewer branch that can disagree with the server.
        """
        key = self._config.api_key
        if key is None:
            return {}
        return {"Authorization": f"Bearer {key.get_secret_value()}"}


def build_embedder(config: SemanticConfig) -> EmbedderPort | None:
    """The configured embedder, or ``None`` when there is none.

    ``None`` and not a null object. A no-op embedder would make every surface
    look as though it worked and return nothing — which is precisely the
    failure §10's definition of done forbids: an empty result reads as "nothing
    matched", and a user believes it. Returning ``None`` forces each caller to
    say what it does without one, and there are only five such callers.

    The composition root builds this once and hands it around; nothing else in
    the project constructs an embedder, so "which model is this archive
    embedded with" has exactly one answer at runtime.
    """
    if config.provider is SemanticProvider.NONE:
        logger.debug("No embedder configured; semantic search stays off")
        return None
    if config.provider is SemanticProvider.OLLAMA:
        return OllamaEmbedder(config)
    return OpenAIEmbedder(config)


def _described(response: httpx.Response) -> str:
    """The provider's own words about a refusal, or an empty string.

    Read opportunistically and never for the decision. OpenAI documents an
    ``error`` object with a ``message``; Ollama documents no error body and in
    practice sends ``{"error": "model \\"x\\" not found"}`` — a bare string.
    Both are quoted if present and neither is required, so a proxy's HTML error
    page costs the reader a sentence rather than an exception.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    envelope = body.get("error")
    if isinstance(envelope, str):
        return f" — {envelope}"
    if isinstance(envelope, dict) and envelope.get("message"):
        return f" — {envelope['message']}"
    return ""


def _float_lists(payload: object, model: str) -> list[list[float]]:
    """Ollama's ``embeddings`` field, checked into shape.

    A body of the right status but the wrong shape is permanent for this batch:
    the same request will produce the same shape, so retrying is a loop, and
    guessing at the structure is how a list of ``None`` reaches the graph as a
    vector of zeros.
    """
    if not isinstance(payload, list):
        raise MailPermanentError(
            f"{model} answered without an 'embeddings' list "
            f"(got {type(payload).__name__})"
        )
    return [_floats(one, model) for one in payload]


def _indexed_vectors(payload: object, model: str) -> list[list[float]]:
    """OpenAI's ``data`` entries, sorted by their own ``index`` field.

    An entry without an ``index`` is a permanent failure rather than a row to
    put back where it was found: the field is required by the API's own schema,
    so its absence means the body is not the one being parsed, and falling back
    to array order would hide that behind an answer that looks right.
    """
    if not isinstance(payload, list):
        raise MailPermanentError(
            f"{model} answered without a 'data' list (got {type(payload).__name__})"
        )
    entries: list[tuple[int, list[float]]] = []
    for one in payload:
        if not isinstance(one, dict) or not isinstance(one.get("index"), int):
            raise MailPermanentError(f"{model} answered an entry with no index")
        entries.append((int(one["index"]), _floats(one.get("embedding"), model)))
    return [vector for _, vector in sorted(entries, key=lambda pair: pair[0])]


def _floats(payload: object, model: str) -> list[float]:
    """One vector, as a list of numbers, or a permanent failure.

    ``bool`` is excluded explicitly because it is an ``int`` in Python, and a
    vector of ``True`` would otherwise sail through as a vector of ones.
    """
    if not isinstance(payload, list) or not payload:
        raise MailPermanentError(f"{model} answered an empty or non-list vector")
    for value in payload:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MailPermanentError(
                f"{model} answered a vector holding {type(value).__name__}"
            )
    return [float(value) for value in payload]


def _retry_after_seconds(header: str | None) -> float | None:
    """A ``Retry-After`` header as seconds, in either form RFC 9110 allows.

    A deliberate second copy of
    :func:`~mailarc_google.source.model.retry_after_seconds`, and the duplicate
    is the lesser evil of two: ``mailarc-analytics`` may not import
    ``mailarc-google`` — §6's import table has neither above the other, and the
    isolation probe checks it — so the alternatives are this or discarding the
    provider's own floor for the job's backoff. The right home for it is
    ``mailarc_core.mail.model``, beside the error that carries the value; until
    that move is made, these two must be changed together.

    A value in neither form, or one already in the past, is simply no floor.
    """
    if not header:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(header)
    except TypeError, ValueError:
        logger.debug("Ignoring unreadable Retry-After header %r", header)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - datetime.now(UTC)).total_seconds(), 0.0)
