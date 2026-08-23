"""Every test runs against a sandbox archive, and never against the real one.

The repository root holds a live archive: ``.state/mail-archive.db`` with the
accounts and the credentials, ``.state/mailstore`` with the original ``.eml``
bytes of every imported message, and ``.state/falkordb`` with the graph. Those
are somebody's actual mail. A test that reaches them does not merely fail
noisily — it writes into them, and the blob store is content-addressed and
write-once, so what it adds cannot be told apart from what belongs there.

Nothing in the suite asks for that on purpose. It happens by *omission*: every
configuration in this project is a ``BaseConfig`` whose defaults point at the
real thing, so a test that builds ``ArchiveConfig()`` or ``GraphConfig()``
without arguments — which is exactly what a test of the defaults must do — is
one careless ``BlobStore(config)`` away from writing to the real store. The
individual fixtures are careful today. This file makes the carelessness
impossible tomorrow, for tests nobody has written yet.

Four mechanisms, because each closes a hole the others cannot reach:

**Redirection.** Every ``app_*`` environment variable the components read is
pointed at a per-run temporary directory before the first configuration object
is constructed. A default-built config therefore lands in the sandbox, not in
``.state``.

**A profile.** Environment variables are not enough for the *composed*
``AppConfig``, and the reason is a source-order rule rather than an oversight:
appkit returns ``init_settings`` first, and ``Configuration[AppConfig]``
validates the YAML mapping under ``app.database`` / ``app.graph`` into the
nested settings classes, so those values arrive as init kwargs and outrank
every ``app_*`` variable set here. Before ``configuration/config.test.yaml``
existed, ``app.database.url`` resolved to ``.state/mail-archive.db`` and the
graph to ``127.0.0.1:6379 / mail-archive`` *inside a sealed run* — the real
archive and the developer's own server. Only another init kwarg beats an init
kwarg, so the suite sets ``PROFILES=test`` and that file names the sandbox.

**No ``.env``.** Every component config declares ``env_file=".env"``, which
``DotEnvSettingsSource`` reads off disk on its own — deleting a key from
``os.environ`` does nothing to it. So the dotenv source is dropped from
appkit's own hook for the length of the run. It costs nothing: appkit calls
``load_dotenv(override=True)`` at import, so everything ``.env`` holds is in
``os.environ`` already and still reaches ``env_settings``; what the drop
removes is the second path by which a scrubbed ``app_*`` key gets back in.

**A tripwire.** ``.state`` is fingerprinted before the first test and again
after the last one. A test that finds a way past the redirection — an absolute
path written by hand, a subprocess with its own environment — fails the run
rather than being discovered months later by a missing mailbox.

The environment is also *scrubbed* of the ``app_*`` keys a developer's ``.env``
may hold. Those are settings for running the application, and letting them
reach the suite means the analyses are calibrated by whatever someone was last
experimenting with: a threshold tuned in ``.env`` would silently move what the
template and topic tests assert. ``mailarc-analytics``'s ``corpus.py`` already
fights this locally for its own prefix; this settles it for the whole run.
"""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Imported for its side effect, and deliberately before the overrides below:
# `appkit_commons/__init__.py` calls `load_dotenv(override=True)` at import
# time, so any environment this module set first would be overwritten by
# whatever `.env` happens to hold. Importing it here means our values are the
# last word — which is what `EnvSettingsSource` reads when a config is built.
import appkit_commons  # noqa: F401
import pytest
from appkit_commons.configuration.base import BaseConfig
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from mailarc_core.graph.runtime import DEFAULT_RUNTIME_DIR

REAL_STATE = Path(__file__).parent / ".state"
"""The live archive this file exists to keep tests away from."""

RUNTIME_DIR = Path(DEFAULT_RUNTIME_DIR).resolve()
"""Where ``task tauri:vendor`` puts the FalkorDB module the graph tests need."""

