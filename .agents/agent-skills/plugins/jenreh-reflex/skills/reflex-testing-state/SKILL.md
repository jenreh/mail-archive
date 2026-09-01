---
name: reflex-testing-state
description: >
  Generates pytest unit tests for Reflex.dev state logic — event handlers,
  computed vars, substates, and mocked external dependencies. Use when the user asks to
  write, scaffold, or review tests for a Reflex State class. Do NOT use for Playwright
  integration tests or UI component rendering tests.
---

# Testing Reflex State

Reflex `State` needs no browser or server — test it directly with `pytest` and
`pytest-asyncio`.

> **It is not a plain Python class, though.** `State.__init__` raises
> `ReflexRuntimeError: State classes should not be instantiated directly`
> unless `PYTEST_CURRENT_TEST` is in the environment
> (`reflex.utils.exec.is_testing_env`). So `MyState()` works inside a test and
> fails in a REPL, a scratch script, or a `python -c` probe. Verify state
> behaviour with a real test file, not an ad-hoc snippet.

## Setup

```bash
pip install pytest pytest-asyncio pytest-cov
```

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests/unit"]
asyncio_mode = "auto"
python_files = ["test_*.py"]
```

## File layout

```
tests/unit/
├── conftest.py
├── test_base_state.py
└── test_project_state.py
```

## Patterns

**Base vars** → test defaults directly
**Sync handlers** → call on instance, assert result
**Async handlers** → `await`, assert `is_loading is False` after
**Streaming handlers** → consume with `async for` (only if the handler yields)
**Computed vars** → mutate base vars, assert property
**Substates** → instantiate subclass independently (inside a test; see note above)
**External I/O** → `unittest.mock.patch` at `myapp.state.*`
**Background tasks** → `await MyState.handler.fn(state)`, patch `__aenter__`/`__aexit__`

See [references/PATTERNS.md](references/PATTERNS.md) for full code examples.
See [references/FIXTURES.md](references/FIXTURES.md) for shared fixture setup.

## Decision: which pattern to use?

**Sync handler?** → Test directly, no `async`
**Async handler?** → Use `await`, check loading flag resets
**Handler bound to Radix UI component?** → Add `str` AND `list[str]` test cases
**Handler calls API/DB?** → Mock with `AsyncMock`, test both success and failure
**Background task (`@rx.event(background=True)`)?** → Patch the context manager AND
call it via `MyState.handler.fn(state)` — a direct `state.handler()` raises

## Run

```bash
pytest tests/unit/ -v --cov=myapp/state --cov-report=term-missing
pytest tests/unit/test_project_state.py -v   # single module
pytest tests/unit/ -x                        # stop on first failure
```
