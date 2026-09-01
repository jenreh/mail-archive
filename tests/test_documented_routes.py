"""Every route a human is told to visit has to be one the application serves.

The three admin pages moved from ``/mail/*`` to ``/admin/*`` and six places
went on naming the old paths: two user-facing guides that send a reader to a
URL that 404s, an operations note, and three module docstrings — including
``app/derive.py``'s, which told a developer that the wrong page enqueues the
job it documents.

Nothing caught it because the tests pin the route against ``ROUTE`` and the
navigation, and prose is not either. This is the two-line assertion that closes
that gap: the routes are already exported as constants, so the check costs one
walk over the documentation and the pages.

The mail-client redesign moved four more. Insights left ``/admin/`` for
``/insights``, the dashboard gave ``/`` to the search and took ``/dashboard``,
and the sign-in went away entirely — with it ``/login``, ``/profile`` and
``/admin/users``, three paths that a reader following them now reaches nothing
at all at. ``/admin/review`` joined them when the review page was removed — the
search at ``/`` reads a message the same way and had made it a second door onto
the same reading pane. All of them are in the pattern below, so prose that
still names one fails here rather than in a browser.
"""

import re
from pathlib import Path

from mailarc_ui.shell import routes

ROOT = Path(__file__).resolve().parent.parent

ROUTES = set(routes.ALL_ROUTES)
"""Where the pages actually live.

Read off ``mailarc_ui.shell.routes`` rather than off the page modules, which is
where the constants moved to and also the cheaper import: the route table names
no Reflex component, so this check no longer registers five pages into a
module-level registry in order to ask what their paths are.
"""

MOVED = re.compile(
    r"/mail/(?:accounts|review|insights)\b"
    r"|/admin/(?:insights|review|users)\b"
    r"|(?<![\w/.-])/profile\b"
    r"|(?<![\w/.-])/login\b"
)
"""The paths that no longer answer, and nothing else.

Deliberately spelled out rather than generalised: a future ``/mail/something``
or ``/admin/something`` is a real route until somebody says otherwise, and a
check that guessed would go off on the first page nobody has moved.

The two bare paths carry a look-behind because ``/login`` and ``/profile`` are
also the tails of URLs that have nothing to do with this application —
``https://login.microsoftonline.com`` is in the configuration guide, and
``getProfile`` is a Gmail API call the provider guide names. Only a path
standing on its own is a route somebody could follow.
"""

SEARCHED = ("docs", "app", "components")
"""Prose a reader follows, and the docstrings a developer reads instead.

``spec/`` is left out: it is the design document that *proposed* the layout,
written before the move, and rewriting history there would lose the record of
what was decided when.
"""

SKIPPED = ("node_modules", ".vitepress")
"""Directories under ``docs/`` that nobody in this repository wrote.

The documentation site vendors its own toolchain and builds into
``.vitepress/dist``; neither is prose this project maintains, and a match in
either says nothing about the archive's routes.
"""


def _files() -> list[Path]:
    """Every Markdown and Python file the check covers."""
    found: list[Path] = []
    for directory in SEARCHED:
        for suffix in ("*.md", "*.py"):
            found.extend(
                path
                for path in sorted((ROOT / directory).rglob(suffix))
                if not any(part in SKIPPED for part in path.parts)
            )
    return found


def test_nothing_sends_a_reader_to_a_route_that_moved() -> None:
    """One test over every file, rather than one test per file.

    Six offenders in six different places was the failure; a parametrised
    version would have reported them one at a time and added three hundred
    ids to the run for a check that has one thing to say.
    """
    stale = {
        str(path.relative_to(ROOT)): sorted(set(found))
        for path in _files()
        if (found := MOVED.findall(path.read_text(encoding="utf-8")))
    }

    assert not stale, (
        f"{stale} — the pages live at {sorted(ROUTES)} and a reader "
        "following one of these paths gets a 404"
    )


def test_the_pages_agree_on_where_they_are() -> None:
    """The premise of the check above, so it cannot pass by naming nothing."""
    assert ROUTES == {
        "/",
        "/dashboard",
        "/insights",
        "/admin/accounts",
        "/admin/embedder",
        "/admin/status",
    }
