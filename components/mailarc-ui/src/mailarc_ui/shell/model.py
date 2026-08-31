"""What one entry in the rail is, and how the entries are grouped.

Value objects, so pydantic models frozen at construction — never a
``TypedDict``. A ``TypedDict`` is a shape a type checker believes and nothing
enforces: a typo in a key is a silent ``None`` at render time, a missing
``href`` is a link to nowhere, and neither shows up until somebody clicks.

Frozen because a navigation table is a decision, not state. Nothing should be
able to hand a different ``href`` to the rail halfway through a session.
"""

from pydantic import BaseModel, ConfigDict


class NavItem(BaseModel):
    """One entry of the rail: what it says, where it goes, which icon it wears.

    No gate fields any more, and their absence is the design rather than an
    omission. The archive ships as a desktop application with no sign-in at
    all, so there is nobody to hide an entry from — an ``admin_only`` flag
    would be a permission the application cannot check, declared on every row
    that names ``/admin/``.

    ``label`` is what the tooltip beside the icon says, because the rail is
    76px wide and shows no text of its own.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    href: str
    icon: str


class NavSection(BaseModel):
    """A run of entries under one heading.

    The heading is the design's own: two 10px uppercase labels, ``MENU`` over
    the three pages a person works in and ``ADMIN`` over what an operator
    maintains. It is a field rather than a caller's argument for the reason
    ``href`` is one — a section without a heading would render as a gap
    nothing explains.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    items: tuple[NavItem, ...]
