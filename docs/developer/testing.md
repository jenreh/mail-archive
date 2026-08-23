# Testing

```sh
task test          # pytest with coverage; the gate is 80 %
```

The whole suite runs in about half a minute. Keep it near that: nothing here
waits on a clock or a network, and the tests that need a real graph server are
behind a marker.

## The sandbox, and why every test already has one

There is one archive on a developer machine and it is real mail — the accounts
and their encrypted credentials in `.state/mail-archive.db`, the original bytes
of every imported message in `.state/mailstore`, the graph in
`.state/falkordb`. The blob store is content-addressed and write-once, so a
fixture written into it cannot afterwards be told apart from a message that was
really archived. The root `conftest.py` is what keeps the suite away from all
three, and it does it four ways:

- **Redirection.** Every `app_*` environment variable is pointed at a
  per-run temporary directory before collection starts, so a default-built
  `ArchiveConfig()` or `GraphConfig()` lands there. The database one is
  `app_database_url_override`, **not** `app_database_url`: `DatabaseConfig.url`
  is a computed field over a stored `url_override`, and the obvious name is
  accepted by the environment and then silently ignored.
- **A profile.** Environment variables lose to YAML. appkit returns
  `init_settings` first and `Configuration[AppConfig]` validates
  `configuration/config.yaml`'s `app.database` / `app.graph` mapping into the
  nested settings classes, so those arrive as init kwargs and outrank every
  `app_*` variable — which is why the composed config used to resolve to the
  real archive and the developer's own FalkorDB from inside a sealed run. The
  suite therefore also sets `PROFILES=test`, and `configuration/config.test.yaml`
  names the sandbox from the one source nothing outranks.
- **No `.env`.** Component configs declare `env_file=".env"` and
  `DotEnvSettingsSource` opens that file itself, so scrubbing `os.environ`
  cannot reach it. The dotenv source is dropped from appkit's hook for the
  length of the run; everything the file holds is in `os.environ` already,
  because appkit calls `load_dotenv(override=True)` at import.
- **A tripwire.** `.state` is fingerprinted before the first test and after the
  last, and a difference fails the run.

**If the tripwire fires**, the run is telling you one of two things: a test
reached past the sandbox, or the application itself was writing to the real
archive while the suite ran. Rule the second out first — stop `task run` and
any worker — then bisect with `-x` and a narrowing `-k`. What the message names
is the file that changed.

The one rule a new test has to follow: **never construct a configuration that
writes without giving it an explicit path.** A test of a default belongs in
`components/mailarc-core/tests/archive/test_archive_config.py`'s shape — read
`Cls.model_fields["name"].default` rather than building the object.

## Where tests live

Beside the component they test, never in one central pile:

```text
tests/                                  the application — composition, worker, states, scripts
components/mailarc-core/tests/          mail/ archive/ database/ graph/  + test_isolation.py
components/mailarc-sync/tests/          engine/ jobs/
components/mailarc-google/tests/
components/mailarc-imap/tests/          + imap_server.py and tls.py, a real IMAP server
components/mailarc-m365/tests/
components/mailarc-ui/tests/
components/mailarc-analytics/tests/
components/mailarc-mcp/tests/          needs the `mcp` extra — `task install` has it
```

All nine trees are listed in `[tool.pytest.ini_options] testpaths`, and all eight
source trees in `[tool.coverage.run] source`. Adding a component means adding it
to both.

One tree is borrowed from: `tests/test_composition.py` imports
`components/mailarc-imap/tests/imap_server.py` rather than growing a second IMAP
server, and appends that directory to `sys.path` itself — which is also why it is
in `[tool.ty.environment] extra-paths`. It is the only such crossing in the
repository, and it exists because IMAP has no `pytest-httpserver`.

`components/mailarc-mcp/` is the one an installation may leave out (`uv sync`
without `--extra mcp` — the desktop bundle's shape), so `task install` asks for
the extra: a checkout that could not collect that tree would be green for the
wrong reason. `test_isolation.py` skips the component rather than failing when
it is absent, because a test that fires on exactly the installation an extra
exists to produce punishes the feature.

`asyncio_mode = "auto"`, so an `async def test_…` needs no decorator.

## Use the real thing wherever it is cheap

That is the house style, and it is what the tests actually do. From the engine
suite's own docstring:

> Real everywhere it is cheap to be: a real `FakeMailSource` over real `.eml`
> files, a real parser, a real blob store on `tmp_path`, a real SQLite file with
> the real repositories, and the real `MessageArchiver`. The one stand-in is the
> graph session.

The UI state tests make the same trade for the same reason:

> Against a real SQLite file and the real `JobQueue`. […] a hand-written double
> would be free to agree with the projection and be wrong.

So: real SQLite on `tmp_path`, real repositories, real parser, real blob store.
The two things faked are the ones that would cost a server or a wall-clock wait
— the graph session, and the clock.

## The markers

```toml
markers = [
    "graph_local: starts the vendored FalkorDB (needs `task tauri:vendor`)",
]
```

Every file whose name ends `_local.py` carries it, and they are the only tests
that need a real graph server — three in `mailarc-core`, seven in
`mailarc-analytics`, two in the repository's own `tests/` (the MCP server end
to end, and the vector-index migration run against a live store). Everything
else runs on a laptop with nothing installed.

The marker is declared three times on purpose, for two reasons: once in the
repository's `pyproject.toml` for a full run, and once each in
`components/mailarc-analytics` and `components/mailarc-core` for a run started
from inside the component, which picks that component's own ini file. An unregistered marker is a warning on
every collected test, so a component that owns `graph_local` tests declares it.

