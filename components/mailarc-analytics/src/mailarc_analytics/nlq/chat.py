"""Three ways to ask a model for JSON, and one way to read what comes back.

Structurally this is :mod:`mailarc_analytics.semantic.embedder` with a
different payload: a shared base that owns the ``httpx.AsyncClient`` and maps a
status code onto :mod:`mailarc_core.mail.errors`, one subclass per provider,
and Azure inheriting OpenAI's request while changing only its header. Where the
two files differ is called out at each point rather than left to be noticed.

**Nothing from the archive is sent from here.** The body of every request is
the system prompt, the question a person typed and a JSON Schema — no address,
no mailbox name, no message text. That is the property the whole feature rests
on and this is the only module in a position to break it, so the argument lives
here: :meth:`_HttpChat._body` takes exactly three arguments and none of them
can carry a row out of the graph.

**Asking for a schema is not the same as getting one.** Every adapter asks its
provider to constrain the answer, and none of them trusts it: a small model
emits a fenced block, a reasoning model emits its own thinking first, and a
model that has run out of tokens emits half an object. :func:`intent_json` is
the one reader all three share, and it is written for what models actually
answer rather than for what they were asked.
"""

import json
import logging
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx

from mailarc_analytics.nlq.config import LlmConfig, LlmProvider
from mailarc_analytics.nlq.ports import ChatPort
from mailarc_core.mail.errors import (
    MailAuthError,
    MailError,
    MailPermanentError,
    MailTransientError,
)

logger = logging.getLogger(__name__)

_SERVER_ERROR = 500

DEFAULT_BASE_URLS: Mapping[LlmProvider, str] = MappingProxyType(
    {
        LlmProvider.OLLAMA: "http://localhost:11434",
        LlmProvider.OPENAI: "https://api.openai.com/v1",
        LlmProvider.AZURE_OPENAI: "",
    }
)
"""Where each provider lives, when the configuration does not say.

Azure is empty and stays empty: an Azure OpenAI resource belongs to one
customer and nothing here can guess its name, so
:meth:`AzureOpenAIChat._require_configured` refuses by name rather than sending
a request nowhere. The same table shape and the same empty entry as
:data:`~mailarc_analytics.semantic.embedder.DEFAULT_BASE_URLS`, for the same
reason.
"""

DEFAULT_MODELS: Mapping[LlmProvider, str] = MappingProxyType(
    {
        LlmProvider.OLLAMA: "qwen2.5:7b-instruct",
        LlmProvider.OPENAI: "gpt-4o-mini",
        LlmProvider.AZURE_OPENAI: "",
    }
)
"""The model each provider is asked for, when the configuration does not say.

Both defaults are chosen for the same property and it is not cleverness: the
job is to fill in a form, and an instruction-following model that reliably
emits the schema it was given beats a stronger one that reasons about the
question first. ``qwen2.5:7b-instruct`` runs on a laptop and honours Ollama's
``format``; ``gpt-4o-mini`` is the cheapest OpenAI model that supports
``response_format: json_schema``, which the adapter requires rather than
degrading past.

Azure is empty for :data:`DEFAULT_BASE_URLS`' reason — the value there is a
*deployment* name, which is unique to one resource.
"""

