"""The embedder form, and the two sentences that stand between it and a save.

Every component here is a function of state vars and event handlers. None of
them opens a session, reads configuration or decides who is asking; that all
sits behind :mod:`mailarc_ui.embedder.state`, where it can be gated.

One thing in this file is load-bearing rather than cosmetic. **The key box has
no ``value`` prop at all** — it is uncontrolled, so there is no binding through
which a stored key could be rendered even if one had somehow reached a state
var. What surrounds it says whether a key is stored and what an empty box
means; neither can say which key it is, because neither is given it.

The two advice alerts sit *above* the Save button rather than beside the field
that produced them. A warning about invalidating every vector in the archive is
about the act of saving, not about the model box, and a reader scanning
downwards to the button should meet it on the way.
"""

import appkit_mantine as mn
import reflex as rx

from mailarc_ui.embedder.model import (
    NO_EMBEDDER_TO_RUN,
    PROVIDER_OPTIONS,
    WORKER_NOTE,
)
from mailarc_ui.embedder.state import EmbedderSettingsState
from mailarc_ui.kit import panel_card


def _advice(advice: rx.Var) -> rx.Component:
    """One piece of advice, or nothing — never an empty alert."""
    return rx.cond(
        advice.text != "",  # ty: ignore[unresolved-attribute]
        mn.alert(
            advice.text,  # ty: ignore[unresolved-attribute]
            color=advice.color,  # ty: ignore[unresolved-attribute]
            variant="light",
            py="xs",
            icon=rx.icon("triangle-alert", size=16),
        ),
        mn.text(""),
    )


def message_alerts() -> rx.Component:
    """Whatever the last action had to say, in the colour it earned."""
    return mn.stack(
        rx.cond(
            EmbedderSettingsState.error != "",
            mn.alert(
                EmbedderSettingsState.error,
                title="That did not work",
                color="red",
                variant="light",
                icon=rx.icon("triangle-alert", size=16),
            ),
            mn.text(""),
        ),
        rx.cond(
            EmbedderSettingsState.notice != "",
            mn.alert(
                EmbedderSettingsState.notice,
                color="green",
                variant="light",
                py="xs",
                icon=rx.icon("check", size=16),
            ),
            mn.text(""),
        ),
        gap="xs",
        w="100%",
    )


def api_key_field() -> rx.Component:
    """The key: collected here, never shown here, and cleared on its own button.

    Three parts, and each of them exists because of the same rule. The box is
    uncontrolled and therefore unable to display a stored key; the line under
    it says whether one is stored and that leaving the box empty keeps it; and
    the button beside it is the only way to remove one, because an empty box
    already means "unchanged".
    """
    return mn.stack(
        mn.password_input(
            label="API key",
            description=(
                "Only OpenAI needs one. Stored encrypted, and never shown "
                "again — not here and not anywhere else."
            ),
            placeholder="Leave empty to keep the stored key",
            default_value="",
            on_change=EmbedderSettingsState.set_api_key,
            disabled=EmbedderSettingsState.blocked,
            w="100%",
        ),
        mn.group(
            mn.badge(
                EmbedderSettingsState.key_status,
                variant="light",
                color=rx.cond(EmbedderSettingsState.api_key_stored, "green", "gray"),
                size="sm",
            ),
            rx.cond(
                EmbedderSettingsState.key_pending,
                mn.badge(
                    "Saving will replace it",
                    variant="light",
                    color="yellow",
                    size="sm",
                ),
                mn.text(""),
            ),
            mn.button(
                "Clear the stored key",
                on_click=EmbedderSettingsState.clear_api_key,
                disabled=~EmbedderSettingsState.can_clear_key,
                loading=EmbedderSettingsState.saving,
                variant="subtle",
                color="red",
                size="xs",
            ),
            gap="xs",
            align="center",
            wrap="wrap",
        ),
        rx.cond(
            EmbedderSettingsState.key_missing,
            mn.alert(
                "OpenAI is selected and no key is stored. Without one the "
                "embedding calls answer 401 and nothing is embedded.",
                color="yellow",
                variant="light",
                py="xs",
                icon=rx.icon("info", size=16),
            ),
            mn.text(""),
        ),
        gap="xs",
        w="100%",
    )


