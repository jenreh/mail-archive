"""One question is decided here: when an embedding endpoint says no, what next?

The same three answers as the mail side, for the same reason — a rate limit
read as an auth failure sends the user to change a key that is fine, and a
missing model read as transient has the job retry forever instead of stopping
with something readable. So every branch of the mapping has a test, and so does
the promise that no ``httpx`` exception escapes: a caller that catches
:class:`~mailarc_core.mail.errors.MailTransientError` will not catch a
``ConnectError``.

The second thing under test is the one real difference between the two
adapters. **Ollama associates answers positionally** and **OpenAI's entries
carry an ``index``** — that field exists precisely because its array order is
not contractual, so one adapter must not sort and the other must. Getting it
backwards produces vectors that are wrong without being invalid: every write
succeeds and every search lies.

**No test here talks to Ollama or OpenAI.** Every call goes to a local
``pytest-httpserver``, which is the whole reason
:class:`~mailarc_analytics.semantic.config.SemanticConfig` carries ``base_url``
as a setting rather than a constant — the same argument
``components/mailarc-google/tests/test_google_client.py`` makes for
``GmailConfig``.
"""

import json
import socket
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest
from werkzeug import Request, Response

from mailarc_analytics.semantic.config import SemanticConfig, SemanticProvider
from mailarc_analytics.semantic.embedder import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    OllamaEmbedder,
    OpenAIEmbedder,
    _retry_after_seconds,
    build_embedder,
)
from mailarc_analytics.semantic.ports import EmbedPurpose
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)

OLLAMA_PATH = "/api/embed"
OPENAI_ROOT = "/v1"
OPENAI_PATH = f"{OPENAI_ROOT}/embeddings"

MODEL = "probe-model"
DIMENSION = 4
KEY = "sk-not-a-real-key"  # noqa: S105 - a fixture

FIRST = [1.0, 0.0, 0.0, 0.0]
SECOND = [0.0, 1.0, 0.0, 0.0]


def ollama_config(httpserver, **overrides: Any) -> SemanticConfig:
    """A configuration pointing the local adapter at the test server."""
    settings: dict[str, Any] = {
        "provider": SemanticProvider.OLLAMA,
        "model": MODEL,
        "dimension": DIMENSION,
        "base_url": httpserver.url_for(""),
        "request_timeout": 5.0,
    }
    return SemanticConfig(**(settings | overrides))


def openai_config(httpserver, **overrides: Any) -> SemanticConfig:
    """The same for the paid adapter, key included by default.

    Its root carries ``/v1`` because that is where OpenAI's own SDK puts the
    version, and a proxy configured without it answers 404 for a path that
    looks like a missing model.
    """
    settings: dict[str, Any] = {
        "provider": SemanticProvider.OPENAI,
        "model": MODEL,
        "dimension": DIMENSION,
        "base_url": httpserver.url_for(OPENAI_ROOT),
        "api_key": KEY,
        "request_timeout": 5.0,
    }
    return SemanticConfig(**(settings | overrides))


def ollama_body(*vectors: Sequence[float]) -> dict[str, Any]:
    """What ``POST /api/embed`` answers: a bare list, in input order."""
    return {"model": MODEL, "embeddings": [list(one) for one in vectors]}


def openai_body(*entries: tuple[int, Sequence[float]]) -> dict[str, Any]:
    """What ``POST /v1/embeddings`` answers: entries carrying their index."""
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": list(vector)}
            for index, vector in entries
        ],
        "model": MODEL,
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


def refusal(message: str) -> dict[str, Any]:
    """OpenAI's error envelope, the shape the API really sends."""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": "bad",
        }
    }