SANDBOX = Path(tempfile.mkdtemp(prefix="mailarc-tests-")).resolve()
"""Where a test's default-built configuration lands instead.

``resolve()``d on creation: on macOS the temporary directory sits under ``/var``,
which is a symlink to ``/private/var``, so a caller comparing a resolved config
path against an unresolved sandbox path would decide the redirection had not
worked. Both sides are resolved once, here.

One directory per run rather than pytest's ``tmp_path``: these are process-wide
environment variables read whenever a config is constructed, including inside a
subprocess, and that is older than any fixture.
"""

SCRUBBED_PREFIXES = (
    "app_analytics_",
    "app_archive_",
    "app_database_",
    "app_graph_",
    "app_mail_",
    "app_semantic_",
    "app_sync_",
)
"""Configuration a developer's ``.env`` must not be able to reach the suite with.

Not a blanklist of everything: ``app_google_*`` and the secret provider are left
alone, because a test that needs them skips itself and one that does not is
unaffected either way.

``app_semantic_`` is on the list for a sharper reason than recalibration.
Sixteen tests construct a bare ``SemanticConfig()``, so
``app_semantic_batch_size=1`` in a developer's shell turns three of them red
about somebody else's change — and ``app_semantic_provider=openai`` would have
those same tests build a real OpenAI adapter. A machine where an embedder is
being tried out is exactly the machine somebody runs this suite on.
"""

OVERRIDES = {
    # The composed `AppConfig`'s sandbox, and the only one that outranks
    # `configuration/config.yaml`. See that file's header for why an
    # environment variable cannot do this job.
    "PROFILES": "test",
    # A file of our own. The real one holds the accounts, the encrypted
    # credentials and the archived-message ledger.
    #
    # `app_database_url_override`, NOT `app_database_url`. Verified against the
    # installed appkit: `DatabaseConfig.url` is a `computed_field` over a stored
    # field called `url_override`, and the obvious-looking `app_database_url` is
    # accepted by the environment and then silently ignored — the config falls
    # back to appkit's `postgresql://postgres@localhost` default, or to whatever
    # `config.yaml` says, which here is the real archive. A sandbox that looks
    # set and is not is worse than no sandbox at all, so this name is pinned by
    # `tests/test_conftest_sandbox.py` rather than left to be rediscovered.
    "app_database_url_override": f"sqlite+aiosqlite:///{SANDBOX / 'mail-archive.db'}",
    # A blob store of our own. This is the one that cannot be undone: the store
    # is content-addressed and write-once, so a fixture written into the real
    # one is indistinguishable from an archived message.
    "app_archive_store_dir": str(SANDBOX / "mailstore"),
    # A graph of our own, and a data directory of our own. The name matters as
    # much as the directory: a server already running on the default port would
    # otherwise serve the real graph to a test that asked for `mail-archive`.
    "app_graph_data_dir": str(SANDBOX / "falkordb"),
    "app_graph_graph_name": "mail-archive-sandbox",
    # Nothing may reach a server on the default port. The suites that need a
    # real one start their own on a port they picked and pass the config
    # explicitly (`components/*/tests/**/conftest.py`); this default is here so
    # that a test which forgets fails to connect instead of connecting to
    # whatever the developer happens to be running.
    "app_graph_port": "0",
}
"""What a default-built configuration resolves to while the suite runs."""


_dotenv_dropped = False
"""Whether :func:`forget_dotenv` has already replaced appkit's hook.

A module flag rather than a marker on the replacement, so that wrapping the
wrapper is impossible however many times the hook is called.
"""


def scrub_environment() -> None:
    """Drop every ``app_*`` key this suite refuses to be configured by.

    Separate from :func:`pytest_configure` so a test can run it against an
    environment it planted itself. Calling it does *not* re-apply
    :data:`OVERRIDES`, which is the whole point: a test wants to see what the
    scrub alone removes.
    """
    for name in [
        key for key in os.environ if key.lower().startswith(SCRUBBED_PREFIXES)
    ]:
        del os.environ[name]


