"""Three providers, one answer shape, and every way a model declines to give it.

Two things are under test and they fail differently.

The first is the **request**, and each provider has one detail that is silently
fatal to get wrong. Ollama streams NDJSON unless told not to, so a missing
``"stream": false`` breaks every call against a server that is working
perfectly. OpenAI rejects ``max_tokens`` outright on newer models. Azure's key
travels in ``api-key``, and a request sent with ``Authorization: Bearer``
instead is answered as though no key had been sent at all.

The second is the **answer**, where the failures are worse because they look
like successes. A truncated object and a refusal both arrive with status 200,
and both would reach the JSON reader as malformed prose — so the reader would
report "the model did not answer with a filter" for two states whose remedies
are precise and different: raise a limit, or ask something else.

**No test here talks to Ollama or OpenAI.** Every call goes to a local
``pytest-httpserver``, which is the whole reason
:class:`~mailarc_analytics.nlq.config.LlmConfig` carries ``base_url`` as a
setting rather than a constant — the argument
``tests/semantic/test_semantic_embedder.py`` already makes for the embedder.
"""

import json
from typing import Any

import pytest
from pydantic import SecretStr

from mailarc_analytics.nlq.chat import (
    AzureOpenAIChat,
    OllamaChat,
    OpenAIChat,
    build_chat_client,
    intent_json,
)
from mailarc_analytics.nlq.config import LlmConfig, LlmProvider
from mailarc_core.mail.errors import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
)

OLLAMA_PATH = "/api/chat"
OPENAI_ROOT = "/v1"
OPENAI_PATH = f"{OPENAI_ROOT}/chat/completions"
AZURE_ROOT = "/openai/v1"
AZURE_PATH = f"{AZURE_ROOT}/chat/completions"

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}}
INTENT = '{"text": "Rechnung"}'


def ollama_config(httpserver, **overrides: Any) -> LlmConfig:
    """A configuration pointed at the local server, Ollama-shaped.

    ``overrides`` is merged rather than passed through, so a test can blank one
    of the defaults below — which is exactly what the Azure cases do.
    """
    return _config(
        {
            "provider": LlmProvider.OLLAMA,
            "model": "test-model",
            "base_url": httpserver.url_for(""),
        },
        overrides,
    )


def openai_config(httpserver, **overrides: Any) -> LlmConfig:
    return _config(
        {
            "provider": LlmProvider.OPENAI,
            "model": "test-model",
            "base_url": httpserver.url_for(OPENAI_ROOT),
            "api_key": SecretStr("sk-test"),
        },
        overrides,
    )


def azure_config(httpserver, **overrides: Any) -> LlmConfig:
    return _config(
        {
            "provider": LlmProvider.AZURE_OPENAI,
            "model": "my-deployment",
            "base_url": httpserver.url_for(AZURE_ROOT),
            "api_key": SecretStr("azure-key"),
        },
        overrides,
    )


def _config(base: dict[str, Any], overrides: dict[str, Any]) -> LlmConfig:
    """One configuration, the test's own values winning over the defaults."""
    return LlmConfig(**{**base, **overrides})


def ollama_answer(content: str, **extra: Any) -> dict[str, Any]:
    return {"message": {"role": "assistant", "content": content}, **extra}


def openai_answer(content: str | None, **choice: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if "refusal" in choice:
        message["refusal"] = choice.pop("refusal")
    return {"choices": [{"message": message, **choice}]}


def sent_bodies(httpserver) -> list[dict[str, Any]]:
    """Every request body the server was given, decoded."""
    return [json.loads(request.get_data()) for request, _ in httpserver.log]


async def ask(client: Any) -> str:
    """One completion, with the client closed afterwards as a caller would."""
    try:
        return await client.complete(system="s", question="q", schema=SCHEMA)
    finally:
        await client.aclose()


class TestBuildingOne:
    """Which adapter a configuration asks for, and the state that asks for none."""

    def test_no_provider_builds_nothing(self) -> None:
        """``None`` rather than a null object: the page has to answer "is this
        available" without making a request, and a null object could only be
        asked by trying."""
        assert build_chat_client(LlmConfig()) is None

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            (LlmProvider.OLLAMA, OllamaChat),
            (LlmProvider.OPENAI, OpenAIChat),
            (LlmProvider.AZURE_OPENAI, AzureOpenAIChat),
        ],
    )
    def test_each_provider_builds_its_own_adapter(
        self, provider: LlmProvider, expected: type
    ) -> None:
        assert type(build_chat_client(LlmConfig(provider=provider))) is expected

    def test_an_unconfigured_azure_still_builds(self) -> None:
        """It has to. ``build_chat_client`` runs while ``app/app.py`` is being
        imported, and a raise there would take the whole application down over
        one unset variable rather than costing this one feature."""
        assert (
            build_chat_client(LlmConfig(provider=LlmProvider.AZURE_OPENAI)) is not None
        )

    def test_a_model_name_falls_back_to_the_providers_own(self) -> None:
        client = build_chat_client(LlmConfig(provider=LlmProvider.OLLAMA))
        assert client is not None
        assert client.model == "qwen2.5:7b-instruct"