def closed_port() -> int:
    """A port nothing is listening on, for the dropped-connection case."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def sent_bodies(httpserver) -> list[dict[str, Any]]:
    """The JSON of every request the server saw."""
    return [json.loads(request.get_data()) for request, _ in httpserver.log]


def headers_seen(httpserver) -> list[str | None]:
    """The ``Authorization`` header of every request the server saw."""
    return [request.headers.get("Authorization") for request, _ in httpserver.log]


async def embed(
    embedder, texts: Sequence[str], **kwargs: Any
) -> Sequence[Sequence[float]]:
    """One call, with the client always released."""
    try:
        return await embedder.embed(list(texts), **kwargs)
    finally:
        await embedder.aclose()


class TestBuildingOne:
    def test_no_provider_means_no_embedder(self) -> None:
        """``None`` and not a null object: a no-op embedder would make every
        surface look as though it worked and return nothing, which is exactly
        the failure the phase's definition of done forbids."""
        assert build_embedder(SemanticConfig(provider=SemanticProvider.NONE)) is None

    def test_each_provider_builds_its_own_adapter(self) -> None:
        assert isinstance(
            build_embedder(SemanticConfig(provider=SemanticProvider.OLLAMA)),
            OllamaEmbedder,
        )
        assert isinstance(
            build_embedder(SemanticConfig(provider=SemanticProvider.OPENAI)),
            OpenAIEmbedder,
        )

    def test_an_empty_model_becomes_the_providers_own_default(self) -> None:
        """One shared default would be wrong for whichever provider did not
        supply it: ``nomic-embed-text`` sent to OpenAI is a 404 for a model
        that does not exist there, and the user would be reading an error
        about their key."""
        local = build_embedder(SemanticConfig(provider=SemanticProvider.OLLAMA))
        paid = build_embedder(SemanticConfig(provider=SemanticProvider.OPENAI))

        assert local is not None
        assert paid is not None
        assert local.model == DEFAULT_MODELS[SemanticProvider.OLLAMA]
        assert paid.model == DEFAULT_MODELS[SemanticProvider.OPENAI]

    def test_a_configured_model_is_used_as_it_stands(self) -> None:
        built = build_embedder(
            SemanticConfig(provider=SemanticProvider.OLLAMA, model="mine")
        )

        assert built is not None
        assert built.model == "mine"

    def test_the_local_default_needs_no_account(self) -> None:
        """The whole point of offering Ollama: nothing leaves the machine."""
        assert DEFAULT_BASE_URLS[SemanticProvider.OLLAMA].startswith("http://localhost")


class TestTheLocalAdapter:
    async def test_it_answers_one_vector_per_text_in_order(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST, SECOND)
        )

        vectors = await embed(
            OllamaEmbedder(ollama_config(httpserver)), ["first", "second"]
        )

        assert [list(one) for one in vectors] == [FIRST, SECOND]

    async def test_it_sends_the_model_and_the_batch(self, httpserver) -> None:
        """The current endpoint takes ``input`` as a list. The superseded
        ``/api/embeddings`` took one ``prompt`` per call, which would make a
        batch loop one HTTP call per message."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST, SECOND)
        )

        await embed(OllamaEmbedder(ollama_config(httpserver)), ["first", "second"])

        assert sent_bodies(httpserver) == [
            {"model": MODEL, "input": ["first", "second"]}
        ]

    async def test_it_sends_no_authorization(self, httpserver) -> None:
        """A local model server has no account, and sending a key to one would
        put a secret into somebody's terminal log for nothing."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST)
        )

        await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

        assert headers_seen(httpserver) == [None]

    async def test_the_task_prefix_is_off_by_default(self, httpserver) -> None:
        """Because it is unverified whether Ollama's template already adds
        one, and adding it twice embeds the instruction rather than the mail."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST)
        )

        await embed(OllamaEmbedder(ollama_config(httpserver)), ["a mail"])

        assert sent_bodies(httpserver)[0]["input"] == ["a mail"]

    async def test_the_prefix_differs_between_a_document_and_a_query(
        self, httpserver
    ) -> None:
        """That asymmetry is the whole reason the port takes a purpose: the
        model is trained to embed a stored document and a search differently,
        and one prefix for both would throw the distinction away."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST)
        )
        config = ollama_config(httpserver, task_prefix=True)

        embedder = OllamaEmbedder(config)
        try:
            await embedder.embed(["a mail"], purpose=EmbedPurpose.DOCUMENT)
            await embedder.embed(["a mail"], purpose=EmbedPurpose.QUERY)
        finally:
            await embedder.aclose()

        assert [body["input"] for body in sent_bodies(httpserver)] == [
            ["search_document: a mail"],
            ["search_query: a mail"],
        ]

    async def test_an_answer_without_embeddings_is_permanent(self, httpserver) -> None:
        """The same request will produce the same shape, so retrying is a
        loop — and guessing at the structure is how a list of nulls reaches
        the graph as a vector of zeros."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {"model": MODEL}
        )

        with pytest.raises(MailPermanentError, match="embeddings"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    async def test_a_short_answer_is_permanent(self, httpserver) -> None:
        """Two texts, one vector: association here is positional, so there is
        no way to know which text went unanswered."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_body(FIRST)
        )

        with pytest.raises(MailPermanentError, match="1 vectors for 2 texts"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first", "second"])

    async def test_a_vector_holding_text_is_permanent(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {"model": MODEL, "embeddings": [[1.0, "nope", 0.0, 0.0]]}
        )

        with pytest.raises(MailPermanentError, match="str"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    async def test_a_vector_of_booleans_is_permanent(self, httpserver) -> None:
        """``bool`` is an ``int`` in Python, so a lazy check lets ``[true,
        true]`` through as a vector of ones — well-formed, indexable, and a
        confident lie that nothing downstream can detect. The guard against it
        is one ``isinstance`` and this is the only thing holding it in place.
        """
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {"model": MODEL, "embeddings": [[True, False, True, True]]}
        )

        with pytest.raises(MailPermanentError, match="bool"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    async def test_an_empty_batch_never_leaves_the_process(self, httpserver) -> None:
        """A page whose every message was refused for its length would
        otherwise buy a round trip to embed nothing."""
        assert await embed(OllamaEmbedder(ollama_config(httpserver)), []) == []
        assert httpserver.log == []


class TestThePaidAdapter:
    async def test_entries_are_sorted_by_their_own_index(self, httpserver) -> None:
        """The ``index`` field exists because the array order is not
        contractual. Trusting the order would be right almost always, which is
        the worst kind of wrong: the vectors are valid, the writes succeed and
        the search confidently returns the wrong mails."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_body((1, SECOND), (0, FIRST))
        )

        vectors = await embed(
            OpenAIEmbedder(openai_config(httpserver)), ["first", "second"]
        )

        assert [list(one) for one in vectors] == [FIRST, SECOND]

    async def test_it_asks_for_the_dimension_the_index_was_migrated_with(
        self, httpserver
    ) -> None:
        """``text-embedding-3-*`` is longer natively and can be shortened on
        request. The index holds one length, and a longer vector would be
        stored and silently left out of it — so asking is the only way to be
        sure what comes back can be indexed."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_body((0, FIRST))
        )

        await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

        assert sent_bodies(httpserver) == [
            {
                "model": MODEL,
                "input": ["first"],
                "encoding_format": "float",
                "dimensions": DIMENSION,
            }
        ]

    async def test_it_sends_the_bearer_token(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_body((0, FIRST))
        )

        await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

        assert headers_seen(httpserver) == [f"Bearer {KEY}"]

    async def test_a_missing_key_is_left_to_the_endpoint(self, httpserver) -> None:
        """ "No key" and "wrong key" are the same problem from the user's side,
        and one message for both is one fewer branch that can disagree with
        the server."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            refusal("missing key"), status=401
        )
        config = openai_config(httpserver, api_key=None)

        with pytest.raises(MailAuthError, match="app_semantic_api_key"):
            await embed(OpenAIEmbedder(config), ["first"])

        assert headers_seen(httpserver) == [None]

    async def test_an_entry_without_an_index_is_permanent(self, httpserver) -> None:
        """The field is required by the API's own schema, so its absence means
        this is not the body being parsed — and falling back to array order
        would hide that behind an answer that looks right."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            {"data": [{"object": "embedding", "embedding": FIRST}]}
        )

        with pytest.raises(MailPermanentError, match="no index"):
            await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

    async def test_an_answer_without_data_is_permanent(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            {"object": "list"}
        )

        with pytest.raises(MailPermanentError, match="'data' list"):
            await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])


