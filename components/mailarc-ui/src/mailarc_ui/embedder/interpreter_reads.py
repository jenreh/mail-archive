"""What the interpreter half of the models page reaches outside itself for.

:mod:`mailarc_ui.embedder.reads`' twin, and separate for the reason the two
state classes are: it writes a different table through a different repository,
and one module holding both would have to name which of the two every function
meant.

What it deliberately does **not** duplicate is
:func:`~mailarc_ui.embedder.reads.is_absolute_http`. Whether a base URL sends a
stored credential over plain HTTP to somewhere that is not this machine is one
question with one answer, and two forms on one page answering it differently
would be worse than either answer.
"""

import logging
from datetime import datetime

from appkit_commons.database.session import get_asyncdb_session
from appkit_commons.registry import service_registry

from mailarc_analytics.nlq import LlmControl
from mailarc_core.database.entities import LLM_SETTINGS_ID, LlmSettingsEntity
from mailarc_core.database.repositories import LlmSettingsRepository

logger = logging.getLogger(__name__)

LLM_SETTINGS = LlmSettingsRepository()
"""The one repository this half writes through. Stateless, so one is enough."""

NO_CONTROL = (
    "The interpreter settings are not wired up in this build — no LlmControl "
    "is registered. The composition root publishes it; did app.composition run?"
)
"""The developer's error, in the one place a user could meet it.

Its own sentence rather than a bare ``KeyError``, and deliberately not the one
shown for an unconfigured provider: that is an ordinary state with a remedy,
and this means the application is half built.
"""


def llm_control() -> LlmControl:
    """The composition root's two verbs. Call inside a method only.

    ``mailarc-ui`` may not import ``app`` (§4.1), and merging a stored row over
    a configuration file is the composition root's work — so it arrives through
    the registry. Never at module level: that would run before the application
    had filled it.
    """
    try:
        return service_registry().get(LlmControl)
    except KeyError as error:
        raise RuntimeError(NO_CONTROL) from error


async def write_llm_settings(
    settings: tuple[str, str, str],
    api_key: str,
    baseline: datetime | None,
) -> None:
    """The three settings and, only if one was typed, the key — one transaction.

    One session for both, so a key that cannot be encrypted takes the settings
    down with it. The alternative is a stored row describing an OpenAI
    interpreter whose credential was never written, which fails later,
    elsewhere, as a 401.

    ``baseline`` is the row's ``updated`` as the form last read it, and turns
    the write into "store this if the row is still the one I read". ``None``
    means the form read no row, which is what a fresh installation honestly is.

    ``api_key`` empty is not passed on at all, which is the write-only rule's
    other half: :meth:`~mailarc_core.database.repositories.LlmSettingsRepository.store`
    has no parameter for it, so "leave the stored key alone" is the only thing
    an empty box can mean.
    """
    provider, model, base_url = settings
    async with get_asyncdb_session() as session:
        await LLM_SETTINGS.store(
            session,
            provider=provider,
            model=model,
            base_url=base_url,
            expected_updated=baseline,
        )
        if api_key:
            await LLM_SETTINGS.set_api_key(session, api_key)


async def clear_llm_api_key() -> None:
    """Drop the stored key and leave the three settings standing.

    Here rather than in the state class for the reason every other write in
    this module is here: the Reflex class holds the form, this module holds the
    session. A handler that opened its own would be the one write a later fix
    to these semantics silently missed.
    """
    async with get_asyncdb_session() as session:
        await LLM_SETTINGS.clear_api_key(session)


async def reset_llm_settings() -> None:
    """Forget every stored column and the key — one transaction, one session.

    Three nulls and a dropped key, which together mean "let the configuration
    file answer again". One session because a key that outlives the settings
    describing it is the same half-written row
    :func:`write_llm_settings` refuses to leave behind.
    """
    async with get_asyncdb_session() as session:
        await LLM_SETTINGS.store(session, provider=None, model=None, base_url=None)
        await LLM_SETTINGS.clear_api_key(session)


async def stored_llm_baseline() -> tuple[bool, datetime | None]:
    """Whether a key is stored, and the row's timestamp — in one read.

    Two questions of the same row, so one session rather than two. Neither
    answer is the key: ``api_key_is_set`` is an ``IS NOT NULL`` the database
    evaluates, and ``updated`` is a timestamp. The ciphertext is not fetched,
    not decrypted and not in this process — ``load`` is the composition root's
    read and is deliberately not called from here.
    """
    async with get_asyncdb_session() as session:
        stored = await LLM_SETTINGS.api_key_is_set(session)
        row = await session.get(LlmSettingsEntity, LLM_SETTINGS_ID)
        baseline = row.updated if row is not None else None
    return stored, baseline
