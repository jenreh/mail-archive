"""The role catalogue, and the fact that it resolves from `mailarc_core`.

Roles are archive-wide policy: Reflex-free, and readable by every layer, which
is why they live here rather than in the UI package. The re-export is the part
worth a test — a page saying ``from mailarc_core import ALL_ROLES`` is the
whole point of the move, and an entry missing from ``__all__`` is a failure
that only shows up in whichever page imports it last.
"""

from appkit_commons.roles import Role

import mailarc_core
from mailarc_core.roles import ALL_ROLES, PROJECT_MANAGER_ROLE


def test_the_catalogue_resolves_from_the_package_itself() -> None:
    assert mailarc_core.ALL_ROLES is ALL_ROLES
    assert mailarc_core.PROJECT_MANAGER_ROLE is PROJECT_MANAGER_ROLE


def test_both_names_are_part_of_the_public_surface() -> None:
    assert {"ALL_ROLES", "PROJECT_MANAGER_ROLE"} <= set(mailarc_core.__all__)


def test_the_catalogue_holds_the_project_manager() -> None:
    assert ALL_ROLES == [PROJECT_MANAGER_ROLE]
    assert isinstance(PROJECT_MANAGER_ROLE, Role)
    assert PROJECT_MANAGER_ROLE.name == "project_manager"
