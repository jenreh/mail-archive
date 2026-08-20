# Testing

```sh
task test          # pytest with coverage; the gate is 80 %
```

The whole suite runs in about ten seconds. Keep it that way: nothing here waits
on a clock or a network, and the two tests that need a real graph server are
behind a marker.

## Where tests live

Beside the component they test, never in one central pile:

```text
tests/                                  the application — composition, worker, states, scripts
components/mailarc-core/tests/          mail/ archive/ database/ graph/  + test_isolation.py
components/mailarc-sync/tests/          engine/ jobs/
components/mailarc-google/tests/
components/mailarc-ui/tests/
components/mailarc-analytics/tests/
```

All six trees are listed in `[tool.pytest.ini_options] testpaths`, and all six
source trees in `[tool.coverage.run] source`. Adding a component means adding it
to both.

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

Two files carry it — `test_archive_writer_local.py` and `test_server_local.py` —
and they are the only tests that need a real graph server. Everything else runs
on a laptop with nothing installed.

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

Try it: add `import reflex` to `mailarc_sync/__init__.py` and watch it fail.

[`tests/test_worker.py`](https://github.com/jenreh/mail-archive/blob/main/tests/test_worker.py) does the same job for the
worker process — it proves from outside that `python -m app.worker` does not
pull Reflex in, rather than trusting that it does not.

## Testing a provider adapter

**No test may talk to the real provider.** Use `pytest-httpserver`, which is
already a dev dependency. The Gmail suite is the model — it covers a 429 with
`Retry-After`, an expired token, and a malformed token response, and never
leaves the machine.

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
