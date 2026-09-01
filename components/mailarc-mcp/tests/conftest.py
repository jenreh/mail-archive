"""Skip this component's tests on a checkout that does not have it installed.

``mailarc-mcp`` sits behind ``[project.optional-dependencies] mcp``, so plain
``uv sync`` — the desktop shape, and the obvious first command in a fresh clone
— resolves a workspace without it. ``pyproject.toml``'s ``testpaths`` still
names this directory unconditionally, and the three modules here import
``mailarc_mcp`` at module level, so such a checkout got three *collection
errors* and a suite that would not run at all:

    ERROR test_mcp_package.py
    ERROR test_mcp_server.py
    ERROR test_mcp_server_local.py
    Interrupted: 3 errors during collection

A missing optional component is a reason to leave its tests out, not a reason
the rest of the suite cannot run. ``collect_ignore_glob`` is how pytest is told
that, and it is the whole file.

``find_spec`` and not ``try: import mailarc_mcp``, the same discipline
``components/mailarc-core/tests/test_isolation.py`` uses: asking whether a
package can be found must not be the thing that imports it, or every later
question about what a fresh interpreter holds is answered by this file's side
effect.

A developer environment has the extra — ``task install`` syncs ``--extra mcp``
and ``docs/developer/testing.md`` says so — so on the machine anybody actually
works on, this file does nothing.
"""

import importlib.util

collect_ignore_glob = (
    [] if importlib.util.find_spec("mailarc_mcp") is not None else ["*.py"]
)