class TestOllama:
    """A local model server, where one missing field breaks every call."""

    async def test_it_answers_with_the_assistant_message(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        assert await ask(OllamaChat(ollama_config(httpserver))) == INTENT

    async def test_it_never_streams(self, httpserver) -> None:
        """``/api/chat`` streams by default and answers NDJSON — several JSON
        objects, one per token. Without this the body is not an object and
        every call reports a transient failure against a healthy server."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        await ask(OllamaChat(ollama_config(httpserver)))
        assert sent_bodies(httpserver)[0]["stream"] is False

    def test_it_asks_for_the_schema_and_says_json_in_words(self) -> None:
        """Both, not either. Ollama's own guidance is that a constrained model
        still needs telling, or a small one pads until it runs out of tokens.
        The words are the prompt's job; the schema is this one's."""
        from mailarc_analytics.nlq.prompt import SYSTEM

        assert "JSON only" in SYSTEM

    async def test_it_sends_the_schema_as_format(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        await ask(OllamaChat(ollama_config(httpserver)))
        assert sent_bodies(httpserver)[0]["format"] == SCHEMA

    async def test_it_suppresses_a_reasoning_models_thinking(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        await ask(OllamaChat(ollama_config(httpserver)))
        assert sent_bodies(httpserver)[0]["think"] is False

    async def test_it_omits_a_temperature_that_is_unset(self, httpserver) -> None:
        """A reasoning model accepts only its own and rejects any other, so
        there is no number that means "leave it alone"."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        await ask(OllamaChat(ollama_config(httpserver, temperature=None)))
        assert "temperature" not in sent_bodies(httpserver)[0]["options"]

    async def test_it_sends_a_temperature_that_is_set(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        await ask(OllamaChat(ollama_config(httpserver)))
        assert sent_bodies(httpserver)[0]["options"]["temperature"] == 0.0

    async def test_a_question_is_cut_before_it_reaches_the_prompt(
        self, httpserver
    ) -> None:
        """What stops a pasted mail thread from becoming the prompt."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer(INTENT)
        )
        client = OllamaChat(ollama_config(httpserver, max_question_chars=5))
        try:
            await client.complete(system="s", question="x" * 500, schema=SCHEMA)
        finally:
            await client.aclose()
        assert sent_bodies(httpserver)[0]["messages"][1]["content"] == "xxxxx"

    async def test_running_out_of_room_names_the_setting(self, httpserver) -> None:
        """Not a parse error. The answer is half an object, and the remedy is
        one specific number rather than a different question."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            ollama_answer('{"text": "Rech', done_reason="length")
        )
        with pytest.raises(MailPermanentError, match="app_llm_max_output_tokens"):
            await ask(OllamaChat(ollama_config(httpserver)))

    async def test_a_body_with_no_message_is_permanent(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json({})
        with pytest.raises(MailPermanentError, match="'message' object"):
            await ask(OllamaChat(ollama_config(httpserver)))


class TestOpenAI:
    """The shape Azure inherits, and the two answers that are not answers."""

    async def test_it_answers_with_the_choices_content(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        assert await ask(OpenAIChat(openai_config(httpserver))) == INTENT

    async def test_it_asks_for_the_schema_strictly(self, httpserver) -> None:
        """``strict: true`` and no fallback to ``json_object``. A silent
        downgrade would keep working while quietly no longer honouring the
        schema, and the first sign would be a filter with an invented field."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        await ask(OpenAIChat(openai_config(httpserver)))
        sent = sent_bodies(httpserver)[0]["response_format"]
        assert sent["type"] == "json_schema"
        assert sent["json_schema"]["strict"] is True
        assert sent["json_schema"]["schema"] == SCHEMA

    async def test_it_uses_the_newer_token_limit_name(self, httpserver) -> None:
        """``max_tokens`` is rejected outright by newer models rather than
        ignored, so the older name is not a compatible spelling of this."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        await ask(OpenAIChat(openai_config(httpserver)))
        body = sent_bodies(httpserver)[0]
        assert body["max_completion_tokens"] == 512
        assert "max_tokens" not in body

    async def test_it_sends_the_key_as_a_bearer_token(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        await ask(OpenAIChat(openai_config(httpserver)))
        request, _ = httpserver.log[0]
        assert request.headers.get("Authorization") == "Bearer sk-test"

    async def test_a_truncated_answer_names_the_setting(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer('{"text": "Rech', finish_reason="length")
        )
        with pytest.raises(MailPermanentError, match="app_llm_max_output_tokens"):
            await ask(OpenAIChat(openai_config(httpserver)))

    async def test_a_refusal_is_quoted_rather_than_parsed(self, httpserver) -> None:
        """A refusal is prose with status 200. Read as a parse failure it would
        be reported as "the model did not answer with a filter", which is true
        and useless — the model said why."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            openai_answer(None, refusal="I cannot help with that")
        )
        with pytest.raises(MailPermanentError, match="I cannot help with that"):
            await ask(OpenAIChat(openai_config(httpserver)))

    async def test_no_choices_is_permanent(self, httpserver) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            {"choices": []}
        )
        with pytest.raises(MailPermanentError, match="no choices"):
            await ask(OpenAIChat(openai_config(httpserver)))


class TestAzure:
    """One header, and two settings that cannot have defaults."""

    async def test_it_sends_azures_own_key_header(self, httpserver) -> None:
        """``api-key`` and ``Authorization: Bearer`` are not synonyms: a
        request with the wrong one is answered as though no key had been sent."""
        httpserver.expect_request(AZURE_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        await ask(AzureOpenAIChat(azure_config(httpserver)))
        request, _ = httpserver.log[0]
        assert request.headers.get("api-key") == "azure-key"
        assert request.headers.get("Authorization") is None

    async def test_it_sends_the_deployment_name_as_the_model(self, httpserver) -> None:
        """Azure calls it a deployment, but it travels in the same ``model``
        field of the same body — which is why the v1 surface needs no path of
        its own and why ``_post`` can stay without a ``params`` argument."""
        httpserver.expect_request(AZURE_PATH, method="POST").respond_with_json(
            openai_answer(INTENT)
        )
        await ask(AzureOpenAIChat(azure_config(httpserver)))
        assert sent_bodies(httpserver)[0]["model"] == "my-deployment"

    @pytest.mark.parametrize(
        ("missing", "named"),
        [({"base_url": ""}, "app_llm_base_url"), ({"model": ""}, "app_llm_model")],
    )
    async def test_an_unset_setting_is_refused_by_name(
        self, httpserver, missing: dict[str, str], named: str
    ) -> None:
        """Refused before a request that could only 404 or connect nowhere, and
        refused at the call rather than at construction — see
        ``_require_configured``."""
        config = azure_config(httpserver, **missing)
        with pytest.raises(MailPermanentError, match=named):
            await ask(AzureOpenAIChat(config))

    async def test_the_endpoint_shape_is_in_the_message(self, httpserver) -> None:
        """A legacy endpoint 404s in a way that reads like a missing
        deployment, so the sentence names the shape and not only the setting."""
        with pytest.raises(MailPermanentError, match=r"openai/v1"):
            await ask(AzureOpenAIChat(azure_config(httpserver, base_url="")))


class TestWhenTheEndpointSaysNo:
    """The status mapping, which decides what the caller does next."""

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_a_busy_endpoint_is_transient(self, httpserver, status: int) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_data(
            "", status=status
        )
        with pytest.raises(MailTransientError):
            await ask(OllamaChat(ollama_config(httpserver)))

    @pytest.mark.parametrize("status", [401, 403])
    async def test_a_refused_credential_names_the_key(
        self, httpserver, status: int
    ) -> None:
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_data(
            "", status=status
        )
        with pytest.raises(MailAuthError, match="app_llm_api_key"):
            await ask(OpenAIChat(openai_config(httpserver)))

    async def test_anything_else_names_the_model_setting(self, httpserver) -> None:
        """A 400 here is one request failing, not one batch of a long job — and
        the likeliest cause is a model too old for structured output."""
        httpserver.expect_request(OPENAI_PATH, method="POST").respond_with_json(
            {"error": {"message": "response_format is not supported"}}, status=400
        )
        with pytest.raises(MailPermanentError, match="app_llm_model"):
            await ask(OpenAIChat(openai_config(httpserver)))

    async def test_the_providers_own_words_are_quoted(self, httpserver) -> None:
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_json(
            {"error": 'model "x" not found'}, status=404
        )
        with pytest.raises(MailPermanentError, match='model "x" not found'):
            await ask(OllamaChat(ollama_config(httpserver)))

    async def test_a_two_hundred_that_is_not_json_is_transient(
        self, httpserver
    ) -> None:
        """A proxy or a captive portal, never the model server — so the same
        call a minute later is the thing most likely to work."""
        httpserver.expect_request(OLLAMA_PATH, method="POST").respond_with_data(
            "<html>hello</html>", content_type="text/html"
        )
        with pytest.raises(MailTransientError, match="not JSON"):
            await ask(OllamaChat(ollama_config(httpserver)))

    async def test_no_httpx_exception_escapes(self, httpserver) -> None:
        """The promise the port makes. A caller catching ``MailTransientError``
        will not catch a ``ConnectError``."""
        client = OllamaChat(ollama_config(httpserver, base_url="http://127.0.0.1:9"))
        with pytest.raises(MailTransientError, match="unreachable"):
            await ask(client)

    async def test_closing_twice_is_safe(self, httpserver) -> None:
        client = OllamaChat(ollama_config(httpserver))
        await client.aclose()
        await client.aclose()


class TestReadingTheAnswer:
    """One reader for all three, written for what models send rather than ask."""

    def test_a_bare_object_is_the_common_case(self) -> None:
        assert intent_json('{"text": "a"}') == {"text": "a"}

    def test_a_fenced_block_is_unwrapped(self) -> None:
        """ "Answer with JSON" is read by many models as "answer with a JSON
        code block"."""
        assert intent_json('```json\n{"text": "a"}\n```') == {"text": "a"}

    def test_an_unlabelled_fence_is_unwrapped_too(self) -> None:
        assert intent_json('```\n{"text": "a"}\n```') == {"text": "a"}

    def test_a_reasoning_models_scratch_work_is_stripped(self) -> None:
        """Newer Ollama puts it in a field of its own; older builds and several
        gateways inline it, where no amount of ``format`` stops it."""
        answer = '<think>The user wants invoices.</think>\n{"text": "a"}'
        assert intent_json(answer) == {"text": "a"}

    def test_prose_around_the_object_is_scanned_past(self) -> None:
        assert intent_json('Sure! Here you go:\n{"text": "a"}\nHope that helps.') == {
            "text": "a"
        }

    def test_a_brace_inside_a_string_does_not_end_the_object(self) -> None:
        """``find('{')`` to ``rfind('}')`` gets this wrong, and gets it wrong
        again on any trailing prose containing a brace."""
        assert intent_json('{"text": "a } b"} and that is that') == {"text": "a } b"}

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        assert intent_json(r'{"text": "say \"hi\" }"}') == {"text": 'say "hi" }'}

    def test_a_nested_object_survives(self) -> None:
        assert intent_json('prefix {"a": {"b": 1}} suffix') == {"a": {"b": 1}}

    @pytest.mark.parametrize(
        "answer",
        ["I cannot do that", "", "[1, 2, 3]", '{"text": '],
    )
    def test_an_answer_with_no_object_in_it_is_permanent(self, answer: str) -> None:
        """Permanent and not transient: the same question to the same model
        produces the same shape, so a retry is a loop."""
        with pytest.raises(MailPermanentError):
            intent_json(answer)

    def test_the_answer_is_quoted_in_the_failure(self) -> None:
        """The reader cannot tell a refusal from a rambling model, but a person
        can — at a glance, from the model's own words."""
        with pytest.raises(MailPermanentError, match="I cannot do that"):
            intent_json("I cannot do that")
