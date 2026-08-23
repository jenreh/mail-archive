"""The sandbox in the root ``conftest.py``, pinned so it cannot rot silently.

Every other test in this repository is protected by that file, which makes it
the one piece of test infrastructure whose failure is invisible: if the
redirection stops working, nothing goes red — the suite simply starts writing
into the real archive under ``.state`` and carries on passing.

So each redirect is asserted here by name, and — the lesson of the review that
added half this module — by *resolved value* rather than by mechanism. Two
things passed for months while doing nothing:

* The bare configs were asserted and the **composed** one was not. Because
  appkit returns ``init_settings`` first, ``configuration/config.yaml``'s
  ``app.database`` / ``app.graph`` mapping outranked every ``app_*`` variable,
  and ``AppConfig`` inside a sealed run resolved to the real archive and the
  developer's own FalkorDB. Asserting ``DatabaseConfig()`` could never see it.
* The environment scrub was asserted by looking at ``os.environ``, which on a
  clean machine is empty of ``app_*`` keys anyway — so deleting the entire
  scrub left the suite green. A test that only fires when the machine is
  already dirty proves nothing on CI.

Both are now asserted by planting the condition and reading what a config
answers.

The root conftest is reached through pytest's plugin manager rather than with
``import conftest``. That import binds to whichever ``conftest.py`` reached
``sys.modules`` first, which in a repository-wide run is a component's — the
same trap ``components/mailarc-analytics/tests/planted_graph.py`` documents.
Registered plugins are keyed by absolute path, so asking for the root one by
path is the only form that means the same thing alone and in a full run.
"""

import os
import re
from pathlib import Path
from typing import Any

import pytest
from appkit_commons.database.configuration import DatabaseConfig

from app.configuration import configure
from mailarc_analytics import AnalyticsConfig
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.graph.config import GraphConfig

REAL_STORE = Path(".state/mailstore").resolve()
REAL_GRAPH_DATA = Path(".state/falkordb").resolve()


@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Any:
    """The root ``conftest.py``, fetched by path so the name cannot collide."""
    root = str(request.config.rootpath / "conftest.py")
    plugin = request.config.pluginmanager.get_plugin(root)
    assert plugin is not None, f"the root conftest is not a registered plugin: {root}"
    return plugin


class TestADefaultBuiltConfigLandsInTheSandbox:
    """The redirection, one assertion per thing a test could destroy."""

    def test_the_blob_store_is_not_the_real_one(self, sandbox: Any) -> None:
        """The write-once store: the one mistake that cannot be undone."""
        store = ArchiveConfig().store_dir.resolve()

        assert store != REAL_STORE
        assert store.is_relative_to(sandbox.SANDBOX)

    def test_the_database_is_not_the_real_one(self, sandbox: Any) -> None:
        """Pins ``url_override``; ``app_database_url`` is silently ignored."""
        url = str(DatabaseConfig().url)

        assert str(sandbox.SANDBOX) in url
        assert ".state/mail-archive.db" not in url

    def test_the_graph_is_neither_the_real_name_nor_the_real_directory(
        self, sandbox: Any
    ) -> None:
        """Both halves matter: a server already up would serve the real graph."""
        config = GraphConfig()

        assert config.graph_name != "mail-archive"
        assert config.data_dir.resolve() != REAL_GRAPH_DATA
        assert config.data_dir.resolve().is_relative_to(sandbox.SANDBOX)

    def test_nothing_defaults_to_a_reachable_graph_port(self) -> None:
        """A test that forgets its own server must fail to connect, not connect.

        Port 0 is never listening, so a forgotten ``GraphConfig()`` cannot reach
        whatever the developer happens to be running on 6379 — which is the
        real archive.
        """
        assert GraphConfig().port == 0