def forget_dotenv() -> None:
    """Take ``.env`` away from pydantic-settings for the length of the run.

    Scrubbing ``os.environ`` cannot suppress a *file* source: every component
    config declares ``env_file=".env"``, and ``DotEnvSettingsSource`` opens
    that file itself. Proved with a planted ``.env`` holding
    ``app_analytics_min_group_size=99``: after the scrub the key was gone from
    ``os.environ`` and ``AnalyticsConfig().min_group_size`` was still 99.

    Patching appkit's own hook is the narrowest seam that reaches every
    config — there is one ``settings_customise_sources`` and every
    ``BaseConfig`` inherits it, whereas ``env_file`` is re-declared in a dozen
    ``SettingsConfigDict``s that are not imported yet when this runs.

    Idempotent, and nothing is lost by it: appkit's ``load_dotenv(override=True)``
    has already copied ``.env`` into ``os.environ``, so every key the file holds
    still reaches ``env_settings`` — every key except the ``app_*`` ones the
    scrub just removed, which is exactly what this closes off.
    """
    global _dotenv_dropped
    if _dotenv_dropped:
        return
    original = BaseConfig.settings_customise_sources.__func__  # type: ignore[attr-defined]

    # The six positional parameters are pydantic-settings' hook signature, not
    # a choice this file gets to make.
    def without_dotenv(  # noqa: PLR0917
        cls: type[BaseConfig],
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = original(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        return tuple(one for one in sources if one is not dotenv_settings)

    BaseConfig.settings_customise_sources = classmethod(without_dotenv)  # type: ignore[assignment]
    _dotenv_dropped = True


def pytest_configure(config: pytest.Config) -> None:
    """Redirect every component's defaults into the sandbox.

    A hook rather than a fixture: configuration objects are built at *import*
    time by several test modules — ``mailarc-analytics``'s corpus builds its
    ``AnalyticsConfig`` as a module constant, precisely because a fixture runs
    too late — so the environment has to be right before collection starts,
    which is what ``pytest_configure`` guarantees.
    """
    del config  # the hook's signature; nothing here reads it
    scrub_environment()
    forget_dotenv()
    os.environ.update(OVERRIDES)
    (SANDBOX / "mailstore").mkdir(parents=True, exist_ok=True)
    (SANDBOX / "falkordb").mkdir(parents=True, exist_ok=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    """The one way to run this suite without the vendored graph server."""
    parser.addoption(
        "--allow-missing-runtime",
        action="store_true",
        default=False,
        help=(
            "let graph_local tests skip when the vendored FalkorDB runtime is "
            "absent instead of failing the run"
        ),
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Refuse a repository-wide run that would skip every graph test in silence.

    Four modules skip themselves when ``task tauri:vendor`` has not produced
    the runtime, and each skip is right on its own terms — a component wheel
    genuinely can be tested without a server. What is not right is the sum:
    over two hundred tests vanish, and with them everything only a real store
    can prove — the KNN, the coverage read, the full-text index detection, the
    vector index the migration builds, ``an operator cannot invert the search``
    — and the run still reports green. A gate that gets quieter as it loses
    coverage is not a gate.

    So a **repository-wide** run insists. A run inside a component keeps the
    skips, because this hook lives in the root ``conftest.py`` and pytest does
    not load it when the component's own ``pyproject.toml`` becomes the
    rootdir — which is exactly the standalone case the skip messages describe.

    ``--allow-missing-runtime`` is the deliberate way out, and being deliberate
    is its whole value: an operator who cannot vendor the runtime says so on
    the command line, and nobody mistakes the result for a full pass.
    """
    del session
    if config.getoption("--allow-missing-runtime"):
        return
    if not any(one.get_closest_marker("graph_local") for one in items):
        return
    if (RUNTIME_DIR / "falkordb.so").is_file():
        return
    raise pytest.UsageError(
        f"the vendored FalkorDB runtime is missing from {RUNTIME_DIR}, so every "
        "graph_local test would skip and the run would still report green. Run "
        "`task tauri:vendor`, or pass --allow-missing-runtime to accept the gap."
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    """Take the sandbox away again; it is a temporary directory, not a fixture."""
    del config
    shutil.rmtree(SANDBOX, ignore_errors=True)


def _fingerprint() -> dict[str, str]:
    """What the real archive looks like right now, cheaply enough to do twice.

    Three readings, because each misses what the others catch.

    The database is hashed with its **write-ahead log**. SQLite runs in WAL
    mode here — ``mailarc_core.database.sqlite.install_pragmas`` sets it on
    every engine this application opens — so a committed write lands in
    ``mail-archive.db-wal`` and leaves the main file's bytes untouched.
    Measured: after a committed insert the ``.db`` hash was unchanged. Hashing
    the ``.db`` alone was a guard that could not see the write it existed for.

    The blob store is counted and its names hashed rather than its bytes: it
    runs to hundreds of megabytes, the files are content-addressed, and a new
    one therefore always shows up as a new name.

    Everything else under ``.state`` is swept by ``(size, mtime_ns)``. That is
    the catch-all — it costs one ``stat`` per file and it notices a file this
    function was never taught to look for. It is also why a *running*
    application can trip the guard: that is the correct answer, because an app
    writing to the real archive while the suite runs is exactly the state in
    which a fingerprint proves nothing.

    What no fingerprint can see is a write into a **live FalkorDB**: the server
    holds the graph in memory and only serialises on ``SAVE`` or shutdown, so
    ``falkordb.rdb`` does not move during a run. Nothing here pretends
    otherwise — the protection against that one is the profile above, which
    stops the composed config naming the real server at all.
    """
    if not REAL_STATE.is_dir():
        return {}

    found: dict[str, str] = {}
    for name in (
        "mail-archive.db",
        "mail-archive.db-wal",
        "mail-archive.db-shm",
        "falkordb/falkordb.rdb",
    ):
        path = REAL_STATE / name
        if path.is_file():
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    store = REAL_STATE / "mailstore"
    if store.is_dir():
        names = sorted(
            str(one.relative_to(store)) for one in store.rglob("*") if one.is_file()
        )
        found["mailstore"] = (
            f"{len(names)}:{hashlib.sha256('|'.join(names).encode()).hexdigest()}"
        )

    found["tree"] = _tree_digest()
    return found


def _tree_digest() -> str:
    """Every file under ``.state`` as ``path:size:mtime``, hashed into one line.

    Deliberately not content: this is the reading that has to stay cheap enough
    to take over hundreds of megabytes twice a run, and a changed ``mtime_ns``
    is evidence enough to stop and look.
    """
    entries = []
    for one in sorted(REAL_STATE.rglob("*")):
        try:
            if not one.is_file():
                continue
            stat = one.stat()
        except OSError:  # vanished or unreadable mid-sweep; the digest moves anyway
            entries.append(f"{one}:?")
            continue
        entries.append(
            f"{one.relative_to(REAL_STATE)}:{stat.st_size}:{stat.st_mtime_ns}"
        )
    return hashlib.sha256("|".join(entries).encode()).hexdigest()


def report_change(before: dict[str, str], after: dict[str, str]) -> None:
    """Raise if two fingerprints of the real archive disagree.

    Split out of the fixture so the comparison itself can be tested. It could
    not be, while it lived inline: three separate mutations that disarmed the
    guard — never raising, never taking the second fingerprint, fingerprinting
    by size instead of content — each left the whole suite green.
    """
    if before == after:
        return

    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    raise AssertionError(
        "the test run modified the real archive under .state — "
        f"{', '.join(changed)} changed. Either a test reached past the sandbox "
        "this conftest sets up, or the application itself was running against "
        "the real archive while the suite ran. Find out which before running "
        "the suite again: what a test wrote into a content-addressed, "
        "write-once blob store cannot be told apart from a real archived "
        "message afterwards."
    )


@pytest.fixture(scope="session", autouse=True)
def _the_real_archive_is_untouched() -> Iterator[None]:
    """Fail the run if anything wrote to ``.state`` while the suite was running.

    The tripwire behind the redirection. It cannot say *which* test did it —
    hashing the store between every test would cost more than the suite — but
    it turns a silent corruption into a failed run, which is the difference
    that matters. Bisect with ``-x`` and a narrowing ``-k`` if it ever fires.

    Absent on a machine with no ``.state`` — a fresh checkout, or CI — where
    there is nothing to protect and both fingerprints are empty.
    """
    before = _fingerprint()
    yield
    report_change(before, _fingerprint())