_THINKING = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
"""A reasoning model's scratch work, which is not part of its answer.

Older Ollama builds and several self-hosted gateways inline it in the content
rather than putting it in a field of its own, so it arrives ahead of the JSON
and no amount of ``format`` stops it.
"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
"""A markdown code fence, which several models add however they are asked."""


def build_chat_client(config: LlmConfig) -> ChatPort | None:
    """The interpreter this configuration asks for, or ``None`` for no provider.

    ``None`` rather than a null object, exactly as
    :func:`~mailarc_analytics.semantic.embedder.build_embedder` returns it: the
    caller has to be able to answer "is natural-language search available"
    without making a request, and a null object that raises on use could only
    be asked by trying.

    Never raises. The composition root calls this while ``app/app.py`` is being
    imported, and a misconfigured provider must cost this one feature rather
    than the whole application — so a provider that cannot answer is built and
    refuses at its first call instead.
    """
    if config.provider is LlmProvider.NONE:
        return None
    if config.provider is LlmProvider.OLLAMA:
        return OllamaChat(config)
    if config.provider is LlmProvider.AZURE_OPENAI:
        return AzureOpenAIChat(config)
    return OpenAIChat(config)


class _HttpChat:
    """What the three adapters share: a client, a URL, and the error mapping.

    A base class rather than a helper module because what is shared is the
    object's *lifetime* — the ``httpx.AsyncClient`` is created here and closed
    here. Not the port: :class:`~mailarc_analytics.nlq.ports.ChatPort` is what
    the rest of the project holds, and nothing outside this module should name
    this class.
    """

    def __init__(self, config: LlmConfig, provider: LlmProvider) -> None:
        self.model = config.model or DEFAULT_MODELS[provider]
        self._config = config
        self._provider = provider
        self._base_url = config.base_url or DEFAULT_BASE_URLS[provider]
        self._http = httpx.AsyncClient(timeout=config.request_timeout)
        self._closed = False

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call twice (§7.1)."""
        if self._closed:
            return
        self._closed = True
        await self._http.aclose()

    def _messages(self, system: str, question: str) -> list[dict[str, str]]:
        """The two turns every provider takes, and the only place the question
        is cut. :attr:`~mailarc_analytics.nlq.config.LlmConfig.max_question_chars`
        is what stops a pasted mail thread from becoming the prompt."""
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": question[: self._config.max_question_chars]},
        ]

    async def _post(
        self, path: str, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """One request, with the taxonomy already applied to whatever came back.

        :meth:`~mailarc_analytics.semantic.embedder._HttpEmbedder._post`'s
        shape, kept deliberately identical — including having **no ``params``
        argument**, which is what confines Azure to its v1 root.
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
                f"the model endpoint {url} is unreachable: {error}"
            ) from error
        if response.status_code != httpx.codes.OK:
            raise self._refusal(url, response)
        return self._payload(url, response)

    def _refusal(self, url: str, response: httpx.Response) -> MailError:
        """Turn a non-200 into the error that says what to do about it.

        The decision comes off the status code and never off the message, for
        the reason :meth:`~mailarc_analytics.semantic.embedder._HttpEmbedder.
        _refusal` gives: providers word their errors freely, a code is stable.

        One difference from the embedder's, and it is about who is waiting. A
        400 there means one batch of a long job will not embed and the job goes
        on; here it means the only request there was has failed, and the most
        likely cause is a model that does not support the structured output
        this adapter requires. So the permanent message names the setting.
        """
        status = response.status_code
        detail = _described(response)
        if status == httpx.codes.TOO_MANY_REQUESTS or status >= _SERVER_ERROR:
            return MailTransientError(
                f"the model endpoint returned {status} for {url}{detail}"
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            return MailAuthError(
                f"the model endpoint refused the credentials with {status} for "
                f"{url}{detail} — check app_llm_api_key"
            )
        return MailPermanentError(
            f"the model endpoint answered {status} for {url}{detail} — check "
            f"app_llm_model, since {self.model} may not support the structured "
            "output this needs"
        )

    def _payload(self, url: str, response: httpx.Response) -> dict[str, Any]:
        """The body as a JSON object, or a transient error.

        A 200 that is not JSON is a proxy or a captive portal, never the model
        server — the same call a minute later is the thing most likely to work.
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


class OllamaChat(_HttpChat):
    """A local model server — ``POST /api/chat``, no account and no key.

    Three things about this endpoint are load-bearing and each one is a way to
    lose an afternoon.

    **``stream`` must be sent as false.** ``/api/chat`` streams by default and
    answers NDJSON — several JSON objects, one per token — so
    :meth:`_HttpChat._payload` would meet a body that is not a single object
    and report a transient failure on *every* call, for a server that is
    working perfectly.

    **``format`` takes a whole JSON Schema**, not the string ``"json"``, since
    Ollama 0.5. Older servers answer 400, which is reported as the permanent
    failure it is rather than retried or quietly downgraded.

    **The prompt still has to ask for JSON.** Ollama's own guidance says so,
    and without it a constrained small model pads its answer with whitespace
    until it hits ``num_predict``. So :mod:`mailarc_analytics.nlq.prompt` ends
    with that sentence for every provider, not only this one.
    """

    def __init__(self, config: LlmConfig) -> None:
        super().__init__(config, LlmProvider.OLLAMA)

    async def complete(
        self, *, system: str, question: str, schema: Mapping[str, Any]
    ) -> str:
        """The model's answer, as the text of its one assistant message."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, question),
            "stream": False,
            "format": dict(schema),
            # `think` is ignored by models that do not reason and suppresses
            # the separate `thinking` field on the ones that do. What it cannot
            # suppress is inline `<think>` on older builds — `intent_json`
            # strips that.
            "think": False,
            "options": _options(self._config),
        }
        body = await self._post("/api/chat", payload, {})
        message = body.get("message")
        if not isinstance(message, dict):
            raise MailPermanentError(
                f"{self.model} answered without a 'message' object "
                f"(got {type(message).__name__})"
            )
        if body.get("done_reason") == "length":
            raise MailPermanentError(
                f"{self.model} ran out of room after "
                f"{self._config.max_output_tokens} tokens — raise "
                "app_llm_max_output_tokens"
            )
        return str(message.get("content") or "")


def _options(config: LlmConfig) -> dict[str, Any]:
    """Ollama's generation options, with the temperature omitted when unset.

    Absent rather than defaulted, for the reason
    :attr:`~mailarc_analytics.nlq.config.LlmConfig.temperature` gives: a
    reasoning model accepts only its own value and rejects any other, so there
    is no number that means "leave it alone".
    """
    options: dict[str, Any] = {"num_predict": config.max_output_tokens}
    if config.temperature is not None:
        options["temperature"] = config.temperature
    return options


class OpenAIChat(_HttpChat):
    """``POST /chat/completions``, with the answer constrained by a schema.

    ``response_format: json_schema`` with ``strict: true`` is required rather
    than preferred, and the adapter does **not** fall back to
    ``{"type": "json_object"}`` when a model refuses it. A silent downgrade is
    the failure this repository names in three other places: the request would
    keep working while quietly no longer honouring the schema, and the first
    sign would be a filter that had invented a field. A model too old for
    structured output is a configuration error, and
    :meth:`_HttpChat._refusal` says which setting.

    ``max_completion_tokens`` and not ``max_tokens``: the older name is
    rejected outright by newer models rather than ignored.
    """

    def __init__(self, config: LlmConfig, provider: LlmProvider | None = None) -> None:
        super().__init__(config, provider or LlmProvider.OPENAI)

    async def complete(
        self, *, system: str, question: str, schema: Mapping[str, Any]
    ) -> str:
        """The model's answer, checked for the two ways it can be no answer."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, question),
            "max_completion_tokens": self._config.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "query_intent",
                    "strict": True,
                    "schema": dict(schema),
                },
            },
        }
        if self._config.temperature is not None:
            # Omitted rather than defaulted, because a reasoning model accepts
            # only its own temperature and answers 400 to any other value.
            payload["temperature"] = self._config.temperature
        body = await self._post("/chat/completions", payload, self._headers())
        return self._content(body)

    def _headers(self) -> dict[str, str]:
        """OpenAI's bearer token, or nothing when no key is configured.

        No key produces a 401 the adapter reports as an auth failure, which is
        a better answer than a client-side guess at whether one was needed.
        """
        key = self._config.api_key
        if key is None:
            return {}
        return {"Authorization": f"Bearer {key.get_secret_value()}"}

    def _content(self, body: Mapping[str, Any]) -> str:
        """The one choice's text, or the reason there is no text to read.

        Both checks come before parsing on purpose. A truncated answer is
        invalid JSON and a refusal is prose, so without them the caller would
        meet "the model did not answer with a filter" for two states that have
        precise, different remedies — raise a limit, or ask something else.
        """
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise MailPermanentError(f"{self.model} answered with no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise MailPermanentError(
                f"{self.model} answered with {type(first).__name__} as a choice"
            )
        if first.get("finish_reason") == "length":
            raise MailPermanentError(
                f"{self.model} ran out of room after "
                f"{self._config.max_output_tokens} tokens — raise "
                "app_llm_max_output_tokens"
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise MailPermanentError(
                f"{self.model} answered a choice with no message object"
            )
        refusal = message.get("refusal")
        if refusal:
            raise MailPermanentError(f"{self.model} declined to answer: {refusal}")
        return str(message.get("content") or "")


class AzureOpenAIChat(OpenAIChat):
    """The same request :class:`OpenAIChat` sends, against your own resource.

    Azure's v1 surface — ``{endpoint}/openai/v1/chat/completions`` — is
    deliberately OpenAI-compatible, so this class inherits
    :meth:`OpenAIChat.complete` rather than repeating it and changes exactly
    the two things that are not interchangeable.

    The header is the first: Azure's key-based auth is ``api-key``, not
    ``Authorization: Bearer``. They are not synonyms, and a request sent with
    the wrong one is answered as though no key had been sent at all.

    The second is that neither default table can offer this provider anything,
    because both would have to name one customer's resource and deployment.
    :meth:`_require_configured` says so, and it runs at the first
    :meth:`complete` rather than at construction — see its docstring.

    **Only the v1 root works.** The legacy surface puts the deployment in the
    path and an ``api-version`` in a query string, and :meth:`_HttpChat._post`
    sends no query string at all. A legacy endpoint therefore 404s in a way
    that reads like a missing deployment, which is why the refusal below names
    the shape rather than only the setting.
    """

    def __init__(self, config: LlmConfig) -> None:
        super().__init__(config, LlmProvider.AZURE_OPENAI)

    async def complete(
        self, *, system: str, question: str, schema: Mapping[str, Any]
    ) -> str:
        """:meth:`OpenAIChat.complete`, refused first if nothing can answer it."""
        self._require_configured()
        return await super().complete(system=system, question=question, schema=schema)

    def _headers(self) -> dict[str, str]:
        """Azure's own key header. The one thing this class changes about the
        request — see :meth:`OpenAIChat._headers` for why an absent key is
        sent as an absent header rather than guessed at."""
        key = self._config.api_key
        if key is None:
            return {}
        return {"api-key": key.get_secret_value()}

    def _require_configured(self) -> None:
        """Refuse before a request that could only 404 or connect nowhere.

        Here and not in :meth:`__init__`, for the reason
        :meth:`~mailarc_analytics.semantic.embedder.AzureOpenAIEmbedder.
        _require_configured` gives: :func:`build_chat_client` runs while
        ``app/app.py`` is being imported, before any event loop or error
        boundary exists, and every provider's constructor is relied on never to
        raise. A config error surfacing there would take the whole application
        down over one unset variable.
        """
        key = self._config.api_key
        missing = [
            name
            for name, value in (
                ("app_llm_base_url", self._base_url),
                ("app_llm_model", self.model),
                ("app_llm_api_key", key.get_secret_value() if key else ""),
            )
            if not value.strip()
        ]
        if missing:
            raise MailPermanentError(
                "Azure OpenAI needs " + " and ".join(missing) + " — the endpoint "
                "is https://YOUR-RESOURCE.openai.azure.com/openai/v1, the "
                "model is the deployment name you gave it in Azure, and the "
                "key is the resource's own"
            )


def intent_json(answer: str) -> dict[str, Any]:
    """The object a model meant to send, out of whatever it actually sent.

    Four attempts in order, each for a shape models genuinely produce:

    1. The whole answer is the object. The only step a schema-constrained
       model needs, and the one that runs almost every time.
    2. A reasoning model's ``<think>`` block is stripped. Newer Ollama puts
       that in a field of its own; older builds and several gateways inline it.
    3. A markdown fence is unwrapped, because "answer with JSON" is read by
       many models as "answer with a JSON code block".
    4. The braces are scanned. Not ``find('{')`` to ``rfind('}')`` — that
       breaks on ``{"text": "a } b"}`` and on any trailing prose containing a
       brace — but a depth count that skips over string literals and honours
       backslash escapes.

    Raises :class:`~mailarc_core.mail.errors.MailPermanentError` when none of
    them finds an object, and permanent is the right verdict: the same question
    to the same model produces the same shape, so a retry is a loop. It is
    :class:`~mailarc_analytics.nlq.interpreter.QueryInterpreter` that turns
    this into a sentence a reader can act on.
    """
    for candidate in _candidates(answer):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    logger.debug("The model answered %d characters that held no object", len(answer))
    raise MailPermanentError(f"the model answered no JSON object: {answer[:200]!r}")


def _candidates(answer: str) -> list[str]:
    """Every reading of an answer worth trying, in order of how likely it is."""
    text = answer.strip()
    stripped = _THINKING.sub("", text).strip()
    found = [text, stripped]
    fenced = _FENCE.search(stripped)
    if fenced:
        found.append(fenced.group(1))
    found.extend(_braced(stripped))
    return [one for one in found if one]


def _braced(text: str) -> list[str]:
    """Every balanced ``{…}`` in the text, outermost first, in order.

    Every one and not just the first, because the first is not always the
    object: a model that prefaces its answer with an aside carrying a brace —
    ``Noted (see item {1} below): {"text": …}`` — puts ``{1}`` in front of the
    real one. Returning only that span made :func:`intent_json` refuse an
    answer that did hold a filter, and report it as no JSON object at all.
    """
    found: list[str] = []
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return found
        span = _balanced_at(text, start)
        if span:
            found.append(span)
            position = start + len(span)
        else:
            position = start + 1


def _balanced_at(text: str, start: int) -> str:
    """The balanced ``{…}`` opening at *start*, or ``""`` if it never closes."""
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    return ""


def _described(response: httpx.Response) -> str:
    """The provider's own words about a refusal, or an empty string.

    :func:`~mailarc_analytics.semantic.embedder._described`'s twin, and read
    the same way: opportunistically, never for the decision. OpenAI documents
    an ``error`` object with a ``message``; Ollama sends a bare ``{"error":
    "..."}`` string. Both are quoted if present and neither is required, so a
    proxy's HTML error page costs the reader a sentence rather than an
    exception.
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