def settings_form() -> rx.Component:
    """Which service, which model, how long a vector, and where it lives."""
    return panel_card(
        mn.stack(
            mn.text("The embedder", fw=600, size="sm"),
            mn.text(
                "These four settings decide what a semantic search compares "
                "and what the sixth topic signal sees. Everything else in the "
                "archive works without them: with no embedder, the analyses "
                "still run and full-text search still answers.",
                size="xs",
                c="dimmed",
            ),
            mn.select(
                label="Provider",
                data=PROVIDER_OPTIONS,
                value=EmbedderSettingsState.provider,
                on_change=EmbedderSettingsState.set_provider,
                disabled=EmbedderSettingsState.blocked,
                allow_deselect=False,
            ),
            mn.text_input(
                label="Model",
                description="Empty means whatever the provider ships as its default.",
                placeholder="nomic-embed-text",
                value=EmbedderSettingsState.model,
                on_change=EmbedderSettingsState.set_model,
                disabled=EmbedderSettingsState.blocked,
            ),
            mn.number_input(
                label="Dimension",
                description=(
                    "Floats per vector. The graph's vector index is built for "
                    "one length; changing this needs a new index."
                ),
                value=EmbedderSettingsState.dimension,
                on_change=EmbedderSettingsState.set_dimension,
                disabled=EmbedderSettingsState.blocked,
                min=1,
                step=1,
                allow_decimal=False,
                allow_negative=False,
            ),
            mn.text_input(
                label="Base URL",
                description="Empty means the provider's own endpoint.",
                placeholder="http://localhost:11434",
                value=EmbedderSettingsState.base_url,
                on_change=EmbedderSettingsState.set_base_url,
                disabled=EmbedderSettingsState.blocked,
            ),
            api_key_field(),
            gap="sm",
        ),
    )


def save_controls() -> rx.Component:
    """The advice, then the buttons — in the order a reader meets them."""
    return mn.stack(
        _advice(EmbedderSettingsState.vector_advice),
        _advice(EmbedderSettingsState.index_advice),
        # Third, and the last one added: moving the base URL is the only change
        # that can send the *stored* key to another host, and the form used to
        # be silent about it while being verbose about the two whose worst
        # outcome is a re-embed.
        _advice(EmbedderSettingsState.host_advice),
        mn.group(
            mn.button(
                "Save",
                on_click=EmbedderSettingsState.save,
                loading=EmbedderSettingsState.saving,
                disabled=~EmbedderSettingsState.can_save,
                left_section=rx.icon("check", size=14),
                size="xs",
            ),
            mn.button(
                "Reload",
                on_click=EmbedderSettingsState.load,
                loading=EmbedderSettingsState.loading,
                left_section=rx.icon("refresh-cw", size=14),
                variant="light",
                size="xs",
            ),
            mn.button(
                "Use the configuration file",
                on_click=EmbedderSettingsState.use_configuration_file,
                disabled=EmbedderSettingsState.blocked,
                loading=EmbedderSettingsState.saving,
                left_section=rx.icon("undo-2", size=14),
                variant="subtle",
                size="xs",
            ),
            gap="sm",
            wrap="wrap",
        ),
        mn.text(
            "Use the configuration file forgets everything stored here, the "
            "API key included, and hands the archive back to config.yaml and "
            "the environment.",
            size="xs",
            c="dimmed",
        ),
        mn.text(WORKER_NOTE, size="xs", c="dimmed"),
        gap="sm",
        w="100%",
    )


def embed_controls() -> rx.Component:
    """Start a vector rebuild, or stop the one that is running."""
    return mn.group(
        mn.button(
            "Rebuild the vectors",
            on_click=EmbedderSettingsState.start_embed,
            loading=EmbedderSettingsState.starting,
            disabled=~EmbedderSettingsState.can_embed,
            left_section=rx.icon("play", size=14),
            variant="filled",
            size="xs",
        ),
        mn.button(
            "Cancel",
            on_click=EmbedderSettingsState.cancel_embed,
            loading=EmbedderSettingsState.cancelling,
            disabled=~EmbedderSettingsState.can_cancel_embed,
            left_section=rx.icon("square", size=14),
            variant="light",
            color="red",
            size="xs",
        ),
        # Beside the embed controls rather than in a card of its own: the two
        # are one procedure. Resizing the index forgets every vector, so
        # "Rebuild the index" is only ever followed by "Rebuild the vectors",
        # and putting them apart would let somebody do the first and walk away
        # from an archive that answers no semantic search at all.
        mn.button(
            "Rebuild the index",
            on_click=EmbedderSettingsState.rebuild_index,
            loading=EmbedderSettingsState.reindexing,
            disabled=~EmbedderSettingsState.can_reindex,
            left_section=rx.icon("refresh-cw", size=14),
            variant="light",
            size="xs",
        ),
        gap="sm",
        wrap="wrap",
    )


