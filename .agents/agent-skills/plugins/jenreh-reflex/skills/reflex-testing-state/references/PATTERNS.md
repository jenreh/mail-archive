# Code Patterns

## Base var defaults

```python
def test_initial_values(state):
    assert state.count == 0
    assert state.is_loading is False
    assert state.items == []
```

## Sync event handlers

```python
def test_increment(state):
    state.increment()
    assert state.count == 1

def test_reset(state):
    state.count = 99
    state.reset_counter()
    assert state.count == 0
```

## UI handlers (str | list[str] guard)

```python
def test_set_tab_str(state):
    state.set_tab_control("overview")
    assert state.tab_control == "overview"

def test_set_tab_list(state):
    state.set_tab_control(["overview"])
    assert state.tab_control == "overview"
```

## Async event handlers

```python
async def test_fetch_data(state):
    await state.fetch_data()
    assert len(state.items) > 0
    assert state.is_loading is False

async def test_fetch_resets_on_error(state):
    state.api_url = "http://invalid"
    await state.fetch_data()
    assert state.is_loading is False
```

## Streaming handlers

```python
async def test_streaming(state):
    async for _ in state.stream_results():
        pass
    assert state.result_text != ""
```

## Computed vars

```python
def test_item_count(state):
    state.items = ["x", "y", "z"]
    assert state.item_count == 3

def test_is_empty(state):
    state.items = []
    assert state.is_empty is True
```

## Substates

```python
from myapp.state import ProjectState

def test_select_project():
    s = ProjectState()
    s.select_project("proj-123")
    assert s.selected_project == "proj-123"
```

## Mocked external I/O

```python
from unittest.mock import patch, AsyncMock

async def test_save_success(state):
    with patch("myapp.state.api_client.post", new_callable=AsyncMock) as m:
        m.return_value = {"id": "abc"}
        await state.save_item("new_item")
        m.assert_called_once()
        assert state.last_saved_id == "abc"

async def test_save_failure(state):
    with patch("myapp.state.api_client.post", side_effect=Exception("timeout")):
        await state.save_item("bad_item")
        assert state.error_message == "timeout"
        assert state.is_loading is False
```

## Background tasks

Two things differ from a normal async handler:

1. **Reflex refuses a direct call.** `await state.poll()` raises
   `RuntimeError: Cannot directly call background task ... use yield/return`.
   Reach the wrapped coroutine through the `EventHandler`: `MyState.poll.fn(state)`.
2. **`await` it, don't `async for` it.** A `@rx.event(background=True)` handler
   is a coroutine, not an async generator, unless it actually yields.

```python
async def test_background_task(state):
    with patch.object(state, "__aenter__", return_value=state), \
         patch.object(state, "__aexit__", return_value=False):
        await MyState.long_running_task.fn(state)   # NOT state.long_running_task()
    assert state.progress == 100
```

Patching `__aenter__`/`__aexit__` on the **instance** works; so does patching on
the class if several instances are involved.

### Driving a polling loop

A loop like `while True: ... if not self.polling: return` needs a scripted
side effect that eventually clears the flag, or the test hangs. Set
`poll_interval = 0` so the sleep is free:

```python
def scripted(state, *results):
    """Feed the loop fixed readings, then stop it."""
    remaining, calls = list(results), {"n": 0}

    async def next_result():
        calls["n"] += 1
        if not remaining:
            state.polling = False       # ends the loop
            return SOME_READING
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    service = AsyncMock()
    service.current_status = AsyncMock(side_effect=next_result)
    return service, calls
```

Mind the loop's real semantics. If the handler re-checks its stop flag *after*
the await — which it should, so a stop takes effect promptly — then a reading
that arrives once polling is off is **discarded by design**. A test asserting
"last reading was applied" will fail; assert on the reading *before* the stop
instead, and add a separate test pinning the discard behaviour:

```python
async def test_a_reading_that_arrives_after_stop_is_discarded(state):
    state.polling, state.poll_interval = True, 0
    service, _ = scripted(state)          # first call already stops the loop
    with patched_loop(service):
        await MyState.poll.fn(state)
    assert state.checked is False         # nothing was applied
```