**The runtime is not optional in a full run.** Each `_local.py` file skips
itself when `task tauri:vendor` has not produced `falkordb.so`, which is right
for a component tested as a standalone wheel and wrong for the repository: over
two hundred tests would vanish and the run would still report green. So the
root `conftest.py` ends a repository-wide run that selected `graph_local` tests
without a runtime, with a message naming the task. `--allow-missing-runtime`
is the deliberate way past it, and saying so on the command line is the point —
nobody then mistakes the result for a full pass.

Each of those two components starts **one** FalkorDB per session and gives each
test a graph name of its own. A function-scoped server would spawn a
`redis-server` per test and leave every one of them to be reaped at interpreter
exit — which turns a suite that runs in seconds into one that looks finished and
then hangs for minutes.

```sh
uv run pytest -m "not graph_local"     # skip them
uv run pytest -m graph_local           # only them, after task tauri:vendor
```

## The isolation test

[`components/mailarc-core/tests/test_isolation.py`](https://github.com/jenreh/mail-archive/blob/main/components/mailarc-core/tests/test_isolation.py)
enforces the two architecture rules that a convention alone would not hold.

It runs **in a subprocess**, because the application's own tests import Reflex
into the shared interpreter, which would make an in-process check meaningless.
And it walks submodules with `pkgutil.walk_packages` rather than a hand-written
list, so it keeps covering whatever later phases add instead of going stale.

**Test 1 — nothing below the UI drags in a browser.** `mailarc_core`,
`mailarc_sync`, `mailarc_analytics` and `mailarc_google` must not pull in
`reflex`, `appkit_mantine`, `appkit_ui` or `appkit_user`. `mailarc_ui` is
deliberately exempt.

**Test 2 — no component reaches for `runic.rag`.** All five, this time. Email
already carries an exact graph in its headers; nothing a model invents may join
that ground truth.

Both tests name their packages in hand-written tuples rather than discovering
them, so **`mailarc_imap` and `mailarc_m365` are not in either list yet.** Both
hold the rules, and each carries its own probe: `mailarc-imap` parses the source
of every module in the package and asserts the bans against what it finds,
`mailarc-m365` imports the package in a subprocess and reads `sys.modules`. A
source check catches a written import but not a transitive one, so the central
file is still the one that matters — it is one edit behind, and the components'
READMEs say so rather than claiming enforcement they do not have.

Try it: add `import reflex` to `mailarc_sync/__init__.py` and watch it fail.

[`tests/test_worker.py`](https://github.com/jenreh/mail-archive/blob/main/tests/test_worker.py) does the same job for the
worker process — it proves from outside that `python -m app.worker` does not
pull Reflex in, rather than trusting that it does not.

## Testing a provider adapter

**No test may talk to the real provider.** Use `pytest-httpserver`, which is
already a dev dependency. The Gmail suite is the model — it covers a 429 with
`Retry-After`, an expired token, and a malformed token response, and never
leaves the machine. `mailarc-m365` does the same against a local Graph, with MSAL
replaced at `refresh_async` so no token request can leave either.

`pytest-httpserver` is no help for a protocol that is not HTTP. `mailarc-imap`
runs a real IMAP4rev1 server on a loopback socket, over TLS with a throwaway
certificate the adapter genuinely verifies — the adapter offers no way to switch
verification off, so a suite that skipped it would never exercise that path. Its
failure paths are knobs on that server: refuse the login, drop the socket
mid-command, answer `EXAMINE` without a `UIDVALIDITY`, put a folder name on the
wire that is not valid modified UTF-7.

`tests/test_composition.py` then holds the whole registry to its descriptors: it
builds every registered provider from a fixture secret, calls `watermark()`, and
asserts that the set of fixture secrets equals the set of registered providers —
so a provider wired in without one fails loudly rather than being skipped.

See [adding a mail provider](adding-a-provider.md#testing-it) for the checklist.

## Testing Reflex state

Follow the `reflex-testing-state` skill's pattern. Two things bite:

- A Reflex `State` needs pytest to instantiate — construct it the way the
  existing tests do.
- A background event handler holds the state lock only around its mutations.
  Test the mutation, not the lock.

The existing suites (`test_ui_accounts_state.py`, `test_ui_imports_state.py`)
fake only the clock and the state lock, so nothing waits.

## Coverage

```toml
fail_under = 80
branch = true
```

`task test` fails below 80 %. Excluded from the count: `if TYPE_CHECKING:`,
`if __name__ == "__main__":`, and anything marked `# pragma: no cover`.

## Diagnosing a slow suite

If the suite feels slow, decide **execution-slow vs teardown-hang** before
optimising anything. Compare the runner's own summary against shell wall-clock:

```sh
time uv run pytest -q
```

Wall-clock far above the runner's number with CPU near 0 % means the process is
hanging at teardown, almost always on a subprocess fixture being reaped. Per-test
durations look fine and will mislead you — the cost is outside the runner's
measured window.

The fix is a session-scoped fixture with an explicit shutdown, isolated per test
by a cheap unique key (a fresh graph name) rather than a fresh server.

## The rest of the gate

```sh
task format      # ruff check --fix, then ruff format
task lint        # ruff check
task typecheck   # ty check app components/*/src
task test
```

All four, green, before anything is called done. Line length 88, Python 3.14,
type annotations on every function and method.

Two rules ruff will not catch:

- **No `print`.** Use `logging`.
- **No f-strings in logger calls.** `log.info("Loaded %d", count)`, never
  `log.info(f"Loaded {count}")`.