def embed_card() -> rx.Component:
    """Queue the job the warnings above name, and watch it from here.

    This card is the reason those warnings can name a button at all. Before it
    existed the only way to compute a vector was ``task graph:embed`` in a
    terminal — which the desktop bundle does not give anybody, so the remedy
    for the one warning this page exists to give named something its reader
    could not reach.

    The bar counts *messages*, unlike the insights page's rebuild bar, which
    counts stages: an embed run knows before its first batch how many messages
    owe it a vector. See
    :class:`~mailarc_ui.embedder.model.EmbedJobView`.
    """
    return panel_card(
        mn.stack(
            mn.group(
                mn.stack(
                    mn.text("Rebuild the vectors", fw=600, size="sm"),
                    mn.text(
                        "Embeds every message that has no vector under the "
                        "stored model, in batches, and can be stopped between "
                        "them. Nothing is deleted and nothing is recomputed "
                        "twice: a message that already carries a vector from "
                        "the model in force is skipped, so running this again "
                        "after a cancel continues where it stopped.",
                        size="xs",
                        c="dimmed",
                    ),
                    gap=4,
                ),
                embed_controls(),
                justify="space-between",
                align="flex-start",
                wrap="wrap",
                w="100%",
            ),
            rx.cond(
                EmbedderSettingsState.embedder_configured,
                mn.text(""),
                mn.alert(
                    NO_EMBEDDER_TO_RUN,
                    color="blue",
                    variant="light",
                    py="xs",
                    icon=rx.icon("info", size=16),
                ),
            ),
            rx.cond(
                EmbedderSettingsState.has_embed_job,
                mn.group(
                    mn.badge(
                        EmbedderSettingsState.job.status,
                        color=EmbedderSettingsState.job.status_color,
                        variant="light",
                        size="sm",
                    ),
                    mn.progress(
                        value=EmbedderSettingsState.job.percent,
                        color="blue",
                        size="lg",
                        striped=EmbedderSettingsState.job.active,
                        animated=EmbedderSettingsState.job.active,
                        flex="1",
                    ),
                    mn.text(
                        EmbedderSettingsState.job.percent_label,
                        size="sm",
                        fw=600,
                        w=52,
                        ta="right",
                        class_name="ma-tabular",
                    ),
                    mn.text(
                        EmbedderSettingsState.job.messages_label,
                        size="sm",
                        c="dimmed",
                        class_name="ma-tabular",
                    ),
                    gap="sm",
                    align="center",
                    w="100%",
                ),
                mn.text(""),
            ),
            rx.cond(
                EmbedderSettingsState.embed_message != "",
                mn.alert(
                    EmbedderSettingsState.embed_message,
                    color="yellow",
                    variant="light",
                    py="xs",
                ),
                mn.text(""),
            ),
            rx.cond(
                EmbedderSettingsState.job.error != "",
                mn.alert(
                    EmbedderSettingsState.job.error,
                    title="The rebuild stopped with an error",
                    color="red",
                    variant="light",
                    icon=rx.icon("triangle-alert", size=16),
                ),
                mn.text(""),
            ),
            gap="sm",
        ),
    )


def embedder_panel() -> rx.Component:
    """Everything above, in the order a page wants it.

    The rebuild card sits last, under the advice that names it: a reader who
    changes the model meets the warning about invalidated vectors on the way
    down to the Save button, and the control that fixes it immediately after.

    Owns the ``on_unmount`` because it owns the card that starts the poll.
    Without it somebody who navigates away mid-rebuild leaves a background task
    asking the database every :attr:`EmbedderSettingsState.poll_interval` for
    the life of the session — one per abandoned page.
    """
    return mn.stack(
        message_alerts(),
        settings_form(),
        save_controls(),
        embed_card(),
        gap="lg",
        w="100%",
        on_unmount=EmbedderSettingsState.stop_polling,
    )