class TestTheComposedConfigLandsThereToo:
    """``AppConfig``, which the bare-config assertions above cannot see.

    ``app/__init__.py`` builds this at import, so every test module that
    touches ``app`` has it — and ``get_asyncdb_session`` and ``JobQueue()``
    both read it. Before ``configuration/config.test.yaml``, all five of these
    resolved to the real archive from inside a sealed run.
    """

    def test_the_composed_database_is_not_the_real_one(self) -> None:
        """The file holding the accounts and their encrypted credentials."""
        database = configure().app.database
        assert database is not None, "the application has to have one at all"
        url = str(database.url)

        assert ".state/mail-archive.db" not in url
        assert url == "sqlite+aiosqlite:///:memory:"

    def test_the_composed_graph_is_not_the_developers_own(self) -> None:
        """Name *and* port: a server on 6379 holds the real archive."""
        graph = configure().app.graph

        assert graph.port == 0
        assert graph.graph_name != "mail-archive"

    def test_the_composed_store_and_data_dir_stay_in_the_sandbox(
        self, sandbox: Any
    ) -> None:
        """The two keys ``config.yaml`` does not name, so the environment wins.

        Asserted here rather than trusted: the day somebody adds ``data_dir``
        or ``store_dir`` to ``config.yaml``, that becomes an init kwarg, the
        environment stops reaching them and these two land back in ``.state``.
        This is what turns that into a red test instead of a silent write.
        """
        composed = configure().app

        assert composed.graph.data_dir.resolve().is_relative_to(sandbox.SANDBOX)
        assert composed.archive.store_dir.resolve().is_relative_to(sandbox.SANDBOX)

    def test_the_suite_runs_under_the_test_profile(self) -> None:
        """The lever the four assertions above depend on."""
        assert configure().profile == "test"
        assert os.environ["PROFILES"] == "test"


