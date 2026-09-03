"""The three things the form warns about, and the three it used to get wrong.

Its own module beside ``test_ui_embedder_state.py`` because the questions are
pure: every case below is a value in and an :class:`~mailarc_ui.embedder.model.
Advice` out, with no database, no registry and no graph. That is what lets them
be enumerated — a standing mismatch, a correction, an unreadable index — instead
of demonstrated once each.

All three were reported by a review and all three are the same shape of defect:
the form compared the typed value against the *configured* one, and the
configured one is exactly what can be wrong.

* The vector length was compared against ``SemanticConfig.dimension`` rather
  than against the live index, so an archive already writing 1536 into a 768
  index was told nothing at all, and an administrator putting it back to 768
  was shown a red warning saying the graph needed an index it already had.
* The base URL was compared against nothing. Moving it re-points the *stored*
  bearer token at another host without the key being re-entered, and the form
  said less about that than about the two changes whose worst outcome is a
  re-embed.
"""

from mailarc_ui.embedder import (
    NO_ADVICE,
    EmbedderReading,
    host_advice,
    index_advice,
)

AT_768 = EmbedderReading(
    provider="ollama",
    model="nomic-embed-text",
    dimension=768,
    base_url="http://127.0.0.1:11434",
    index_dimension=768,
    index_known=True,
)
"""An archive whose configuration and whose index agree, which is the normal case."""


class TestTheVectorLengthIsComparedAgainstTheLiveIndex:
    """``indexing.verify`` and ``SemanticSearch._knn`` both read the real index.

    Their stated reason is that the configuration is what can be wrong. The
    form was the one surface that trusted it, which made it silent in the one
    state the page exists to warn about.
    """

    def test_agreeing_with_the_index_says_nothing(self) -> None:
        assert index_advice(AT_768, dimension=768) is NO_ADVICE

    def test_a_change_away_from_the_index_is_flagged(self) -> None:
        said = index_advice(AT_768, dimension=1536)

        assert said.color == "red"
        assert "768" in said.text
        assert "1536" in said.text

    def test_a_standing_mismatch_is_flagged_with_nothing_typed(self) -> None:
        """The case that used to be silent, and the expensive one.

        Once 1536 has been saved, ``_read`` reports ``dimension=1536`` and the
        form compared 1536 against 1536 and said nothing — while every vector
        written under it was accepted by the graph and left out of the index,
        with no error, no log line and no ``indexingFailures`` count.
        """
        drifted = AT_768.model_copy(update={"dimension": 1536})

        said = index_advice(drifted, dimension=1536)

        assert said.color == "red"
        assert "1536" in said.text

    def test_correcting_a_standing_mismatch_is_not_warned_about(self) -> None:
        """The fix must not be reported as the fault.

        Typing 768 over a stored 1536 makes the configuration match the index
        the graph already carries. The old comparison called that a change and
        said the graph needed an index at 768 "before an embed job writes
        anything" — which was false, and told an administrator to undo the one
        correct edit on the page.
        """
        drifted = AT_768.model_copy(update={"dimension": 1536})

        assert index_advice(drifted, dimension=768) is NO_ADVICE

    def test_an_unreadable_index_falls_back_to_the_configured_length(self) -> None:
        """A settings page has to work on an installation whose graph is down.

        Configuring the embedder is something you do *before* the graph works,
        so an unreadable index cannot be an error — but it must not become
        silence either, and the fallback warns on the same comparison the form
        made before.
        """
        blind = AT_768.model_copy(update={"index_known": False, "index_dimension": 0})

        assert index_advice(blind, dimension=768) is NO_ADVICE
        said = index_advice(blind, dimension=1536)

        assert said.color == "red"
        assert "could not be read" in said.text

    def test_a_length_of_zero_is_refused_before_anything_else(self) -> None:
        said = index_advice(AT_768, dimension=0)

        assert said.color == "red"
        assert "at least one float" in said.text

    def test_the_warning_names_the_procedure_it_asks_for(self) -> None:
        """ "The graph needs an index at 1536" named no way to make one.

        It takes a new ``graph_migrations`` revision with a different
        ``DIMENSION`` and a ``task graph:upgrade``; on the desktop bundle it
        cannot be done at all. A cost with no remedy beside it reads as a
        refusal the reader is expected to work around.
        """
        said = index_advice(AT_768, dimension=1536)

        assert "graph:upgrade" in said.text


