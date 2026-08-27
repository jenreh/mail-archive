"""What one entry in the sidebar is, and how the entries are grouped.

Value objects, so pydantic models frozen at construction — never a
``TypedDict``. A ``TypedDict`` is a shape a type checker believes and nothing
enforces: a typo in a key is a silent ``None`` at render time, a missing
``href`` is a link to nowhere, and neither shows up until somebody clicks. The
navigation is also an access-control surface, and "the gate is declared on the
item" only means anything if the item cannot be built without one.

Frozen because a navigation table is a decision, not state. Nothing should be
able to hand a different ``href`` to the sidebar halfway through a session.
"""

from pydantic import BaseModel, ConfigDict


class NavItem(BaseModel):
    """One row of the sidebar: what it says, where it goes, who may see it.

    The gate lives here rather than in the code that renders the row, which is
    the whole point of the arrangement — reading this file tells you who can
    see what, and a new entry cannot be added without answering the question.
    ``/`` is public, so an entry that forgot its gate would put a link into the
    archive's administration in front of an anonymous visitor.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    href: str
    icon: str
    admin_only: bool = False
    """Hidden from anyone who is not an administrator."""

    requires_login: bool = False
    """Hidden from anyone who is not signed in.

    Distinct from ``admin_only`` and not a weaker version of it: ``/profile``
    is every signed-in person's own page, and gating it on administration
    would take a regular account's profile link away.
    """

    requires_role: str | None = None
    """Hidden from anyone without this role. ``None`` means no role check."""


class NavSection(BaseModel):
    """A run of entries with a dotted rule under it.

    A section carries no label. The design separates groups with a rule rather
    than with headings, and a title field nothing renders is a field that will
    eventually be filled in by someone who assumes it does.
    """

    model_config = ConfigDict(frozen=True)

    items: tuple[NavItem, ...]
