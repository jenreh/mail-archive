"""Every route a human is told to visit has to be one the application serves.

The three admin pages moved from ``/mail/*`` to ``/admin/*`` and six places
went on naming the old paths: two user-facing guides that send a reader to a
URL that 404s, an operations note, and three module docstrings — including
``app/derive.py``'s, which told a developer that the wrong page enqueues the
job it documents.

Nothing caught it because the tests pin the route against ``ROUTE`` and the
navbar, and prose is not either. This is the two-line assertion that closes
that gap: the routes are already exported as constants, so the check costs one
walk over the documentation and the pages.
"""

import re
from pathlib import Path

from app.pages import mail_accounts, mail_embedder, mail_insights, mail_review

ROOT = Path(__file__).resolve().parent.parent

ROUTES = {
    mail_accounts.ROUTE,
    mail_embedder.ROUTE,
    mail_insights.ROUTE,
    mail_review.ROUTE,
}
"""Where the four admin pages actually live."""

MOVED = re.compile(r"/mail/(accounts|review|insights)\b")
"""The paths they used to live at, and nothing else.

Deliberately the three names rather than ``/mail/`` in general: a future
``/mail/something`` is a real route until somebody says otherwise, and a check
that guessed would go off on the first page nobody has moved.
"""

SEARCHED = ("docs", "app", "components")
"""Prose a reader follows, and the docstrings a developer reads instead.

``spec/`` is left out: it is the design document that *proposed* the layout,
written before the move, and rewriting history there would lose the record of
what was decided when.
"""


def _files() -> list[Path]:
    """Every Markdown and Python file the check covers."""
    found: list[Path] = []
    for directory in SEARCHED:
        for suffix in ("*.md", "*.py"):
            found.extend(sorted((ROOT / directory).rglob(suffix)))
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
        f"{stale} — the admin pages live at {sorted(ROUTES)} and a reader "
        "following the old path gets a 404"
    )


def test_the_admin_pages_agree_on_where_they_are() -> None:
    """The premise of the check above, so it cannot pass by naming nothing."""
    assert ROUTES == {
        "/admin/accounts",
        "/admin/embedder",
        "/admin/insights",
        "/admin/review",
    }