class TestMovingTheEmbedderToAnotherHost:
    """The change that sends a stored credential somewhere new."""

    def test_an_unchanged_host_says_nothing(self) -> None:
        assert (
            host_advice(
                AT_768,
                provider="ollama",
                base_url="http://127.0.0.1:11434",
                keyed=False,
            )
            is NO_ADVICE
        )

    def test_a_new_host_is_flagged_even_without_a_key(self) -> None:
        """Two hosts serving a model of the same name are not the same embedder.

        A different build or quantisation of ``nomic-embed-text`` produces
        vectors in another space, and nothing downstream can tell: the vectors
        land in the same index under the same ``embedding_model``, so the KNN
        filter matches both and ranks across them. ``vector_advice`` never
        fires, because provider and model are unchanged.
        """
        said = host_advice(
            AT_768, provider="ollama", base_url="http://gpu.internal:11434", keyed=False
        )

        assert "gpu.internal" in said.text
        assert said.color == "blue"

    def test_a_new_host_with_a_key_in_force_says_the_key_travels(self) -> None:
        """The key does not have to be re-typed, so nothing else signals it."""
        openai = AT_768.model_copy(
            update={"provider": "openai", "base_url": "https://api.openai.com/v1"}
        )

        said = host_advice(
            openai, provider="openai", base_url="https://proxy.example", keyed=True
        )

        assert said.color == "yellow"
        assert "stored API key" in said.text
        assert "proxy.example" in said.text

    def test_azure_openai_says_the_key_travels_as_well(self) -> None:
        """The second provider that attaches the stored key to every call.

        Its adapter sends it as ``api-key`` rather than as a bearer token,
        which changes nothing about the cost: the host in this box is where
        the secret goes, and the key is not re-typed to send it there. A gate
        that asked only for ``openai`` was silent for the whole provider.
        """
        azure = AT_768.model_copy(
            update={
                "provider": "azure_openai",
                "base_url": "https://mine.openai.azure.com/openai/v1",
            }
        )

        said = host_advice(
            azure,
            provider="azure_openai",
            base_url="https://elsewhere.example/openai/v1",
            keyed=True,
        )

        assert said.color == "yellow"
        assert "stored API key" in said.text
        assert "elsewhere.example" in said.text

    def test_cleartext_to_a_host_that_is_not_this_machine_is_called_out(self) -> None:
        openai = AT_768.model_copy(
            update={"provider": "openai", "base_url": "https://api.openai.com/v1"}
        )

        said = host_advice(
            openai, provider="openai", base_url="http://proxy.internal:8080", keyed=True
        )

        assert said.color == "red"
        assert "not encrypted" in said.text

    def test_cleartext_to_this_machine_is_not_called_out(self) -> None:
        """Loopback is how Ollama is normally reached, and it leaves no wire."""
        said = host_advice(
            AT_768, provider="ollama", base_url="http://localhost:11434", keyed=False
        )

        assert said.color == "blue"
        assert "not encrypted" not in said.text

    def test_clearing_the_host_back_to_the_providers_own_is_not_a_secret_leak(
        self,
    ) -> None:
        """Empty means "the provider's own endpoint", which is where it began."""
        openai = AT_768.model_copy(
            update={"provider": "openai", "base_url": "https://proxy.example"}
        )

        said = host_advice(openai, provider="openai", base_url="", keyed=True)

        assert said.color == "blue"
        assert "stored API key" not in said.text