class TestTheEnvironmentIsScrubbed:
    """A developer's ``.env`` must not be able to recalibrate the suite."""

    def test_no_component_prefix_survives_from_dotenv(self, sandbox: Any) -> None:
        """Every scrubbed prefix is either gone or one this file put back."""
        leaked = {
            name
            for name in os.environ
            if name.lower().startswith(sandbox.SCRUBBED_PREFIXES)
            and name not in sandbox.OVERRIDES
        }

        assert not leaked, f"{sorted(leaked)} reached the suite from the environment"

    def test_every_component_that_has_settings_is_on_the_prefix_list(
        self, sandbox: Any
    ) -> None:
        """The list is the whole scrub, so a prefix nobody added is a hole.

        Written out rather than derived from ``AppConfig``, because the point
        is that adding a component is a *decision* about whether its settings
        may reach a test — ``app_google_*`` is deliberately absent (a test that
        needs it skips itself). ``app_semantic_*`` was the hole this test was
        added for: sixteen call sites construct a bare ``SemanticConfig()``,
        and ``app_semantic_batch_size=1`` in a developer's environment turned
        three of the new tests red for a reason that had nothing to do with
        their change.
        """
        assert set(sandbox.SCRUBBED_PREFIXES) == {
            "app_analytics_",
            "app_archive_",
            "app_database_",
            "app_graph_",
            "app_mail_",
            "app_semantic_",
            "app_sync_",
        }

    def test_an_embedder_in_the_environment_cannot_reach_the_suite(
        self, sandbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worst case is not a red test, it is a green one: with
        ``app_semantic_provider=openai`` set, a bare ``SemanticConfig()`` in a
        test builds a real OpenAI adapter."""
        monkeypatch.setenv("app_semantic_provider", "openai")
        monkeypatch.setenv("app_semantic_batch_size", "1")

        sandbox.scrub_environment()

        assert "app_semantic_provider" not in os.environ
        assert "app_semantic_batch_size" not in os.environ

    def test_the_scrub_removes_a_key_planted_for_it(
        self, sandbox: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The condition, created rather than hoped for.

        The assertion above is correct and vacuous on a clean machine: this
        repository's ``.env`` holds no ``app_*`` key, so deleting the scrub
        entirely left the whole suite green. This one plants what the scrub
        exists to remove.
        """
        monkeypatch.setenv("app_analytics_min_group_size", "99")
        # Lower case on purpose: these are the names the component configs
        # actually read, which is what SCRUBBED_PREFIXES matches against.
        monkeypatch.setenv("app_google_client_id", "left-alone")  # noqa: SIM112

        sandbox.scrub_environment()

        assert "app_analytics_min_group_size" not in os.environ
        assert os.environ["app_google_client_id"] == "left-alone", (  # noqa: SIM112
            "the prefix list is deliberately narrow; app_google_* must survive"
        )

    def test_a_dotenv_on_disk_cannot_recalibrate_an_analysis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half the scrub cannot do, and the reason it exists.

        ``AnalyticsConfig`` declares ``env_file=".env"``, so its
        ``DotEnvSettingsSource`` opens that file relative to the working
        directory — it never consults ``os.environ``, and deleting a key from
        there does not touch it. Run in a directory holding a planted ``.env``
        rather than against the repository's own, because a test that wrote a
        threshold into the developer's ``.env`` to prove a point would be the
        accident this module is about.

        Without ``forget_dotenv`` this answers 99, which is what a threshold
        somebody left in ``.env`` months ago would do to the template and topic
        tests: move what they assert, silently.
        """
        (tmp_path / ".env").write_text("app_analytics_min_group_size=99\n")
        monkeypatch.chdir(tmp_path)

        assert AnalyticsConfig().min_group_size == 3


class TestTheTripwire:
    """The guard behind the redirection, exercised rather than assumed."""

    def test_it_reports_nothing_when_the_archive_is_untouched(
        self, sandbox: Any
    ) -> None:
        """Two fingerprints in a row agree — no spurious failure at teardown."""
        assert sandbox._fingerprint() == sandbox._fingerprint()

    def test_it_notices_a_new_blob(self, sandbox: Any, tmp_path: Path) -> None:
        """A single added file changes the store's fingerprint.

        Run against a stand-in ``.state`` rather than the real one, because a
        test that proved this by writing into the real store would be the exact
        accident the whole file exists to prevent.
        """
        store = tmp_path / "mailstore" / "ab"
        store.mkdir(parents=True)
        (store / "deadbeef.eml").write_bytes(b"planted")

        before = _fingerprint_of(sandbox, tmp_path)
        (store / "cafebabe.eml").write_bytes(b"planted too")

        assert _fingerprint_of(sandbox, tmp_path) != before

    def test_it_notices_a_database_rewritten_to_the_same_length(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """The database is fingerprinted by content, not by size.

        Replacing the hash with ``st_size`` left the full suite green, and a
        row edited in place is exactly the write that keeps a file's length.

        Size *and* timestamp are held still, so the ``(size, mtime)`` sweep
        cannot answer this one on the content hash's behalf — each mechanism is
        pinned by the case only it can see.
        """
        database = tmp_path / "mail-archive.db"
        database.write_bytes(b"aaaaaaaa")
        stamp = database.stat().st_mtime_ns

        before = _fingerprint_of(sandbox, tmp_path)
        database.write_bytes(b"bbbbbbbb")
        os.utime(database, ns=(stamp, stamp))

        assert _fingerprint_of(sandbox, tmp_path) != before

    def test_it_notices_a_committed_write_that_only_reaches_the_wal(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """SQLite runs in WAL mode, so the ``.db`` itself need never move.

        ``install_pragmas`` sets ``journal_mode=WAL`` on every engine this
        application opens, and ``.state`` already carries a ``-wal`` newer than
        its ``.db``. A guard that hashed only the main file could not see the
        one write it was built to catch.
        """
        (tmp_path / "mail-archive.db").write_bytes(b"unchanged")
        wal = tmp_path / "mail-archive.db-wal"
        wal.write_bytes(b"one frame")
        stamp = wal.stat().st_mtime_ns

        before = _fingerprint_of(sandbox, tmp_path)
        wal.write_bytes(b"two frame")
        os.utime(wal, ns=(stamp, stamp))

        assert _fingerprint_of(sandbox, tmp_path) != before

    def test_it_notices_a_file_it_was_never_taught_to_look_for(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """The catch-all, which is the half the named-file hashes cannot be.

        Neither ``mail-archive.db`` nor the blob store nor the graph dump: a
        file under ``.state`` that this guard has no name for still has to move
        the fingerprint, because the next thing somebody puts there will not be
        on the list either.
        """
        (tmp_path / "falkordb").mkdir()

        before = _fingerprint_of(sandbox, tmp_path)
        (tmp_path / "falkordb" / "appendonly.aof").write_bytes(b"planted")

        assert _fingerprint_of(sandbox, tmp_path) != before

    def test_it_is_quiet_where_there_is_no_archive(
        self, sandbox: Any, tmp_path: Path
    ) -> None:
        """A fresh checkout or CI has no ``.state``; there is nothing to guard."""
        assert _fingerprint_of(sandbox, tmp_path / "absent") == {}

    def test_two_equal_fingerprints_are_not_reported(self, sandbox: Any) -> None:
        """The comparison itself, which used to be untestable inline."""
        sandbox.report_change({"mailstore": "a"}, {"mailstore": "a"})

    def test_a_changed_fingerprint_fails_the_run(self, sandbox: Any) -> None:
        """Making the guard never raise left 1635 tests green. Not any more."""
        with pytest.raises(AssertionError, match="modified the real archive"):
            sandbox.report_change({"mailstore": "a"}, {"mailstore": "b"})

    def test_the_failure_names_what_changed(self, sandbox: Any) -> None:
        """The message has to be actionable; bisecting starts from the name."""
        with pytest.raises(AssertionError, match=re.escape("falkordb.rdb")):
            sandbox.report_change({}, {"falkordb.rdb": "written"})


def _fingerprint_of(sandbox: Any, state: Path) -> dict[str, str]:
    """The guard's own fingerprint, pointed at a directory this test owns.

    Swapped back in a ``finally``: the module-level ``REAL_STATE`` is what the
    session-scoped tripwire reads at teardown, so a test that left it pointing
    at its own ``tmp_path`` would disarm the guard for the whole run.
    """
    original = sandbox.REAL_STATE
    sandbox.REAL_STATE = state
    try:
        return sandbox._fingerprint()
    finally:
        sandbox.REAL_STATE = original


class TestTheGraphRuntimeIsNotOptional:
    """A repository-wide run may not get quieter as it loses coverage.

    Four modules skip themselves when ``task tauri:vendor`` has not produced
    the FalkorDB module, and every one of those skips is right on its own
    terms. The sum is not: over two hundred tests vanish — the KNN, the
    coverage read, the full-text index detection, the vector index the
    migration builds, "an operator cannot invert the search" — and the run
    still reports green, which is the shape of a gate that cannot fail.
    """

    def test_a_graph_selection_without_the_runtime_ends_the_run(
        self, sandbox: Any, monkeypatch: pytest.MonkeyPatch, request: Any
    ) -> None:
        monkeypatch.setattr(sandbox, "RUNTIME_DIR", Path("/nowhere/at/all"))

        with pytest.raises(pytest.UsageError, match="tauri:vendor"):
            sandbox.pytest_collection_modifyitems(
                None, request.config, [_marked("graph_local")]
            )

    def test_the_opt_out_is_the_deliberate_way_past_it(
        self, sandbox: Any, monkeypatch: pytest.MonkeyPatch, request: Any
    ) -> None:
        """Said on the command line, so nobody mistakes the result for a
        full pass."""
        monkeypatch.setattr(sandbox, "RUNTIME_DIR", Path("/nowhere/at/all"))
        allowed = _WithOption(request.config, allow=True)

        sandbox.pytest_collection_modifyitems(None, allowed, [_marked("graph_local")])

    def test_a_run_that_selected_no_graph_test_is_unaffected(
        self, sandbox: Any, monkeypatch: pytest.MonkeyPatch, request: Any
    ) -> None:
        """``pytest tests/test_worker.py`` on a machine with no runtime is a
        legitimate thing to do and loses nothing."""
        monkeypatch.setattr(sandbox, "RUNTIME_DIR", Path("/nowhere/at/all"))

        sandbox.pytest_collection_modifyitems(None, request.config, [_marked(None)])

    def test_the_runtime_this_machine_has_is_where_the_guard_looks(
        self, sandbox: Any
    ) -> None:
        """The guard is only worth its message if it reads the real path; a
        constant that had drifted would fail every run on every machine."""
        assert sandbox.RUNTIME_DIR.name == "falkordb"
        assert sandbox.RUNTIME_DIR.is_absolute()


class _WithOption:
    """A config that answers one option differently from the real one."""

    def __init__(self, config: Any, *, allow: bool) -> None:
        self._config = config
        self._allow = allow

    def getoption(self, name: str) -> Any:
        if name == "--allow-missing-runtime":
            return self._allow
        return self._config.getoption(name)


class _Marked:
    """A collected item that carries one marker and nothing else."""

    def __init__(self, marker: str | None) -> None:
        self._marker = marker

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name == self._marker else None


def _marked(marker: str | None) -> Any:
    return _Marked(marker)
