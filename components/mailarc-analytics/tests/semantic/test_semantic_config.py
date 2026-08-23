"""The settings that decide whether this archive needs anything installed.

Two of them are load-bearing beyond their own module and both are checked here
rather than trusted. ``provider`` defaults to ``none``, which is what keeps the
desktop application free of prerequisites — a default of ``ollama`` would make
a mail archive refuse to analyse anything until a model server was running.
And ``dimension`` has to equal the number the vector-index migration was
written with, because FalkorDB accepts a vector of any other length, stores it
and silently declines to index it.

Defaults are read off ``model_fields`` and not by constructing the class. A
constructed config reads the environment and a developer's ``.env``, so a test
that built one would be asserting what that machine happens to be set to.

:class:`SemanticOverrides` is checked at the end, and its rule is the one the
composition root depends on: an unset override falls through to the file, and
"unset" is ``None`` rather than empty — an empty string is already how ``model``
and ``base_url`` say "this provider's own default".
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from mailarc_analytics.semantic.config import (
    SemanticConfig,
    SemanticOverrides,
    SemanticProvider,
)


def default(name: str) -> Any:
    """One field's declared default, without building a config.

    Typed ``Any`` on purpose: the values are of nine different types and every
    caller below knows which one it asked for, so a narrower annotation would
    only buy a cast per assertion.
    """
    return SemanticConfig.model_fields[name].default


def migration_sources() -> list[Path]:
    """**Every** vector-index migration this checkout has.

    Searched upwards rather than hard-coded: the component is a wheel of its
    own and a checkout that holds only ``components/`` is a legitimate way to
    run these tests. An empty list skips the agreements below rather than
    failing them — there is nothing to disagree with.

    All of them, not the first. This used to answer ``sorted(glob)[0]``, which
    is the alphabetically first revision and not the head — so the day a second
    one lands (the migration's own docstring says changing ``DIMENSION`` needs
    exactly that) the pin would have gone on asserting against the superseded
    768 while the live index was something else. Per that same docstring a
    length mismatch is stored without error and simply not indexed, so the job
    would report every message embedded and the search would find none. Asking
    all of them is the shape that fails instead: a second revision that
    disagrees with the configuration is a red test, whichever order they sort
    in.
    """
    for parent in Path(__file__).resolve().parents:
        found = sorted((parent / "graph_migrations" / "versions").glob("*vector*.py"))
        if found:
            return found
    return []


def literal_in(source: Path, name: str) -> Any:
    """A module-level constant's value, read from the syntax tree.

    Not imported: a migration module is loaded by runic with its own machinery
    and importing one from a test would drag the whole migration environment in
    for a single integer.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{source.name} declares no {name}")


class TestTheDefaultIsNoEmbedder:
    def test_the_provider_defaults_to_none(self) -> None:
        """§7.4's decision, and the reason the app has no prerequisites.

        Without an embedder A1-A3 run in full and only semantic search and
        A2's sixth signal are missing. A default of ``ollama`` would trade
        that for a mail archive that cannot analyse anything until somebody
        installs a model server.
        """
        assert default("provider") is SemanticProvider.NONE

    def test_no_key_and_no_url_are_assumed(self) -> None:
        """An empty URL means "this provider's own default", which is the only
        answer that can be right for both: ``localhost:11434`` pointed at
        OpenAI is a connection error, and OpenAI's root pointed at a local
        model server is a 404."""
        assert default("base_url") == ""
        assert default("model") == ""
        assert default("api_key") is None

    def test_the_task_prefix_is_off_until_somebody_measures_it(self) -> None:
        """``nomic-embed-text`` wants a task instruction; whether Ollama's
        template already adds one is unverified, and adding it twice would
        embed the instruction rather than the mail."""
        assert default("task_prefix") is False


