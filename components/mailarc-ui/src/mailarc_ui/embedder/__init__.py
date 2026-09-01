"""Setting the embedder, and being told what changing it costs.

``model``
    :class:`~mailarc_ui.embedder.model.EmbedderReading` — one load's answer —
    :class:`~mailarc_ui.embedder.model.EmbedJobView`, the functions that turn a
    pending change into a sentence, and every message the page can show. No
    I/O, so the warnings are checkable against a table of cases.
``state``
    :class:`~mailarc_ui.embedder.state.EmbedderSettingsState`: the
    write-only key, the merge read back through
    :class:`~mailarc_analytics.semantic.config.SemanticControl`, the adopt that
    makes a save take effect without a restart, and the ``embed`` job the
    warnings name as their remedy.
``components``
    The form, the key field that cannot show a key, the advice above the Save
    button, and the rebuild card under it.

Its own package and its own route rather than a panel on ``/insights``,
for three reasons that all point the same way. This one *writes* configuration
where that page reports on the archive, and a form with archive-wide
consequences does not belong as the sixth card under five tables somebody
scrolls past. It needs no graph, and must not: configuring an embedder is
something you do *before* semantic search works, so it has to be reachable on
an installation whose graph is down — while the insights page primes five graph
reads in its ``on_load``. And the search panel already tells a user that
semantic search is off and names the setting; a route is what makes that
sentence something they can act on.
"""

from mailarc_ui.embedder.components import (
    api_key_field,
    embed_card,
    embed_controls,
    embedder_panel,
    message_alerts,
    save_controls,
    settings_form,
)
from mailarc_ui.embedder.model import (
    REINDEX_FAILED,
    reindexed,
    EMBED_CANCEL_ASKED,
    EMBED_CANCEL_TOOK_EFFECT,
    EMBED_REMEDY,
    EMBED_RUNNING,
    KEY_CLEARED,
    KEY_NOT_STORED,
    LOAD_FAILED,
    NO_ADVICE,
    NO_CONTROL,
    NO_EMBED_JOB,
    NO_EMBEDDER_TO_RUN,
    NO_MODEL,
    PROVIDER_OPTIONS,
    RESET,
    SAVE_FAILED,
    SAVED,
    SAVED_NOT_ADOPTED,
    SAVED_NOT_SHOWN,
    SETTINGS_MOVED,
    UNSAVED_BEFORE_EMBED,
    WORKER_NOTE,
    Advice,
    EmbedderReading,
    EmbedJobView,
    gave_up_on,
    identity,
    host_advice,
    index_advice,
    key_status,
    vector_advice,
)
from mailarc_ui.embedder.state import (
    POLL_TICKS_ALLOWED,
    EmbedderSettingsState,
    semantic_control,
)

__all__ = [
    "EMBED_CANCEL_ASKED",
    "EMBED_CANCEL_TOOK_EFFECT",
    "EMBED_REMEDY",
    "EMBED_RUNNING",
    "KEY_CLEARED",
    "KEY_NOT_STORED",
    "LOAD_FAILED",
    "NO_ADVICE",
    "NO_CONTROL",
    "NO_EMBEDDER_TO_RUN",
    "NO_EMBED_JOB",
    "NO_MODEL",
    "POLL_TICKS_ALLOWED",
    "PROVIDER_OPTIONS",
    "REINDEX_FAILED",
    "RESET",
    "SAVED",
    "SAVED_NOT_ADOPTED",
    "SAVED_NOT_SHOWN",
    "SAVE_FAILED",
    "SETTINGS_MOVED",
    "UNSAVED_BEFORE_EMBED",
    "WORKER_NOTE",
    "Advice",
    "EmbedJobView",
    "EmbedderReading",
    "EmbedderSettingsState",
    "api_key_field",
    "embed_card",
    "embed_controls",
    "embedder_panel",
    "gave_up_on",
    "host_advice",
    "identity",
    "index_advice",
    "key_status",
    "message_alerts",
    "reindexed",
    "save_controls",
    "semantic_control",
    "settings_form",
    "vector_advice",
]