class TestWhatARefusalMeans:
    async def test_a_rate_limit_is_transient_and_carries_the_floor(
        self, httpserver
    ) -> None:
        """The provider's own ``Retry-After`` is a floor for the job's
        backoff, not a replacement: the queue still adds jitter."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_response(
            Response(
                json.dumps(refusal("slow down")),
                status=429,
                headers={"Retry-After": "3"},
                content_type="application/json",
            )
        )

        with pytest.raises(MailTransientError) as caught:
            await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

        assert caught.value.retry_after == 3.0
        assert "slow down" in str(caught.value)

    async def test_a_retry_after_date_is_read_too(self, httpserver) -> None:
        """RFC 9110 allows a delta or an HTTP-date and which one arrives
        depends on which front end refused. Reading only one form discards the
        provider's floor half the time."""
        moment = datetime.now(UTC) + timedelta(seconds=30)
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_response(
            Response(
                "{}",
                status=429,
                headers={"Retry-After": format_datetime(moment, usegmt=True)},
                content_type="application/json",
            )
        )

        with pytest.raises(MailTransientError) as caught:
            await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

        assert caught.value.retry_after is not None
        assert 20.0 <= caught.value.retry_after <= 31.0

    async def test_a_rate_limit_without_a_header_has_no_floor(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {}, status=429
        )

        with pytest.raises(MailTransientError) as caught:
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

        assert caught.value.retry_after is None

    async def test_a_server_error_is_transient(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {}, status=503
        )

        with pytest.raises(MailTransientError, match="503"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_credential_is_an_auth_failure(
        self, httpserver, status: int
    ) -> None:
        """Read as transient it would be retried forever; read as permanent it
        would skip one batch and go on failing quietly."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            refusal("nope"), status=status
        )

        with pytest.raises(MailAuthError):
            await embed(OpenAIEmbedder(openai_config(httpserver)), ["first"])

    @pytest.mark.parametrize("status", [400, 404, 413, 422])
    async def test_everything_else_costs_this_batch_and_no_more(
        self, httpserver, status: int
    ) -> None:
        """One over-long body must not end a run over a hundred thousand
        messages. A 404 for a model that was never pulled lands here too —
        which is why the job probes the embedder once before the loop instead
        of discovering it per batch."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {"error": 'model "probe-model" not found'}, status=status
        )

        with pytest.raises(MailPermanentError, match="not found"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    async def test_a_refusal_that_is_not_json_still_decides(self, httpserver) -> None:
        """A proxy or a captive portal answers HTML. The status code already
        said what to do, so the missing envelope costs a sentence and not an
        exception."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_data(
            "<html>gateway</html>", status=502, content_type="text/html"
        )

        with pytest.raises(MailTransientError, match="502"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])


class TestNoHttpxErrorEscapes:
    async def test_a_dropped_connection_is_transient(self) -> None:
        """The rule this class exists for: a caller catching
        ``MailTransientError`` will not catch a ``ConnectError``."""
        config = SemanticConfig(
            provider=SemanticProvider.OLLAMA,
            model=MODEL,
            dimension=DIMENSION,
            base_url=f"http://127.0.0.1:{closed_port()}",
            request_timeout=2.0,
        )

        with pytest.raises(MailTransientError, match="unreachable"):
            await embed(OllamaEmbedder(config), ["first"])

    async def test_a_hung_socket_is_transient(self, httpserver) -> None:
        """A local model on a cold CPU is slow and a hung one is silent; the
        timeout is what tells them apart."""

        def hang(request: Request) -> Response:
            time.sleep(1.0)
            return Response("{}", content_type="application/json")

        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_handler(hang)
        config = ollama_config(httpserver, request_timeout=0.2)

        with pytest.raises(MailTransientError, match="timed out"):
            await embed(OllamaEmbedder(config), ["first"])

    async def test_a_200_that_is_not_json_is_transient(self, httpserver) -> None:
        """Never the model server: the same call a minute later is the thing
        most likely to work."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_data(
            "not json at all", content_type="text/plain"
        )

        with pytest.raises(MailTransientError, match="not JSON"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])

    async def test_a_200_that_is_a_list_is_transient(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json([1, 2])

        with pytest.raises(MailTransientError, match="list"):
            await embed(OllamaEmbedder(ollama_config(httpserver)), ["first"])


class TestTheClient:
    async def test_closing_twice_is_safe(self, httpserver) -> None:
        """§7.1's rule for every adapter that owns a pool: the owner may not
        have to remember whether it already let go."""
        embedder = OllamaEmbedder(ollama_config(httpserver))

        await embedder.aclose()
        await embedder.aclose()

    async def test_the_declared_dimension_is_the_configured_one(
        self, httpserver
    ) -> None:
        """Read before a single vector is written, and checked against the
        live index — the store will not check it."""
        embedder = OllamaEmbedder(ollama_config(httpserver))
        try:
            assert embedder.dimension == DIMENSION
            assert embedder.model == MODEL
        finally:
            await embedder.aclose()


class TestTheRetryAfterCopy:
    """A deliberate duplicate of the Gmail adapter's helper, tested as one.

    ``mailarc-analytics`` may not import ``mailarc-google`` — §6's import table
    has neither above the other — so the choice was this or discarding the
    provider's own floor for the job's backoff. Because it is a copy, it is
    checked here rather than assumed to behave like the original; the two have
    to be changed together until the helper moves into the core.
    """

    def test_a_delta_in_seconds_is_a_number(self) -> None:
        assert _retry_after_seconds("12") == 12.0

    def test_an_absent_header_is_no_floor(self) -> None:
        assert _retry_after_seconds(None) is None
        assert _retry_after_seconds("") is None

    def test_a_header_in_neither_form_is_no_floor(self) -> None:
        """A value that parses as neither a delta nor a date is not worth
        failing a batch over — the engine's own backoff still applies."""
        assert _retry_after_seconds("soon-ish") is None

    def test_a_date_already_past_is_no_wait_rather_than_a_negative_one(self) -> None:
        """A negative floor would be subtracted from a backoff somewhere
        upstream and turn a wait into an immediate retry."""
        past = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)

        assert _retry_after_seconds(past) == 0.0

    def test_a_naive_date_is_read_as_utc(self) -> None:
        """``Retry-After`` is defined in GMT, and a naive value compared
        against an aware ``now`` raises rather than answering."""
        naive = "Wed, 21 Oct 2099 07:28:00"

        found = _retry_after_seconds(naive)

        assert found is not None
        assert found > 0