class TestTheDimensionMatchesTheIndex:
    def test_the_configured_dimension_is_the_migrated_one(self) -> None:
        """The one number that cannot be fixed after the fact.

        A vector of the wrong length is not refused by FalkorDB — it is
        stored and left out of the index, with no exception, no log line and
        no ``indexingFailures`` count. A configuration that drifted from the
        migration would therefore report every message embedded and find none
        of them.
        """
        sources = migration_sources()
        if not sources:
            pytest.skip("no graph_migrations/ in this checkout")

        assert [literal_in(one, "DIMENSION") for one in sources] == [
            default("dimension") for _ in sources
        ]

    def test_the_migration_asks_for_cosine(self) -> None:
        """Both models are trained to be compared by cosine, and FalkorDB
        accepts only ``cosine`` or ``euclidean`` — it refuses ``l2``, ``dot``
        and ``ip`` at the server, not merely in runic."""
        sources = migration_sources()
        if not sources:
            pytest.skip("no graph_migrations/ in this checkout")

        assert {literal_in(one, "SIMILARITY") for one in sources} == {"cosine"}

    def test_the_migration_does_not_ship_runics_default_ef_runtime(self) -> None:
        """There is no query-time override — the KNN procedure takes exactly
        four arguments — so the migration's literal is the only chance to set
        it, and runic's default of 10 measured 14 % recall@10 where 512
        measured 99 %."""
        sources = migration_sources()
        if not sources:
            pytest.skip("no graph_migrations/ in this checkout")
        settings = [literal_in(one, "EF_RUNTIME") for one in sources]

        assert all(isinstance(one, int) for one in settings)
        assert all(one >= 256 for one in settings)


class TestTheNumbersThatBoundAJob:
    def test_a_page_is_larger_than_a_batch(self) -> None:
        """A page is one graph round trip and a batch is one HTTP call. The
        other way round would make every page a partial batch and turn the
        embedder's batching into a per-message loop."""
        assert int(default("page_size")) > int(default("batch_size"))

    def test_the_body_is_cut_well_under_every_provider_limit(self) -> None:
        """Roughly two thousand tokens. Past that, an embedding of a quoted
        thread describes the thread rather than the message."""
        assert 1_000 <= int(default("max_body_chars")) <= 16_000

    def test_a_semantic_search_over_fetches(self) -> None:
        """FalkorDB's KNN cannot be filtered before the fact, so a search that
        asked for exactly what it wanted would come back short by whatever it
        then dropped."""
        assert int(default("knn_over_fetch")) > 1

    def test_the_topic_gate_is_strict(self) -> None:
        """Signal 6's only defence. At 0.7 an invoice and a delivery note are
        neighbours in every model, and the suggestion would swallow the
        archive."""
        assert float(default("topic_similarity_min")) >= 0.8


def configured(**overrides: Any) -> SemanticConfig:
    """A ``SemanticConfig`` whose five overridable fields are all stated.

    Every one is passed explicitly rather than left to its default, for the
    reason this module reads defaults off ``model_fields``: a constructed
    ``BaseSettings`` reads the environment, and a test asserting precedence
    must not be asserting what the machine it runs on happens to be set to.
    Init kwargs outrank every other settings source, so these five are pinned.
    """
    values: dict[str, Any] = {
        "provider": SemanticProvider.OLLAMA,
        "model": "from-the-file",
        "dimension": 768,
        "base_url": "http://file.invalid",
        "api_key": SecretStr("from-the-file"),
    }
    return SemanticConfig(**(values | overrides))


class TestOverridesFallThroughToTheFile:
    def test_nothing_stored_is_the_configuration_itself(self) -> None:
        """Identity, not equality. A fresh installation must resolve to the
        object the file produced, which is what lets the composition root see
        that it has nothing to rebuild."""
        config = configured()

        assert SemanticOverrides().applied_to(config) is config

    def test_an_unset_field_leaves_the_configured_one_alone(self) -> None:
        """The whole precedence rule: ``None`` means "not set", so changing
        the model does not also make somebody restate the provider."""
        merged = SemanticOverrides(model="stored").applied_to(configured())

        assert merged.model == "stored"
        assert merged.provider is SemanticProvider.OLLAMA
        assert merged.dimension == 768
        assert merged.base_url == "http://file.invalid"
        assert merged.api_key is not None
        assert merged.api_key.get_secret_value() == "from-the-file"

    def test_an_empty_string_is_a_decision_and_not_an_absence(self) -> None:
        """``""`` already means "whatever this provider's own default is", so
        clearing a field back to that is expressible without a second
        sentinel — and must not be mistaken for "unset"."""
        merged = SemanticOverrides(model="", base_url="").applied_to(configured())

        assert merged.model == ""
        assert merged.base_url == ""

    def test_every_stored_value_wins(self) -> None:
        stored = SemanticOverrides(
            provider=SemanticProvider.OPENAI,
            model="text-embedding-3-small",
            dimension=1536,
            base_url="http://stored.invalid",
            api_key=SecretStr("from-the-store"),
        )

        merged = stored.applied_to(configured())

        assert merged.provider is SemanticProvider.OPENAI
        assert merged.model == "text-embedding-3-small"
        assert merged.dimension == 1536
        assert merged.base_url == "http://stored.invalid"
        assert merged.api_key is not None
        assert merged.api_key.get_secret_value() == "from-the-store"

    def test_the_calibration_settings_are_not_overridable(self) -> None:
        """Five fields and not thirteen. ``topic_similarity_min`` is the only
        thing keeping signal 6 out of half the archive, and a form that
        offered it would invite somebody to move it without knowing that."""
        assert set(SemanticOverrides.model_fields) == {
            "provider",
            "model",
            "dimension",
            "base_url",
            "api_key",
        }

    def test_the_merge_leaves_the_configuration_untouched(self) -> None:
        """A copy, not a mutation: the registered config is shared, and a
        merge that wrote into it would change what every later reader sees."""
        config = configured()

        SemanticOverrides(provider=SemanticProvider.OPENAI).applied_to(config)

        assert config.provider is SemanticProvider.OLLAMA


class TestOverridesRefuseNonsense:
    def test_a_provider_no_release_ever_wrote_is_refused(self) -> None:
        """A hand-edited database, or a downgrade that left a newer value
        behind. Refusing here is what lets the composition root log it and
        keep the file's embedder instead of building something impossible."""
        with pytest.raises(ValidationError):
            SemanticOverrides(provider="gemini")

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_dimension_that_cannot_produce_a_vector_is_refused(
        self, bad: int
    ) -> None:
        """Not a smaller index — an embedder that can never write a vector the
        graph will accept, and per this module's other tests a wrong length is
        stored without error and simply not indexed."""
        with pytest.raises(ValidationError):
            SemanticOverrides(dimension=bad)

    def test_a_provider_name_is_still_accepted_as_the_string_it_is_stored_as(
        self,
    ) -> None:
        """The column is a ``VARCHAR(32)``; the value object is the enum. The
        coercion is what the composition root leans on when it reads a row."""
        stored = SemanticOverrides(provider="openai")

        assert stored.provider is SemanticProvider.OPENAI


class TestTheKeyIsNotPrinted:
    def test_the_repr_does_not_carry_the_key(self) -> None:
        """``SecretStr`` is what makes this true, and it is worth a test: this
        object is built from a decrypted database column, and a ``repr`` in a
        log line or a traceback is exactly how a key escapes."""
        stored = SemanticOverrides(api_key=SecretStr("sk-do-not-print"))

        assert "sk-do-not-print" not in repr(stored)
        assert "sk-do-not-print" not in str(stored)

    def test_the_merged_configuration_does_not_carry_it_either(self) -> None:
        merged = SemanticOverrides(api_key=SecretStr("sk-do-not-print")).applied_to(
            configured()
        )

        assert "sk-do-not-print" not in repr(merged)
